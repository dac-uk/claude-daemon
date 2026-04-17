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
        self._check_integrity()
        self._init_schema()
        self._migrate_schema()

    def _check_integrity(self) -> None:
        """Run integrity check on startup. Log warning if database is corrupted."""
        try:
            result = self._db.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                log.error("DATABASE INTEGRITY CHECK FAILED: %s", result[0])
            else:
                log.debug("Database integrity check passed")
        except sqlite3.DatabaseError as e:
            log.error("DATABASE CORRUPT — cannot verify integrity: %s", e)

    def _init_schema(self) -> None:
        schema_path = Path(__file__).parent / "schema.sql"
        schema = schema_path.read_text()
        self._db.executescript(schema)
        self._db.commit()

    def _migrate_schema(self) -> None:
        """Apply incremental schema migrations for upgrades.

        CREATE TABLE IF NOT EXISTS handles new tables, but not new columns on
        existing tables. This method checks for missing structures and applies
        ALTER TABLE statements as needed.
        """
        try:
            # Check if audit_log table exists (added in v0.6)
            self._db.execute("SELECT 1 FROM audit_log LIMIT 0")
        except sqlite3.OperationalError:
            log.info("Migrating schema: creating audit_log table")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    action TEXT NOT NULL,
                    agent_name TEXT,
                    user_id TEXT,
                    platform TEXT,
                    details TEXT,
                    cost_usd REAL DEFAULT 0.0,
                    success BOOLEAN DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(timestamp);
                CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
                CREATE INDEX IF NOT EXISTS idx_audit_log_agent ON audit_log(agent_name);
            """)
            self._db.commit()

        try:
            self._db.execute("SELECT 1 FROM discussions LIMIT 0")
        except sqlite3.OperationalError:
            log.info("Migrating schema: creating discussions table")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS discussions (
                    id TEXT PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    discussion_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    initiator TEXT NOT NULL,
                    participants TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'running',
                    total_turns INTEGER DEFAULT 0,
                    total_cost_usd REAL DEFAULT 0.0,
                    duration_ms INTEGER DEFAULT 0,
                    synthesis TEXT,
                    transcript TEXT,
                    completed_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_discussions_type ON discussions(discussion_type);
                CREATE INDEX IF NOT EXISTS idx_discussions_ts ON discussions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_discussions_initiator ON discussions(initiator);
            """)
            self._db.commit()

        # budgets table (Phase 2 native orchestration)
        try:
            self._db.execute("SELECT 1 FROM budgets LIMIT 0")
        except sqlite3.OperationalError:
            log.info("Migrating schema: creating budgets table")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS budgets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    scope_value TEXT,
                    limit_usd REAL NOT NULL,
                    period TEXT NOT NULL,
                    current_spend REAL NOT NULL DEFAULT 0.0,
                    reset_at TIMESTAMP,
                    approval_threshold_usd REAL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_budgets_scope ON budgets(scope, scope_value);
            """)
            self._db.commit()

        # goals table (Phase 3 native orchestration)
        try:
            self._db.execute("SELECT 1 FROM goals LIMIT 0")
        except sqlite3.OperationalError:
            log.info("Migrating schema: creating goals table")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    owner_agent TEXT,
                    target_date DATE,
                    status TEXT NOT NULL DEFAULT 'active',
                    parent_goal_id INTEGER REFERENCES goals(id),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_goals_owner ON goals(owner_agent, status);
            """)
            self._db.commit()

        # approvals table (Phase 4 native orchestration)
        try:
            self._db.execute("SELECT 1 FROM approvals LIMIT 0")
        except sqlite3.OperationalError:
            log.info("Migrating schema: creating approvals table")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    approver_user TEXT,
                    reason TEXT,
                    threshold_usd REAL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
            """)
            self._db.commit()

        # teams table (Phase 4 native orchestration)
        try:
            self._db.execute("SELECT 1 FROM teams LIMIT 0")
        except sqlite3.OperationalError:
            log.info("Migrating schema: creating teams table")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    lead_agent TEXT,
                    members TEXT NOT NULL
                );
            """)
            self._db.commit()

        # task_queue: add metadata (JSON) and goal_id columns for native orchestration
        try:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(task_queue)").fetchall()}
            if "metadata" not in cols:
                log.info("Migrating schema: adding task_queue.metadata column")
                self._db.execute("ALTER TABLE task_queue ADD COLUMN metadata TEXT")
                self._db.commit()
            if "goal_id" not in cols:
                log.info("Migrating schema: adding task_queue.goal_id column")
                self._db.execute("ALTER TABLE task_queue ADD COLUMN goal_id INTEGER")
                self._db.commit()
        except sqlite3.OperationalError:
            # task_queue doesn't exist yet — schema.sql will create it on next startup
            pass

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

        # Look for existing active conversation for this user ACROSS ALL platforms.
        # This enables seamless handover between Telegram, Discord, CLI, and API.
        row = self._db.execute(
            "SELECT * FROM conversations WHERE user_id = ? AND status = 'active' "
            "ORDER BY last_active DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        if row:
            return dict(row)

        # Create new conversation — use INSERT OR IGNORE to handle concurrent inserts
        # on the same session_id (unique index prevents duplicates).
        new_session_id = session_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT OR IGNORE INTO conversations (session_id, platform, user_id, started_at, last_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_session_id, platform, user_id, now, now),
        )
        self._db.commit()
        # Re-fetch to get the actual row (whether we inserted or another thread did)
        row = self._db.execute(
            "SELECT * FROM conversations WHERE session_id = ?", (new_session_id,)
        ).fetchone()
        if row:
            return dict(row)
        # Fallback: should not happen, but construct from what we know
        return {
            "id": 0,
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

    def reset_conversation(self, user_id: str, platform: str | None = None) -> None:
        """Archive active conversations. If platform is None, clears across all platforms."""
        if platform:
            self._db.execute(
                "UPDATE conversations SET status = 'archived' "
                "WHERE user_id = ? AND platform = ? AND status = 'active'",
                (user_id, platform),
            )
        else:
            self._db.execute(
                "UPDATE conversations SET status = 'archived' "
                "WHERE user_id = ? AND status = 'active'",
                (user_id,),
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

    def get_user_stats(self, user_id: str, platform: str | None = None) -> dict:
        """Get per-user statistics. If platform is None, aggregates across all platforms."""
        if platform:
            row = self._db.execute(
                "SELECT COUNT(*) as sessions, "
                "COALESCE(SUM(total_cost_usd), 0) as total_cost, "
                "COALESCE(SUM(message_count), 0) as total_messages "
                "FROM conversations WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) as sessions, "
                "COALESCE(SUM(total_cost_usd), 0) as total_cost, "
                "COALESCE(SUM(message_count), 0) as total_messages "
                "FROM conversations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else {}

    # -- Full-Text Search --

    @staticmethod
    def _escape_fts5(query: str) -> str:
        """Escape an FTS5 query to prevent syntax errors from special characters."""
        # Wrap each word in double quotes to treat as literal, strip dangerous chars
        words = query.strip().split()
        if not words:
            return '""'
        return " ".join(f'"{w.replace(chr(34), "")}"' for w in words)

    def search_conversations(self, query: str, limit: int = 20) -> list[dict]:
        """Search message content using FTS5. Returns matching messages with context."""
        safe_query = self._escape_fts5(query)
        rows = self._db.execute(
            "SELECT m.id, m.conversation_id, m.role, m.content, m.timestamp, "
            "c.platform, c.user_id "
            "FROM messages_fts fts "
            "JOIN messages m ON m.id = fts.rowid "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY m.timestamp DESC LIMIT ?",
            (safe_query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Agent Metrics --

    def record_agent_metric(
        self,
        agent_name: str,
        metric_type: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        model: str = "",
        platform: str = "",
        success: bool = True,
    ) -> None:
        """Record a metric for agent activity."""
        self._db.execute(
            "INSERT INTO agent_metrics "
            "(agent_name, metric_type, input_tokens, output_tokens, cost_usd, "
            "duration_ms, model, platform, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_name, metric_type, input_tokens, output_tokens,
             cost_usd, duration_ms, model, platform, success),
        )
        self._db.commit()

    def get_agent_metrics(
        self, agent_name: str | None = None, days: int = 7,
    ) -> list[dict]:
        """Get agent metrics, optionally filtered by agent name."""
        if agent_name:
            rows = self._db.execute(
                "SELECT agent_name, metric_type, "
                "SUM(cost_usd) as total_cost, COUNT(*) as count, "
                "SUM(input_tokens) as total_input, SUM(output_tokens) as total_output "
                "FROM agent_metrics "
                "WHERE agent_name = ? AND timestamp > datetime('now', ?) "
                "GROUP BY metric_type",
                (agent_name, f"-{days} days"),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT agent_name, "
                "SUM(cost_usd) as total_cost, COUNT(*) as count, "
                "SUM(input_tokens) as total_input, SUM(output_tokens) as total_output "
                "FROM agent_metrics "
                "WHERE timestamp > datetime('now', ?) "
                "GROUP BY agent_name",
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- Audit Log --

    def record_audit(
        self,
        action: str,
        agent_name: str = "",
        user_id: str = "",
        platform: str = "",
        details: str = "",
        cost_usd: float = 0.0,
        success: bool = True,
    ) -> None:
        """Record a structured audit log entry."""
        self._db.execute(
            "INSERT INTO audit_log "
            "(action, agent_name, user_id, platform, details, cost_usd, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action, agent_name, user_id, platform, details[:5000], cost_usd, success),
        )
        self._db.commit()

    def get_audit_log(
        self,
        action: str | None = None,
        agent_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query audit log entries with optional filters."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list = []
        if action:
            query += " AND action = ?"
            params.append(action)
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- Task Queue --

    def create_task(
        self, task_id: str, agent_name: str, prompt: str,
        task_type: str = "default", platform: str = "spawn", user_id: str = "local",
        metadata: str | None = None, goal_id: int | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO task_queue "
            "(id, agent_name, prompt, status, task_type, platform, user_id, metadata, goal_id) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (task_id, agent_name, prompt, task_type, platform, user_id, metadata, goal_id),
        )
        self._db.commit()

    def update_task_status(
        self, task_id: str, status: str,
        result: str | None = None, error: str | None = None, cost_usd: float = 0.0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if status == "running":
            self._db.execute(
                "UPDATE task_queue SET status = ?, started_at = ? WHERE id = ?",
                (status, now, task_id),
            )
        elif status in ("completed", "failed"):
            self._db.execute(
                "UPDATE task_queue SET status = ?, result = ?, error = ?, cost_usd = ?, "
                "completed_at = ? WHERE id = ?",
                (status, result, error, cost_usd, now, task_id),
            )
        else:
            self._db.execute(
                "UPDATE task_queue SET status = ? WHERE id = ?",
                (status, task_id),
            )
        self._db.commit()

    def get_pending_tasks(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM task_queue WHERE status IN ('pending', 'running') "
            "ORDER BY created_at",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task(self, task_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM task_queue WHERE id = ?", (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_recent_tasks(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM task_queue ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Failure Analyses --

    def record_failure(
        self, agent_name: str, task_type: str, category: str,
        root_cause: str, lesson: str, severity: str,
        recurrence_risk: str, error_hash: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO failure_analyses "
            "(agent_name, task_type, category, root_cause, lesson, severity, "
            "recurrence_risk, error_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_name, task_type, category, root_cause, lesson, severity,
             recurrence_risk, error_hash),
        )
        self._db.commit()

    def get_failure_patterns(self, days: int = 7) -> list[dict]:
        rows = self._db.execute(
            "SELECT category, root_cause, lesson, severity, COUNT(*) as occurrences "
            "FROM failure_analyses "
            "WHERE timestamp >= datetime('now', ?)"
            "GROUP BY error_hash ORDER BY occurrences DESC LIMIT 20",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_failures(self, agent_name: str | None = None, limit: int = 20) -> list[dict]:
        if agent_name:
            rows = self._db.execute(
                "SELECT * FROM failure_analyses WHERE agent_name = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (agent_name, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM failure_analyses ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- Evolution Log --

    def record_evolution(
        self, agent_name: str, file_changed: str, operation: str,
        section_heading: str | None = None, rationale: str | None = None,
        old_content_hash: str | None = None, new_content_hash: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._db.execute(
            "INSERT INTO evolution_log "
            "(agent_name, file_changed, operation, section_heading, rationale, "
            "old_content_hash, new_content_hash, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_name, file_changed, operation, section_heading, rationale,
             old_content_hash, new_content_hash, dry_run),
        )
        self._db.commit()

    def get_evolution_history(
        self, agent_name: str | None = None, limit: int = 50,
    ) -> list[dict]:
        if agent_name:
            rows = self._db.execute(
                "SELECT * FROM evolution_log WHERE agent_name = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (agent_name, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM evolution_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- Discussions --

    def record_discussion(
        self, discussion_id: str, discussion_type: str, topic: str,
        initiator: str, participants: list[str], outcome: str,
        total_turns: int, total_cost_usd: float, duration_ms: int,
        synthesis: str = "", transcript: str = "",
    ) -> None:
        import json as _json
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT OR REPLACE INTO discussions "
            "(id, discussion_type, topic, initiator, participants, outcome, "
            "total_turns, total_cost_usd, duration_ms, synthesis, transcript, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (discussion_id, discussion_type, topic[:2000], initiator,
             _json.dumps(participants), outcome, total_turns, total_cost_usd,
             duration_ms, synthesis[:5000], transcript[:10000], now),
        )
        self._db.commit()

    def get_recent_discussions(
        self, discussion_type: str | None = None,
        initiator: str | None = None, limit: int = 20,
    ) -> list[dict]:
        query = "SELECT * FROM discussions WHERE 1=1"
        params: list = []
        if discussion_type:
            query += " AND discussion_type = ?"
            params.append(discussion_type)
        if initiator:
            query += " AND initiator = ?"
            params.append(initiator)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_discussion(self, discussion_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM discussions WHERE id = ?", (discussion_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_discussion_stats(self, days: int = 7) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*) as total, "
            "COALESCE(SUM(total_cost_usd), 0) as total_cost, "
            "COALESCE(AVG(total_turns), 0) as avg_turns, "
            "SUM(CASE WHEN outcome = 'converged' THEN 1 ELSE 0 END) as converged "
            "FROM discussions "
            "WHERE timestamp >= datetime('now', ?)",
            (f"-{days} days",),
        ).fetchone()
        return dict(row) if row else {}
