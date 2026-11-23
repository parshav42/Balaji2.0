BEGIN;
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','device')),
    status TEXT NOT NULL CHECK(status IN ('pending','approved','revoked')),
    secret_hash TEXT NOT NULL UNIQUE, expires_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_codes (
    secret_hash TEXT PRIMARY KEY, role TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, actor TEXT NOT NULL, session_id TEXT NOT NULL,
    request_hash TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY, run_id TEXT REFERENCES runs(id), actor TEXT NOT NULL,
    action TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_approvals (
    approval_id TEXT PRIMARY KEY REFERENCES approvals(id),
    run_id TEXT NOT NULL REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('reminder','task_summary')),
    recurrence TEXT NOT NULL CHECK(recurrence IN ('once','daily','weekly')),
    timezone TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
    next_run INTEGER NOT NULL, last_run INTEGER
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), scheduled_at INTEGER NOT NULL,
    ran_at INTEGER NOT NULL, outcome TEXT NOT NULL, message TEXT NOT NULL,
    UNIQUE(job_id, scheduled_at)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    digest TEXT PRIMARY KEY, actor TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_drafts (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, thread_id TEXT NOT NULL, message_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft', sent_id TEXT
);
CREATE TABLE IF NOT EXISTS memory_categories (
    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE, category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_settings (category TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
COMMIT;
