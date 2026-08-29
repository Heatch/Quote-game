from dotenv import load_dotenv
import os
from pymongo.mongo_client import MongoClient
import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from datetime import datetime, timezone
import re
import asyncio
import webserver

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


def restore_quote(quote):
    name_iter = iter(quote["name"])
    return re.sub(r'#{3,}', lambda _: next(name_iter), quote["quote"])


async def game_timeout(message_id: int, delay: int = 60):
    try:
        await asyncio.sleep(delay)
        if message_id not in active_games:
            return
        game = active_games.pop(message_id)
        channel = client.get_channel(game.channel_id)
        if channel is None:
            return
        full_quote = restore_quote(game.quote)
        await channel.send(
            f"Time's up! No one guessed correctly.\n"
            f"The full quote was: {full_quote}"
        )
    except asyncio.CancelledError:
        pass


# Play command
@client.tree.command(name="play", description="Get a random #quotes quote and guess who said it!", guild=GUILD_ID)
async def play(interaction: discord.Interaction):
    quote = quotes_collection.aggregate([{"$sample": {"size": 1}}]).next()
    if history.count_documents({}) >= 100:
        move_quotes_back()
    quote["moved_to_history_at"] = datetime.now(timezone.utc)
    history.insert_one(quote)
    quotes_collection.delete_one({"_id": quote["_id"]})

    quote_text = re.sub(r'#{3,}', '❓', quote["quote"])
    await interaction.response.send_message(
        f"**Quote:** {quote_text}\n\nReply to this message with @mention of who you think said it!"
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


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.reference and message.reference.message_id in active_games:
        game = active_games[message.reference.message_id]

        guesses = [u for u in message.mentions if not u.bot]
        if not guesses:
            await message.reply("Please @mention who you think said the quote!")
        else:
            guessed_user = guesses[0]
            member = members_collection.find_one({"id": guessed_user.id})

            if member is None or "nicks" not in member:
                await message.reply(f"Could not find aliases for {guessed_user.display_name}.")
            else:
                aliases = member["nicks"]
                quoter_names = game.quote["name"]
                correct = any(alias.lower() in (name.lower() for name in quoter_names) for alias in aliases)

                if game.timeout_task:
                    game.timeout_task.cancel()
                del active_games[game.message_id]

                if correct:
                    await message.reply(f"You guessed: {guessed_user.display_name} ✅ Correct!")
                else:
                    full_quote = restore_quote(game.quote)
                    await message.reply(
                        f"You guessed: {guessed_user.display_name} ❌ Wrong!\n"
                        f"The full quote was: {full_quote}"
                    )

    await client.process_commands(message)


# Start the bot
TOKEN = os.getenv('DISCORD_TOKEN')
# webserver.keep_alive()
client.run(TOKEN)
