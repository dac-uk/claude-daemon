-- Claude Daemon SQLite schema

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0.0,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tokens_used INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS memory_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES conversations(id),
    summary TEXT NOT NULL,
    summary_type TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_session
    ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON conversations(user_id, platform);
CREATE INDEX IF NOT EXISTS idx_conversations_status
    ON conversations(status);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_summaries_type
    ON memory_summaries(summary_type);

-- Full-text search index on message content
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

-- Triggers to keep FTS index in sync
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

-- Per-agent metrics for observability
CREATE TABLE IF NOT EXISTS agent_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metric_type TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    duration_ms INTEGER DEFAULT 0,
    model TEXT,
    platform TEXT,
    success BOOLEAN DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_name ON agent_metrics(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_ts ON agent_metrics(timestamp);

-- Structured audit log for every significant daemon action
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

-- Persistent task queue (survives daemon restarts)
CREATE TABLE IF NOT EXISTS task_queue (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    error TEXT,
    cost_usd REAL DEFAULT 0.0,
    task_type TEXT DEFAULT 'default',
    platform TEXT DEFAULT 'spawn',
    user_id TEXT DEFAULT 'local',
    metadata TEXT,
    goal_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);

-- Failure analysis records for lesson extraction
CREATE TABLE IF NOT EXISTS failure_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_name TEXT,
    task_type TEXT,
    category TEXT,
    root_cause TEXT,
    lesson TEXT,
    severity TEXT,
    recurrence_risk TEXT,
    error_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_failure_category ON failure_analyses(category);

-- Evolution log tracking self-applied prompt mutations
CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_name TEXT NOT NULL,
    file_changed TEXT NOT NULL,
    operation TEXT NOT NULL,
    section_heading TEXT,
    rationale TEXT,
    old_content_hash TEXT,
    new_content_hash TEXT,
    dry_run BOOLEAN DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_evolution_agent ON evolution_log(agent_name);

-- Inter-agent discussion session records
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

-- Budget caps for cost governance (native orchestration Phase 2)
CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                -- global | agent | user | task_type
    scope_value TEXT,                   -- e.g. agent name, user id (null for global)
    limit_usd REAL NOT NULL,
    period TEXT NOT NULL,               -- daily | weekly | monthly | lifetime
    current_spend REAL NOT NULL DEFAULT 0.0,
    reset_at TIMESTAMP,
    approval_threshold_usd REAL,        -- null = no approval needed
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_budgets_scope ON budgets(scope, scope_value);

-- Goal tracking (native orchestration Phase 3)
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

-- Approval queue (native orchestration Phase 4)
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

-- Team definitions
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lead_agent TEXT,
    members TEXT NOT NULL
);
