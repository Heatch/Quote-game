#!/usr/bin/env python
"""One-off MongoDB -> SQLite migration for the quote game bot.

Reads the live MongoDB (read-only), stages everything in memory, backfills
`source_message_id` from quotes.json and (optionally) from the Discord
#quotes channel, writes a SQLite database, then validates it against MongoDB.

Usage:
    python migrate_to_sqlite.py [--db PATH] [--force] [--skip-discord]

Exit code is non-zero if any validation check fails. MongoDB is never written to.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import database

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail else ""))
    return bool(ok)


def report(name, detail):
    print(f"  [INFO] {name} -- {detail}")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_mongo(uri):
    """Read all collections read-only. Returns staged dicts."""
    from pymongo import MongoClient

    db = MongoClient(uri, serverSelectionTimeoutMS=10000)["quote-game"]

    members = []
    skipped_members = []
    for doc in db.members.find():
        if "id" not in doc:
            skipped_members.append(doc.get("name"))
            continue
        members.append({
            "id": doc["id"],
            "name": doc.get("name"),
            "discriminator": doc.get("discriminator"),
            "display_name": doc.get("display_name"),
            "preferred_name": doc.get("preferred_name"),
            "bot": bool(doc.get("bot")),
            "joined_at": doc.get("joined_at"),
            "created_at": doc.get("created_at"),
            "nicks": list(doc.get("nicks") or []),
        })

    quotes = []
    for coll, played in ((db.quotes, False), (db.history, True)):
        for doc in coll.find():
            quotes.append({
                "mongo_id": str(doc["_id"]),
                "quoter_id": doc["quoter_id"],
                "original_quote": doc["original_quote"],
                "redacted_quote": doc["redacted_quote"],
                "timestamp": doc["timestamp"],
                "played_at": database.normalize_timestamp(doc["moved_to_history_at"]) if played else None,
                "source_message_id": None,
                "members_mentioned": [
                    {
                        "id": m["id"],
                        "name": m.get("name"),
                        "display_name": m.get("display_name"),
                        "preferred_name": m.get("preferred_name"),
                        "nicks": list(m.get("nicks") or []),
                    }
                    for m in (doc.get("members_mentioned") or [])
                ],
            })

    metadata = {doc["key"]: doc.get("timestamp") for doc in db.metadata.find()}
    return {
        "members": members,
        "skipped_members": skipped_members,
        "quotes": quotes,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# source_message_id backfill
# ---------------------------------------------------------------------------

def backfill_from_dump(staged, dump_path):
    """Match quotes to their #quotes Discord message id via quotes.json,
    keyed by (author id, normalized timestamp). Returns matched count."""
    if not os.path.exists(dump_path):
        report("quotes.json backfill", f"{dump_path} not found, skipped")
        return 0

    with open(dump_path, encoding="utf-8") as f:
        dump = json.load(f)

    by_key = {}
    for item in dump:
        key = (item["author"]["id"], database.normalize_timestamp(item["timestamp"]))
        by_key.setdefault(key, []).append(item.get("id"))

    matched = 0
    for quote in staged["quotes"]:
        key = (quote["quoter_id"], database.normalize_timestamp(quote["timestamp"]))
        ids = by_key.get(key, [])
        if len(ids) == 1 and ids[0] is not None:
            quote["source_message_id"] = ids[0]
            matched += 1
    return matched


def backfill_from_discord(staged, channel_id, token):
    """Fetch #quotes after the last known dump message and match the remaining
    quotes by (author id, normalized timestamp). Returns matched count."""
    import asyncio

    import discord

    remaining = [
        q for q in staged["quotes"] if q["source_message_id"] is None
    ]
    if not remaining:
        return 0

    need = {
        (q["quoter_id"], database.normalize_timestamp(q["timestamp"])):
            q for q in remaining
    }
    after = min(datetime.fromisoformat(database.normalize_timestamp(q["timestamp"]))
                for q in remaining) - timedelta(days=1)

    found = {}
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(int(channel_id))
            if channel is None:
                channel = await client.fetch_channel(int(channel_id))
            async for message in channel.history(limit=None, after=after, oldest_first=True):
                key = (message.author.id, database.normalize_timestamp(message.created_at))
                if key in need and key not in found:
                    found[key] = message.id
        finally:
            # client.close() cancels this very task, so connector cleanup
            # happens in runner() below, outside the cancelled task.
            await client.close()

    async def runner():
        try:
            await client.start(token)
        finally:
            # discord.py's HTTPClient never closes the connector it auto-created
            for cleanup in (client.http.close, client.http.connector.close):
                try:
                    await cleanup()
                except Exception:
                    pass

    asyncio.run(runner())

    matched = 0
    for key, message_id in found.items():
        need[key]["source_message_id"] = message_id
        matched += 1
    return matched


def heal_member_nicks(staged):
    """Union any nicks found in quote snapshots into members' nick lists so no
    matching behavior can be lost. Returns how many nicks were added."""
    by_id = {m["id"]: m for m in staged["members"]}
    added = 0
    for quote in staged["quotes"]:
        for mention in quote["members_mentioned"]:
            member = by_id.get(mention["id"])
            if member is None:
                continue
            for nick in mention.get("nicks") or []:
                if nick not in member["nicks"]:
                    member["nicks"].append(nick)
                    added += 1
    return added


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_sqlite(db_path, staged):
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)

    # Write through the production database.py paths so the migration
    # exercises exactly the code the bot will use. Each insert manages its own
    # transaction; a mid-write failure is caught by validation (re-run with
    # --force to start over).
    conn = database.connect(db_path)
    for member in staged["members"]:
        if database.insert_member(member) is None:
            raise SystemExit(f"ERROR: duplicate member id {member['id']} in MongoDB")

    for quote in staged["quotes"]:
        if database.insert_quote(quote) is None:
            raise SystemExit(
                f"ERROR: duplicate quote {quote['quoter_id']}@{quote['timestamp']} in MongoDB"
            )

    for key, value in staged["metadata"].items():
        database.set_metadata(key, value)
    return conn


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(conn, staged, mongo_uri, dump_path):
    counts = {
        "members": conn.execute("SELECT COUNT(*) n FROM members").fetchone()["n"],
        "member_nicks": conn.execute("SELECT COUNT(*) n FROM member_nicks").fetchone()["n"],
        "quotes": conn.execute("SELECT COUNT(*) n FROM quotes").fetchone()["n"],
        "quote_mentions": conn.execute("SELECT COUNT(*) n FROM quote_mentions").fetchone()["n"],
        "played": conn.execute("SELECT COUNT(*) n FROM quotes WHERE played_at IS NOT NULL").fetchone()["n"],
        "metadata": conn.execute("SELECT COUNT(*) n FROM metadata").fetchone()["n"],
    }
    expected = {
        "members": len(staged["members"]),
        "member_nicks": sum(len(m["nicks"]) for m in staged["members"]),
        "quotes": len(staged["quotes"]),
        "quote_mentions": sum(len(q["members_mentioned"]) for q in staged["quotes"]),
        "played": sum(1 for q in staged["quotes"] if q["played_at"]),
        "metadata": len(staged["metadata"]),
    }
    for name in counts:
        check(f"count: {name}", counts[name] == expected[name],
              f"{counts[name]} rows (expected {expected[name]})")

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    check("PRAGMA integrity_check", integrity == "ok", integrity)

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    check("PRAGMA foreign_key_check", not fk_violations,
          f"{len(fk_violations)} violations" if fk_violations else "no violations")

    bad_ts = conn.execute(
        "SELECT COUNT(*) n FROM quotes WHERE quote_timestamp NOT LIKE "
        "'____-__-__T__:__:__.______+00:00'"
    ).fetchone()["n"]
    check("timestamps canonical ISO-8601 UTC", bad_ts == 0, f"{bad_ts} non-canonical")

    # Strict field-by-field parity against MongoDB, re-read independently.
    from pymongo import MongoClient

    db = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)["quote-game"]
    mismatches = []
    for coll, played in ((db.quotes, False), (db.history, True)):
        for doc in coll.find():
            ts = database.normalize_timestamp(doc["timestamp"])
            row = conn.execute(
                "SELECT * FROM quotes WHERE quoter_id = ? AND quote_timestamp = ?",
                (doc["quoter_id"], ts),
            ).fetchone()
            if row is None:
                mismatches.append(f"missing quote {doc['quoter_id']}@{ts}")
                continue
            problems = []
            if row["original_quote"] != doc["original_quote"]:
                problems.append("original_quote")
            if row["redacted_quote"] != doc["redacted_quote"]:
                problems.append("redacted_quote")
            if (row["played_at"] is not None) != played:
                problems.append("played_at")
            mongo_ids = [m["id"] for m in (doc.get("members_mentioned") or [])]
            sql_ids = [
                r["member_id"] for r in conn.execute(
                    "SELECT member_id FROM quote_mentions WHERE quote_id = ? ORDER BY position",
                    (row["id"],),
                )
            ]
            if mongo_ids != sql_ids:
                problems.append(f"mention order {mongo_ids} != {sql_ids}")
            for mongo_m in (doc.get("members_mentioned") or []):
                mention = conn.execute(
                    """SELECT m.name, m.display_name, m.preferred_name FROM quote_mentions qm
                       JOIN members m ON m.discord_id = qm.member_id
                       WHERE qm.quote_id = ? AND qm.member_id = ?""",
                    (row["id"], mongo_m["id"]),
                ).fetchone()
                if mention is None:
                    problems.append(f"member {mongo_m['id']} missing")
                    continue
                for field in ("name", "display_name", "preferred_name"):
                    if (mention[field] or None) != (mongo_m.get(field) or None):
                        problems.append(f"{field} @{mongo_m['id']}")
                sql_nicks = {
                    r["nick"] for r in conn.execute(
                        "SELECT nick FROM member_nicks WHERE member_id = ?", (mongo_m["id"],)
                    )
                }
                missing = set(mongo_m.get("nicks") or []) - sql_nicks
                if missing:
                    problems.append(f"nicks lost @{mongo_m['id']}: {sorted(missing)}")
            if problems:
                mismatches.append(f"{doc['quoter_id']}@{ts}: " + "; ".join(problems))

    check("strict parity vs MongoDB (all quotes)", not mismatches,
          f"{len(mismatches)} mismatches" if not mismatches
          else "; ".join(mismatches[:5]))
    return counts


def informational_reparse(conn, dump_path):
    """Re-parse quotes.json with the migrated members and diff against the DB.
    Informational only: parsing depends on nick state at parse time."""
    from quoteparse import build_name_to_members, parse_quote_message

    if not os.path.exists(dump_path):
        return
    with open(dump_path, encoding="utf-8") as f:
        dump = json.load(f)

    members = database.all_members_with_nicks()
    name_map = build_name_to_members(members)

    parsed, skipped, diffs = 0, 0, 0
    for item in dump:
        quote = parse_quote_message(item, name_map)
        if quote is None:
            skipped += 1
            continue
        parsed += 1
        ts = database.normalize_timestamp(quote["timestamp"])
        row = conn.execute(
            "SELECT * FROM quotes WHERE quoter_id = ? AND quote_timestamp = ?",
            (quote["quoter_id"], ts),
        ).fetchone()
        if row is None:
            diffs += 1
            continue
        if row["redacted_quote"] != quote["redacted_quote"]:
            diffs += 1
    report(
        "re-parse of quotes.json (informational)",
        f"{parsed} parsed / {skipped} skipped / {diffs} differ from stored rows",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate MongoDB to SQLite")
    parser.add_argument("--db", default=os.getenv("SQLITE_PATH", "quote-game.db"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing database file")
    parser.add_argument("--skip-discord", action="store_true",
                        help="skip the Discord backfill for quotes missing source_message_id")
    args = parser.parse_args()

    load_dotenv()
    mongo_uri = os.getenv("uri")
    if not mongo_uri:
        print("ERROR: 'uri' (MongoDB connection string) not set in .env")
        return 1
    if os.path.exists(args.db) and not args.force:
        print(f"ERROR: {args.db} already exists. Re-run with --force to overwrite.")
        return 1

    print(f"Migrating MongoDB -> {args.db}\n")

    print("Loading MongoDB (read-only)...")
    staged = load_mongo(mongo_uri)
    print(f"  members: {len(staged['members'])} (skipped {len(staged['skipped_members'])}: "
          f"{staged['skipped_members'] or 'none'})")
    print(f"  quotes: {len(staged['quotes'])}")
    print(f"  metadata keys: {sorted(staged['metadata'])}")

    matched = backfill_from_dump(staged, "quotes.json")
    print(f"\nsource_message_id backfilled from quotes.json: {matched}")
    healed = heal_member_nicks(staged)
    print(f"nicks healed from quote snapshots into members: {healed}")

    if not args.skip_discord:
        channel_id = os.getenv("QUOTES_CHANNEL_ID")
        token = os.getenv("DISCORD_TOKEN")
        if channel_id and token:
            remaining = sum(1 for q in staged["quotes"] if q["source_message_id"] is None)
            if remaining:
                print(f"Backfilling {remaining} remaining source_message_ids from Discord...")
                discord_matched = backfill_from_discord(staged, channel_id, token)
                print(f"  matched {discord_matched} of {remaining}")
        else:
            print("Discord backfill skipped: QUOTES_CHANNEL_ID or DISCORD_TOKEN not set")

    unmatched = [q for q in staged["quotes"] if q["source_message_id"] is None]
    print(f"source_message_id coverage: "
          f"{len(staged['quotes']) - len(unmatched)}/{len(staged['quotes'])}")

    print("\nWriting SQLite database...")
    conn = write_sqlite(args.db, staged)

    print("\nValidation:")
    validate(conn, staged, mongo_uri, "quotes.json")
    informational_reparse(conn, "quotes.json")

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{'=' * 60}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if unmatched:
        print(f"{len(unmatched)} quotes have no source_message_id "
              f"(dedupe falls back to quoter_id + timestamp):")
        for q in unmatched[:10]:
            print(f"  - {q['timestamp']} {q['original_quote'][:60]!r}")
    if failed:
        print(f"\nMIGRATION FAILED: {len(failed)} check(s) failed")
        return 1
    print(f"\nMIGRATION OK: {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
