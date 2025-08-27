# cogs/Admin.py
"""
A cog for bot owner administrative commands.

This cog includes essential commands for managing the bot's operation,
such as reloading other cogs without needing a full restart. Access is
restricted to the bot owner.
"""
import os
import discord
from discord.ext import commands


class Admin(commands.Cog):
    """Hosts administrative commands for the bot owner."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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