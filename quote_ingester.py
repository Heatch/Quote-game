import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import discord
import asyncio

import database
from quoteparse import build_name_to_members, parse_quote_message

load_dotenv()
QUOTES_CHANNEL_ID = os.getenv('QUOTES_CHANNEL_ID')


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
    timestamp = database.get_metadata(METADATA_KEY)
    if timestamp:
        return _parse_timestamp(timestamp)

    # No previous check. Use the most recent quote timestamp, or 24 hours ago.
    most_recent = database.latest_quote_timestamp()
    if most_recent:
        return _parse_timestamp(most_recent)

    return datetime.now(timezone.utc) - timedelta(hours=24)


def _update_last_check_time(timestamp):
    """Store the last check timestamp in metadata."""
    database.set_metadata(METADATA_KEY, timestamp.isoformat())


def _is_duplicate(quote):
    """Check if this quote already exists by message id or quoter_id + timestamp."""
    return database.quote_exists(
        quote["quoter_id"],
        quote["timestamp"],
        quote.get("source_message_id"),
    )


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
    members = database.all_members_with_nicks()
    name_map = build_name_to_members(members)

    after_time = _get_last_check_time()
    newest_time = after_time
    added_count = 0
    checked_count = 0
    scan_failed = False

    print(f"Checking for new quotes after {after_time.isoformat()}")

    try:
        async for message in channel.history(limit=None, after=after_time, oldest_first=True):
            checked_count += 1

            # Everything up to this message has now been examined, whether or
            # not it becomes a quote. Advance the watermark before any skip so
            # duplicates and unparseable messages are never scanned twice.
            if message.created_at > newest_time:
                newest_time = message.created_at

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

            if database.insert_quote(quote) is not None:
                added_count += 1

            # Small delay to be nice to the API
            if checked_count % 100 == 0:
                await asyncio.sleep(1)

    except discord.errors.DiscordServerError as e:
        scan_failed = True
        print(f"Discord server error while checking quotes: {e}")
    except Exception as e:
        scan_failed = True
        print(f"Error checking quotes: {e}")
        import traceback
        traceback.print_exc()

    # Resume point = the newest message actually examined. If the scan failed
    # before examining anything, keep the old watermark so the window is
    # retried instead of silently skipped.
    if newest_time > after_time:
        final_time = newest_time
    elif scan_failed:
        final_time = after_time
    else:
        final_time = datetime.now(timezone.utc)
    _update_last_check_time(final_time)

    print(f"Checked {checked_count} messages. Added {added_count} new quotes.")
    return added_count
