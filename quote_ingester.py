import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pymongo.mongo_client import MongoClient
import discord
import asyncio

from quoteparse import build_name_to_members, parse_quote_message

load_dotenv()
MONGO_URI = os.getenv('uri')
QUOTES_CHANNEL_ID = os.getenv('QUOTES_CHANNEL_ID')

mclient = MongoClient(MONGO_URI)
db = mclient["quote-game"]
quotes_collection = db.quotes
members_collection = db.members
metadata_collection = db.metadata


METADATA_KEY = "last_quote_check"


def _parse_timestamp(ts):
    """Parse an ISO timestamp string or datetime to datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


def _get_last_check_time():
    """Return the datetime after which we should fetch messages."""
    doc = metadata_collection.find_one({"key": METADATA_KEY})
    if doc and doc.get("timestamp"):
        return _parse_timestamp(doc["timestamp"])

    # No previous check. Use the most recent quote timestamp, or 24 hours ago.
    most_recent = quotes_collection.find_one(sort=[("timestamp", -1)])
    if most_recent and most_recent.get("timestamp"):
        return _parse_timestamp(most_recent["timestamp"])

    return datetime.now(timezone.utc) - timedelta(hours=24)


def _update_last_check_time(timestamp):
    """Store the last check timestamp in metadata."""
    metadata_collection.update_one(
        {"key": METADATA_KEY},
        {"$set": {"timestamp": timestamp.isoformat()}},
        upsert=True,
    )


def _is_duplicate(quote):
    """Check if this quote already exists by quoter_id + timestamp."""
    return quotes_collection.find_one({
        "quoter_id": quote["quoter_id"],
        "timestamp": quote["timestamp"],
    }) is not None


async def check_new_quotes(bot_client):
    """
    Fetch new messages from the quotes channel, parse them, and insert valid quotes.
    Returns the number of new quotes added.
    """
    if not QUOTES_CHANNEL_ID:
        print("QUOTES_CHANNEL_ID not set. Skipping quote check.")
        return 0

    channel = bot_client.get_channel(int(QUOTES_CHANNEL_ID))
    if channel is None:
        try:
            channel = await bot_client.fetch_channel(int(QUOTES_CHANNEL_ID))
        except discord.NotFound:
            print("Quotes channel not found.")
            return 0
        except discord.Forbidden:
            print("No permission to access quotes channel.")
            return 0

    if channel is None:
        print("Could not find quotes channel.")
        return 0

    # Load members and build name map
    members = list(members_collection.find())
    name_map = build_name_to_members(members)

    after_time = _get_last_check_time()
    newest_time = after_time
    added_count = 0
    checked_count = 0

    print(f"Checking for new quotes after {after_time.isoformat()}")

    try:
        async for message in channel.history(limit=None, after=after_time, oldest_first=True):
            checked_count += 1

            # Skip messages with attachments
            if message.attachments:
                continue

            message_data = {
                "id": message.id,
                "author": {
                    "id": message.author.id,
                    "username": message.author.name,
                    "display_name": message.author.display_name,
                    "discriminator": getattr(message.author, 'discriminator', None),
                },
                "content": message.content,
                "timestamp": message.created_at.isoformat(),
            }

            quote = parse_quote_message(message_data, name_map)
            if quote is None:
                continue

            if _is_duplicate(quote):
                continue

            quotes_collection.insert_one(quote)
            added_count += 1

            if message.created_at > newest_time:
                newest_time = message.created_at

            # Small delay to be nice to the API
            if checked_count % 100 == 0:
                await asyncio.sleep(1)

    except discord.errors.DiscordServerError as e:
        print(f"Discord server error while checking quotes: {e}")
    except Exception as e:
        print(f"Error checking quotes: {e}")
        import traceback
        traceback.print_exc()

    # Update last check time to the newest processed message time, or now if nothing processed
    final_time = newest_time if newest_time > after_time else datetime.now(timezone.utc)
    _update_last_check_time(final_time)

    print(f"Checked {checked_count} messages. Added {added_count} new quotes.")
    return added_count
