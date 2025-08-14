from dotenv import load_dotenv
import os
from pymongo.mongo_client import MongoClient
import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from datetime import datetime, timedelta
import re
import webserver

GAME_ACTIVE = False

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

# Play command
@client.tree.command(name="play", description="Get a random #quotes quote and guess who said it!", guild=GUILD_ID)
async def play(interaction: discord.Interaction):
    global GAME_ACTIVE

    if GAME_ACTIVE:
        await interaction.response.send_message("A game is already in progress!", ephemeral=True)
        return

    quote = quotes_collection.aggregate([{"$sample": {"size": 1}}]).next()
    if history.count_documents({}) >= 100:
        move_quotes_back()
    quote["moved_to_history_at"] = datetime.now()
    history.insert_one(quote)
    quotes_collection.delete_one({"_id": quote["_id"]})

    quote_text = re.sub(r'#{3,}', '❓', quote["quote"])
    await interaction.response.send_message(f"**Quote:** {quote_text}")
    await interaction.followup.send("Who said it? Use /guess to place a guess")
    GAME_ACTIVE = True

@client.tree.command(name="guess", description="Guess who said the quote!", guild=GUILD_ID)
async def guess(interaction: discord.Interaction, author: discord.Member = None):
    global GAME_ACTIVE

    if not GAME_ACTIVE:
        await interaction.response.send_message("No game is currently active!", ephemeral=True)
        return

    # Get the most recent quote
    quote = history.find_one(sort=[("moved_to_history_at", -1)])
    quoter = quote["name"]  # list of names
    member = members_collection.find_one({"id": author.id})
    aliases = member["nicks"]  # list of nicknames/aliases

    # Check if any alias matches any name in quoter (case-insensitive)
    correct = any(alias.lower() in (name.lower() for name in quoter) for alias in aliases)

    if correct:
        await interaction.response.send_message(f"You guessed: {author.display_name} ✅ Correct!")
    else:
        # Restore full quote by replacing each ### with the corresponding name
        name_iter = iter(quote["name"])
        full_quote = re.sub(r'#{3,}', lambda _: next(name_iter), quote["quote"])

        await interaction.response.send_message(f"You guessed: {author.display_name} ❌ Wrong!") 
        await interaction.followup.send(f"The full quote was: {full_quote}")

    GAME_ACTIVE = False

# Start the bot
TOKEN = os.getenv('DISCORD_TOKEN')
webserver.keep_alive()
client.run(TOKEN)
