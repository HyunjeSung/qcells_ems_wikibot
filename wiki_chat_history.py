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


def add_message(conversation_id, role, content, sources=None):
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, json.dumps(sources) if sources else None, _now()),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )
        conn.commit()


def replace_last_message(conversation_id, role, content, sources=None):
    """모델 escalation 재시도(같은 turn) 시 새 행을 또 쌓지 않고 마지막 메시지를 덮어쓴다."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND role = ? ORDER BY id DESC LIMIT 1",
            (conversation_id, role),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, content, json.dumps(sources) if sources else None, _now()),
            )
        else:
            conn.execute(
                "UPDATE messages SET content = ?, sources = ?, created_at = ? WHERE id = ?",
                (content, json.dumps(sources) if sources else None, _now(), row["id"]),
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
            "SELECT role, content, sources FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
    messages = [
        {
            "role": r["role"],
            "content": r["content"],
            "sources": json.loads(r["sources"]) if r["sources"] else None,
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


init_db()
