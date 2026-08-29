from dotenv import load_dotenv
import os
from pymongo.mongo_client import MongoClient
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View
from datetime import datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo
import re
import asyncio
import webserver

from quote_ingester import check_new_quotes

# Getting environment variables
load_dotenv()
SERVER_ID = os.getenv('COVID_ID')
GUILD_ID = discord.Object(id=int(SERVER_ID))
MONGO_URI = os.getenv('uri')

# Create a new client and connect to the server
mclient = MongoClient(MONGO_URI)
db = mclient["quote-game"]
members_collection = db.members
quotes_collection = db.quotes
history = db.history

# Active games keyed by bot message ID
active_games = {}


class GameSession:
    def __init__(self, quote, channel_id, starter_id, message_id):
        self.quote = quote
        self.channel_id = channel_id
        self.starter_id = starter_id
        self.message_id = message_id
        self.created_at = datetime.now(timezone.utc)
        self.timeout_task = None


# Bot initial boot up
class Client(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def on_ready(self):
        print(f'Logged in as {self.user}')

        try:
            GUILD_ID = discord.Object(id=int(SERVER_ID))
            synced = await self.tree.sync(guild=GUILD_ID)
            print(f"Synced {len(synced)} commands to {GUILD_ID}")

        except Exception as e:
            print(f"Failed to sync commands: {e}")

        # Catch up on missed quote checks
        try:
            last_check = db.metadata.find_one({"key": "last_quote_check"})
            should_catch_up = False
            if last_check and last_check.get("timestamp"):
                last_time = datetime.fromisoformat(last_check["timestamp"])
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_time).total_seconds() > 86400:
                    should_catch_up = True
            else:
                should_catch_up = True

            if should_catch_up:
                print("Catching up on missed quote check...")
                await check_new_quotes(self)
        except Exception as e:
            print(f"Failed to catch up on quote check: {e}")

        # Start daily quote ingestion loop
        try:
            daily_quote_ingest.start(self)
            print("Started daily quote ingestion loop.")
        except Exception as e:
            print(f"Failed to start daily quote ingestion loop: {e}")

# Intent setup
intents = discord.Intents.default()
intents.message_content = True
client = Client(command_prefix='!', intents=intents)

# Move all quotes from history back to quotes
def move_quotes_back():
    for quote in history.find():
        quote.pop('_id', None)  # Remove the _id field
        quote.pop('moved_to_history_at', None)  # Also remove the timestamp field
        quotes_collection.insert_one(quote)
    history.delete_many({})


async def game_timeout(message_id: int, delay: int = 60):
    try:
        await asyncio.sleep(delay)
        if message_id not in active_games:
            return
        game = active_games.pop(message_id)
        channel = client.get_channel(game.channel_id)
        if channel is None:
            return
        full_quote = game.quote["original_quote"]
        await channel.send(
            f"Time's up! No one guessed correctly.\n"
            f"The full quote was: {full_quote}"
        )
    except asyncio.CancelledError:
        pass


@tasks.loop(time=time(hour=6, tzinfo=ZoneInfo("America/New_York")))
async def daily_quote_ingest(bot_client):
    print("Running daily quote ingestion...")
    try:
        await check_new_quotes(bot_client)
    except Exception as e:
        print(f"Daily quote ingestion failed: {e}")
        import traceback
        traceback.print_exc()


def get_display_name(member_entry):
    """Return preferred_name, display_name, or name from a members_mentioned entry."""
    return (
        member_entry.get("preferred_name")
        or member_entry.get("display_name")
        or member_entry.get("name")
        or "Unknown"
    )


def match_guess(guess_text, members_mentioned):
    """
    Match a text guess against the nicks stored in members_mentioned.
    Returns (member_entry, None) on single match,
    (None, 'ambiguous') if multiple match, (None, 'not_found') if none match.
    """
    guess_lower = guess_text.lower()
    matches = []
    for member in members_mentioned:
        names = [
            (member.get("name") or "").lower(),
            (member.get("display_name") or "").lower(),
            (member.get("preferred_name") or "").lower(),
        ]
        names.extend((n or "").lower() for n in member.get("nicks", []))
        if guess_lower in names:
            matches.append(member)

    if len(matches) == 1:
        return matches[0], None
    elif len(matches) > 1:
        return None, "ambiguous"
    return None, "not_found"


