"""SQLite data access layer for the quote game bot.

All bot code goes through this module; no SQL lives outside it. Rows are
exposed as dicts shaped like the old MongoDB documents, so game logic
(match_guess, parse_guesses, get_display_name) is unchanged.

Connection settings: foreign keys enforced, WAL journal, busy timeout.
"""

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
DEFAULT_DB_PATH = "quote-game.db"

_conn = None


def normalize_timestamp(ts):
    """Return a canonical ISO-8601 UTC string: YYYY-MM-DDTHH:MM:SS.ffffff+00:00.

    Accepts an ISO string (with or without microseconds/offset) or a datetime.
    Naive values are assumed to be UTC. Canonical form keeps lexicographic
    ordering identical to chronological ordering.
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


def connect(path=None):
    """Open (and create if needed) the database, apply schema, and cache it."""
    global _conn
    if path is None:
        path = os.getenv("SQLITE_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA journal_mode = WAL")
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    _conn = conn
    return conn


def get_conn():
    global _conn
    if _conn is None:
        return connect()
    return _conn


def close():
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

def get_metadata(key):
    row = get_conn().execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_metadata(key, value):
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------

def _nicks_by_member(member_ids):
    """Return {member_id: [nick, ...]} preserving insertion order of the nicks."""
    if not member_ids:
        return {}
    placeholders = ",".join("?" * len(member_ids))
    rows = get_conn().execute(
        f"SELECT member_id, nick FROM member_nicks WHERE member_id IN ({placeholders}) "
        "ORDER BY member_id, rowid",
        list(member_ids),
    ).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["member_id"], []).append(row["nick"])
    return out


def _member_dict(row, nicks):
    return {
        "id": row["discord_id"],
        "name": row["name"],
        "discriminator": row["discriminator"],
        "display_name": row["display_name"],
        "preferred_name": row["preferred_name"],
        "bot": bool(row["is_bot"]),
        "joined_at": row["joined_at"],
        "created_at": row["created_at"],
        "nicks": nicks,
    }


def get_member(discord_id):
    row = get_conn().execute(
        "SELECT * FROM members WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    if row is None:
        return None
    return _member_dict(row, _nicks_by_member([discord_id]).get(discord_id, []))


def all_members_with_nicks():
    rows = get_conn().execute("SELECT * FROM members ORDER BY discord_id").fetchall()
    nicks = _nicks_by_member([row["discord_id"] for row in rows])
    return [_member_dict(row, nicks.get(row["discord_id"], [])) for row in rows]


def insert_member(member):
    """Insert a member (Mongo-shaped dict, nicks included). Returns the
    discord_id, or None if a member with that id already exists."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """INSERT INTO members
               (discord_id, name, discriminator, display_name, preferred_name, is_bot, joined_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_id) DO NOTHING""",
            (
                member["id"],
                member["name"],
                member.get("discriminator"),
                member.get("display_name") or member["name"],
                member.get("preferred_name"),
                1 if member.get("bot") else 0,
                normalize_timestamp(member.get("joined_at")) if member.get("joined_at") else None,
                normalize_timestamp(member.get("created_at")) if member.get("created_at") else None,
            ),
        )
        if not cur.rowcount:
            return None
        for nick in member.get("nicks") or []:
            conn.execute(
                "INSERT OR IGNORE INTO member_nicks (member_id, nick) VALUES (?, ?)",
                (member["id"], nick),
            )
    return member["id"]


def upsert_member(member):
    """Insert or refresh a member's profile from Discord, preserving
    preferred_name and nicks (which are not derivable from Discord)."""
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO members
               (discord_id, name, discriminator, display_name, preferred_name, is_bot, joined_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
                 name = excluded.name,
                 discriminator = excluded.discriminator,
                 display_name = excluded.display_name,
                 is_bot = excluded.is_bot,
                 joined_at = excluded.joined_at,
                 created_at = excluded.created_at,
                 preferred_name = COALESCE(members.preferred_name, excluded.preferred_name)""",
            (
                member["id"],
                member["name"],
                member.get("discriminator"),
                member.get("display_name") or member["name"],
                member.get("preferred_name"),
                1 if member.get("bot") else 0,
                normalize_timestamp(member.get("joined_at")) if member.get("joined_at") else None,
                normalize_timestamp(member.get("created_at")) if member.get("created_at") else None,
            ),
        )


def add_nicks(discord_id, nicks):
    """Add nicks for a member, ignoring duplicates. Returns how many were new."""
    conn = get_conn()
    added = 0
    with conn:
        for nick in nicks:
            cur = conn.execute(
                "INSERT OR IGNORE INTO member_nicks (member_id, nick) VALUES (?, ?)",
                (discord_id, nick),
            )
            added += cur.rowcount
    return added


