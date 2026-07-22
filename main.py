import asyncio
import logging
import math
import os
import signal
import sys
import tempfile

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env file (a no-op if none exists, e.g. on Coolify
# where variables are injected directly).
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("lambda-bot")

BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# File touched periodically while the bot is connected; the Docker HEALTHCHECK
# (healthcheck.py) reads it to decide whether the container is healthy.
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", os.path.join(tempfile.gettempdir(), "lambda-bot-heartbeat"))
HEARTBEAT_INTERVAL_SECONDS = 30

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
    log.info("Logged in as %s (%s)", bot.user.name, bot.user.id)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="!play"))
    await sync_commands()


async def sync_commands():
    """
    Registers slash (application) commands with Discord, once per process.

    If TEST_GUILD_ID is set, commands are synced to that guild for instant
    availability (handy while developing); otherwise they sync globally, which
    can take up to an hour to appear the first time.
    """
    if getattr(bot, "_commands_synced", False):
        return
    try:
        test_guild_id = os.getenv("TEST_GUILD_ID")
        if test_guild_id:
            guild = discord.Object(id=int(test_guild_id))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        bot._commands_synced = True
        log.info("Synced %d application (slash) commands", len(synced))
    except Exception:
        log.exception("Failed to sync application commands")


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler so a bad command never crashes a cog."""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await ctx.send(f"❌ {error}")
    if isinstance(error, (commands.MissingPermissions, commands.NotOwner, commands.NoPrivateMessage)):
        return await ctx.send("❌ You don't have permission to use that command here.")
    log.error("Unhandled error in command %s", ctx.command, exc_info=error)


async def heartbeat():
    """Refreshes the healthcheck heartbeat file while the bot is connected."""
    while not bot.is_closed():
        # latency is NaN until the first gateway heartbeat is acknowledged.
        if bot.is_ready() and not math.isnan(bot.latency):
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(str(asyncio.get_running_loop().time()))
            except OSError:
                pass
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def load_cogs():
    """Loads all cogs from the 'cogs' directory."""
    for filename in sorted(os.listdir('./cogs')):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                log.info("Loaded cog: %s", filename)
            except Exception:
                log.exception("Failed to load cog %s", filename)


async def main():
    """Main function to load cogs and start the bot."""
    if not BOT_TOKEN:
        log.critical("DISCORD_TOKEN is not set. Set it in your environment or .env file.")
        sys.exit(1)

    async with bot:
        # Docker/Coolify send SIGTERM on stop or redeploy; close the bot cleanly
        # so cogs get a chance to save their state.
        if os.name != "nt":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.close()))

        heartbeat_task = asyncio.create_task(heartbeat())
        await load_cogs()
        try:
            await bot.start(BOT_TOKEN)
        finally:
            heartbeat_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot shutting down...")
