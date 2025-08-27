# cogs/Music.py

"""
A comprehensive music bot for Discord using discord.py, yt-dlp, and Spotipy.

This bot supports playback from YouTube and Spotify, playlist handling,
persistent queues via a database, and a vote-based control system for
moderating player actions like skipping, stopping, and pausing. It features
interactive UI components built with discord.ui for a modern user experience.

Key Features:
- YouTube and Spotify (tracks, playlists, albums) support.
- Persistent queues and volume settings stored in an SQLite database.
- Interactive player controls (play/pause, skip, stop, previous) via buttons.
- Vote-based system for player actions to ensure democratic control.
- Looping functionality (song or queue).
- Automatic disconnection when idle or left alone in a channel.
- A suite of commands for queue management (view, shuffle, remove, clear).
"""

import discord
from discord.ext import commands, tasks
import yt_dlp
import asyncio
import aiosqlite
import json
import math
import traceback
import random
import time
import spotipy
import os
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque
from typing import Union
import urllib.parse

# --- Bot Setup: yt-dlp and FFmpeg Configuration ---

# Suppress yt-dlp's default bug report message on console errors.
yt_dlp.utils.bug_reports_message = lambda **kwargs: ''

# Configuration for yt-dlp to optimize for audio-only streams.
ytdl_format_options = {
    'cachedir': '.cache',
    # Prioritizes best audio quality, preferring opus format and streams over 128kbps.
    'format': 'bestaudio[ext=opus]/bestaudio[abr>128]/bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    # Ensures only single videos are processed unless a playlist is explicitly requested.
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    # Binds to '0.0.0.0' to mitigate potential IP-related blocking from services like YouTube.
    'source_address': '0.0.0.0',
    'cookiefile': 'cookies.txt',
}

# Configuration for FFmpeg, the audio processing library.
ffmpeg_options = {
    # Arguments passed to FFmpeg before the input, useful for reconnection on stream interruptions.
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    # Core arguments; '-vn' specifies no video processing, which saves resources.
    'options': '-vn'
}

# Initialize the YoutubeDL client with the specified options.
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


# --- Data Structures ---

class Song:
    """
    A data class representing a song in the queue.

    This class standardizes song information, whether it comes from YouTube or
    another source. It serves as a structured model for both in-memory queue
    management and for serialization into the database.
    """
    def __init__(self, data, requester):
        # The full data dictionary from yt-dlp, kept for creating the player source later.
        self.data = data
        self.url = data.get('webpage_url')
        self.title = data.get('title', 'Unknown Title')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requester = requester
        self.requester_id = requester.id
        # The direct audio stream URL, which can expire.
        self.stream_url = data.get('url')

    def to_dict(self):
        """
        Serializes the Song object into a dictionary for database storage.

        This method prepares the object for JSON conversion, ensuring only
        essential, non-expiring metadata is saved. The full 'data' blob and
        temporary 'stream_url' are excluded.
        """
        return {
            'url': self.url,
            'title': self.title,
            'thumbnail': self.thumbnail,
            'duration': self.duration,
            'uploader': self.uploader,
            'requester_id': self.requester_id
        }


class YTDLSource(discord.PCMVolumeTransformer):
    """
    Represents an audio source fetched from YouTube or a similar service.

    This class wraps the raw audio stream from FFmpeg and is a subclass of
    `discord.PCMVolumeTransformer`, which allows for real-time volume control.
    It acts as a factory, using class methods to create instances from either
    pre-fetched data or a URL.
    """
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requester = data.get('requester')

    @classmethod
    async def from_data(cls, data, *, volume=0.5):
        """
        Creates a YTDLSource instance directly from pre-fetched yt-dlp data.
        This is the "fast path" for playback as it avoids blocking network calls.
        """
        filename = data.get('url')
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data, volume=volume)

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False, requester=None, volume=0.5):
        """
        Creates a YTDLSource by fetching info from a URL.
        This is the "slower path" or fallback, used for songs loaded from the
        database that need their stream URL re-fetched. It runs the blocking
        `extract_info` call in an executor to avoid stalling the bot's event loop.
        """
        loop = loop or asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            data = data['entries'][0]

        data['requester'] = requester
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data, volume=volume)


# --- UI Views for Interactive Controls ---

