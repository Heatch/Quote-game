import re
import json
from dotenv import load_dotenv

import database


def build_name_to_members(members):
    """Build mapping from lowercase name to list of member docs."""
    name_map = {}
    for member in members:
        if "id" not in member:
            print(f"Warning: member document missing 'id' field: {member}")
            continue
        names = [
            member.get("name", ""),
            member.get("display_name", ""),
            member.get("preferred_name", ""),
        ]
        names.extend(member.get("nicks", []))
        for name in names:
            if name:
                name_lower = name.lower()
                if name_lower not in name_map:
                    name_map[name_lower] = []
                if member not in name_map[name_lower]:
                    name_map[name_lower].append(member)
    return name_map


def has_quotes(text):
    """Return True if text has 2+ single quotes or 2+ double quotes."""
    return text.count('"') >= 2 or text.count("'") >= 2


def extract_outside_quotes(text):
    """Return text with double-quoted and single-quoted sections removed."""
    outside = re.sub(r'"[^"]*"', '', text)
    outside = re.sub(r"'[^']*'", '', outside)
    return outside


def find_name_matches(text, name_map):
    """
    Find all member name matches in text.
    Returns (matches, ambiguous) where matches is a list of dicts with
    member, name, and position; ambiguous is True if any matched name
    resolves to multiple members.
    """
    matches = {}
    ambiguous = False

    # Sort names longest first to handle overlapping names (e.g., "Michael Smith" before "Michael")
    sorted_names = sorted(name_map.keys(), key=len, reverse=True)

    for name in sorted_names:
        members_for_name = name_map[name]
        pattern = r'\b' + re.escape(name) + r'\b'
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if len(members_for_name) > 1:
                ambiguous = True
                break
            member = members_for_name[0]
            member_id = member["id"]
            position = match.start()
            if member_id not in matches or position < matches[member_id]["position"]:
                matches[member_id] = {
                    "member": member,
                    "name": name,
                    "position": position,
                }
        if ambiguous:
            break

    sorted_matches = sorted(matches.values(), key=lambda x: x["position"])
    return sorted_matches, ambiguous


def redact_names(text, members):
    """Replace all names for the given members with []."""
    redacted = text
    for member in members:
        names = [
            member.get("name", ""),
            member.get("display_name", ""),
            member.get("preferred_name", ""),
        ]
        names.extend(member.get("nicks", []))
        for name in names:
            if name:
                pattern = r'\b' + re.escape(name) + r'\b'
                redacted = re.sub(pattern, '[]', redacted, flags=re.IGNORECASE)
    return redacted


def parse_quote_message(message, name_map):
    """Parse a dumped Discord message into the new quote schema."""
    content = message.get("content", "")

    if not has_quotes(content):
        return None

    outside_text = extract_outside_quotes(content)
    if not outside_text.strip():
        return None

    matches, ambiguous = find_name_matches(outside_text, name_map)

    if ambiguous or not matches:
        return None

    members = [m["member"] for m in matches]
    redacted_quote = redact_names(content, members)

    return {
        "quoter_id": message["author"]["id"],
        "original_quote": content,
        "redacted_quote": redacted_quote,
        "members_mentioned": [
            {
                "id": m["id"],
                "name": m.get("name"),
                "display_name": m.get("display_name"),
                "preferred_name": m.get("preferred_name"),
                "nicks": m.get("nicks", []),
            }
            for m in members
        ],
        "timestamp": message.get("timestamp"),
        "source_message_id": message.get("id"),
    }


def main():
    load_dotenv()
    database.connect()

    with open("quotes.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    # Load members and build name map
    members = database.all_members_with_nicks()
    name_map = build_name_to_members(members)

    # Reset quotes (old format incompatible); quote_mentions cascade away
    conn = database.get_conn()
    with conn:
        conn.execute("DELETE FROM quotes")

    parsed_count = 0
    skipped_count = 0
    duplicate_count = 0

    for item in data:
        result = parse_quote_message(item, name_map)
        if result is None:
            skipped_count += 1
        elif database.insert_quote(result) is None:
            duplicate_count += 1
        else:
            parsed_count += 1

    print(f"Parsed {parsed_count} quotes. Skipped {skipped_count} messages.")
    if duplicate_count:
        print(f"Skipped {duplicate_count} duplicates (same quoter_id + timestamp).")


if __name__ == "__main__":
    main()
