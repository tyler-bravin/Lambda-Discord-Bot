# cogs/Playlists.py
"""
Named, per-server playlists saved from the live queue.

``!save <name>`` snapshots whatever is playing plus everything queued behind it;
``!load <name>`` appends it back. This is a thin persistence layer over the Music
cog's queue rather than part of it, which keeps the playback engine focused and
means a broken playlist can never stop the music.

Only the metadata is stored — the same fields the resume-after-restart queue
uses. Stream URLs expire within hours, so a loaded playlist re-resolves each
track at play time, exactly as a Spotify playlist does.

Playlists live in the Music cog's database so they sit on the same volume and are
picked up by the backup snapshots.
"""
import json
import logging
import time

import aiosqlite
import discord
from discord.ext import commands

from cogs.sources import Song

log = logging.getLogger(__name__)


class Playlists(commands.Cog):
    """Saving and restoring named queues."""

    # Bounds so a server can't quietly grow the database without limit.
    MAX_PER_GUILD = 25
    MAX_SONGS = 500
    MAX_NAME_LENGTH = 50

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._initialize_database())

    # --- Storage ---

    @property
    def _music(self):
        """The Music cog, or None if it failed to load."""
        return self.bot.get_cog("Music")

    def _db_path(self):
        music = self._music
        return music.db_path if music else None

    async def _initialize_database(self):
        await self.bot.wait_until_ready()
        path = self._db_path()
        if path is None:
            return log.warning("Music cog unavailable; playlists are disabled.")
        async with aiosqlite.connect(path) as db:
            # NOCASE on the name makes lookups and the uniqueness constraint
            # case-insensitive, so "Chill" and "chill" are one playlist.
            await db.execute(
                'CREATE TABLE IF NOT EXISTS saved_playlists ('
                ' guild_id INTEGER NOT NULL,'
                ' name TEXT NOT NULL COLLATE NOCASE,'
                ' songs TEXT NOT NULL,'
                ' created_by INTEGER NOT NULL,'
                ' created_at INTEGER NOT NULL,'
                ' PRIMARY KEY (guild_id, name))')
            await db.commit()
        log.info("Saved playlists ready.")

    async def _fetch(self, guild_id: int, name: str):
        async with aiosqlite.connect(self._db_path()) as db:
            async with db.execute(
                    "SELECT songs, created_by FROM saved_playlists "
                    "WHERE guild_id = ? AND name = ?", (guild_id, name)) as cursor:
                return await cursor.fetchone()

    async def _list(self, guild_id: int):
        async with aiosqlite.connect(self._db_path()) as db:
            async with db.execute(
                    "SELECT name, songs, created_by FROM saved_playlists "
                    "WHERE guild_id = ? ORDER BY name", (guild_id,)) as cursor:
                return await cursor.fetchall()

    async def _store(self, guild_id: int, name: str, songs: list, author_id: int):
        async with aiosqlite.connect(self._db_path()) as db:
            await db.execute(
                "INSERT OR REPLACE INTO saved_playlists "
                "(guild_id, name, songs, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, name, json.dumps(songs), author_id, int(time.time())))
            await db.commit()

    async def _delete(self, guild_id: int, name: str):
        async with aiosqlite.connect(self._db_path()) as db:
            await db.execute("DELETE FROM saved_playlists WHERE guild_id = ? AND name = ?",
                             (guild_id, name))
            await db.commit()

    # --- Helpers ---

    @staticmethod
    def _error(description: str):
        return discord.Embed(description=f"❌ {description}", color=discord.Color.red())

    def _validate_name(self, name: str):
        """Returns the cleaned name, or None if it is unusable."""
        cleaned = " ".join((name or "").split())
        if not cleaned or len(cleaned) > self.MAX_NAME_LENGTH:
            return None
        return cleaned

    # --- Commands ---

    @commands.hybrid_command(name='save')
    async def save_playlist(self, ctx, *, name: str):
        """Saves the current song and queue as a named playlist."""
        music = self._music
        if music is None:
            return await ctx.send(embed=self._error("Music isn't available right now."))

        cleaned = self._validate_name(name)
        if cleaned is None:
            return await ctx.send(embed=self._error(
                f"Give the playlist a name of 1-{self.MAX_NAME_LENGTH} characters."))

        # The playing song first, so loading reproduces what you were listening to.
        current = music.current_song.get(ctx.guild.id)
        songs = ([current] if current else []) + list(music.queues.get(ctx.guild.id, []))
        if not songs:
            return await ctx.send(embed=self._error(
                "There's nothing playing or queued to save."))

        existing = await self._fetch(ctx.guild.id, cleaned)
        if existing is None and len(await self._list(ctx.guild.id)) >= self.MAX_PER_GUILD:
            return await ctx.send(embed=self._error(
                f"This server already has {self.MAX_PER_GUILD} playlists. "
                f"Delete one with `{self.bot.command_prefix}delplaylist <name>` first."))

        trimmed = songs[:self.MAX_SONGS]
        await self._store(ctx.guild.id, cleaned,
                          [song.to_dict() for song in trimmed], ctx.author.id)

        description = f"💾 Saved **{len(trimmed)}** track(s) as **{cleaned}**."
        if existing is not None:
            description += "\nThe previous playlist of that name was replaced."
        if len(songs) > self.MAX_SONGS:
            description += f"\nOnly the first {self.MAX_SONGS} tracks were kept."
        description += f"\nLoad it with `{self.bot.command_prefix}load {cleaned}`."
        await ctx.send(embed=discord.Embed(description=description, color=discord.Color.green()))

    @commands.hybrid_command(name='load')
    async def load_playlist(self, ctx, *, name: str):
        """Adds a saved playlist to the end of the queue."""
        music = self._music
        if music is None:
            return await ctx.send(embed=self._error("Music isn't available right now."))
        if not ctx.author.voice:
            return await ctx.send(embed=self._error("You are not in a voice channel."))

        cleaned = self._validate_name(name)
        row = await self._fetch(ctx.guild.id, cleaned) if cleaned else None
        if row is None:
            return await ctx.send(embed=self._error(
                f"No playlist called **{name}**. See "
                f"`{self.bot.command_prefix}playlists`."))

        try:
            stored = json.loads(row[0])
        except (TypeError, ValueError):
            log.exception("Playlist %r in guild %s is corrupt", cleaned, ctx.guild.id)
            return await ctx.send(embed=self._error(
                f"**{cleaned}** couldn't be read. Save over it to replace it."))

        # Connecting can exceed the 3s a slash interaction allows.
        await ctx.defer()

        vc = ctx.voice_client
        was_playing = bool(vc and (vc.is_playing() or vc.is_paused()))
        if not vc:
            vc = await ctx.author.voice.channel.connect(self_deaf=True)
            music.voice_clients[ctx.guild.id] = vc

        # Whoever loads the playlist owns the tracks, so skip and remove votes
        # treat them as that person's requests.
        queue = music.queues.setdefault(ctx.guild.id, [])
        added = 0
        for song_dict in stored:
            try:
                queue.append(Song(song_dict, ctx.author))
                added += 1
            except Exception:
                log.exception("Skipping an unreadable track in playlist %r", cleaned)

        if not added:
            return await ctx.send(embed=self._error(
                f"**{cleaned}** has no playable tracks left."))

        await music.save_queue_to_db(ctx.guild.id)
        await ctx.send(embed=discord.Embed(
            description=f"📥 Queued **{added}** track(s) from **{cleaned}**.",
            color=discord.Color.green()))

        if not was_playing:
            await music.play_next(ctx)

    @commands.hybrid_command(name='playlists')
    async def list_playlists(self, ctx):
        """Lists the playlists saved on this server."""
        if self._music is None:
            return await ctx.send(embed=self._error("Music isn't available right now."))

        rows = await self._list(ctx.guild.id)
        if not rows:
            return await ctx.send(embed=discord.Embed(
                description=f"ℹ️ No saved playlists yet. Use "
                            f"`{self.bot.command_prefix}save <name>` while music is playing.",
                color=discord.Color.blue()))

        lines = []
        for name, songs, created_by in rows:
            try:
                count = len(json.loads(songs))
            except (TypeError, ValueError):
                count = 0
            owner = ctx.guild.get_member(created_by)
            by = f" · saved by {owner.display_name}" if owner else ""
            lines.append(f"**{name}** — {count} track(s){by}")

        embed = discord.Embed(title="💾 Saved playlists", description="\n".join(lines),
                              color=discord.Color.blurple())
        embed.set_footer(text=f"{len(rows)}/{self.MAX_PER_GUILD} used")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name='delplaylist', aliases=['deleteplaylist'])
    async def delete_playlist(self, ctx, *, name: str):
        """Deletes a saved playlist (its owner or an admin)."""
        if self._music is None:
            return await ctx.send(embed=self._error("Music isn't available right now."))

        cleaned = self._validate_name(name)
        row = await self._fetch(ctx.guild.id, cleaned) if cleaned else None
        if row is None:
            return await ctx.send(embed=self._error(f"No playlist called **{name}**."))

        if not (ctx.author.guild_permissions.administrator or row[1] == ctx.author.id):
            return await ctx.send(embed=self._error(
                "Only an admin or the person who saved it can delete that playlist."))

        await self._delete(ctx.guild.id, cleaned)
        await ctx.send(embed=discord.Embed(
            description=f"🗑️ Deleted the playlist **{cleaned}**.", color=discord.Color.green()))


async def setup(bot: commands.Bot):
    await bot.add_cog(Playlists(bot))
