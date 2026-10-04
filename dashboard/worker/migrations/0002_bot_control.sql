-- Bot control plane + per-trade config snapshot.
-- NOT idempotent (ALTER TABLE ADD COLUMN): apply exactly once.
--   wrangler d1 execute confluence-paper-journal --remote --file=migrations/0002_bot_control.sql
-- Existing trades and equity snapshots are tagged bot_id='book2' via the column default.

ALTER TABLE trades ADD COLUMN bot_id TEXT DEFAULT 'book2';
ALTER TABLE trades ADD COLUMN setup TEXT;
ALTER TABLE trades ADD COLUMN risk_pct REAL;            -- risk % requested for this trade
ALTER TABLE trades ADD COLUMN realized_risk_pct REAL;   -- risk % actually taken after the 5% margin cap
ALTER TABLE trades ADD COLUMN tp_pct REAL;              -- take-profit override used (NULL = rules' 1:2 R:R)
ALTER TABLE trades ADD COLUMN trail_pct REAL;           -- trailing stop % of price used (0 = off)
ALTER TABLE trades ADD COLUMN config_version INTEGER;
ALTER TABLE trades ADD COLUMN run_id INTEGER;
ALTER TABLE account_snapshots ADD COLUMN bot_id TEXT DEFAULT 'book2';

CREATE TABLE bots (
  bot_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  flatten_requested INTEGER NOT NULL DEFAULT 0,
  params_editable INTEGER NOT NULL DEFAULT 0,
  risk_pct REAL NOT NULL DEFAULT 1.0,
  tp_pct REAL,
  trail_pct REAL,
  version INTEGER NOT NULL DEFAULT 1,
  run_id INTEGER NOT NULL DEFAULT 1,
  run_started_at TEXT,
  updated_at TEXT,
  last_seen_at TEXT,
  last_bar TEXT,
  config_source TEXT
);

CREATE TABLE bot_config_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);

-- The running Book 2 bot: start/stop only. Its parameters are locked so its validation run stays unmodified.
INSERT INTO bots (bot_id, label, enabled, params_editable, risk_pct, run_started_at)
VALUES ('book2', 'Book 2 engine (live paper run)', 1, 0, 1.0, '2026-07-30T00:00:00Z');

-- The 3-setup bot starts PAUSED; it trades only after you press Start.
INSERT INTO bots (bot_id, label, enabled, params_editable, risk_pct)
VALUES ('setups', '3-setup bot (ORB / Pullback / Reversal)', 0, 1, 1.0);
