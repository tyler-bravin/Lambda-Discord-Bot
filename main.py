import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get configuration from environment variables
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# Define the bot's intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

# Define the bot instance
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

@bot.event
async def on_ready():
    """Event that fires when the bot is ready and connected to Discord."""
    print(f'✅ Logged in as {bot.user.name} ({bot.user.id})')
    print('------')
    # Set a custom status
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"!play"))


async def load_cogs():
    """Loads all cogs from the 'cogs' directory."""
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                print(f'🔧 Loaded cog: {filename}')
            except Exception as e:
                print(f'❌ Failed to load cog {filename}: {e.__class__.__name__} - {e}')


async def main():
    """Main function to load cogs and start the bot."""
    async with bot:
        await load_cogs()
        await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shutting down...")