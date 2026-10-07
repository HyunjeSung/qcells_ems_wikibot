"""위키봇 대화 기록(사이드바용) SQLite 저장소.

Rovo Chat처럼 왼쪽 사이드바에서 과거 대화를 목록으로 보고 이어서 열람할 수 있게
conversations/messages 두 테이블로 영속화한다.
"""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / ".wiki_chat_history.db"

_TITLE_MAX_LEN = 40


def _now():
    return datetime.now(timezone.utc).isoformat()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)")

        # 사용자 계정 테이블. 비밀번호는 관리자 페이지에서 평문으로 조회 가능해야 한다는
        # 요구사항(2026-09-22, 사용자 확정 — 해시는 원문 복원이 불가능해서 이 요구사항 자체를
        # 만족 못 함)에 따라 의도적으로 평문 저장한다. 사내 LAN 전용 도구라는 전제 하의 트레이드오프.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "deleted_at" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN deleted_at TEXT")
        if "user_id" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id)")
        if "anon_session_id" not in existing_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN anon_session_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_anon ON conversations(anon_session_id)")

        # 계정 기능 도입(2026-09-22) 이전에 쌓인 대화는 user_id/anon_session_id가 둘 다 NULL이라
        # list_anon_sessions()(anon_session_id IS NOT NULL 조건)에서 안 잡혀 관리자 페이지
        # "비로그인" 목록에서 영영 안 보이게 된다 — 실제로는 지우지 않았지만 관리자 입장에선
        # "사라진 것"처럼 보이는 셈. 하나의 공용 anon_session_id로 묶어 예전 그대로 목록에
        # 남긴다(사용자 요청: "지금까지 쌓인 비로그인 대화를 버리지 않고 유지"). 새 방문자가
        # anon_id 없이 대화를 만드는 경로는 없으므로(_ensure_anon_id가 항상 발급) 이 UPDATE는
        # 이 마이그레이션 이후로는 매번 대상이 0건이라 그냥 안전하게 반복 실행된다.
        conn.execute(
            "UPDATE conversations SET anon_session_id = ? WHERE user_id IS NULL AND anon_session_id IS NULL",
            ("legacy-pre-login",),
        )

        # 작업대/실행 기록(2026-10-07 신규) — 답변 하나가 만들어지기까지 거친 파이프라인 단계
        # (검색어 분석 → Rovo 검색 → 인물/Jira 보강 → 답변 생성 → 모델 비교 판정)를 run 단위로
        # 남긴다. 답변 메시지는 run_id로 자기 run을 가리켜서, 과거 대화를 다시 열어도 그 답변이
        # 어떤 과정을 거쳤는지 우측 작업대에서 그대로 재생할 수 있다.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                mode TEXT NOT NULL,
                adopted_model TEXT,
                status TEXT NOT NULL,
                events TEXT,
                candidates TEXT,
                verdict TEXT,
                sources_count INTEGER,
                duration_ms INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_conv ON runs(conversation_id)")
        msg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "run_id" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN run_id TEXT")
        # 실행 기록 1건 단위 소프트 삭제(2026-10-07) — 사용자 화면의 실행 기록 ✕는 그 질문·답변 한
        # 쌍만 숨기고, 관리자 화면에서는 "삭제됨"으로 계속 보인다.
        if "deleted_at" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN deleted_at TEXT")
        run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "deleted_at" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN deleted_at TEXT")

        # 사용자가 올린 파일(2026-10-07 신규). 원본은 디스크(uploads/)에, 여기에는 메타데이터와
        # 추출 텍스트 경로만 둔다. 소유자 구분은 conversations와 같은 user_id/anon_session_id 규칙.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                kind TEXT,
                text_chars INTEGER NOT NULL,
                stored_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                anon_session_id TEXT
            )
        """)

        conn.commit()


def _make_title(first_user_message):
    title = " ".join(first_user_message.split())
    if len(title) > _TITLE_MAX_LEN:
        title = title[:_TITLE_MAX_LEN].rstrip() + "…"
    return title or "새 대화"


def create_conversation(first_user_message, user_id=None, anon_session_id=None):
    conversation_id = uuid.uuid4().hex
    now = _now()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, user_id, anon_session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, _make_title(first_user_message), now, now, user_id, anon_session_id),
        )
        conn.commit()
    return conversation_id


def touch_conversation(conversation_id):
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )
        conn.commit()


def add_message(conversation_id, role, content, sources=None, run_id=None):
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources, created_at, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, json.dumps(sources) if sources else None, _now(), run_id),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )
        conn.commit()


def replace_last_message(conversation_id, role, content, sources=None, run_id=None):
    """모델 escalation 재시도(같은 turn) 시 새 행을 또 쌓지 않고 마지막 메시지를 덮어쓴다."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND role = ? ORDER BY id DESC LIMIT 1",
            (conversation_id, role),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, sources, created_at, run_id) VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, role, content, json.dumps(sources) if sources else None, _now(), run_id),
            )
        else:
            conn.execute(
                "UPDATE messages SET content = ?, sources = ?, created_at = ?, run_id = ? WHERE id = ?",
                (content, json.dumps(sources) if sources else None, _now(), run_id, row["id"]),
            )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )
        conn.commit()


