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
        conn.commit()


def _make_title(first_user_message):
    title = " ".join(first_user_message.split())
    if len(title) > _TITLE_MAX_LEN:
        title = title[:_TITLE_MAX_LEN].rstrip() + "…"
    return title or "새 대화"


def create_conversation(first_user_message):
    conversation_id = uuid.uuid4().hex
    now = _now()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, _make_title(first_user_message), now, now),
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


def list_conversations(limit=100):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id):
    with closing(_connect()) as conn:
        conv = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
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
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()


init_db()
