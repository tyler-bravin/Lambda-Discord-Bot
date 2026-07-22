# cogs/Help.py
"""
A custom help command.

The bot disables discord.py's built-in help (``help_command=None``) so this cog
provides a cleaner, category-grouped replacement that works as both a prefix
command (``!help``) and a slash command (``/help``). ``!help <command>`` shows
detailed usage for a single command.
"""
import discord
from discord.ext import commands


class Help(commands.Cog):
    """Hosts the custom help command."""

    # Order and emoji used to present each cog's commands in the overview.
    COG_ORDER = ["Music", "TaskBoard", "Admin", "Help"]
    COG_EMOJI = {"Music": "🎵", "TaskBoard": "✅", "Admin": "🛠️", "Help": "❓"}

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _prefix(self) -> str:
        """Best-effort display prefix (the configured prefix may be callable)."""
        prefix = self.bot.command_prefix
        if isinstance(prefix, (list, tuple)):
            return prefix[0]
        if isinstance(prefix, str):
            return prefix
        return "!"

    @commands.hybrid_command(name="help")
    async def help_command(self, ctx: commands.Context, *, command: str = None):
        """Shows all commands, or detailed help for a specific command."""
        if command:
            await self._command_help(ctx, command)
        else:
            await self._overview(ctx)

    async def _overview(self, ctx: commands.Context):
        """Lists every visible command, grouped by category."""
        prefix = self._prefix()
        show_hidden = await self.bot.is_owner(ctx.author)

        embed = discord.Embed(
            title="Lambda — Commands",
            description=f"Use `{prefix}command` or `/command`. "
                        f"For details on one command: `{prefix}help <command>`.",
            color=discord.Color.blurple())

        # Preferred cogs first, then any others that might be added later.
        cog_names = self.COG_ORDER + [c for c in self.bot.cogs if c not in self.COG_ORDER]
        for cog_name in cog_names:
            cog = self.bot.get_cog(cog_name)
            if cog is None:
                continue
            visible = [c for c in cog.get_commands() if show_hidden or not c.hidden]
            if not visible:
                continue
            lines = [f"`{prefix}{c.name}` — {c.short_doc or 'No description.'}"
                     for c in sorted(visible, key=lambda c: c.name)]
            emoji = self.COG_EMOJI.get(cog_name, "•")
            embed.add_field(name=f"{emoji} {cog_name}", value="\n".join(lines), inline=False)

        await ctx.send(embed=embed)

    async def _command_help(self, ctx: commands.Context, name: str):
        """Shows detailed usage for a single command."""
        prefix = self._prefix()
        lookup = name.strip().lstrip("/").removeprefix(prefix)
        cmd = self.bot.get_command(lookup)
        if cmd is None or (cmd.hidden and not await self.bot.is_owner(ctx.author)):
            return await ctx.send(embed=discord.Embed(
                description=f"❌ There's no command called `{name}`.", color=discord.Color.red()))

        embed = discord.Embed(
            title=f"{prefix}{cmd.qualified_name}",
            description=cmd.help or cmd.short_doc or "No description.",
            color=discord.Color.blurple())
        if cmd.aliases:
            embed.add_field(name="Aliases", value=", ".join(f"`{a}`" for a in cmd.aliases), inline=False)
        signature = f"{prefix}{cmd.qualified_name} {cmd.signature}".strip()
        embed.add_field(name="Usage", value=f"`{signature}`", inline=False)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    """The entry point for loading the Help cog."""
    await bot.add_cog(Help(bot))