def list_conversations(limit=100, include_deleted=False, user_id=None, anon_session_id=None):
    """user_id/anon_session_id를 주면 그 소유자 것만 필터링한다(둘 다 None이면 전체 —
    관리자 페이지의 사용자별/비로그인별 조회에서 명시적으로 쓴다)."""
    query = "SELECT id, title, created_at, updated_at, deleted_at, user_id, anon_session_id FROM conversations"
    clauses = []
    params = []
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    elif anon_session_id is not None:
        clauses.append("anon_session_id = ?")
        params.append(anon_session_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id, include_deleted=False):
    query = "SELECT id, title, created_at, updated_at, deleted_at, user_id, anon_session_id FROM conversations WHERE id = ?"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    with closing(_connect()) as conn:
        conv = conn.execute(query, (conversation_id,)).fetchone()
        if conv is None:
            return None
        rows = conn.execute(
            "SELECT role, content, sources, run_id, deleted_at FROM messages WHERE conversation_id = ?"
            + ("" if include_deleted else " AND deleted_at IS NULL") + " ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
    messages = [
        {
            "role": r["role"],
            "content": r["content"],
            "sources": json.loads(r["sources"]) if r["sources"] else None,
            "run_id": r["run_id"],
            "deleted_at": r["deleted_at"],
        }
        for r in rows
    ]
    result = dict(conv)
    result["messages"] = messages
    return result


def delete_conversation(conversation_id):
    """사용자용 삭제 — 소프트 삭제. 사이드바/조회에서 숨겨지지만 admin은 계속 볼 수 있다."""
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE conversations SET deleted_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )
        conn.commit()


def restore_conversation(conversation_id):
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE conversations SET deleted_at = NULL WHERE id = ?",
            (conversation_id,),
        )
        conn.commit()


def purge_conversation(conversation_id):
    """admin 전용 완전 삭제 — 되돌릴 수 없음."""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()


class UsernameTaken(Exception):
    pass


def create_user(username, password):
    now = _now()
    with closing(_connect()) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password, created_at) VALUES (?, ?, ?)",
                (username, password, now),
            )
        except sqlite3.IntegrityError:
            raise UsernameTaken(username)
        conn.commit()
        return cur.lastrowid


def get_user_by_username(username):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def delete_user(user_id):
    """관리자 전용 계정 삭제 — 되돌릴 수 없음. 그 사용자의 대화도 함께 완전히 지운다
    (messages는 conversations.id의 ON DELETE CASCADE로 같이 정리됨, purge_conversation과 동일 원리)."""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def list_users():
    """관리자 페이지용 — 사용자별 대화 개수(소프트 삭제 제외)까지 같이 뽑는다."""
    with closing(_connect()) as conn:
        rows = conn.execute("""
            SELECT u.id, u.username, u.password, u.created_at,
                   COUNT(c.id) FILTER (WHERE c.deleted_at IS NULL) AS conversation_count
            FROM users u
            LEFT JOIN conversations c ON c.user_id = u.id
            GROUP BY u.id
            ORDER BY u.created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def list_anon_sessions():
    """관리자 페이지의 '비로그인' 목록용 — anon_session_id 별로 묶어서 대화 개수/최근 활동을 낸다."""
    with closing(_connect()) as conn:
        rows = conn.execute("""
            SELECT anon_session_id,
                   COUNT(*) FILTER (WHERE deleted_at IS NULL) AS conversation_count,
                   MAX(updated_at) AS last_activity
            FROM conversations
            WHERE anon_session_id IS NOT NULL
            GROUP BY anon_session_id
            ORDER BY last_activity DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ── 실행 기록(runs) ──

_RUN_JSON_FIELDS = ("events", "candidates", "verdict")


def _run_from_row(row):
    run = dict(row)
    for key in _RUN_JSON_FIELDS:
        run[key] = json.loads(run[key]) if run.get(key) else None
    return run


def save_run(run):
    """run dict(id/conversation_id/question/mode/adopted_model/status/events/candidates/verdict/
    sources_count/duration_ms)를 저장한다. 같은 id가 있으면 덮어쓴다(escalation 재시도 대비)."""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, conversation_id, question, mode, adopted_model, status, events, "
            "candidates, verdict, sources_count, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run["id"], run.get("conversation_id"), run["question"], run["mode"], run.get("adopted_model"),
                run["status"], json.dumps(run.get("events") or [], ensure_ascii=False),
                json.dumps(run.get("candidates") or [], ensure_ascii=False),
                json.dumps(run.get("verdict"), ensure_ascii=False) if run.get("verdict") else None,
                run.get("sources_count") or 0, run.get("duration_ms") or 0, run.get("created_at") or _now(),
            ),
        )
        conn.commit()


