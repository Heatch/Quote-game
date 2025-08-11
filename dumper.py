import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import os
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# Getting environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
SERVER_ID = os.getenv('COVID_ID')
GUILD_ID = discord.Object(id=int(SERVER_ID))
QUOTES_CHANNEL = os.getenv('QUOTES_CHANNEL_ID')

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    print(f"Bot is in {len(bot.guilds)} guild(s)")
    
    # Wait a moment for the bot to fully initialize
    await asyncio.sleep(2)
    
    # Dump quotes automatically when bot starts
    await dump_all_quotes()
    
    # Close the bot after dumping (remove this line if you want the bot to stay running)
    await bot.close()

async def dump_all_quotes():
    """Dump all messages from the quotes channel to quotes.json"""
    
    try:
        print(f"🔍 Looking for quotes channel with ID: {QUOTES_CHANNEL}")
        
        # Try get_channel first
        quotes_channel = bot.get_channel(int(QUOTES_CHANNEL))
        
        # If that fails, try fetch_channel (API call)
        if not quotes_channel:
            print("📡 Channel not in cache, fetching from API...")
            try:
                quotes_channel = await bot.fetch_channel(int(QUOTES_CHANNEL))
            except discord.NotFound:
                print("❌ Channel not found! Check if the channel ID is correct.")
                return
            except discord.Forbidden:
                print("❌ No permission to access this channel!")
                return
        
        if not quotes_channel:
            print("❌ Could not find the quotes channel!")
            print(f"   - Channel ID from env: {QUOTES_CHANNEL}")
            print(f"   - Bot is in {len(bot.guilds)} guild(s)")
            for guild in bot.guilds:
                print(f"   - Guild: {guild.name} (ID: {guild.id})")
            return
        
        print(f"📥 Starting to dump messages from #{quotes_channel.name}...")
        
        quotes_data = []
        message_count = 0
        last_message_id = None
        batch_size = 50  # Smaller batches to avoid rate limits
        
        # Load existing data if resuming
        try:
            with open('quotes_progress.json', 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
                quotes_data = existing_data.get('messages', [])
                last_message_id = existing_data.get('last_message_id')
                message_count = len(quotes_data)
                print(f"📄 Resuming from message count: {message_count}")
        except FileNotFoundError:
            print("📄 Starting fresh dump...")
        
        retry_count = 0
        max_retries = 5
        
        while retry_count < max_retries:
            try:
                batch_count = 0
                current_batch_start = last_message_id
                
                # Iterate through messages with rate limiting
                async for message in quotes_channel.history(
                    limit=None,  # Get all remaining messages
                    oldest_first=True,
                    after=discord.Object(id=last_message_id) if last_message_id else None
                ):
                    # Skip messages with attachments
                    if message.attachments:
                        continue
                        
                    message_data = {
                        "id": message.id,
                        "author": {
                            "id": message.author.id,
                            "username": message.author.name,
                            "display_name": message.author.display_name,
                            "discriminator": getattr(message.author, 'discriminator', None)
                        },
                        "content": message.content,
                        "timestamp": message.created_at.isoformat(),
                        "edited_timestamp": message.edited_at.isoformat() if message.edited_at else None,
                        "pinned": message.pinned,
                        "mention_everyone": message.mention_everyone,
                        "mentions": [
                            {
                                "id": user.id,
                                "username": user.name,
                                "display_name": user.display_name
                            } for user in message.mentions
                        ],
                        "reference": {
                            "message_id": message.reference.message_id,
                            "channel_id": message.reference.channel_id,
                            "guild_id": message.reference.guild_id
                        } if message.reference else None
                    }
                    
                    quotes_data.append(message_data)
                    message_count += 1
                    batch_count += 1
                    last_message_id = message.id
                    
                    # Print progress and save every 100 messages
                    if message_count % 100 == 0:
                        print(f"📄 Processed {message_count} messages...")
                        
                        # Save progress every 100 messages
                        progress_data = {
                            "messages": quotes_data,
                            "last_message_id": last_message_id,
                            "timestamp": datetime.now().isoformat()
                        }
                        with open('quotes_progress.json', 'w', encoding='utf-8') as f:
                            json.dump(progress_data, f, indent=2, ensure_ascii=False)
                        
                        # Add a small delay every 100 messages to be nice to the API
                        await asyncio.sleep(1)
                
                # If we reach here, we've processed all messages successfully
                print(f"✅ Completed! Processed all messages.")
                break
                
            except discord.errors.DiscordServerError as e:
                retry_count += 1
                wait_time = retry_count * 10  # Exponential backoff
                print(f"⚠️ Discord server error (attempt {retry_count}/{max_retries}): {e}")
                print(f"💤 Waiting {wait_time} seconds before retry...")
                await asyncio.sleep(wait_time)
                continue
                
            except Exception as e:
                print(f"❌ Unexpected error: {e}")
                import traceback
                traceback.print_exc()
                break
        
        if retry_count >= max_retries:
            print(f"❌ Max retries ({max_retries}) reached. Saving progress...")
        
        # Save final data
        with open('quotes.json', 'w', encoding='utf-8') as f:
            json.dump(quotes_data, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Successfully dumped {message_count} messages to quotes.json")
        
        # Print some statistics
        authors = set(msg["author"]["username"] for msg in quotes_data)
        print(f"📊 Statistics:")
        print(f"   - Total messages (no attachments): {message_count}")
        print(f"   - Unique authors: {len(authors)}")
        
        # Clean up progress file
        try:
            os.remove('quotes_progress.json')
            print("🗑️ Cleaned up progress file")
        except:
            pass
        
    except Exception as e:
        print(f"❌ Error dumping quotes: {e}")
        import traceback
        traceback.print_exc()

bot.run(TOKEN)
