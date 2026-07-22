# cogs/Admin.py
"""
A cog for bot owner administrative commands.

This cog includes essential commands for managing the bot's operation,
such as reloading other cogs without needing a full restart. Access is
restricted to the bot owner.
"""
import asyncio
import os
import time

import discord
import yt_dlp
from discord.ext import commands


class Admin(commands.Cog):
    """Hosts administrative commands for the bot owner."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Wall-clock time the cog was loaded, used to report uptime.
        self.start_time = time.time()

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Formats a duration in seconds as e.g. '2d 3h 4m 5s'."""
        seconds = int(seconds)
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        parts = []
        if days: parts.append(f"{days}d")
        if hours: parts.append(f"{hours}h")
        if minutes: parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    @commands.hybrid_command(name='status', aliases=['botstatus'])
    async def status(self, ctx: commands.Context):
        """Shows bot uptime, latency, yt-dlp version and active players."""
        music_cog = self.bot.get_cog("Music")
        active_players = 0
        pending_restart = False
        if music_cog is not None:
            active_players = sum(
                1 for vc in self.bot.voice_clients if vc.is_playing() or vc.is_paused())
            pending_restart = getattr(music_cog, "pending_ytdlp_restart", False)

        embed = discord.Embed(title="🤖 Bot Status", color=discord.Color.blurple())
        embed.add_field(name="Uptime", value=self._format_duration(time.time() - self.start_time), inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)} ms", inline=True)
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="yt-dlp", value=yt_dlp.version.__version__, inline=True)
        embed.add_field(name="Active players", value=str(active_players), inline=True)
        if pending_restart:
            embed.add_field(name="⚠️ Pending", value="Restart queued for yt-dlp update (once idle).", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name='ytdlp', hidden=True)
    @commands.is_owner()
    async def ytdlp_update(self, ctx: commands.Context):
        """(Bot Owner Only) Forces an immediate yt-dlp update check."""
        from cogs.Music import _upgrade_ytdlp_blocking

        music_cog = self.bot.get_cog("Music")
        current = yt_dlp.version.__version__
        message = await ctx.send(embed=discord.Embed(
            description=f"⏳ Checking for a yt-dlp update (currently `{current}`)…",
            color=discord.Color.yellow()))

        updated = await asyncio.get_running_loop().run_in_executor(None, _upgrade_ytdlp_blocking)

        if updated:
            if music_cog is not None:
                music_cog.pending_ytdlp_restart = True
            await message.edit(embed=discord.Embed(
                title="✅ yt-dlp updated",
                description="A new version was installed. The bot will restart to apply it "
                            "the next time nothing is playing.",
                color=discord.Color.green()))
        else:
            await message.edit(embed=discord.Embed(
                description=f"✅ Already up to date (`{current}`).",
                color=discord.Color.green()))

    @commands.command(name='sync', hidden=True)
    @commands.is_owner()
    async def sync(self, ctx: commands.Context):
        """(Bot Owner Only) Re-syncs slash commands with Discord."""
        try:
            synced = await self.bot.tree.sync()
        except Exception as e:
            return await ctx.send(embed=discord.Embed(
                description=f"❌ Sync failed: {e}", color=discord.Color.red()))
        await ctx.send(embed=discord.Embed(
            description=f"✅ Synced **{len(synced)}** slash commands globally. "
                        "They can take up to an hour to appear.",
            color=discord.Color.green()))

    @commands.command(name='reload', hidden=True)
    @commands.is_owner()
    async def reload_cogs(self, ctx: commands.Context):
        """
        Reloads all cogs in the ./cogs directory, providing a summary.
        This will load new cogs and reload existing ones.
        """
        succeeded = []
        failed = []

        # Iterate through all files in the cogs directory
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                cog_name = f'cogs.{filename[:-3]}'
                try:
                    # Try reloading the extension
                    await self.bot.reload_extension(cog_name)
                    succeeded.append(cog_name)
                except commands.ExtensionNotLoaded:
                    # If the cog wasn't loaded, load it for the first time
                    try:
                        await self.bot.load_extension(cog_name)
                        succeeded.append(f"{cog_name} (Loaded)")
                    except commands.ExtensionError as e:
                        failed.append(f"{cog_name} ({e.__class__.__name__})")
                except commands.ExtensionError as e:
                    # Handle other reloading errors
                    failed.append(f"{cog_name} ({e.__class__.__name__})")

        # Create a summary embed to report the results
        embed = discord.Embed(title="Cog Reload Summary", color=discord.Color.blue())

        if succeeded:
            embed.add_field(name="✅ Reloaded Successfully", value="\n".join(succeeded), inline=False)

        if failed:
            embed.add_field(name="❌ Failed to Reload", value="\n".join(failed), inline=False)
            embed.color = discord.Color.red()

        if not succeeded and not failed:
            embed.description = "No cogs found to reload."

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    """The entry point for loading the Admin cog."""
    await bot.add_cog(Admin(bot))