def set_preferred_name(discord_id, name):
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "UPDATE members SET preferred_name = ? WHERE discord_id = ?",
            (name, discord_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------

def _quote_dict(row, members_mentioned):
    return {
        "id": row["id"],
        "quoter_id": row["quoter_id"],
        "original_quote": row["original_quote"],
        "redacted_quote": row["redacted_quote"],
        "timestamp": row["quote_timestamp"],
        "source_message_id": row["source_message_id"],
        "played_at": row["played_at"],
        "members_mentioned": members_mentioned,
    }


def _mentions_for(quote_id):
    rows = get_conn().execute(
        """SELECT qm.position, m.* FROM quote_mentions qm
           JOIN members m ON m.discord_id = qm.member_id
           WHERE qm.quote_id = ? ORDER BY qm.position""",
        (quote_id,),
    ).fetchall()
    nicks = _nicks_by_member([row["discord_id"] for row in rows])
    return [
        {
            "id": row["discord_id"],
            "name": row["name"],
            "display_name": row["display_name"],
            "preferred_name": row["preferred_name"],
            "nicks": nicks.get(row["discord_id"], []),
        }
        for row in rows
    ]


def quote_exists(quoter_id, timestamp, source_message_id=None):
    """Duplicate check by Discord message id when known, else quoter_id + timestamp."""
    conn = get_conn()
    if source_message_id is not None:
        row = conn.execute(
            "SELECT 1 FROM quotes WHERE source_message_id = ?", (source_message_id,)
        ).fetchone()
        if row:
            return True
    row = conn.execute(
        "SELECT 1 FROM quotes WHERE quoter_id = ? AND quote_timestamp = ?",
        (quoter_id, normalize_timestamp(timestamp)),
    ).fetchone()
    return row is not None


def insert_quote(quote):
    """Insert a parsed quote (Mongo-shaped dict) with its mentions.

    Returns the new quote id, or None when the quote is a duplicate
    (matched by source_message_id or quoter_id + timestamp).
    """
    conn = get_conn()
    timestamp = normalize_timestamp(quote["timestamp"])
    source_message_id = quote.get("source_message_id")
    with conn:
        if source_message_id is not None:
            if conn.execute(
                "SELECT 1 FROM quotes WHERE source_message_id = ?", (source_message_id,)
            ).fetchone():
                return None
        if conn.execute(
            "SELECT 1 FROM quotes WHERE quoter_id = ? AND quote_timestamp = ?",
            (quote["quoter_id"], timestamp),
        ).fetchone():
            return None

        cur = conn.execute(
            """INSERT INTO quotes
               (quoter_id, original_quote, redacted_quote, quote_timestamp, source_message_id, played_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                quote["quoter_id"],
                quote["original_quote"],
                quote["redacted_quote"],
                timestamp,
                source_message_id,
                normalize_timestamp(quote["played_at"]) if quote.get("played_at") else None,
            ),
        )
        quote_id = cur.lastrowid
        for position, member in enumerate(quote.get("members_mentioned") or []):
            conn.execute(
                "INSERT INTO quote_mentions (quote_id, member_id, position) VALUES (?, ?, ?)",
                (quote_id, member["id"], position),
            )
    return quote_id


def get_random_unplayed_quote():
    """Return a random quote from the active pool (played_at IS NULL), or None."""
    row = get_conn().execute(
        "SELECT * FROM quotes WHERE played_at IS NULL ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return _quote_dict(row, _mentions_for(row["id"]))


def mark_played(quote_id, when=None):
    when = normalize_timestamp(when) if when else normalize_timestamp(datetime.now(timezone.utc))
    conn = get_conn()
    with conn:
        conn.execute("UPDATE quotes SET played_at = ? WHERE id = ?", (when, quote_id))


def played_count():
    return get_conn().execute(
        "SELECT COUNT(*) AS n FROM quotes WHERE played_at IS NOT NULL"
    ).fetchone()["n"]


def count_quotes():
    return get_conn().execute("SELECT COUNT(*) AS n FROM quotes").fetchone()["n"]


def recycle_history():
    """Return all played quotes to the active pool. Returns how many were reset."""
    conn = get_conn()
    with conn:
        cur = conn.execute("UPDATE quotes SET played_at = NULL WHERE played_at IS NOT NULL")
    return cur.rowcount


def latest_quote_timestamp():
    row = get_conn().execute(
        "SELECT MAX(quote_timestamp) AS ts FROM quotes"
    ).fetchone()
    return row["ts"] if row else None
