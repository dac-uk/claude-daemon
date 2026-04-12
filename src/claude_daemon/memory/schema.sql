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
