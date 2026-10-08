from dotenv import load_dotenv
import os
import discord
import asyncio

import database

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
SERVER_ID = int(os.getenv('COVID_ID'))  # Your guild/server ID as int

database.connect()

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
        # Upsert members. preferred_name and nicks are preserved: they cannot be
        # derived from Discord and would otherwise be lost.
        for member_doc in members_data:
            database.upsert_member(member_doc)
        print(f"Upserted {len(members_data)} members into the database.")
    else:
        print("No members found to insert.")

    await client.close()

async def main():
    await client.start(TOKEN)

asyncio.run(main())
