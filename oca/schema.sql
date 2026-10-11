-- OCA platform schema. Executed on every startup; all statements are idempotent.
-- Timestamps are ISO-8601 UTC text; JSON columns are TEXT.

CREATE TABLE IF NOT EXISTS counters (
  name  TEXT PRIMARY KEY,
  next  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
  id                    TEXT PRIMARY KEY,          -- INC-0042
  source                TEXT NOT NULL,             -- health_check | jira | slack | manual
  service               TEXT NOT NULL,
  title                 TEXT NOT NULL,
  status                TEXT NOT NULL,
  severity              TEXT NOT NULL,
  description           TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  resolved_at           TEXT,
  metrics_before        TEXT,                      -- JSON baseline snapshot
  metrics_at_detect     TEXT,                      -- JSON; NULL for externally reported tickets
  external_ref          TEXT,                      -- JSON {"system","key","url","last_synced_at"}
  attempt               INTEGER NOT NULL DEFAULT 0,
  current_deployment_id TEXT,
  pending_action_id     INTEGER
);
CREATE INDEX IF NOT EXISTS tickets_service_status ON tickets (service, status);

CREATE TABLE IF NOT EXISTS ticket_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id  TEXT NOT NULL REFERENCES tickets(id),
  ts         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ticket_events_ticket ON ticket_events (ticket_id, id);

CREATE TABLE IF NOT EXISTS actions (                 -- the gate's ledger
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id     TEXT NOT NULL REFERENCES tickets(id),
  action        TEXT NOT NULL,
  args          TEXT NOT NULL,
  requested_by  TEXT NOT NULL,
  evidence      TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL,
  checks        TEXT NOT NULL DEFAULT '[]',
  reason        TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  decided_at    TEXT,
  decided_by    TEXT,
  deployment_id TEXT
);

CREATE TABLE IF NOT EXISTS deployments (
  id                   TEXT PRIMARY KEY,           -- DEP-0081
  ticket_id            TEXT REFERENCES tickets(id),
  action               TEXT NOT NULL,
  args                 TEXT NOT NULL,
  backup_ref           TEXT,
  status               TEXT NOT NULL,
  started_at           TEXT NOT NULL,
  finished_at          TEXT,
  evidence             TEXT NOT NULL DEFAULT '{}',
  git_sha              TEXT,
  prev_sha             TEXT,
  driftwood_deploy_id  INTEGER,
  note                 TEXT
);

CREATE TABLE IF NOT EXISTS sandbox_runs (
  id          TEXT PRIMARY KEY,                    -- SBX-0003
  ticket_id   TEXT NOT NULL REFERENCES tickets(id),
  patch_ref   TEXT,
  base_sha    TEXT,
  before_json TEXT,
  after_json  TEXT,
  tests_json  TEXT,
  baseline    TEXT NOT NULL DEFAULT '{}',
  reproduced  INTEGER,
  passed      INTEGER,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS health_checks (
  name          TEXT PRIMARY KEY,
  service       TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  interval_s    INTEGER NOT NULL,
  opens_ticket  INTEGER NOT NULL DEFAULT 1,
  last_run      TEXT,
  last_ok       INTEGER,
  last_result   TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL                              -- JSON
);

CREATE TABLE IF NOT EXISTS integration_outbox (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id     TEXT NOT NULL REFERENCES tickets(id),
  target        TEXT NOT NULL,                      -- jira | slack
  kind          TEXT NOT NULL,                      -- create_issue | comment | transition | message
  payload       TEXT NOT NULL,                      -- already policy-filtered; exactly what goes over the wire
  status        TEXT NOT NULL DEFAULT 'pending',
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_attempt  TEXT NOT NULL,
  bytes_sent    INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON integration_outbox (status, next_attempt);

-- Per-ticket state an integration keeps about its own copy of the ticket, one row per
-- (ticket, target): Slack stores the channel and thread it posts into. Kept apart from
-- tickets.external_ref, which names the system that reported the ticket.
CREATE TABLE IF NOT EXISTS integration_links (
  ticket_id   TEXT NOT NULL REFERENCES tickets(id),
  target      TEXT NOT NULL,                        -- jira | slack
  ref         TEXT NOT NULL DEFAULT '{}',           -- JSON, e.g. {"channel","thread_ts"}
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (ticket_id, target)
);