def unknown_guess_entry(text):
    """Create a placeholder entry for a guess that did not match anyone."""
    return {
        "id": None,
        "name": text,
        "display_name": text,
        "preferred_name": None,
        "nicks": [],
    }


def parse_guesses(message, members_mentioned):
    """
    Parse guesses from a reply message in order.
    Supports @mentions and text nicks separated by comma.
    Returns (guessed_members, error_message).
    """
    ordered_guesses = []
    for part in re.split(r',\s*', message.content):
        part = part.strip()
        if not part:
            continue
        mention_match = re.match(r'<@!?(\d+)>', part)
        if mention_match:
            user_id = int(mention_match.group(1))
            user = discord.utils.get(message.mentions, id=user_id)
            if user and not user.bot:
                ordered_guesses.append(("mention", user))
        else:
            ordered_guesses.append(("text", part))

    guesses = []
    for guess_type, value in ordered_guesses:
        if guess_type == "mention":
            user = value
            member_entry = next(
                (m for m in members_mentioned if m["id"] == user.id), None
            )
            if member_entry is None:
                # Mentioned user is not in the quote; treat as a wrong guess
                guesses.append(unknown_guess_entry(user.display_name or user.name))
            else:
                guesses.append(member_entry)
        else:
            member_entry, status = match_guess(value, members_mentioned)
            if status == "ambiguous":
                return None, f'"{value}" matches multiple people. Please be more specific or use @mention.'
            elif status == "not_found":
                # Unknown nick; treat as a wrong guess
                guesses.append(unknown_guess_entry(value))
            else:
                guesses.append(member_entry)

    return guesses, None