class PlayerControls(discord.ui.View):
    """
    A persistent view attached to the 'Now Playing' message with player controls.

    This view is designed with `timeout=None` to be persistent, meaning it will
    remain active and functional even after the bot restarts.
    """
    def __init__(self, music_cog, player):
        super().__init__(timeout=None)
        self.cog = music_cog

        # Dynamically create and add a "Lyrics" button that links to a Genius search.
        if player and player.title:
            search_query = f"{player.title} {player.uploader}"
            encoded_query = urllib.parse.quote_plus(search_query)
            lyrics_url = f"https://genius.com/search?q={encoded_query}"

            lyrics_button = discord.ui.Button(
                label="Lyrics", emoji="📜", style=discord.ButtonStyle.link, url=lyrics_url
            )
            self.add_item(lyrics_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """
        Validates that the interacting user is in the bot's voice channel.
        This prevents users outside the current music session from using the controls.
        """
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("❌ You must be in the voice channel to use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji='⏮️', style=discord.ButtonStyle.secondary, custom_id="player_previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Button to play the previously played song."""
        guild_id = interaction.guild.id
        history = self.cog.song_history.get(guild_id)

        if not history or len(history) < 2:
            return await interaction.response.send_message("There is no previous song in the history.", ephemeral=True)

        # Pop the current song (just finished) and then the actual previous song.
        current_song = history.pop()
        previous_song = history.pop()

        # Add them back to the front of the main queue in the correct order.
        queue = self.cog.queues.setdefault(guild_id, [])
        queue.insert(0, current_song)
        queue.insert(0, previous_song)

        # Stop the current player (if any) to trigger the 'after' callback.
        if vc := interaction.guild.voice_client:
            vc.stop()
        await interaction.response.send_message("⏪ Playing previous song.", ephemeral=True)

    @discord.ui.button(emoji='⏯️', style=discord.ButtonStyle.secondary, custom_id="player_play_pause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggles between playing and pausing the current song."""
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            await self.cog._pause_logic(interaction)
        elif vc and vc.is_paused():
            # Resuming does not require a vote.
            vc.resume()
            self.cog.inactive_since.pop(interaction.guild.id, None)
            self.cog.pause_votes.pop(interaction.guild.id, None)
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)

    @discord.ui.button(emoji='⏹️', style=discord.ButtonStyle.danger, custom_id="player_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stops playback and clears the queue, subject to a vote."""
        await self.cog._stop_logic(interaction)

    @discord.ui.button(emoji='⏭️', style=discord.ButtonStyle.secondary, custom_id="player_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skips the current song, subject to a vote."""
        await self.cog._skip_logic(interaction)


class LoopControlsView(discord.ui.View):
    """A temporary, user-restricted view for the `!loop` command to select a loop mode."""
    def __init__(self, music_cog, ctx):
        super().__init__(timeout=120.0)
        self.cog = music_cog
        self.ctx = ctx
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensures only the command author can use this interactive menu."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("❌ You cannot use this menu.", ephemeral=True)
            return False
        # Further checks ensure the user is still in the correct voice channel.
        vc = interaction.guild.voice_client
        if not vc or not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("❌ You must be in the voice channel to use this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        """Disables all buttons and updates the message when the view expires."""
        if self.message:
            for item in self.children:
                item.disabled = True
            await self.message.edit(content="*This loop menu has expired.*", view=self)

    async def _handle_vote(self, interaction: discord.Interaction, mode: str):
        """Handles the logic for voting on a loop mode."""
        # Admins can bypass the vote entirely.
        if interaction.user.guild_permissions.administrator:
            self.cog.loop_states[interaction.guild.id] = mode
            for item in self.children: item.disabled = True
            await self.message.edit(view=self)
            self.stop()
            await interaction.response.send_message(f"✅ Loop mode force-set to **{mode}** by an admin.", ephemeral=True)
            return

        # Calculate the required number of votes for a majority.
        vc = interaction.guild.voice_client
        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1

        guild_votes = self.cog.loop_votes.setdefault(interaction.guild.id, {})
        voters = guild_votes.setdefault(mode, set())

        if interaction.user.id in voters:
            await interaction.response.send_message(f"ℹ️ You have already voted to set loop to **{mode}**.", ephemeral=True)
            return

        voters.add(interaction.user.id)

        # Check if the vote threshold has been met.
        if len(voters) >= required_votes:
            self.cog.loop_states[interaction.guild.id] = mode
            self.cog.loop_votes.pop(interaction.guild.id, None)
            for item in self.children: item.disabled = True
            await self.message.edit(view=self)
            self.stop()
            await interaction.response.send_message(f"🗳️ Vote passed! Loop mode has been set to **{mode}**.")
        else:
            await interaction.response.send_message(
                f"🗳️ Your vote to set loop to **{mode}** was added. Now at **{len(voters)}/{required_votes}** votes.",
                ephemeral=True)

    @discord.ui.button(label="Loop Song", emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="loop_song")
    async def loop_song_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 'song')

    @discord.ui.button(label="Loop Queue", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="loop_queue")
    async def loop_queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 'queue')

    @discord.ui.button(label="Turn Off", emoji="❌", style=discord.ButtonStyle.danger, custom_id="loop_off")
    async def loop_off_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, 'off')


class QueuePaginator(discord.ui.View):
    """A view for paginating through the song queue with interactive buttons."""
    QUEUE_SONGS_PER_PAGE = 5

    def __init__(self, queue, now_playing):
        super().__init__(timeout=120)
        self.queue = queue
        self.now_playing = now_playing
        self.current_page = 0
        self.songs_per_page = self.QUEUE_SONGS_PER_PAGE
        self.total_pages = math.ceil(len(self.queue) / self.songs_per_page)

        # Disable navigation buttons if there's only one page or no pages.
        if self.total_pages <= 1:
            self.previous_button.disabled = True
            self.next_button.disabled = True

    async def get_page_embed(self):
        """Constructs the embed for the current page of the queue."""
        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())
        if self.now_playing:
            embed.add_field(name="Now Playing", value=f"[{self.now_playing.title}]({self.now_playing.url})", inline=False)
            if self.now_playing.thumbnail:
                embed.set_thumbnail(url=self.now_playing.thumbnail)
        else:
            embed.add_field(name="Now Playing", value="Nothing is currently playing.", inline=False)

        # Calculate the slice of the queue for the current page.
        start_index = self.current_page * self.songs_per_page
        end_index = start_index + self.songs_per_page
        if self.queue:
            upcoming_list = ""
            for i, song in enumerate(self.queue[start_index:end_index], start=start_index):
                if song.url:
                    upcoming_list += f"**{i + 1}.** [{discord.utils.escape_markdown(song.title)}]({song.url})\n"
                else:
                    upcoming_list += f"**{i + 1}.** {discord.utils.escape_markdown(song.title)}\n"
            if upcoming_list:
                embed.add_field(name="Up Next", value=upcoming_list, inline=False)

        footer_text = f"{len(self.queue)} songs in queue"
        if self.total_pages > 0:
            footer_text = f"Page {self.current_page + 1}/{self.total_pages} | {footer_text}"
        embed.set_footer(text=footer_text)
        return embed

    @discord.ui.button(label='⬅️', style=discord.ButtonStyle.primary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Navigates to the previous page of the queue."""
        self.current_page = (self.current_page - 1 + self.total_pages) % self.total_pages
        embed = await self.get_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='➡️', style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Navigates to the next page of the queue."""
        self.current_page = (self.current_page + 1) % self.total_pages
        embed = await self.get_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# --- Core Music Cog ---

class Music(commands.Cog):
    """The main cog for handling all music-related commands and events."""
    INACTIVITY_TIMEOUT_SECONDS = 120
    SONG_HISTORY_MAXLEN = 20

    def __init__(self, bot):
        self.bot = bot
        # State Management Dictionaries
        self.queues: dict[int, list[Song]] = {}               # guild_id: [Song, ...]
        self.voice_clients: dict[int, discord.VoiceClient] = {} # guild_id: discord.VoiceClient
        self.loop_states: dict[int, str] = {}                 # guild_id: 'off' | 'song' | 'queue'
        self.song_start_times: dict[int, float] = {}          # guild_id: float (timestamp)
        self.song_history: dict[int, deque[Song]] = {}        # guild_id: deque([Song, ...])
        self.now_playing_messages: dict[int, discord.Message] = {} # guild_id: discord.Message
        self.guild_volumes: dict[int, int] = {}               # guild_id: int (0-200)
        self.inactive_since: dict[int, float] = {}            # guild_id: float (timestamp)

        # Vote Tracking Dictionaries
        self.skip_votes: dict[int, set[int]] = {}             # guild_id: {user_id, ...}
        self.pause_votes: dict[int, set[int]] = {}            # guild_id: {user_id, ...}
        self.stop_votes: dict[int, set[int]] = {}             # guild_id: {user_id, ...}
        self.clear_votes: dict[int, set[int]] = {}            # guild_id: {user_id, ...}
        self.remove_votes: dict[int, dict[int, set[int]]] = {} # guild_id: {song_index: {user_id, ...}}
        self.shuffle_votes: dict[int, set[int]] = {}          # guild_id: {user_id, ...}
        self.disconnect_votes: dict[int, set[int]] = {}       # guild_id: {user_id, ...}
        self.loop_votes: dict[int, dict[str, set[int]]] = {}  # guild_id: {mode: {user_id, ...}}

        # Database and API Initialization
        self.db_path = 'music_queue.db'
        self.sp = None

        load_dotenv()
        try:
            client_id = os.getenv("SPOTIPY_CLIENT_ID")
            client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise ValueError("Spotify credentials not found in .env file.")
            client_credentials_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
            self.sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)
            print("✅ Spotipy initialized successfully from .env file.")
        except Exception as e:
            print(f"❌ Could not initialize Spotipy. Spotify links will not work. Error: {e}")
            self.sp = None

        self.bot.loop.create_task(self.initialize_database())
        self.auto_disconnect.start()

    # --- Internal Helper Methods ---
    async def _process_playlist_in_background(self, ctx: commands.Context, search_queries: list):
        """Processes a list of search queries in the background, adding them to the queue."""
        requester = ctx.author
        guild_id = ctx.guild.id

        for query in search_queries:
            # A small sleep helps distribute the load and avoid rate-limiting.
            await asyncio.sleep(1)
            song = await self._search_and_create_song(query, requester)
            if song:
                self.queues.setdefault(guild_id, []).append(song)
                await self.save_queue_to_db(guild_id)

    async def _search_and_create_song(self, query: str, requester: discord.Member) -> Union[Song, None]:
        """Searches for a query using yt-dlp and creates a Song object from the result."""
        try:
            loop = self.bot.loop or asyncio.get_running_loop()
            # Run the blocking yt-dlp search in an executor to avoid stalling the bot.
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            if not data: return None
            # For searches, yt-dlp returns a playlist; we take the first entry.
            if 'entries' in data:
                if not data['entries']: return None
                data = data['entries'][0]
            data['requester'] = requester
            return Song(data, requester)
        except Exception:
            traceback.print_exc()
            return None

    async def _cleanup_player_message(self, guild_id: int):
        """
        Removes the interactive view from the previous 'Now Playing' message.
        This prevents users from interacting with outdated controls.
        """
        old_message = self.now_playing_messages.pop(guild_id, None)
        if old_message:
            try:
                await old_message.edit(view=None)
            except (discord.NotFound, discord.Forbidden):
                # Ignore if the message was already deleted manually.
                pass

    async def _handle_disconnect(self, vc: discord.VoiceClient):
        """A centralized cleanup routine for when the bot disconnects from a voice channel."""
        guild_id = vc.guild.id
        self.queues.pop(guild_id, None)
        await self.save_queue_to_db(guild_id)
        await self._cleanup_player_message(guild_id)
        # Reset all state for the guild to ensure a fresh start next time.
        self.loop_states.pop(guild_id, None)
        self.song_start_times.pop(guild_id, None)
        self.voice_clients.pop(guild_id, None)
        self.inactive_since.pop(guild_id, None)
        if vc.is_connected():
            await vc.disconnect()

    async def _get_context(self, interaction_or_ctx):
        """
        A helper to abstract away the difference between an Interaction and a Context.
        This allows command logic to be reused for both prefix commands and button interactions.
        """
        if isinstance(interaction_or_ctx, discord.Interaction):
            return {"is_interaction": True, "ctx": None, "interaction": interaction_or_ctx,
                    "author": interaction_or_ctx.user, "guild": interaction_or_ctx.guild,
                    "channel": interaction_or_ctx.channel}
        else:  # It's a Context object
            return {"is_interaction": False, "ctx": interaction_or_ctx, "interaction": None,
                    "author": interaction_or_ctx.author, "guild": interaction_or_ctx.guild,
                    "channel": interaction_or_ctx.channel}

    async def _send_response(self, context_data, *args, **kwargs):
        """A helper to send a response correctly to either an Interaction or a Context."""
        if context_data["is_interaction"]:
            interaction = context_data["interaction"]
            # Interactions must be responded to differently depending on if it's the first response.
            if not interaction.response.is_done():
                await interaction.response.send_message(*args, **kwargs)
            else:
                await interaction.followup.send(*args, **kwargs)
        else:
            await context_data["channel"].send(*args, **kwargs)

    # --- Database & State Persistence ---
    async def initialize_database(self):
        """Initializes the SQLite database and creates tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('CREATE TABLE IF NOT EXISTS queues (guild_id INTEGER PRIMARY KEY, queue_data TEXT NOT NULL)')
            await db.execute('CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER PRIMARY KEY, volume INTEGER DEFAULT 50)')
            await db.commit()
        await self.load_queues_from_db()
        await self.load_volumes_from_db()

    async def load_queues_from_db(self):
        """Loads all guild queues from the database into memory on startup."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT guild_id, queue_data FROM queues") as cursor:
                async for row in cursor:
                    guild_id, queue_json = row
                    guild = self.bot.get_guild(guild_id)
                    if not guild: continue # Skip if the bot is no longer in this guild.
                    queue_data = json.loads(queue_json)
                    self.queues[guild_id] = []
                    for song_dict in queue_data:
                        # Reconstruct the requester Member object from the stored ID.
                        requester = guild.get_member(song_dict['requester_id']) or self.bot.user
                        self.queues[guild_id].append(Song(song_dict, requester))
        print("✅ Queues loaded from database.")

    async def load_volumes_from_db(self):
        """Loads all guild volume settings from the database into memory on startup."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT guild_id, volume FROM guild_settings") as cursor:
                async for row in cursor:
                    guild_id, volume = row
                    self.guild_volumes[guild_id] = volume
        print("✅ Guild volumes loaded from database.")

    async def save_queue_to_db(self, guild_id):
        """Saves a guild's current queue to the database."""
        queue = self.queues.get(guild_id, [])
        queue_data = [song.to_dict() for song in queue]
        async with aiosqlite.connect(self.db_path) as db:
            if not queue_data:
                # If the queue is empty, remove its entry from the database to keep it clean.
                await db.execute("DELETE FROM queues WHERE guild_id = ?", (guild_id,))
            else:
                queue_json = json.dumps(queue_data)
                await db.execute("INSERT OR REPLACE INTO queues (guild_id, queue_data) VALUES (?, ?)", (guild_id, queue_json))
            await db.commit()

    async def save_volume_to_db(self, guild_id: int, volume: int):
        """Saves a guild's volume setting to the database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO guild_settings (guild_id, volume) VALUES (?, ?)", (guild_id, volume))
            await db.commit()

    # --- Core Playback Engine ---
    async def on_song_end(self, ctx, finished_song_data):
        """
        Callback function that is executed automatically after a song finishes playing.
        This handles looping logic and triggers the next song in the queue.
        """
        guild_id = ctx.guild.id
        loop_state = self.loop_states.get(guild_id, 'off')

        # Add the finished song to the history deque for the 'previous' command.
        if finished_song_data:
            if guild_id not in self.song_history:
                self.song_history[guild_id] = deque(maxlen=self.SONG_HISTORY_MAXLEN)
            self.song_history[guild_id].append(finished_song_data)

        # Handle loop logic.
        if loop_state == 'song' and finished_song_data:
            self.queues.setdefault(guild_id, []).insert(0, finished_song_data)
        elif loop_state == 'queue' and finished_song_data:
            self.queues.setdefault(guild_id, []).append(finished_song_data)

        await self.save_queue_to_db(guild_id)

        # If the bot is still in a voice channel, attempt to play the next song.
        if ctx.guild.voice_client:
            await self.play_next(ctx)

    async def play_next(self, ctx):
        """
        The main playback handler. Pops the next song, plays it, and updates state.
        """
        guild_id = ctx.guild.id
        await self._cleanup_player_message(guild_id)
        self.inactive_since.pop(guild_id, None)

        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            self.voice_clients.pop(guild_id, None)
            return

        if guild_id in self.queues and self.queues[guild_id]:
            # Reset all vote counts for the new song.
            self.skip_votes.pop(guild_id, None)
            self.pause_votes.pop(guild_id, None)
            self.stop_votes.pop(guild_id, None)
            # ... clear other votes ...

            next_song = self.queues[guild_id].pop(0)
            guild_volume = self.guild_volumes.get(guild_id, 50)

            try:
                # Fast path: If the song has a pre-fetched stream URL, use it.
                if hasattr(next_song, 'stream_url') and next_song.stream_url:
                    player = await YTDLSource.from_data(next_song.data, volume=guild_volume / 100.0)
                # Fallback path: If it's a song from the DB, re-fetch the stream URL.
                else:
                    player = await YTDLSource.from_url(
                        next_song.url, loop=self.bot.loop, stream=True,
                        requester=next_song.requester, volume=guild_volume / 100.0
                    )
            except Exception as e:
                await ctx.send(embed=discord.Embed(
                    title="❌ Playback Error",
                    description=f"Could not play `{next_song.title}`.\nIt may be unavailable or restricted.\nSkipping.",
                    color=discord.Color.red()))
                traceback.print_exc()
                return await self.on_song_end(ctx, None)

            try:
                # The 'after' parameter registers the on_song_end callback to run when playback finishes.
                vc.play(player, after=lambda e: self.bot.loop.create_task(self.on_song_end(ctx, next_song)))
            except discord.ClientException:
                # This can happen if the bot disconnects while trying to play.
                return

            self.song_start_times[guild_id] = time.time()

            # Create and send the 'Now Playing' embed with controls.
            embed = discord.Embed(title="🎶 Now Playing", description=f"[{player.title}]({player.url})", color=discord.Color.blue())
            if player.thumbnail: embed.set_thumbnail(url=player.thumbnail)
            embed.add_field(name="Uploader", value=player.uploader, inline=True)
            if player.duration:
                duration_min, duration_sec = divmod(player.duration, 60)
                embed.add_field(name="Duration", value=f"{duration_min}:{duration_sec:02d}", inline=True)
            embed.set_footer(text=f"Requested by {player.requester.display_name}", icon_url=player.requester.display_avatar.url)

            controls = PlayerControls(self, player)
            now_playing_message = await ctx.send(embed=embed, view=controls)
            self.now_playing_messages[guild_id] = now_playing_message

            # Update the bot's rich presence to show the currently playing song.
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=player.title))
            await self.save_queue_to_db(guild_id)
        else:
            # If the queue is empty, reset presence and mark the bot as inactive for auto-disconnection.
            self.song_start_times.pop(guild_id, None)
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
            await ctx.send(embed=discord.Embed(description="✅ Queue finished.", color=discord.Color.green()))
            self.inactive_since[guild_id] = time.time()

    # --- Event Listeners & Background Tasks ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Listener that triggers on voice state changes to detect if the bot is left alone."""
        # Ignore bots' voice state changes.
        if not member.bot and after.channel is None:
            vc = member.guild.voice_client
            # If the bot is in a channel and there's only 1 member left (the bot itself)...
            if vc and len(vc.channel.members) == 1:
                await self._handle_disconnect(vc)

    @tasks.loop(seconds=30)
    async def auto_disconnect(self):
        """A background task that runs periodically to check for inactivity."""
        for guild_id, inactive_start_time in list(self.inactive_since.items()):
            if time.time() - inactive_start_time >= self.INACTIVITY_TIMEOUT_SECONDS:
                guild = self.bot.get_guild(guild_id)
                if guild and guild.voice_client:
                    if not guild.voice_client.is_playing() and not guild.voice_client.is_paused():
                        try:
                            await guild.voice_client.channel.send("👋 Disconnecting due to 2 minutes of inactivity.")
                        except Exception:
                            pass # Channel might not be accessible.
                        await self._handle_disconnect(guild.voice_client)

    # --- User-Facing Commands ---
    @commands.command(name='play', aliases=['p'])
    async def play(self, ctx, *, query: str = None):
        """Plays a song from a URL or search query, or resumes playback."""
        if not ctx.author.voice:
            return await ctx.send(embed=discord.Embed(description="❌ You are not in a voice channel.", color=discord.Color.red()))

        vc = ctx.voice_client
        was_playing = vc and (vc.is_playing() or vc.is_paused())

        if not vc:
            vc = await ctx.author.voice.channel.connect()
            self.voice_clients[ctx.guild.id] = vc

        if vc and vc.is_paused() and query is None:
            vc.resume()
            self.inactive_since.pop(ctx.guild.id, None)
            self.pause_votes.pop(ctx.guild.id, None)
            return await ctx.send(embed=discord.Embed(description="▶️ Resumed playback.", color=discord.Color.green()))

        if query is None:
            return await ctx.send(embed=discord.Embed(title="ℹ️ How to use `!play`",
                                                      description=f"`{self.bot.command_prefix}play <song name, URL, or playlist URL>`",
                                                      color=discord.Color.blue()))

        processing_embed = await ctx.send(embed=discord.Embed(description="🔎 Processing request...", color=discord.Color.yellow()))

        search_queries = []
        is_playlist = False
        try:
            # A more robust check for spotify links.
            parsed_url = urllib.parse.urlparse(query)
            is_spotify_link = "spotify.com" in parsed_url.netloc
        except:
            is_spotify_link = False

        # Spotify link processing logic
        if is_spotify_link and self.sp:
            await processing_embed.edit(embed=discord.Embed(description="🎶 Found a Spotify link, getting info...", color=discord.Color.green()))
            try:
                if "track" in query:
                    track = self.sp.track(query)
                    artist_names = ", ".join([artist['name'] for artist in track['artists']])
                    search_queries.append(f"{track['name']} {artist_names}")
                elif "playlist" in query or "album" in query:
                    is_playlist = True
                    items = []
                    if "playlist" in query:
                        results = self.sp.playlist_items(query, limit=100)
                        items.extend(results['items'])
                        while results['next']:
                            results = self.sp.next(results)
                            items.extend(results['items'])
                    else:  # album
                        results = self.sp.album_tracks(query, limit=50)
                        items.extend(results['items'])
                        while results['next']:
                            results = self.sp.next(results)
                            items.extend(results['items'])

                    album_info = self.sp.album(query) if "album" in query else None
                    for item in items:
                        track = item if "album" in query else item.get('track')
                        if track and track.get('name'):
                            artist_names = ", ".join([artist['name'] for artist in track['artists']])
                            if not artist_names and album_info:
                                artist_names = ", ".join([artist['name'] for artist in album_info['artists']])
                            search_queries.append(f"{track['name']} {artist_names}")
            except Exception:
                traceback.print_exc()
                return await processing_embed.edit(embed=discord.Embed(title="❌ Spotify Error", description="Could not process the Spotify link. It may be invalid or private.", color=discord.Color.red()))
        else:
            search_queries.append(query)

        if not search_queries:
            return await processing_embed.edit(embed=discord.Embed(title="❌ Nothing Found", description="Could not find any songs to add from the provided link.", color=discord.Color.red()))

        queue = self.queues.setdefault(ctx.guild.id, [])

        # Playlist handling: add the first song immediately, then process the rest in the background.
        if is_playlist:
            await processing_embed.edit(embed=discord.Embed(description=f"✅ Playlist detected! Starting the first song and adding **{len(search_queries)}** others in the background.", color=discord.Color.green()))
            first_song = None
            for i, individual_query in enumerate(search_queries):
                song = await self._search_and_create_song(individual_query, ctx.author)
                if song:
                    first_song = song
                    queue.append(first_song)
                    await self.save_queue_to_db(ctx.guild.id)
                    remaining_queries = search_queries[i + 1:]
                    if remaining_queries:
                        self.bot.loop.create_task(self._process_playlist_in_background(ctx, remaining_queries))
                    break
            if not first_song:
                return await processing_embed.edit(embed=discord.Embed(title="❌ Could Not Find Songs", description="Could not find any playable YouTube videos for your playlist.", color=discord.Color.red()))
        # Single song handling
        else:
            song = await self._search_and_create_song(search_queries[0], ctx.author)
            if not song:
                return await processing_embed.edit(embed=discord.Embed(title="❌ Could Not Find Song", description="Could not find a playable YouTube video for your request.", color=discord.Color.red()))

            queue.append(song)
            await self.save_queue_to_db(ctx.guild.id)

            if was_playing:
                await processing_embed.edit(embed=discord.Embed(title="✅ Added to Queue", description=f"Added **[{song.title}]({song.url})** to the queue.", color=discord.Color.green()))
            else:
                # If nothing was playing, delete the "Processing..." message as the "Now Playing" will appear.
                await processing_embed.delete()

        # Start playback if the bot wasn't already playing.
        if not was_playing:
            await self.play_next(ctx)

    @commands.command(name='volume', aliases=['vol'])
    async def volume(self, ctx, volume: int = None):
        """Sets the player's volume (0-200), saved permanently for the server."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=discord.Embed(description="❌ I am not in a voice channel.", color=discord.Color.red()), delete_after=10)

        if volume is None:
            current_volume = self.guild_volumes.get(ctx.guild.id, 50)
            return await ctx.send(embed=discord.Embed(description=f"ℹ️ Current volume is set to **{current_volume}%**.", color=discord.Color.blue()))

        if not 0 <= volume <= 200:
            return await ctx.send(embed=discord.Embed(description="❌ Volume must be between 0 and 200.", color=discord.Color.red()))

        if vc.source:
            vc.source.volume = volume / 100
        self.guild_volumes[ctx.guild.id] = volume
        await self.save_volume_to_db(ctx.guild.id, volume)
        await ctx.send(embed=discord.Embed(description=f"✅ Volume permanently set to **{volume}%**.", color=discord.Color.green()))

    @commands.command(name='nowplaying', aliases=['np', 'now'])
    async def nowplaying(self, ctx):
        """Displays detailed information about the currently playing song."""
        vc = ctx.voice_client
        if not vc or not vc.source:
            return await ctx.send(embed=discord.Embed(description="ℹ️ I am not playing anything.", color=discord.Color.blue()), delete_after=10)

        song = vc.source
        start_time = self.song_start_times.get(ctx.guild.id)
        embed = discord.Embed(title="🎶 Now Playing", description=f"**[{song.title}]({song.url})**", color=discord.Color.green())
        if song.thumbnail: embed.set_thumbnail(url=song.thumbnail)
        embed.set_footer(text=f"Requested by {song.requester.display_name}", icon_url=song.requester.display_avatar.url)

        # Create a visual progress bar based on elapsed time.
        if song.duration and start_time:
            elapsed = time.time() - start_time
            if elapsed > song.duration: elapsed = song.duration
            elapsed_str = time.strftime('%M:%S', time.gmtime(elapsed))
            duration_str = time.strftime('%M:%S', time.gmtime(song.duration))
            progress = int((elapsed / song.duration) * 20)
            progress_bar = '─' * progress + '◉' + '─' * (20 - progress)
            embed.add_field(name="Progress", value=f"`{elapsed_str} {progress_bar} {duration_str}`", inline=False)

        await ctx.send(embed=embed)

    @commands.command(name='loop')
    async def loop(self, ctx, mode: str = None):
        """Sets the loop mode (off, song, queue) via command or interactive view."""
        if mode:
            modes = ['song', 'queue', 'off']
            if mode.lower() not in modes:
                return await ctx.send(embed=discord.Embed(description=f"❌ Invalid mode. Please choose from: `{'`, `'.join(modes)}`.", color=discord.Color.red()))
            mode = mode.lower()

            if ctx.author.guild_permissions.administrator:
                self.loop_states[ctx.guild.id] = mode
                return await ctx.send(embed=discord.Embed(description=f"✅ Loop mode force-set to **{mode}**.", color=discord.Color.green()))

            vc = ctx.voice_client
            if not ctx.author.voice or not vc or ctx.author.voice.channel != vc.channel:
                return await ctx.send(embed=discord.Embed(description="❌ You must be in my voice channel to vote.", color=discord.Color.red()))

            listeners = [member for member in vc.channel.members if not member.bot]
            required_votes = (len(listeners) // 2) + 1
            guild_votes = self.loop_votes.setdefault(ctx.guild.id, {})
            voters = guild_votes.setdefault(mode, set())

            if ctx.author.id in voters:
                return await ctx.send(embed=discord.Embed(description=f"ℹ️ You have already voted to set loop to **{mode}**.", color=discord.Color.yellow()))
            voters.add(ctx.author.id)

            if len(voters) >= required_votes:
                self.loop_states[ctx.guild.id] = mode
                self.loop_votes.pop(ctx.guild.id, None)
                await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! Loop mode set to **{mode}**.", color=discord.Color.green()))
            else:
                await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to set loop to **{mode}** added. **{len(voters)}/{required_votes}** votes now.", color=discord.Color.blue()))
            return

        # If no mode is provided, show the interactive view.
        current_mode = self.loop_states.get(ctx.guild.id, 'off')
        embed = discord.Embed(title="🔁 Loop Settings", description=f"Select a loop mode below. The current mode is **{current_mode}**.", color=discord.Color.purple())
        view = LoopControlsView(music_cog=self, ctx=ctx)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

    @commands.command(name='disconnect', aliases=['leave', 'dc'])
    async def disconnect(self, ctx):
        """Disconnects the bot from the voice channel via a vote."""
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=discord.Embed(description="❌ I'm already disconnected.", color=discord.Color.red()))

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=discord.Embed(description="❌ You must be in my voice channel to vote-disconnect.", color=discord.Color.red()))

        if ctx.author.guild_permissions.administrator:
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
            await self._handle_disconnect(vc)
            return await ctx.send(embed=discord.Embed(description="👋 Force-disconnected by an admin.", color=discord.Color.blue()))

        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        voters = self.disconnect_votes.setdefault(ctx.guild.id, set())

        if ctx.author.id in voters:
            return await ctx.send(embed=discord.Embed(description="ℹ️ You have already voted to disconnect.", color=discord.Color.yellow()))
        voters.add(ctx.author.id)

        if len(voters) >= required_votes:
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
            await self._handle_disconnect(vc)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! ({len(voters)}/{required_votes}). Disconnecting.", color=discord.Color.blue()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to disconnect added. **{len(voters)}/{required_votes}** votes now.", color=discord.Color.blue()))

    @commands.command(name='queue', aliases=['q'])
    async def queue(self, ctx):
        """Displays the current song queue with pagination."""
        vc = ctx.voice_client
        now_playing = vc.source if vc and (vc.is_playing() or vc.is_paused()) else None
        current_queue = self.queues.get(ctx.guild.id, [])

        if not current_queue and not now_playing:
            return await ctx.send(embed=discord.Embed(description="ℹ️ The queue is empty.", color=discord.Color.blue()))

        paginator = QueuePaginator(queue=current_queue, now_playing=now_playing)
        initial_embed = await paginator.get_page_embed()
        await ctx.send(embed=initial_embed, view=paginator)

    @commands.command(name='shuffle', aliases=['shuf'])
    async def shuffle(self, ctx):
        """Shuffles the current queue via a vote."""
        guild_queue = self.queues.get(ctx.guild.id)
        if not guild_queue or len(guild_queue) < 2:
            return await ctx.send(embed=discord.Embed(description="❌ The queue is too short to shuffle.", color=discord.Color.red()))

        vc = ctx.voice_client
        if not ctx.author.voice or not vc or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=discord.Embed(description="❌ You must be in my voice channel to vote-shuffle.", color=discord.Color.red()))

        if ctx.author.guild_permissions.administrator:
            random.shuffle(guild_queue)
            await self.save_queue_to_db(ctx.guild.id)
            return await ctx.send(embed=discord.Embed(description="🔀 Queue has been force-shuffled by an admin.", color=discord.Color.green()))

        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        voters = self.shuffle_votes.setdefault(ctx.guild.id, set())

        if ctx.author.id in voters:
            return await ctx.send(embed=discord.Embed(description="ℹ️ You have already voted to shuffle.", color=discord.Color.yellow()))
        voters.add(ctx.author.id)

        if len(voters) >= required_votes:
            random.shuffle(guild_queue)
            await self.save_queue_to_db(ctx.guild.id)
            self.shuffle_votes.pop(ctx.guild.id, None)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! ({len(voters)}/{required_votes}). The queue has been shuffled.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to shuffle added. **{len(voters)}/{required_votes}** votes now, need **{required_votes}** to pass.", color=discord.Color.blue()))

    @commands.command(name='remove', aliases=['rm'])
    async def remove(self, ctx, number: int):
        """Removes a specific song from the queue by its number, via a vote."""
        guild_queue = self.queues.get(ctx.guild.id)
        if not guild_queue:
            return await ctx.send(embed=discord.Embed(description="❌ The queue is empty.", color=discord.Color.red()))
        if not 1 <= number <= len(guild_queue):
            return await ctx.send(embed=discord.Embed(description=f"❌ Invalid number. Please provide a number between 1 and {len(guild_queue)}.", color=discord.Color.red()))

        vc = ctx.voice_client
        if not ctx.author.voice or not vc or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=discord.Embed(description="❌ You must be in my voice channel to vote.", color=discord.Color.red()))

        song_to_remove = guild_queue[number - 1]
        # Admin or the original requester can remove a song without a vote.
        if ctx.author.guild_permissions.administrator or ctx.author.id == song_to_remove.requester_id:
            removed_song = guild_queue.pop(number - 1)
            await self.save_queue_to_db(ctx.guild.id)
            self.remove_votes.pop(ctx.guild.id, None)
            return await ctx.send(embed=discord.Embed(description=f"✅ Force-removed **{removed_song.title}** from the queue.", color=discord.Color.green()))

        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        guild_votes = self.remove_votes.setdefault(ctx.guild.id, {})
        voters = guild_votes.setdefault(number, set())

        if ctx.author.id in voters:
            return await ctx.send(embed=discord.Embed(description=f"ℹ️ You have already voted to remove song #{number}.", color=discord.Color.yellow()))
        voters.add(ctx.author.id)

        if len(voters) >= required_votes:
            removed_song = guild_queue.pop(number - 1)
            await self.save_queue_to_db(ctx.guild.id)
            self.remove_votes.pop(ctx.guild.id, None)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! Removed **{removed_song.title}** from the queue.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to remove song #{number} added. **{len(voters)}/{required_votes}** votes now.", color=discord.Color.blue()))

    @commands.command(name='clear')
    async def clear(self, ctx):
        """Clears all songs from the queue, via a vote."""
        guild_queue = self.queues.get(ctx.guild.id)
        if not guild_queue:
            return await ctx.send(embed=discord.Embed(description="❌ The queue is already empty.", color=discord.Color.red()))

        vc = ctx.voice_client
        if not ctx.author.voice or not vc or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=discord.Embed(description="❌ You must be in my voice channel to vote-clear.", color=discord.Color.red()))

        if ctx.author.guild_permissions.administrator:
            guild_queue.clear()
            await self.save_queue_to_db(ctx.guild.id)
            return await ctx.send(embed=discord.Embed(description="✅ Queue force-cleared by an admin.", color=discord.Color.green()))

        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        voters = self.clear_votes.setdefault(ctx.guild.id, set())

        if ctx.author.id in voters:
            return await ctx.send(embed=discord.Embed(description="ℹ️ You have already voted to clear the queue.", color=discord.Color.yellow()))
        voters.add(ctx.author.id)

        if len(voters) >= required_votes:
            guild_queue.clear()
            await self.save_queue_to_db(ctx.guild.id)
            self.clear_votes.pop(ctx.guild.id, None)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! ({len(voters)}/{required_votes}). The queue has been cleared.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to clear added. **{len(voters)}/{required_votes}** votes now, need **{required_votes}** to pass.", color=discord.Color.blue()))

    # --- Reusable Command Logic with Voting ---

    async def _stop_logic(self, interaction_or_ctx):
        """Reusable logic for stopping the player, callable by commands and buttons."""
        context_data = await self._get_context(interaction_or_ctx)
        author = context_data["author"]
        guild = context_data["guild"]

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return await self._send_response(context_data,
                                             embed=discord.Embed(description="❌ I'm not connected to a voice channel.",
                                                                 color=discord.Color.red()), ephemeral=True)

        # The core stopping logic is now merged here for consistency.
        self.queues[guild.id] = []
        self.loop_states.pop(guild.id, None)
        await self.save_queue_to_db(guild.id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await self._cleanup_player_message(guild.id)
        await self.bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
        self.inactive_since[guild.id] = time.time()

        if author.guild_permissions.administrator:
            msg = "⏹️ Music force-stopped and queue cleared by an admin."
            return await self._send_response(context_data,
                                             embed=discord.Embed(description=msg, color=discord.Color.red()))

        # --- Voting Logic ---
        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        voters = self.stop_votes.setdefault(guild.id, set())

        if author.id in voters:
            return await self._send_response(context_data,
                                             embed=discord.Embed(description="ℹ️ You have already voted to stop.",
                                                                 color=discord.Color.yellow()), ephemeral=True)
        voters.add(author.id)

        if len(voters) >= required_votes:
            self.stop_votes.pop(guild.id, None)
            msg = f"🗳️ Vote passed! ({len(voters)}/{required_votes}). Music has been stopped and queue cleared."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.red()))
        else:
            msg = f"🗳️ Vote to stop added. **{len(voters)}/{required_votes}** votes now."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.blue()),
                                      ephemeral=True)

    @commands.command(name='stop')
    async def stop(self, ctx):
        """Stops the music, clears the queue, and disconnects, via a vote."""
        await self._stop_logic(ctx)

    async def _pause_logic(self, interaction_or_ctx):
        context_data = await self._get_context(interaction_or_ctx)
        author = context_data["author"]
        guild = context_data["guild"]

        vc = guild.voice_client
        if not vc or not vc.is_playing():
            return await self._send_response(context_data, embed=discord.Embed(description="❌ I'm not playing anything to pause.", color=discord.Color.red()), ephemeral=True)

        # The song requester or an admin can pause without a vote.
        if author.guild_permissions.administrator or author == vc.source.requester:
            vc.pause()
            self.inactive_since[guild.id] = time.time()
            return await self._send_response(context_data, embed=discord.Embed(description="⏸️ Force-paused the song.", color=discord.Color.orange()), ephemeral=True)

        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        voters = self.pause_votes.setdefault(guild.id, set())

        if author.id in voters:
            return await self._send_response(context_data, embed=discord.Embed(description="ℹ️ You have already voted to pause this song.", color=discord.Color.yellow()), ephemeral=True)

        voters.add(author.id)

        if len(voters) >= required_votes:
            vc.pause()
            self.inactive_since[guild.id] = time.time()
            self.pause_votes.pop(guild.id, None)
            msg = f"🗳️ Vote passed! ({len(voters)}/{required_votes}). Pausing song."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.orange()))
        else:
            msg = f"🗳️ Vote to pause added. **{len(voters)}/{required_votes}** votes now."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.blue()), ephemeral=True)

    @commands.command(name='pause')
    async def pause(self, ctx):
        """Pauses the current song, via a vote."""
        await self._pause_logic(ctx)

    async def _skip_logic(self, interaction_or_ctx):
        context_data = await self._get_context(interaction_or_ctx)
        author = context_data["author"]
        guild = context_data["guild"]

        vc = guild.voice_client
        if not vc or not vc.source:
            return await self._send_response(context_data, embed=discord.Embed(description="❌ Nothing to skip.", color=discord.Color.red()), ephemeral=True)

        # The song requester or an admin can skip without a vote.
        if author.guild_permissions.administrator or author == vc.source.requester:
            vc.stop() # Stopping the player triggers the 'after' callback, which plays the next song.
            return await self._send_response(context_data, embed=discord.Embed(description="⏭️ Force-skipped the song.", color=discord.Color.blue()), ephemeral=True)

        listeners = [member for member in vc.channel.members if not member.bot]
        required_votes = (len(listeners) // 2) + 1
        voters = self.skip_votes.setdefault(guild.id, set())

        if author.id in voters:
            return await self._send_response(context_data, embed=discord.Embed(description="ℹ️ You have already voted to skip this song.", color=discord.Color.yellow()), ephemeral=True)

        voters.add(author.id)

        if len(voters) >= required_votes:
            vc.stop()
            msg = f"🗳️ Vote passed! ({len(voters)}/{required_votes}). Skipping song."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.green()))
        else:
            msg = f"🗳️ Vote to skip added. **{len(voters)}/{required_votes}** votes now."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.blue()), ephemeral=True)

    @commands.command(name='skip', aliases=['s'])
    async def skip(self, ctx):
        """Skips the current song, via a vote."""
        await self._skip_logic(ctx)


async def setup(bot):
    """The entry point for loading the Music cog."""
    await bot.add_cog(Music(bot))