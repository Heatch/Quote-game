from dotenv import load_dotenv
import os
from pymongo.mongo_client import MongoClient
import discord
import asyncio

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
SERVER_ID = int(os.getenv('COVID_ID'))  # Your guild/server ID as int

MONGO_URI = os.getenv('uri')
mclient = MongoClient(MONGO_URI)
db = mclient["quote-game"]
members_collection = db.members

intents = discord.Intents.default()
intents.members = True  # Important to fetch members
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    guild = client.get_guild(SERVER_ID)
    if guild is None:
        print(f"Could not find guild with ID {SERVER_ID}")
        await client.close()
        return

    print(f"Fetching members for guild: {guild.name} ({guild.id})...")
    await guild.chunk()  # Make sure all members are fetched

    members_data = []
    for member in guild.members:
        # Prepare your member document as needed
        member_doc = {
            "id": member.id,
            "name": member.name,
            "discriminator": member.discriminator,
            "display_name": member.display_name,
            "bot": member.bot,
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "created_at": member.created_at.isoformat(),
            # Add any other info you want here
        }
        members_data.append(member_doc)

    if members_data:
        # Optional: Clear existing members collection before inserting new data
        members_collection.delete_many({})
        result = members_collection.insert_many(members_data)
        print(f"Inserted {len(result.inserted_ids)} members into the database.")
    else:
        print("No members found to insert.")

    await client.close()

async def main():
    await client.start(TOKEN)

asyncio.run(main())