# Play command
@client.tree.command(name="play", description="Get a random #quotes quote and guess who said it!", guild=GUILD_ID)
async def play(interaction: discord.Interaction):
    quote = quotes_collection.aggregate([{"$sample": {"size": 1}}]).next()
    if history.count_documents({}) >= 100:
        move_quotes_back()
    quote["moved_to_history_at"] = datetime.now(timezone.utc)
    history.insert_one(quote)
    quotes_collection.delete_one({"_id": quote["_id"]})

    quote_text = quote["redacted_quote"]
    mentioned = quote["members_mentioned"]
    if len(mentioned) > 1:
        instruction = (
            f"This quote involves {len(mentioned)} people. "
            f"Reply with all members mentioned, comma-separated!"
        )
    else:
        instruction = "Reply to this message with who you think said it!"

    await interaction.response.send_message(
        f"**Quote:** {quote_text}\n\n{instruction}"
    )
    bot_message = await interaction.original_response()

    game = GameSession(
        quote=quote,
        channel_id=interaction.channel_id,
        starter_id=interaction.user.id,
        message_id=bot_message.id,
    )
    game.timeout_task = asyncio.create_task(game_timeout(bot_message.id))
    active_games[bot_message.id] = game


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@client.tree.command(name="onboard", description="Add a new user to the members collection", guild=GUILD_ID)
async def onboard(interaction: discord.Interaction, user: discord.Member):
    if members_collection.find_one({"id": user.id}) is not None:
        await interaction.response.send_message(
            f"{user.display_name} is already in the members collection.", ephemeral=True
        )
        return

    member_doc = {
        "id": user.id,
        "name": user.name,
        "discriminator": getattr(user, 'discriminator', None),
        "display_name": user.display_name,
        "preferred_name": user.display_name,
        "bot": user.bot,
        "joined_at": user.joined_at.isoformat() if user.joined_at else None,
        "created_at": user.created_at.isoformat(),
        "nicks": [],
    }
    members_collection.insert_one(member_doc)
    await interaction.response.send_message(
        f"Onboarded {user.display_name}.", ephemeral=True
    )


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@client.tree.command(name="nicks", description="Show all nicks for a user", guild=GUILD_ID)
async def nicks(interaction: discord.Interaction, user: discord.Member):
    member = members_collection.find_one({"id": user.id})
    if member is None:
        await interaction.response.send_message(
            f"{user.display_name} is not in the members collection.", ephemeral=True
        )
        return

    aliases = member.get("nicks", [])
    if not aliases:
        await interaction.response.send_message(
            f"{user.display_name} has no nicks set.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"Nicks for {user.display_name}: {', '.join(aliases)}", ephemeral=True
        )


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@client.tree.command(name="add", description="Add nicks to a user", guild=GUILD_ID)
async def add(interaction: discord.Interaction, user: discord.Member, nicks: str):
    member = members_collection.find_one({"id": user.id})
    if member is None:
        await interaction.response.send_message(
            f"{user.display_name} is not in the members collection. Use /onboard first.",
            ephemeral=True,
        )
        return

    new_nicks = [n.strip() for n in nicks.split(',') if n.strip()]
    if not new_nicks:
        await interaction.response.send_message(
            "No valid nicks provided.", ephemeral=True
        )
        return

    members_collection.update_one(
        {"id": user.id},
        {"$addToSet": {"nicks": {"$each": new_nicks}}}
    )

    # Propagate new nicks to any quotes/history where this member is mentioned
    array_filter = [{"member.id": user.id}]
    quotes_collection.update_many(
        {"members_mentioned.id": user.id},
        {"$addToSet": {"members_mentioned.$[member].nicks": {"$each": new_nicks}}},
        array_filters=array_filter,
    )
    history.update_many(
        {"members_mentioned.id": user.id},
        {"$addToSet": {"members_mentioned.$[member].nicks": {"$each": new_nicks}}},
        array_filters=array_filter,
    )

    await interaction.response.send_message(
        f"Added nicks to {user.display_name}: {', '.join(new_nicks)}", ephemeral=True
    )


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@client.tree.command(name="pref", description="Set a preferred name for a user", guild=GUILD_ID)
async def pref(interaction: discord.Interaction, user: discord.Member, name: str):
    member = members_collection.find_one({"id": user.id})
    if member is None:
        await interaction.response.send_message(
            f"{user.display_name} is not in the members collection. Use /onboard first.",
            ephemeral=True,
        )
        return

    preferred = name.strip()
    members_collection.update_one(
        {"id": user.id},
        {"$set": {"preferred_name": preferred}}
    )
    await interaction.response.send_message(
        f"Set preferred name for {user.display_name} to: {preferred}", ephemeral=True
    )


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@client.tree.command(name="refresh_quotes", description="Manually check for new quotes in the quotes channel", guild=GUILD_ID)
async def refresh_quotes(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        count = await check_new_quotes(client)
        await interaction.followup.send(
            f"Quote check complete. {count} new quotes added.", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"Quote check failed: {e}", ephemeral=True
        )


@onboard.error
@nicks.error
@add.error
@pref.error
@refresh_quotes.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send(
                "You need administrator permissions to use this command.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You need administrator permissions to use this command.", ephemeral=True
            )


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.reference and message.reference.message_id in active_games:
        game = active_games[message.reference.message_id]
        members_mentioned = game.quote["members_mentioned"]

        guesses, error = parse_guesses(message, members_mentioned)
        if error:
            await message.reply(error)
            return

        if not guesses:
            await message.reply("Please reply with who you think said the quote!")
            return

        expected_count = len(members_mentioned)
        if len(guesses) != expected_count:
            await message.reply(
                f"This quote involves {expected_count} people. Please guess all of them, comma-separated!"
            )
            return

        if game.timeout_task:
            game.timeout_task.cancel()
        del active_games[game.message_id]

        correct_ids = [m["id"] for m in members_mentioned]
        guessed_ids = [g["id"] for g in guesses]
        all_correct = guessed_ids == correct_ids

        guess_names = ", ".join(get_display_name(g) for g in guesses)

        if len(members_mentioned) == 1:
            correct_name = get_display_name(members_mentioned[0])
            if all_correct:
                result = f"You guessed: {guess_names} ✅ Correct!"
            else:
                result = f"You guessed: {guess_names} ❌ Wrong! (correct: {correct_name})"
        else:
            if all_correct:
                result = f"You guessed: {guess_names} ✅ All correct!"
            else:
                lines = [f"You guessed: {guess_names} ❌"]
                for i, (guessed, actual) in enumerate(zip(guesses, members_mentioned), start=1):
                    status = "✅" if guessed["id"] == actual["id"] else "❌"
                    lines.append(f"{i}. {get_display_name(guessed)} {status} (correct: {get_display_name(actual)})")
                result = "\n".join(lines)

        full_quote = game.quote["original_quote"]
        await message.reply(f"{result}\nThe full quote was: {full_quote}")

    await client.process_commands(message)


# Start the bot
TOKEN = os.getenv('DISCORD_TOKEN')
# webserver.keep_alive()
client.run(TOKEN)
