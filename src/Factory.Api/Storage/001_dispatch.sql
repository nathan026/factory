CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, activity TEXT NOT NULL, turn BIGINT NOT NULL, document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(activity, turn);
CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL REFERENCES tasks(id), seq INTEGER NOT NULL, entry TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS dispatch_outbox (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL,
    request TEXT NOT NULL, scheduler_id TEXT UNIQUE, first_submission BIGINT
);
CREATE TABLE IF NOT EXISTS dispatch_inbox (
    id TEXT PRIMARY KEY REFERENCES dispatch_outbox(id), claim TEXT NOT NULL, deadline BIGINT NOT NULL,
    output TEXT, applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0,1))
);
CREATE TABLE IF NOT EXISTS gate_evaluations (
    task_id TEXT NOT NULL REFERENCES tasks(id), seq INTEGER NOT NULL, evaluation TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS dispatch_sessions (
    operation_id TEXT PRIMARY KEY REFERENCES dispatch_outbox(id), session_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS dashboard_queue (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id), summary TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_discussion (
    task_id TEXT NOT NULL REFERENCES tasks(id), message_id TEXT NOT NULL, seq INTEGER NOT NULL, exchange TEXT NOT NULL,
    PRIMARY KEY(task_id,message_id), UNIQUE(task_id,seq)
);
CREATE TABLE IF NOT EXISTS discussion_jobs (
    operation TEXT PRIMARY KEY REFERENCES dispatch_outbox(id), task_id TEXT NOT NULL REFERENCES tasks(id),
    message_id TEXT NOT NULL, deadline BIGINT NOT NULL, pending INTEGER NOT NULL CHECK(pending IN (0,1)),
    UNIQUE(task_id,message_id)
);
CREATE INDEX IF NOT EXISTS discussion_pending ON discussion_jobs(pending,deadline);
CREATE TABLE IF NOT EXISTS dispatch_watch (
    id TEXT PRIMARY KEY REFERENCES dispatch_outbox(id), deadline BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_holds (
    id TEXT PRIMARY KEY REFERENCES dispatch_outbox(id), kind TEXT NOT NULL, gate_seq INTEGER NOT NULL,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS github_intake (
    repo_id BIGINT NOT NULL, issue_number INTEGER NOT NULL, version BIGINT NOT NULL,
    observation TEXT NOT NULL, eligible INTEGER NOT NULL, task_id TEXT UNIQUE REFERENCES tasks(id), origin TEXT,
    PRIMARY KEY(repo_id,issue_number)
);
CREATE TABLE IF NOT EXISTS github_publications (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id), intent TEXT NOT NULL, head_sha TEXT,
    ref_attempted INTEGER NOT NULL DEFAULT 0, pr_attempted INTEGER NOT NULL DEFAULT 0, receipt TEXT, problem TEXT
);
CREATE TABLE IF NOT EXISTS github_publication_holds (
    task_id TEXT PRIMARY KEY REFERENCES github_publications(task_id), head_sha TEXT NOT NULL,
    reason TEXT NOT NULL, observed_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS controller_project (
    id INTEGER PRIMARY KEY, profile TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ci_runs (
    id TEXT PRIMARY KEY REFERENCES dispatch_outbox(id), task_id TEXT NOT NULL REFERENCES tasks(id),
    binding TEXT NOT NULL, evidence TEXT, receipt_hash TEXT
);
CREATE TABLE IF NOT EXISTS funding_requests (
    task_id TEXT NOT NULL REFERENCES tasks(id), id TEXT NOT NULL, question TEXT NOT NULL,
    context TEXT NOT NULL, account TEXT NOT NULL, identity_hash TEXT NOT NULL, PRIMARY KEY(task_id,id), UNIQUE(id)
);
CREATE TABLE IF NOT EXISTS funding_attempts (
    request_id TEXT NOT NULL REFERENCES funding_requests(id), attempt INTEGER NOT NULL,
    state TEXT NOT NULL, reason TEXT NOT NULL, reserved BIGINT NOT NULL, input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL, estimate BIGINT NOT NULL DEFAULT 0,
    created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL, receipt TEXT, PRIMARY KEY(request_id,attempt)
);
CREATE TABLE IF NOT EXISTS funding_waits (
    account TEXT PRIMARY KEY, epoch TEXT NOT NULL, kind TEXT NOT NULL, retry_at BIGINT
);
CREATE INDEX IF NOT EXISTS funding_requests_account ON funding_requests(account,task_id);
CREATE TABLE IF NOT EXISTS subscription_waits (
    id TEXT PRIMARY KEY REFERENCES dispatch_holds(id), observed_after TEXT NOT NULL, clearance TEXT
);
CREATE TABLE IF NOT EXISTS restore_history (
    seq BIGINT PRIMARY KEY, restore_id TEXT NOT NULL UNIQUE, backup_id TEXT NOT NULL, revision TEXT NOT NULL,
    restored_utc TEXT NOT NULL, released_utc TEXT, reconciliation TEXT
);
CREATE TABLE IF NOT EXISTS restore_retirements (
    restore_id TEXT NOT NULL REFERENCES restore_history(restore_id),
    operation_id TEXT NOT NULL REFERENCES dispatch_outbox(id), evidence TEXT NOT NULL,
    PRIMARY KEY(restore_id,operation_id)
);
CREATE TABLE IF NOT EXISTS slack_notices (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), event_key TEXT NOT NULL, destination TEXT NOT NULL,
    notice TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, retry_at BIGINT,
    message_ts TEXT, actor TEXT, choice TEXT, UNIQUE(task_id,event_key)
);
CREATE TABLE IF NOT EXISTS github_issue_actions (
    task_id TEXT NOT NULL REFERENCES tasks(id), head_sha TEXT NOT NULL, id TEXT NOT NULL,
    kind TEXT NOT NULL, scope TEXT NOT NULL, base_sha TEXT NOT NULL, stage TEXT NOT NULL,
    body TEXT NOT NULL, comment_id TEXT, archived_task TEXT NOT NULL, intent TEXT NOT NULL,
    origin TEXT NOT NULL, draft TEXT NOT NULL, ci_total INTEGER NOT NULL, sequence INTEGER NOT NULL, owns_branch INTEGER NOT NULL,
    PRIMARY KEY(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS github_current_issue_action ON github_issue_actions(task_id,head_sha) WHERE stage <> 'withdrawn';
CREATE TABLE IF NOT EXISTS github_desk_scopes (
    repo_id BIGINT NOT NULL, issue_number INTEGER NOT NULL, document TEXT NOT NULL,
    PRIMARY KEY(repo_id,issue_number)
);
CREATE TABLE IF NOT EXISTS verification_revisions (
    task_id TEXT NOT NULL REFERENCES tasks(id), checkpoint TEXT NOT NULL, approval_id TEXT NOT NULL UNIQUE,
    archived_task TEXT NOT NULL, origin TEXT NOT NULL, base_sha TEXT NOT NULL, policy_hash TEXT NOT NULL, ci_total INTEGER NOT NULL,
    PRIMARY KEY(task_id,checkpoint)
);
CREATE TABLE IF NOT EXISTS github_branch_cleanup (
    action_id TEXT NOT NULL REFERENCES github_issue_actions(id), branch TEXT NOT NULL, result TEXT,
    PRIMARY KEY(action_id,branch)
);

CREATE TABLE IF NOT EXISTS github_scope_discussions (
    repo_id BIGINT NOT NULL, issue_number INTEGER NOT NULL, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    document TEXT NOT NULL, PRIMARY KEY(repo_id,issue_number)
);