def get_run(run_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_from_row(row) if row else None


def list_runs(limit=200, user_id=None, anon_session_id=None, include_deleted=False):
    """기록 화면용 — 소유자 필터는 list_conversations와 같은 규칙(둘 다 None이면 전체)."""
    query = (
        "SELECT r.id, r.conversation_id, r.question, r.mode, r.adopted_model, r.status, r.candidates, "
        "r.verdict, r.sources_count, r.duration_ms, r.created_at, c.title AS conversation_title "
        "FROM runs r JOIN conversations c ON c.id = r.conversation_id"
    )
    clauses = []
    params = []
    if not include_deleted:
        clauses.append("c.deleted_at IS NULL")
        clauses.append("r.deleted_at IS NULL")
    if user_id is not None:
        clauses.append("c.user_id = ?")
        params.append(user_id)
    elif anon_session_id is not None:
        clauses.append("c.anon_session_id = ?")
        params.append(anon_session_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        run = dict(r)
        run["candidates"] = json.loads(run["candidates"]) if run.get("candidates") else []
        run["verdict"] = json.loads(run["verdict"]) if run.get("verdict") else None
        out.append(run)
    return out


def list_conversations_overview(limit=1000):
    """관리자 "전체 기록" 화면용 — 모든 소유자의 대화를 소유자 이름·메시지/실행 수·마지막 실행
    상태와 함께 한 번에 뽑는다(소프트 삭제된 대화 포함)."""
    with closing(_connect()) as conn:
        rows = conn.execute("""
            SELECT c.id, c.title, c.created_at, c.updated_at, c.deleted_at, c.user_id, c.anon_session_id,
                   u.username,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.conversation_id = c.id) AS run_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.conversation_id = c.id AND r.deleted_at IS NOT NULL) AS deleted_run_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.conversation_id = c.id AND r.mode = 'compare') AS compare_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.conversation_id = c.id AND r.status = 'error') AS error_count,
                   (SELECT r.adopted_model FROM runs r WHERE r.conversation_id = c.id
                     ORDER BY r.created_at DESC LIMIT 1) AS last_model,
                   (SELECT SUM(r.duration_ms) FROM runs r WHERE r.conversation_id = c.id) AS total_ms
            FROM conversations c
            LEFT JOIN users u ON u.id = c.user_id
            ORDER BY c.updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_run(run_id):
    """실행 기록 1건 소프트 삭제 — 그 run과, 그 run의 질문·답변 메시지 한 쌍을 숨긴다.
    대화에 남은 메시지가 하나도 없으면 대화 자체도 소프트 삭제한다. 반환: 대화도 지워졌는지."""
    now = _now()
    with closing(_connect()) as conn:
        run = conn.execute("SELECT conversation_id, question FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return False
        conv_id = run["conversation_id"]
        conn.execute("UPDATE runs SET deleted_at = ? WHERE id = ?", (now, run_id))
        linked = conn.execute(
            "SELECT id, role FROM messages WHERE run_id = ? AND deleted_at IS NULL", (run_id,)
        ).fetchall()
        for m in linked:
            conn.execute("UPDATE messages SET deleted_at = ? WHERE id = ?", (now, m["id"]))
        if not any(m["role"] == "user" for m in linked):
            # 질문 메시지에 run_id를 남기기 전(2026-10-07 이전)에 쌓인 기록 — 답변 바로 앞쪽의 같은
            # 내용 질문 중 아직 안 지워졌고 다른 run에 묶이지 않은 것을 짝으로 본다.
            answer_id = max((m["id"] for m in linked if m["role"] == "assistant"), default=None)
            q = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' AND content = ? "
                "AND deleted_at IS NULL AND run_id IS NULL" + (" AND id < ?" if answer_id else "")
                + " ORDER BY id DESC LIMIT 1",
                (conv_id, run["question"], answer_id) if answer_id else (conv_id, run["question"]),
            ).fetchone()
            if q:
                conn.execute("UPDATE messages SET deleted_at = ? WHERE id = ?", (now, q["id"]))
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND deleted_at IS NULL", (conv_id,)
        ).fetchone()[0]
        conv_deleted = left == 0
        if conv_deleted:
            conn.execute("UPDATE conversations SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now, conv_id))
        conn.commit()
    return conv_deleted


def list_runs_for_conversation(conversation_id):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,)
        ).fetchall()
    return [_run_from_row(r) for r in rows]


# ── 업로드 파일(files) ──

def create_file(file_id, filename, size, kind, text_chars, stored_name, user_id=None, anon_session_id=None):
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO files (id, filename, size, kind, text_chars, stored_name, created_at, user_id, anon_session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, filename, size, kind, text_chars, stored_name, _now(), user_id, anon_session_id),
        )
        conn.commit()


def get_file(file_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def list_files(user_id=None, anon_session_id=None, limit=200):
    query = "SELECT * FROM files"
    params = []
    if user_id is not None:
        query += " WHERE user_id = ?"
        params.append(user_id)
    elif anon_session_id is not None:
        query += " WHERE anon_session_id = ?"
        params.append(anon_session_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_file(file_id):
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()


init_db()
