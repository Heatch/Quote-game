-- SQLite schema for the quote game bot.
-- Applied by database.py (at connect) and migrate_to_sqlite.py (at migration).
-- user_version tracks schema revisions for future migrations.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS members (
    discord_id     INTEGER PRIMARY KEY,   -- Discord snowflake
    name           TEXT NOT NULL,
    discriminator  TEXT,
    display_name   TEXT NOT NULL,
    preferred_name TEXT,
    is_bot         INTEGER NOT NULL DEFAULT 0,
    joined_at      TEXT,                  -- ISO-8601 UTC
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS member_nicks (
    member_id INTEGER NOT NULL REFERENCES members(discord_id) ON DELETE CASCADE,
    nick      TEXT NOT NULL,
    PRIMARY KEY (member_id, nick)
);

-- Nick matching is case-insensitive in Python and shared nicks across members
-- drive ambiguity detection, so this index is deliberately NOT unique.
CREATE INDEX IF NOT EXISTS idx_member_nicks_nick ON member_nicks(nick);

CREATE TABLE IF NOT EXISTS quotes (
    id               INTEGER PRIMARY KEY,
    quoter_id        INTEGER NOT NULL,    -- no FK: quoter need not be an onboarded member
    original_quote   TEXT NOT NULL,
    redacted_quote   TEXT NOT NULL,
    quote_timestamp  TEXT NOT NULL,       -- normalized ISO-8601 UTC
    source_message_id INTEGER UNIQUE,     -- Discord message id of the #quotes message
    played_at        TEXT,                -- NULL = still in the active pool
    UNIQUE (quoter_id, quote_timestamp)
);

CREATE TABLE IF NOT EXISTS quote_mentions (
    quote_id  INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(discord_id),
    position  INTEGER NOT NULL,           -- 0-based order of appearance (drives multi-guess ordering)
    PRIMARY KEY (quote_id, position)
);

CREATE INDEX IF NOT EXISTS idx_quote_mentions_member ON quote_mentions(member_id);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);

PRAGMA user_version = 1;
