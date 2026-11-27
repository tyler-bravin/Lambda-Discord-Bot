# cogs/JuiceWRLD.py
"""
Commands for the Juice WRLD API catalogue.

The catalogue holds far more than streaming services do — around 1,500 leaked
unreleased tracks and 500 recording sessions alongside the official releases —
and none of it is searchable by name through yt-dlp, which is the point of these
commands. ``!play`` already handles a pasted juicewrldapi.com link; this cog is
how you find something without one.

Queueing goes through the Music cog, so Juice WRLD tracks sit in the same queue
as everything else and work with skip, seek, loop, filters and the saved
playlists. The audio comes straight off the API as a plain MP3 (see
cogs/juicewrld_api.py), so no yt-dlp lookup is involved at any point.
"""
import logging

import discord
from discord.ext import commands

from cogs import juicewrld_api as api

log = logging.getLogger(__name__)


class JuiceWRLD(commands.Cog):
    """Searching and queueing from the Juice WRLD catalogue."""

    # How many alternatives to offer alongside a queued match, and how many
    # results !juicesearch lists.
    ALTERNATIVES_SHOWN = 4
    SEARCH_RESULTS_SHOWN = 10

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- Helpers ---

    @property
    def _music(self):
        """The Music cog, or None if it failed to load."""
        return self.bot.get_cog("Music")

    @staticmethod
    def _error(description: str) -> discord.Embed:
        return discord.Embed(description=f"❌ {description}", color=discord.Color.red())

    @staticmethod
    def _entry_embed(entry: dict, title: str) -> discord.Embed:
        """A result embed carrying the catalogue details worth seeing."""
        embed = discord.Embed(title=title, description=f"**{entry.get('name')}**",
                              color=discord.Color.from_rgb(153, 153, 153))
        fields = (
            ("Category", (entry.get("category") or "").replace("_", " ").title()),
            ("Era", api.era_name(entry)),
            ("Length", (entry.get("length") or "").strip()),
            ("Producers", entry.get("producers")),
        )
        for name, value in fields:
            if value:
                embed.add_field(name=name, value=str(value)[:1024], inline=True)
        if entry.get("path"):
            embed.set_thumbnail(url=api.cover_url(entry["path"]))
        embed.set_footer(text=f"Juice WRLD API · id {entry.get('id')}")
        return embed

    async def _queue(self, ctx, entry: dict, embed: discord.Embed) -> None:
        """
        Adds a catalogue entry to the guild's queue and starts playback if idle.

        Mirrors what the Playlists cog does: the Music cog owns the queue and the
        voice client, so this only ever hands it a song.
        """
        music = self._music
        song = api.build_song(entry, ctx.author)
        if song is None:
            return await ctx.send(embed=self._error(
                f"**{entry.get('name')}** has no audio file in the catalogue."))

        vc = ctx.voice_client
        was_playing = bool(vc and (vc.is_playing() or vc.is_paused()))
        if not vc:
            vc = await ctx.author.voice.channel.connect(self_deaf=True)
            music.voice_clients[ctx.guild.id] = vc

        music.queues.setdefault(ctx.guild.id, []).append(song)
        await music.save_queue_to_db(ctx.guild.id)
        await ctx.send(embed=embed)

        if not was_playing:
            await music.play_next(ctx)

    def _requires_voice(self, ctx):
        """The error embed to send when the command can't run, or None."""
        if self._music is None:
            return self._error("Music isn't available right now.")
        if not ctx.author.voice:
            return self._error("You are not in a voice channel.")
        return None

    # --- Commands ---

    @commands.hybrid_command(name='juice', aliases=['jw'])
    async def juice(self, ctx, *, query: str = None):
        """Plays a track from the Juice WRLD catalogue (name or catalogue id)."""
        if query is None:
            totals = await api.stats()
            counts = (totals or {}).get("category_stats") or {}
            lines = [f"`{self.bot.command_prefix}juice <song name or id>` — queue a track",
                     f"`{self.bot.command_prefix}juicesearch <name>` — list matches",
                     f"`{self.bot.command_prefix}juicerandom [category]` — queue a random one"]
            if counts:
                lines.append("\n" + " · ".join(
                    f"**{count}** {name.replace('_', ' ')}" for name, count in counts.items()))
            return await ctx.send(embed=discord.Embed(
                title="🧃 Juice WRLD catalogue", description="\n".join(lines),
                color=discord.Color.from_rgb(153, 153, 153)))

        blocked = self._requires_voice(ctx)
        if blocked:
            return await ctx.send(embed=blocked)

        # Searching and connecting can both exceed the 3s a slash interaction allows.
        await ctx.defer()

        query = query.strip()
        if query.isdigit():
            entry = await api.fetch_song(int(query))
            if entry is None:
                return await ctx.send(embed=self._error(
                    f"No catalogue entry with the id `{query}`."))
            if not api.is_playable(entry):
                return await ctx.send(embed=self._error(
                    f"**{entry.get('name')}** is {entry.get('category')} — there is no "
                    "audio file for it in the catalogue."))
            return await self._queue(ctx, entry, self._entry_embed(entry, "🧃 Added to Queue"))

        matches = await api.search(query)
        if not matches:
            return await ctx.send(embed=self._error(
                f"Nothing playable in the Juice WRLD catalogue matches **{query}**."))

        embed = self._entry_embed(matches[0], "🧃 Added to Queue")
        others = matches[1:1 + self.ALTERNATIVES_SHOWN]
        if others:
            embed.add_field(
                name="Other matches",
                value="\n".join(f"`{entry['id']}` {api.describe(entry)}" for entry in others)
                      + f"\n\nQueue one with `{self.bot.command_prefix}juice <id>`.",
                inline=False)
        await self._queue(ctx, matches[0], embed)

    @commands.hybrid_command(name='juicesearch', aliases=['jws'])
    async def juice_search(self, ctx, *, query: str):
        """Lists Juice WRLD catalogue matches with their ids."""
        await ctx.defer()
        matches = await api.search(query)
        if not matches:
            return await ctx.send(embed=self._error(
                f"Nothing playable in the Juice WRLD catalogue matches **{query}**."))

        shown = matches[:self.SEARCH_RESULTS_SHOWN]
        embed = discord.Embed(
            title=f"🧃 Catalogue matches for “{query}”",
            description="\n".join(f"`{entry['id']}` {api.describe(entry)}" for entry in shown),
            color=discord.Color.from_rgb(153, 153, 153))
        embed.set_footer(text=f"Showing {len(shown)} of {len(matches)} · "
                              f"queue one with {self.bot.command_prefix}juice <id>")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name='juicerandom', aliases=['jwr'])
    async def juice_random(self, ctx, category: str = api.RANDOM_DEFAULT_CATEGORY):
        """Queues a random track from the Juice WRLD catalogue."""
        blocked = self._requires_voice(ctx)
        if blocked:
            return await ctx.send(embed=blocked)

        category = (category or "").strip().lower().replace(" ", "_")
        if category in ("any", "all"):
            category = None
        elif category not in api.PLAYABLE_CATEGORIES:
            return await ctx.send(embed=self._error(
                "Pick one of " + ", ".join(f"`{name}`" for name in api.PLAYABLE_CATEGORIES)
                + " or `any`."))

        await ctx.defer()
        entry = await api.random_entry(category)
        if entry is None:
            return await ctx.send(embed=self._error(
                "Couldn't reach the Juice WRLD catalogue. Try again in a moment."))
        await self._queue(ctx, entry, self._entry_embed(entry, "🧃 Random Pick"))


async def setup(bot: commands.Bot):
    await bot.add_cog(JuiceWRLD(bot))
