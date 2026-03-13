-- D1 schema for JamBetter ops pins

CREATE TABLE IF NOT EXISTS pins (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  pinned INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pins_pinned_created ON pins(pinned, created_at);
