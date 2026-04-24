"""ConversationStore - SQLite-backed conversation history and session tracking."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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

        # conversations: ensure status column exists (added after initial schema)
        try:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(conversations)").fetchall()}
            if "status" not in cols:
                log.info("Migrating schema: adding conversations.status column")
                self._db.execute(
                    "ALTER TABLE conversations ADD COLUMN status TEXT DEFAULT 'active'"
                )
                self._db.commit()
        except sqlite3.OperationalError:
            pass

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
            if "source" not in cols:
                log.info("Migrating schema: adding task_queue.source column")
                self._db.execute(
                    "ALTER TABLE task_queue ADD COLUMN source TEXT NOT NULL DEFAULT 'api'"
                )
                self._db.commit()
            if "session_id" not in cols:
                log.info("Migrating schema: adding task_queue.session_id column")
                self._db.execute("ALTER TABLE task_queue ADD COLUMN session_id TEXT")
                self._db.commit()
            # Backfill session_id from conversations for tasks that predate the
            # column. Conversation user_id patterns:
            #   spawn_task → "{user_id}:spawn:{task_id}:{agent_name}"
            #   chat / heartbeat / direct send_to_agent → "{user_id}:{agent_name}"
            # Try the spawn pattern first (more specific), then fall back to
            # the plain pattern for any rows still NULL. Idempotent.
            try:
                self._db.execute(
                    "UPDATE task_queue SET session_id = ("
                    "  SELECT c.session_id FROM conversations c"
                    "  WHERE c.user_id = task_queue.user_id || ':spawn:' || task_queue.id || ':' || task_queue.agent_name"
                    "  ORDER BY c.last_active DESC LIMIT 1"
                    ") WHERE session_id IS NULL"
                )
                self._db.execute(
                    "UPDATE task_queue SET session_id = ("
                    "  SELECT c.session_id FROM conversations c"
                    "  WHERE c.user_id = task_queue.user_id || ':' || task_queue.agent_name"
                    "  ORDER BY c.last_active DESC LIMIT 1"
                    ") WHERE session_id IS NULL"
                )
                self._db.commit()
            except sqlite3.OperationalError:
                pass
        except sqlite3.OperationalError:
            # task_queue doesn't exist yet — schema.sql will create it on next startup
            pass

        # discussions: add action_task_ids column for council→task linking
        try:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(discussions)").fetchall()}
            if "action_task_ids" not in cols:
                log.info("Migrating schema: adding discussions.action_task_ids column")
                self._db.execute(
                    "ALTER TABLE discussions ADD COLUMN action_task_ids TEXT DEFAULT '[]'"
                )
                self._db.commit()
        except sqlite3.OperationalError:
            pass

        # workflow_state: persistent workflow checkpoint/resume
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_state (
                workflow_id TEXT PRIMARY KEY,
                workflow_type TEXT NOT NULL,
                initiator TEXT,
                steps_json TEXT NOT NULL,
                results_json TEXT NOT NULL DEFAULT '[]',
                current_step INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                original_request TEXT,
                max_total_cost REAL DEFAULT 0,
                actual_cost REAL DEFAULT 0,
                platform TEXT,
                user_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # task_plans: conversation-level multi-step planning
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS task_plans (
                plan_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                goal TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                conv_id INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
        """)
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

    def has_recent_user_messages(self, minutes: int = 10) -> bool:
        """Check if any user messages were sent in the last N minutes."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        row = self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE timestamp > ? AND role = 'user'",
            (cutoff,),
        ).fetchone()
        return bool(row and row[0] > 0)

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

    def get_cost_snapshot(self, days: int | None = None) -> dict:
        """Return a unified cost view across conversations + agent_metrics.

        The dashboard displays cost in three places (topbar, Agent Fleet cards,
        Cost tab). Historically each read a different source and showed a
        different number. This snapshot is the single source of truth.

        The by_agent map is built primarily from agent_metrics (which records
        every completed agent action including heartbeats and spawn tasks),
        then folded in with conversations-only agents that have no metrics
        row yet. user_id format is either "<user>:<agent>" or
        "<user>:spawn:<task_id>" — the spawn form is already covered via
        agent_metrics, so we skip it when parsing conversations.

        When ``days`` is given, agent_metrics rows are filtered by their
        per-row ``timestamp``. ``conversations`` rows lack a per-cost
        timestamp (costs accumulate into ``total_cost_usd`` over the
        conversation's lifetime), so in windowed mode we treat the
        conversations source as unavailable and rely entirely on
        agent_metrics. ``by_source.conversations`` is reported as 0 to
        make this explicit to callers. Picking ``max()`` over both
        sources would otherwise leak all-time conversation cost into a
        7-day window and produce the inverted totals the dashboard had
        before this fix.
        """
        windowed = days is not None and days > 0

        if windowed:
            window_clause = f"-{int(days)} days"
            metrics_total_row = self._db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS t FROM agent_metrics "
                "WHERE timestamp > datetime('now', ?)",
                (window_clause,),
            ).fetchone()
            metrics_total = float(
                metrics_total_row["t"] if metrics_total_row else 0,
            )
            conv_total = 0.0

            by_agent: dict[str, float] = {}
            for r in self._db.execute(
                "SELECT agent_name, COALESCE(SUM(cost_usd), 0) AS cost "
                "FROM agent_metrics "
                "WHERE timestamp > datetime('now', ?) "
                "GROUP BY agent_name",
                (window_clause,),
            ).fetchall():
                name = r["agent_name"]
                if name:
                    by_agent[name] = float(r["cost"])

            deduped_total = metrics_total
        else:
            conv_total_row = self._db.execute(
                "SELECT COALESCE(SUM(total_cost_usd), 0) AS t "
                "FROM conversations",
            ).fetchone()
            conv_total = float(
                conv_total_row["t"] if conv_total_row else 0,
            )

            metrics_total_row = self._db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS t FROM agent_metrics",
            ).fetchone()
            metrics_total = float(
                metrics_total_row["t"] if metrics_total_row else 0,
            )

            by_agent = {}
            for r in self._db.execute(
                "SELECT agent_name, COALESCE(SUM(cost_usd), 0) AS cost "
                "FROM agent_metrics GROUP BY agent_name",
            ).fetchall():
                name = r["agent_name"]
                if name:
                    by_agent[name] = float(r["cost"])

            for r in self._db.execute(
                "SELECT user_id, COALESCE(SUM(total_cost_usd), 0) AS cost "
                "FROM conversations GROUP BY user_id",
            ).fetchall():
                uid = str(r["user_id"] or "")
                parts = uid.split(":")
                if len(parts) >= 3 and parts[1] == "spawn":
                    continue
                if len(parts) < 2:
                    continue
                name = parts[-1]
                if name and name not in by_agent:
                    by_agent[name] = float(r["cost"])

            deduped_total = max(conv_total, metrics_total)

        return {
            "total_usd": deduped_total,
            "by_agent": by_agent,
            "days": days,
            "by_source": {
                "conversations": conv_total,
                "agent_metrics": metrics_total,
                "deduped_total": deduped_total,
            },
        }

    def count_agents_with_conversations(self) -> int:
        """Number of distinct agents that appear in the conversations table.

        Used by the dashboard topbar to distinguish "agents configured" from
        "agents that have actually talked". The 7-vs-6 mismatch the user
        reported was this: 7 agents exist, 6 have a conversation row.
        """
        row = self._db.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM conversations "
            "WHERE user_id LIKE '%:%' AND user_id NOT LIKE '%:spawn:%'",
        ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _attribute_session(uid: str) -> tuple[str | None, str | None]:
        """Parse a conversations.user_id into (agent_name, task_id).

        user_id shapes we recognise:
          "<user>:<agent>"          -> (agent, None)
          "<user>:spawn:<task_id>"  -> (None, task_id)  # caller joins task_queue
          "<plain>"                 -> (None, None)
        """
        if ":spawn:" in uid:
            _, _, tid = uid.partition(":spawn:")
            return None, (tid or None)
        if ":" in uid:
            agent = uid.rsplit(":", 1)[1]
            return (agent or None), None
        return None, None

    def list_sessions(self, limit: int = 500) -> list[dict]:
        """All sessions (chatted + spawned) with agent attribution.

        For spawn sessions, the agent name is resolved by joining the
        embedded task_id against task_queue.agent_name. Ordered newest-first
        by last_active so the dashboard can show "what's live right now".
        """
        rows = self._db.execute(
            "SELECT session_id, user_id, started_at, last_active, "
            "total_cost_usd, message_count, platform, status "
            "FROM conversations "
            "ORDER BY last_active DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            uid = str(r["user_id"] or "")
            agent, task_id = self._attribute_session(uid)
            if task_id and not agent:
                tr = self._db.execute(
                    "SELECT agent_name FROM task_queue WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if tr:
                    agent = tr["agent_name"]
            out.append({
                "session_id": r["session_id"],
                "user_id": uid,
                "agent": agent,
                "task_id": task_id,
                "started_at": r["started_at"],
                "last_active": r["last_active"],
                "cost_usd": float(r["total_cost_usd"] or 0),
                "message_count": int(r["message_count"] or 0),
                "platform": r["platform"],
                "status": r["status"],
                "kind": "spawn" if task_id else ("chat" if agent else "unknown"),
            })
        return out

    def session_summary(self) -> dict:
        """Aggregate session counts for the topbar Sessions stat.

        Returns {total, by_agent: {name: {chat, spawn, total}}, unattributed}.
        Unattributed covers legacy conversations with no colon in user_id
        and spawn rows whose task_id no longer resolves.

        Runs directly off SQL and tolerates malformed rows — a single bad
        user_id must not 500 the whole dashboard click-through.
        """
        try:
            total_row = self._db.execute(
                "SELECT COUNT(*) AS n FROM conversations",
            ).fetchone()
            total = int(total_row["n"]) if total_row else 0
        except Exception:
            log.exception("session_summary: count failed")
            total = 0

        by_agent: dict[str, dict[str, int]] = {}
        unattributed = 0

        try:
            rows = self._db.execute(
                "SELECT user_id FROM conversations",
            ).fetchall()
        except Exception:
            log.exception("session_summary: user_id fetch failed")
            rows = []

        # Cache task_queue agent lookups so a single spawn-task row isn't
        # queried thousands of times in legacy databases.
        task_agent_cache: dict[str, str | None] = {}

        for r in rows:
            try:
                uid = str(r["user_id"] or "")
                agent, task_id = self._attribute_session(uid)
                kind: str
                if task_id:
                    if task_id not in task_agent_cache:
                        try:
                            tr = self._db.execute(
                                "SELECT agent_name FROM task_queue "
                                "WHERE id = ?",
                                (task_id,),
                            ).fetchone()
                            task_agent_cache[task_id] = (
                                tr["agent_name"] if tr else None
                            )
                        except Exception:
                            log.exception(
                                "session_summary: task lookup for %s failed",
                                task_id,
                            )
                            task_agent_cache[task_id] = None
                    agent = agent or task_agent_cache[task_id]
                    kind = "spawn"
                elif agent:
                    kind = "chat"
                else:
                    kind = "unknown"

                if not agent:
                    unattributed += 1
                    continue
                bucket = by_agent.setdefault(
                    agent, {"chat": 0, "spawn": 0, "total": 0},
                )
                bucket[kind if kind in ("chat", "spawn") else "chat"] += 1
                bucket["total"] += 1
            except Exception:
                # One malformed row must not poison the whole summary.
                log.exception("session_summary: row skipped")
                unattributed += 1

        return {
            "total": total,
            "by_agent": by_agent,
            "unattributed": unattributed,
        }

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

    def get_daily_agent_model_costs(self) -> list[dict]:
        """Return today's cost per (agent_name, model) from agent_metrics.

        Results are ordered by cost descending and include only rows where
        both agent_name and model are non-empty, so the caller can apply
        per-model thresholds without surprises.
        """
        rows = self._db.execute(
            "SELECT agent_name, model, COALESCE(SUM(cost_usd), 0) AS total_cost "
            "FROM agent_metrics "
            "WHERE date(timestamp) = date('now') "
            "  AND agent_name IS NOT NULL AND agent_name != '' "
            "  AND model IS NOT NULL AND model != '' "
            "GROUP BY agent_name, model "
            "ORDER BY total_cost DESC",
        ).fetchall()
        return [
            {
                "agent_name": r["agent_name"],
                "model": r["model"],
                "total_cost": float(r["total_cost"]),
            }
            for r in rows
        ]

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
        initial_status: str = "pending", source: str = "api",
        session_id: str | None = None,
    ) -> None:
        """Insert a new task_queue row with the given initial status.

        ``initial_status`` lets callers create rows directly as
        ``pending_approval`` without a follow-up UPDATE that would briefly
        misclassify the row as ``pending``.
        ``source`` tags the origin (api|chat|spawn|heartbeat) for Operations
        filtering.
        """
        self._db.execute(
            "INSERT INTO task_queue "
            "(id, agent_name, prompt, status, task_type, platform, user_id, "
            "metadata, goal_id, source, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, agent_name, prompt, initial_status, task_type, platform,
             user_id, metadata, goal_id, source, session_id),
        )
        self._db.commit()

    def update_task_status(
        self, task_id: str, status: str,
        result: str | None = None, error: str | None = None, cost_usd: float = 0.0,
        session_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if status == "running":
            if session_id:
                self._db.execute(
                    "UPDATE task_queue SET status = ?, started_at = ?, session_id = ? "
                    "WHERE id = ?",
                    (status, now, session_id, task_id),
                )
            else:
                self._db.execute(
                    "UPDATE task_queue SET status = ?, started_at = ? WHERE id = ?",
                    (status, now, task_id),
                )
        elif status in ("completed", "failed"):
            if session_id:
                self._db.execute(
                    "UPDATE task_queue SET status = ?, result = ?, error = ?, cost_usd = ?, "
                    "completed_at = ?, session_id = ? WHERE id = ?",
                    (status, result, error, cost_usd, now, session_id, task_id),
                )
            else:
                self._db.execute(
                    "UPDATE task_queue SET status = ?, result = ?, error = ?, cost_usd = ?, "
                    "completed_at = ? WHERE id = ?",
                    (status, result, error, cost_usd, now, task_id),
                )
        else:
            if session_id:
                self._db.execute(
                    "UPDATE task_queue SET status = ?, session_id = ? WHERE id = ?",
                    (status, session_id, task_id),
                )
            else:
                self._db.execute(
                    "UPDATE task_queue SET status = ? WHERE id = ?",
                    (status, task_id),
                )
        self._db.commit()

    def update_task_metadata(self, task_id: str, patch: dict) -> bool:
        """Merge ``patch`` into the task's metadata JSON and persist.

        Used by the approval-dispatch path to stash fresh budget reservations
        onto the existing task row.  Returns True if the row was updated.
        """
        row = self._db.execute(
            "SELECT metadata FROM task_queue WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (ValueError, TypeError):
            log.warning("update_task_metadata: corrupt metadata for %s — overwriting", task_id)
            meta = {}
        meta.update(patch)
        self._db.execute(
            "UPDATE task_queue SET metadata = ? WHERE id = ?",
            (json.dumps(meta), task_id),
        )
        self._db.commit()
        return True

    def sweep_orphan_tasks(self, live_ids: set[str]) -> list[dict]:
        """Mark ``running``/``pending`` rows not in ``live_ids`` as failed orphans.

        Called on daemon startup to clean up tasks whose in-memory state was
        lost to a crash.  ``pending_approval`` rows are left alone — they are
        legitimately awaiting a human.  Returns the rows that were swept so
        the caller can release their metadata reservations.
        """
        rows = self._db.execute(
            "SELECT * FROM task_queue WHERE status IN ('running', 'pending')",
        ).fetchall()
        orphans = [dict(r) for r in rows if r["id"] not in live_ids]
        if not orphans:
            return []
        now = datetime.now(timezone.utc).isoformat()
        for o in orphans:
            self._db.execute(
                "UPDATE task_queue SET status = 'failed', error = ?, "
                "completed_at = ? "
                "WHERE id = ? AND status IN ('running', 'pending')",
                ("daemon restarted — orphan task", now, o["id"]),
            )
        self._db.commit()
        log.info("Swept %d orphan task(s) on startup", len(orphans))
        return orphans

    def get_pending_tasks(self, agent_name: str | None = None) -> list[dict]:
        if agent_name:
            rows = self._db.execute(
                "SELECT * FROM task_queue "
                "WHERE status IN ('pending', 'pending_approval') "
                "AND agent_name = ? ORDER BY created_at",
                (agent_name,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM task_queue "
                "WHERE status IN ('pending', 'running', 'pending_approval') "
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
            "WHERE timestamp >= datetime('now', ?) "
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
        action_task_ids: list[str] | None = None,
    ) -> None:
        import json as _json
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT OR REPLACE INTO discussions "
            "(id, discussion_type, topic, initiator, participants, outcome, "
            "total_turns, total_cost_usd, duration_ms, synthesis, transcript, "
            "completed_at, action_task_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (discussion_id, discussion_type, topic[:2000], initiator,
             _json.dumps(participants), outcome, total_turns, total_cost_usd,
             duration_ms, synthesis[:5000], transcript[:10000], now,
             _json.dumps(action_task_ids or [])),
        )
        self._db.commit()

    def update_discussion_post_synthesis(
        self, discussion_id: str, synthesis: str,
        action_task_ids: list[str] | None = None,
    ) -> None:
        """Update synthesis + action task IDs after council completes."""
        import json as _json
        self._db.execute(
            "UPDATE discussions SET synthesis = ?, action_task_ids = ? WHERE id = ?",
            (synthesis[:5000], _json.dumps(action_task_ids or []), discussion_id),
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

    # -- Workflow State --

    def save_workflow_state(
        self, workflow_id: str, workflow_type: str, steps_json: str,
        results_json: str = "[]", current_step: int = 0,
        status: str = "running", original_request: str = "",
        max_total_cost: float = 0, actual_cost: float = 0,
        platform: str = "", user_id: str = "", initiator: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT OR REPLACE INTO workflow_state "
            "(workflow_id, workflow_type, initiator, steps_json, results_json, "
            "current_step, status, original_request, max_total_cost, actual_cost, "
            "platform, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE((SELECT created_at FROM workflow_state WHERE workflow_id = ?), ?), ?)",
            (workflow_id, workflow_type, initiator, steps_json, results_json,
             current_step, status, original_request, max_total_cost, actual_cost,
             platform, user_id, workflow_id, now, now),
        )
        self._db.commit()

    def get_pending_workflows(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM workflow_state WHERE status = 'running' ORDER BY created_at",
        ).fetchall()
        return [dict(r) for r in rows]

    def update_workflow_status(self, workflow_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "UPDATE workflow_state SET status = ?, updated_at = ? WHERE workflow_id = ?",
            (status, now, workflow_id),
        )
        self._db.commit()

    # -- Task Plans (conversation-level multi-step reasoning) --

    def create_plan(
        self, plan_id: str, agent_name: str, goal: str,
        steps_json: str, conv_id: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT OR REPLACE INTO task_plans "
            "(plan_id, agent_name, goal, steps_json, status, conv_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (plan_id, agent_name, goal, steps_json, conv_id, now, now),
        )
        self._db.commit()

    def update_plan(self, plan_id: str, steps_json: str, status: str = "active") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "UPDATE task_plans SET steps_json = ?, status = ?, updated_at = ? "
            "WHERE plan_id = ?",
            (steps_json, status, now, plan_id),
        )
        self._db.commit()

    def get_active_plans(self, agent_name: str | None = None) -> list[dict]:
        if agent_name:
            rows = self._db.execute(
                "SELECT * FROM task_plans WHERE status = 'active' AND agent_name = ? "
                "ORDER BY created_at DESC LIMIT 5",
                (agent_name,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM task_plans WHERE status = 'active' "
                "ORDER BY created_at DESC LIMIT 10",
            ).fetchall()
        return [dict(r) for r in rows]
