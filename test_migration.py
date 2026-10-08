"""Tests for the SQLite data layer and the MongoDB -> SQLite migration.

Runs against throwaway databases in a temp directory -- never the real one.

    python test_migration.py
"""

import os
import sys
import tempfile

import database

PASSED = 0
FAILED = 0


def ok(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def fresh_db():
    """Point database.py at a brand-new temp database and return its path."""
    database.close()
    path = os.path.join(tempfile.mkdtemp(prefix="quotegame-test-"), "test.db")
    database.connect(path)
    return path


def sample_members():
    return [
        {
            "id": 111, "name": "michael", "discriminator": "0",
            "display_name": "Michael", "preferred_name": "Mike",
            "bot": False, "joined_at": "2021-01-01T00:00:00+00:00",
            "created_at": "2020-01-01T00:00:00+00:00",
            "nicks": ["mike", "mikey"],
        },
        {
            "id": 222, "name": "sarah", "discriminator": "1234",
            "display_name": "Sarah", "preferred_name": None,
            "bot": False, "joined_at": "2021-02-01T00:00:00+00:00",
            "created_at": "2020-02-01T00:00:00+00:00",
            "nicks": ["saz"],
        },
    ]


def sample_quote(**overrides):
    quote = {
        "quoter_id": 999,
        "original_quote": 'Michael said "hello" to Sarah',
        "redacted_quote": '[] said "hello" to []',
        "timestamp": "2024-03-15T10:30:00.000000+00:00",
        "source_message_id": 555001,
        "members_mentioned": [
            {"id": 111, "name": "michael", "display_name": "Michael",
             "preferred_name": "Mike", "nicks": ["mike", "mikey"]},
            {"id": 222, "name": "sarah", "display_name": "Sarah",
             "preferred_name": None, "nicks": ["saz"]},
        ],
    }
    quote.update(overrides)
    return quote


def test_members():
    print("\nmembers / member_nicks")
    fresh_db()
    for member in sample_members():
        database.insert_member(member)

    member = database.get_member(111)
    ok("get_member shape", member == {
        "id": 111, "name": "michael", "discriminator": "0",
        "display_name": "Michael", "preferred_name": "Mike",
        "bot": False, "joined_at": "2021-01-01T00:00:00.000000+00:00",
        "created_at": "2020-01-01T00:00:00.000000+00:00",
        "nicks": ["mike", "mikey"],
    }, repr(member))
    ok("insert_member refuses duplicate", database.insert_member(sample_members()[0]) is None)
    ok("get_member missing -> None", database.get_member(333) is None)

    added = database.add_nicks(111, ["mikey", "big mike"])
    ok("add_nicks dedupes", added == 1 and database.get_member(111)["nicks"] == ["mike", "mikey", "big mike"],
       str(database.get_member(111)["nicks"]))

    ok("set_preferred_name", database.set_preferred_name(111, "Mickey")
       and database.get_member(111)["preferred_name"] == "Mickey")

    # /pref and /add must be visible without touching any quote
    quote_id = database.insert_quote(sample_quote())
    fetched = database.get_random_unplayed_quote()
    ok("member updates visible through quote joins",
       fetched["members_mentioned"][0]["preferred_name"] == "Mickey"
       and "big mike" in fetched["members_mentioned"][0]["nicks"],
       repr(fetched["members_mentioned"][0]))

    # upsert_member refreshes profile but preserves preferred_name and nicks
    database.upsert_member({**sample_members()[0], "display_name": "Michael R",
                            "preferred_name": "ignored", "nicks": []})
    member = database.get_member(111)
    ok("upsert_member preserves preferred_name and nicks",
       member["display_name"] == "Michael R" and member["preferred_name"] == "Mickey"
       and member["nicks"] == ["mike", "mikey", "big mike"], repr(member))


def test_quotes_and_dedupe():
    print("\nquotes / dedupe")
    fresh_db()
    for member in sample_members():
        database.insert_member(member)

    quote_id = database.insert_quote(sample_quote())
    ok("insert_quote returns id", isinstance(quote_id, int), str(quote_id))
    ok("insert_quote rejects same message id",
       database.insert_quote(sample_quote(timestamp="2024-03-16T10:30:00.000000+00:00")) is None)
    ok("insert_quote rejects same quoter_id + timestamp",
       database.insert_quote(sample_quote(source_message_id=555002)) is None)
    ok("quote_exists by message id",
       database.quote_exists(1, "2024-03-15T10:30:00+00:00", 555001))
    ok("quote_exists by quoter + timestamp",
       database.quote_exists(999, "2024-03-15T10:30:00+00:00"))
    ok("quote_exists false for unknown", not database.quote_exists(123, "2020-01-01T00:00:00+00:00"))

    # Timestamps normalize to one canonical form regardless of input format
    database.insert_quote(sample_quote(timestamp="2024-03-17T10:30:00+00:00", source_message_id=555003))
    row = database.get_conn().execute(
        "SELECT quote_timestamp FROM quotes WHERE source_message_id = 555003").fetchone()
    ok("timestamps normalized to canonical form",
       row["quote_timestamp"] == "2024-03-17T10:30:00.000000+00:00", row["quote_timestamp"])

    quote = database.get_random_unplayed_quote()
    ok("quote dict shape", sorted(quote.keys()) == [
        "id", "members_mentioned", "original_quote", "played_at",
        "quoter_id", "redacted_quote", "source_message_id", "timestamp"], str(sorted(quote.keys())))
    ok("mention order preserved (position)",
       [m["id"] for m in quote["members_mentioned"]] == [111, 222],
       str([m["id"] for m in quote["members_mentioned"]]))
    ok("mention dict shape", sorted(quote["members_mentioned"][0].keys()) == [
        "display_name", "id", "name", "nicks", "preferred_name"])
    ok("latest_quote_timestamp",
       database.latest_quote_timestamp() == "2024-03-17T10:30:00.000000+00:00",
       database.latest_quote_timestamp())


def test_game_pool_lifecycle():
    print("\ngame pool lifecycle")
    fresh_db()
    for member in sample_members():
        database.insert_member(member)

    for i in range(3):
        database.insert_quote(sample_quote(timestamp=f"2024-04-0{i + 1}T10:30:00+00:00",
                                           source_message_id=600 + i))

    ok("played_count starts at 0", database.played_count() == 0)
    quote = database.get_random_unplayed_quote()
    database.mark_played(quote["id"], when="2024-05-01T00:00:00+00:00")
    ok("mark_played moves quote out of the pool", database.played_count() == 1)
    drawn = {database.get_random_unplayed_quote()["id"] for _ in range(20)}
    ok("played quote no longer served",
       quote["id"] not in drawn and drawn == {1, 2, 3} - {quote["id"]}, str(drawn))
    row = database.get_conn().execute(
        "SELECT played_at FROM quotes WHERE id = ?", (quote["id"],)).fetchone()
    ok("played_at stored canonically", row["played_at"] == "2024-05-01T00:00:00.000000+00:00",
       row["played_at"])

    ok("recycle_history resets pool", database.recycle_history() == 1
       and database.played_count() == 0)
    row = database.get_conn().execute(
        "SELECT played_at FROM quotes WHERE id = ?", (quote["id"],)).fetchone()
    ok("recycled quote playable again", row["played_at"] is None)

    # Drain the pool completely: the bot must not crash on an empty pool
    for _ in range(3):
        database.mark_played(database.get_random_unplayed_quote()["id"])
    ok("empty pool returns None (no crash)", database.get_random_unplayed_quote() is None)
    ok("count_quotes", database.count_quotes() == 3)


def test_metadata():
    print("\nmetadata")
    fresh_db()
    ok("get_metadata missing -> None", database.get_metadata("last_quote_check") is None)
    database.set_metadata("last_quote_check", "2026-09-26T03:13:09.934674+00:00")
    ok("set_metadata roundtrip",
       database.get_metadata("last_quote_check") == "2026-09-26T03:13:09.934674+00:00")
    database.set_metadata("last_quote_check", "2026-10-01T00:00:00+00:00")
    ok("set_metadata overwrites",
       database.get_metadata("last_quote_check") == "2026-10-01T00:00:00+00:00")


def test_guess_matching_against_db_rows():
    """End-to-end: rows from the DB feed the game's guess matching unchanged."""
    print("\nguess matching on DB rows")
    # Importing main connects database.py to its default path, so re-point at
    # the temp database afterwards.
    import main as bot
    fresh_db()
    for member in sample_members():
        database.insert_member(member)
    database.insert_quote(sample_quote())
    quote = database.get_random_unplayed_quote()
    mentioned = quote["members_mentioned"]

    ok("get_display_name uses preferred_name", bot.get_display_name(mentioned[0]) == "Mike")
    ok("match_guess by nick", bot.match_guess("mikey", mentioned) == (mentioned[0], None))
    ok("match_guess by display name", bot.match_guess("sarah", mentioned) == (mentioned[1], None))
    ok("match_guess case-insensitive", bot.match_guess("MIKEY", mentioned) == (mentioned[0], None))
    ok("match_guess unknown", bot.match_guess("nobody", mentioned) == (None, "not_found"))

    database.add_nicks(111, ["pal"])
    database.add_nicks(222, ["pal"])
    quote = database.get_random_unplayed_quote()
    mentioned = quote["members_mentioned"]
    ok("match_guess ambiguous on shared nick",
       bot.match_guess("pal", mentioned) == (None, "ambiguous"))


def test_schema_guards():
    print("\nschema constraints")
    fresh_db()
    conn = database.get_conn()
    for member in sample_members():
        database.insert_member(member)
    database.insert_quote(sample_quote())

    try:
        conn.execute("INSERT INTO quote_mentions (quote_id, member_id, position) VALUES (1, 999, 3)")
        ok("foreign key enforced", False, "insert of unknown member_id succeeded")
    except Exception:
        ok("foreign key enforced", True)

    try:
        conn.execute("INSERT INTO quote_mentions (quote_id, member_id, position) VALUES (1, 111, 0)")
        ok("mention position unique per quote", False, "duplicate position inserted")
    except Exception:
        ok("mention position unique per quote", True)

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    ok("PRAGMA foreign_key_check clean", not fk)
    ok("PRAGMA integrity_check clean",
       conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok")


def main():
    print("SQLite migration test suite")
    # Keep every database this suite touches (including the one created when
    # main.py is imported) inside the system temp directory.
    os.environ["SQLITE_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="quotegame-test-"), "default.db"
    )
    test_members()
    test_quotes_and_dedupe()
    test_game_pool_lifecycle()
    test_metadata()
    test_schema_guards()
    test_guess_matching_against_db_rows()
    print(f"\n{'=' * 60}")
    print(f"{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
