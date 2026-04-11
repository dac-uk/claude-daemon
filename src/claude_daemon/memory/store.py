"""ConversationStore - SQLite-backed conversation history and session tracking."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class ConversationStore:
    """Persistent storage for conversations, messages, and summaries."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        schema_path = Path(__file__).parent / "schema.sql"
        schema = schema_path.read_text()
        self._db.executescript(schema)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- Conversations --

    def get_or_create_conversation(
        self, session_id: str | None, platform: str, user_id: str
    ) -> dict:
        if session_id:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row:
                return dict(row)

        # Look for existing active conversation for this user on this platform
        row = self._db.execute(
            "SELECT * FROM conversations WHERE user_id = ? AND platform = ? AND status = 'active' "
            "ORDER BY last_active DESC LIMIT 1",
            (user_id, platform),
        ).fetchone()

        if row:
            return dict(row)

        # Create new conversation
        new_session_id = session_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        cur = self._db.execute(
            "INSERT INTO conversations (session_id, platform, user_id, started_at, last_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_session_id, platform, user_id, now, now),
        )
        self._db.commit()
        return {
            "id": cur.lastrowid,
            "session_id": new_session_id,
            "platform": platform,
            "user_id": user_id,
            "started_at": now,
            "last_active": now,
            "message_count": 0,
            "total_cost_usd": 0.0,
            "status": "active",
        }

    def get_conversation_by_session(self, session_id: str) -> dict | None:
        """Look up a conversation by its Claude session ID."""
        row = self._db.execute(
            "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_conversation(
        self, conv_id: int, session_id: str | None = None, cost: float = 0
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if session_id:
            self._db.execute(
                "UPDATE conversations SET last_active = ?, session_id = ?, "
                "message_count = message_count + 1, total_cost_usd = total_cost_usd + ? "
                "WHERE id = ?",
                (now, session_id, cost, conv_id),
            )
        else:
            self._db.execute(
                "UPDATE conversations SET last_active = ?, "
                "message_count = message_count + 1, total_cost_usd = total_cost_usd + ? "
                "WHERE id = ?",
                (now, cost, conv_id),
            )
        self._db.commit()

    def archive_conversation(self, conv_id: int) -> None:
        self._db.execute(
            "UPDATE conversations SET status = 'archived' WHERE id = ?", (conv_id,)
        )
        self._db.commit()

    def get_active_conversations(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM conversations WHERE status = 'active' ORDER BY last_active DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_expired(self, max_age_hours: int = 72) -> int:
        cur = self._db.execute(
            "UPDATE conversations SET status = 'archived' "
            "WHERE status = 'active' AND last_active < datetime('now', ? || ' hours')",
            (f"-{max_age_hours}",),
        )
        self._db.commit()
        return cur.rowcount

    def reset_conversation(self, user_id: str, platform: str) -> None:
        self._db.execute(
            "UPDATE conversations SET status = 'archived' "
            "WHERE user_id = ? AND platform = ? AND status = 'active'",
            (user_id, platform),
        )
        self._db.commit()

    # -- Messages --

    def add_message(
        self, conv_id: int, role: str, content: str,
        tokens: int = 0, cost: float = 0
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._db.execute(
            "INSERT INTO messages (conversation_id, role, content, timestamp, tokens_used, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, role, content, now, tokens, cost),
        )
        self._db.commit()
        return cur.lastrowid

    def get_recent_messages(self, conv_id: int, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_conversation_text(self, conv_id: int, limit: int = 20) -> str:
        messages = self.get_recent_messages(conv_id, limit)
        lines = []
        for msg in messages:
            role = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role}: {msg['content']}")
        return "\n".join(lines)

    # -- Summaries --

    def add_summary(
        self, conv_id: int | None, summary: str, summary_type: str
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._db.execute(
            "INSERT INTO memory_summaries (conversation_id, summary, summary_type, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conv_id, summary, summary_type, now),
        )
        self._db.commit()
        return cur.lastrowid

    def get_latest_summary(self, conv_id: int) -> str | None:
        row = self._db.execute(
            "SELECT summary FROM memory_summaries "
            "WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conv_id,),
        ).fetchone()
        return row["summary"] if row else None

    def get_summaries_by_type(self, summary_type: str, limit: int = 5) -> list[str]:
        rows = self._db.execute(
            "SELECT summary FROM memory_summaries "
            "WHERE summary_type = ? ORDER BY created_at DESC LIMIT ?",
            (summary_type, limit),
        ).fetchall()
        return [r["summary"] for r in rows]

    # -- Stats --

    def get_stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active, "
            "COALESCE(SUM(total_cost_usd), 0) as total_cost, "
            "COALESCE(SUM(message_count), 0) as total_messages "
            "FROM conversations"
        ).fetchone()
        return dict(row) if row else {}

    def get_user_stats(self, user_id: str, platform: str) -> dict:
        """Get per-user statistics."""
        row = self._db.execute(
            "SELECT COUNT(*) as sessions, "
            "COALESCE(SUM(total_cost_usd), 0) as total_cost, "
            "COALESCE(SUM(message_count), 0) as total_messages "
            "FROM conversations WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        ).fetchone()
        return dict(row) if row else {}
