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
import logging
import math
import subprocess
import sys
import threading
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

log = logging.getLogger(__name__)

# --- Bot Setup: yt-dlp and FFmpeg Configuration ---

# Directory for persistent data (database, cookies, cache). On Docker/Coolify this
# is set to a mounted volume (e.g. /data) so state survives redeploys.
DATA_DIR = os.getenv("DATA_DIR", ".")

# Suppress yt-dlp's default bug report message on console errors.
yt_dlp.utils.bug_reports_message = lambda **kwargs: ''


def _find_cookie_file() -> Union[str, None]:
    """Returns the first existing cookies file, or None if there isn't one."""
    candidates = [
        os.getenv("YTDLP_COOKIES"),
        os.path.join(DATA_DIR, "cookies.txt"),
        "cookies.txt",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


# Configuration for yt-dlp to optimize for audio-only streams.
ytdl_format_options = {
    'cachedir': os.path.join(DATA_DIR, '.yt-dlp-cache'),
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
    # Retry transient network/extraction failures instead of failing the request.
    'retries': 3,
    'fragment_retries': 10,
    'extractor_retries': 3,
    'socket_timeout': 15,
}

_cookie_file = _find_cookie_file()
if _cookie_file:
    ytdl_format_options['cookiefile'] = _cookie_file
    log.info("Using YouTube cookies from %s", _cookie_file)

# Alternative to a cookies file for non-Docker setups: read cookies straight from
# a browser profile on the same machine, e.g. "chrome" or "firefox:ProfileName".
# Inside a container there is no browser, so use the cookies file there instead.
_cookies_from_browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER")
if _cookies_from_browser:
    browser, _, profile = _cookies_from_browser.partition(':')
    ytdl_format_options['cookiesfrombrowser'] = (browser, profile or None, None, None)
    log.info("Reading YouTube cookies from browser: %s", _cookies_from_browser)

# Optional PO token provider (bgutil-ytdlp-pot-provider sidecar). This is what
# lets YouTube playback keep working headlessly without any cookies for normal
# videos — it answers YouTube's "confirm you're not a bot" attestation checks.
_pot_provider_url = os.getenv("POT_PROVIDER_URL")
if _pot_provider_url:
    ytdl_format_options['extractor_args'] = {
        'youtubepot-bgutilhttp': {'base_url': [_pot_provider_url]},
    }
    log.info("Using PO token provider at %s", _pot_provider_url)


def create_ytdl() -> yt_dlp.YoutubeDL:
    """Creates a fresh YoutubeDL instance."""
    return yt_dlp.YoutubeDL(ytdl_format_options)


# All extractions go through one shared YoutubeDL instance behind a lock. This
# serializes yt-dlp (whose instances are not thread-safe) and, crucially, keeps a
# single cookie jar alive: YouTube *rotates* cookies during use, and saving the
# jar after every extraction writes the rotated values back to cookies.txt so the
# file never goes stale and never needs to be re-exported by hand.
_ytdl_lock = threading.Lock()
_shared_ytdl: Union[yt_dlp.YoutubeDL, None] = None


def extract_info_blocking(query: str, download: bool = False) -> dict:
    """Thread-safe yt-dlp extraction. Blocking — run in an executor."""
    global _shared_ytdl
    with _ytdl_lock:
        if _shared_ytdl is None:
            _shared_ytdl = create_ytdl()
        data = _shared_ytdl.extract_info(query, download=download)
        _save_cookies(_shared_ytdl)
        return data


def _save_cookies(ydl: yt_dlp.YoutubeDL):
    """Persists rotated YouTube cookies back to the cookie file, if one is in use."""
    try:
        jar = ydl.cookiejar
        if getattr(jar, 'filename', None):
            jar.save()
    except Exception as e:
        log.debug("Could not save rotated cookies: %s", e)


def extract_flat_playlist_blocking(url: str) -> list:
    """
    Flat-extracts a playlist's entries (titles + URLs only, no per-video network
    calls). Blocking — run in an executor. Returns a list of entry dicts.
    """
    opts = dict(ytdl_format_options)
    opts['noplaylist'] = False
    # 'in_playlist' returns lightweight entries without resolving each video.
    opts['extract_flat'] = 'in_playlist'
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=False)
    return data.get('entries') or []


def _upgrade_ytdlp_blocking() -> bool:
    """
    Upgrades yt-dlp via pip. Blocking — run in an executor.

    Returns True if a new version was actually installed (as opposed to
    already being up to date or the upgrade failing).
    """
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir", "yt-dlp"],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        log.warning("yt-dlp upgrade failed: %s", result.stderr.strip()[-500:])
        return False
    return "Successfully installed" in result.stdout

# Configuration for FFmpeg, the audio processing library.
ffmpeg_options = {
    # Arguments passed to FFmpeg before the input, useful for reconnection on stream interruptions.
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    # Core arguments; '-vn' specifies no video processing, which saves resources.
    'options': '-vn'
}

# --- Data Structures ---

class ResumeContext:
    """
    A minimal stand-in for ``commands.Context`` used to drive playback when there
    is no real command invocation — specifically when resuming a queue on startup.

    ``play_next`` and ``on_song_end`` only ever touch ``.guild``, ``.channel`` and
    ``.send`` on the context, so this exposes exactly those.
    """
    def __init__(self, guild: discord.Guild, channel: discord.abc.Messageable):
        self.guild = guild
        self.channel = channel

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


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
        # Fresh yt-dlp results carry 'webpage_url' (the page) and 'url' (the direct,
        # expiring stream URL). Dicts restored from the database only have 'url',
        # which is the *page* URL (see to_dict), so it must not be treated as a stream.
        if 'webpage_url' in data:
            self.url = data.get('webpage_url')
            self.stream_url = data.get('url')
        else:
            self.url = data.get('url')
            self.stream_url = None
        self.title = data.get('title', 'Unknown Title')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requester = requester
        self.requester_id = requester.id
        # Set for unresolved placeholder songs (e.g. Spotify playlist tracks):
        # the YouTube lookup for this query is deferred until just before playback.
        self.search_query = data.get('search_query')
        # When the stream URL was fetched; stream URLs expire after a few hours.
        self.fetched_at = time.time() if self.stream_url else 0.0

    @classmethod
    def from_spotify_track(cls, track: dict, requester, album_info: dict = None) -> 'Song':
        """
        Creates an unresolved placeholder Song from Spotify track metadata.

        The YouTube search is deferred until just before the song plays (or the
        prefetcher warms it up), which makes adding large playlists instant.
        """
        artists = ", ".join(a['name'] for a in track.get('artists', []) if a.get('name'))
        if not artists and album_info:
            artists = ", ".join(a['name'] for a in album_info.get('artists', []) if a.get('name'))
        images = (track.get('album') or {}).get('images') or (album_info or {}).get('images') or []
        duration_ms = track.get('duration_ms')
        data = {
            'title': f"{track['name']} - {artists}" if artists else track['name'],
            'uploader': artists or None,
            'duration': (duration_ms // 1000) if duration_ms else None,
            'thumbnail': images[0].get('url') if images else None,
            'search_query': f"{track['name']} {artists}".strip(),
        }
        return cls(data, requester)

    @classmethod
    def from_youtube_entry(cls, entry: dict, requester) -> Union['Song', None]:
        """
        Creates a placeholder Song from a flat YouTube playlist entry.

        The entry already carries the video's page URL, so the stream is resolved
        lazily (like Spotify placeholders) just before playback.
        """
        video_id = entry.get('id')
        webpage_url = entry.get('url')
        if webpage_url and not webpage_url.startswith('http') and video_id:
            webpage_url = None  # 'url' was just the bare id; rebuild it below.
        if not webpage_url:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
        if not webpage_url:
            return None
        thumbnails = entry.get('thumbnails') or []
        data = {
            'webpage_url': webpage_url,
            'title': entry.get('title', 'Unknown Title'),
            'duration': entry.get('duration'),
            'uploader': entry.get('uploader') or entry.get('channel'),
            'thumbnail': thumbnails[-1].get('url') if thumbnails else None,
        }
        return cls(data, requester)

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
            'requester_id': self.requester_id,
            # Persisted so unresolved placeholders survive restarts.
            'search_query': self.search_query
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

    @staticmethod
    def _ffmpeg_options(seek: int = 0) -> dict:
        """FFmpeg options, optionally starting playback at a given offset (seconds)."""
        if seek and seek > 0:
            # -ss before the input seeks quickly by keyframe.
            return {**ffmpeg_options,
                    'before_options': f"{ffmpeg_options['before_options']} -ss {int(seek)}"}
        return ffmpeg_options

    @classmethod
    async def from_data(cls, data, *, volume=0.5, seek=0):
        """
        Creates a YTDLSource instance directly from pre-fetched yt-dlp data.
        This is the "fast path" for playback as it avoids blocking network calls.
        """
        filename = data.get('url')
        return cls(discord.FFmpegPCMAudio(filename, **cls._ffmpeg_options(seek)), data=data, volume=volume)

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False, requester=None, volume=0.5, seek=0):
        """
        Creates a YTDLSource by fetching info from a URL.
        This is the "slower path" or fallback, used for songs loaded from the
        database that need their stream URL re-fetched. It runs the blocking
        `extract_info` call in an executor to avoid stalling the bot's event loop.
        """
        loop = loop or asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: extract_info_blocking(url, download=not stream))

        if 'entries' in data:
            data = data['entries'][0]

        data['requester'] = requester
        filename = data['url'] if stream else create_ytdl().prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **cls._ffmpeg_options(seek)), data=data, volume=volume)


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

        vc = interaction.guild.voice_client
        guild_votes = self.cog.loop_votes.setdefault(interaction.guild.id, {})
        voters = guild_votes.setdefault(mode, set())
        status, votes, required_votes = self.cog._tally_vote(vc, voters, interaction.user.id)

        if status == self.cog.VOTE_ALREADY:
            await interaction.response.send_message(f"ℹ️ You have already voted to set loop to **{mode}**.", ephemeral=True)
            return

        # Check if the vote threshold has been met.
        if status == self.cog.VOTE_PASSED:
            self.cog.loop_states[interaction.guild.id] = mode
            self.cog.loop_votes.pop(interaction.guild.id, None)
            for item in self.children: item.disabled = True
            await self.message.edit(view=self)
            self.stop()
            await interaction.response.send_message(f"🗳️ Vote passed! Loop mode has been set to **{mode}**.")
        else:
            await interaction.response.send_message(
                f"🗳️ Your vote to set loop to **{mode}** was added. Now at **{votes}/{required_votes}** votes.",
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
    # Reuse a pre-fetched stream URL only while it is this fresh. YouTube stream
    # URLs expire after ~6 hours; refresh well before that to avoid mid-queue failures.
    STREAM_URL_TTL_SECONDS = 3600
    # How long a cached "search text -> video" mapping stays valid.
    SEARCH_CACHE_TTL_SECONDS = 7 * 24 * 3600

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
        self.current_song: dict[int, Song] = {}               # guild_id: currently playing Song
        self.autoplay_enabled: dict[int, bool] = {}           # guild_id: autoplay related tracks when queue empties
        self.seek_in_progress: set[int] = set()               # guild_ids being seeked (suppress queue advance)

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
        self.db_path = os.path.join(DATA_DIR, 'music_queue.db')
        self.sp = None

        # Set once the background updater installs a new yt-dlp; the bot restarts
        # to pick it up as soon as no guild is playing music.
        self.pending_ytdlp_restart = False

        load_dotenv()
        try:
            client_id = os.getenv("SPOTIPY_CLIENT_ID")
            client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise ValueError("SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET not set.")
            client_credentials_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
            self.sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)
            log.info("Spotipy initialized successfully.")
        except Exception as e:
            log.warning("Could not initialize Spotipy. Spotify links will not work. Error: %s", e)
            self.sp = None

        self.bot.loop.create_task(self.initialize_database())
        self.auto_disconnect.start()
        if os.getenv("YTDLP_AUTO_UPDATE", "1") not in ("0", "false", "False"):
            self.ytdlp_update.start()

    async def cog_load(self):
        """Re-registers the persistent player controls so the buttons on old
        'Now Playing' messages keep working after a restart."""
        self.bot.add_view(PlayerControls(self, None))

    def cog_unload(self):
        """Cancels background tasks when the cog is unloaded."""
        self.auto_disconnect.cancel()
        self.ytdlp_update.cancel()

    # --- Internal Helper Methods ---
    def _song_has_fresh_stream(self, song) -> bool:
        """Whether the song's pre-fetched stream URL is still safe to play directly."""
        return bool(
            getattr(song, 'stream_url', None)
            and getattr(song, 'data', None)
            and (time.time() - getattr(song, 'fetched_at', 0)) < self.STREAM_URL_TTL_SECONDS
        )

    async def _refresh_song_data(self, song: Song) -> bool:
        """
        Fetches fresh YouTube data for a song, updating it in place.

        Handles both unresolved placeholders (Spotify tracks that only carry a
        search query) and songs whose direct stream URL has expired. Returns
        False if nothing playable could be found.
        """
        query = song.url or song.search_query
        if not query:
            return False
        resolved = await self._search_and_create_song(query, song.requester)
        if not resolved:
            return False
        song.data = resolved.data
        song.url = resolved.url
        song.stream_url = resolved.stream_url
        song.fetched_at = resolved.fetched_at
        song.title = resolved.title or song.title
        song.thumbnail = resolved.thumbnail or song.thumbnail
        song.duration = resolved.duration or song.duration
        song.uploader = resolved.uploader or song.uploader
        return True

    async def _prefetch_next(self, guild_id: int):
        """
        Resolves the upcoming song while the current one plays, so the transition
        between songs is gapless instead of pausing for a YouTube lookup.
        """
        queue = self.queues.get(guild_id)
        if not queue:
            return
        song = queue[0]
        if self._song_has_fresh_stream(song):
            return
        try:
            if await self._refresh_song_data(song):
                await self.save_queue_to_db(guild_id)
        except Exception:
            log.debug("Prefetching the next song failed", exc_info=True)

    @staticmethod
    def _youtube_id(url: str) -> Union[str, None]:
        """Extracts the 11-character video id from a YouTube URL, or None."""
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        if "youtu.be" in parsed.netloc:
            return parsed.path.lstrip("/") or None
        if "youtube.com" in parsed.netloc:
            return urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
        return None

    async def _enqueue_autoplay(self, guild_id: int) -> bool:
        """
        Appends a related track to the queue using YouTube's mix (radio) list for
        the last played song. Skips anything already in the recent history so the
        radio doesn't loop on itself. Returns True if a track was added.
        """
        last = self.current_song.get(guild_id)
        video_id = self._youtube_id(last.url) if last else None
        if not video_id:
            return False

        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        try:
            loop = self.bot.loop or asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, lambda: extract_flat_playlist_blocking(mix_url))
        except Exception:
            log.debug("Autoplay mix fetch failed", exc_info=True)
            return False

        history = self.song_history.get(guild_id) or deque()
        recent_ids = {self._youtube_id(s.url) for s in history}
        recent_ids.add(video_id)

        for entry in entries:
            if not entry:
                continue
            if entry.get("id") in recent_ids:
                continue
            song = Song.from_youtube_entry(entry, self.bot.user)
            if song:
                self.queues.setdefault(guild_id, []).append(song)
                await self.save_queue_to_db(guild_id)
                return True
        return False

    async def _youtube_playlist_songs(self, url: str, requester: discord.Member) -> list:
        """Flat-extracts a YouTube playlist into a list of placeholder Songs."""
        loop = self.bot.loop or asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, lambda: extract_flat_playlist_blocking(url))
        songs = []
        for entry in entries:
            if not entry:
                continue
            song = Song.from_youtube_entry(entry, requester)
            if song:
                songs.append(song)
        return songs

    @staticmethod
    def _is_url(query: str) -> bool:
        """Whether a query is a URL rather than free-text search terms."""
        return query.startswith("http://") or query.startswith("https://")

    async def _search_cache_get(self, query: str) -> Union[dict, None]:
        """Returns cached video metadata for a search query, or None if absent/expired."""
        key = query.strip().lower()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT data, cached_at FROM search_cache WHERE query = ?", (key,)) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return None
                    data_json, cached_at = row
                    if time.time() - cached_at > self.SEARCH_CACHE_TTL_SECONDS:
                        await db.execute("DELETE FROM search_cache WHERE query = ?", (key,))
                        await db.commit()
                        return None
                    return json.loads(data_json)
        except Exception:
            log.debug("Search cache read failed", exc_info=True)
            return None

    async def _search_cache_put(self, query: str, song: Song):
        """Stores a resolved song's non-expiring metadata for a search query."""
        key = query.strip().lower()
        payload = {
            'webpage_url': song.url,
            'title': song.title,
            'duration': song.duration,
            'uploader': song.uploader,
            'thumbnail': song.thumbnail,
        }
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO search_cache (query, data, cached_at) VALUES (?, ?, ?)",
                    (key, json.dumps(payload), int(time.time())))
                await db.commit()
        except Exception:
            log.debug("Search cache write failed", exc_info=True)

    async def _search_and_create_song(self, query: str, requester: discord.Member) -> Union[Song, None]:
        """
        Searches for a query using yt-dlp and creates a Song object from the result.

        For free-text searches the resolved query->video mapping is cached, so a
        repeated request returns instantly as a placeholder (resolved to a fresh
        stream at play time) without hitting YouTube's search again.
        """
        is_text_search = not self._is_url(query)
        if is_text_search:
            cached = await self._search_cache_get(query)
            if cached and cached.get('webpage_url'):
                return Song(cached, requester)

        try:
            loop = self.bot.loop or asyncio.get_running_loop()
            # Run the blocking yt-dlp search in an executor to avoid stalling the bot.
            data = await loop.run_in_executor(None, lambda: extract_info_blocking(query, download=False))
            if not data: return None
            # For searches, yt-dlp returns a playlist; we take the first entry.
            if 'entries' in data:
                if not data['entries']: return None
                data = data['entries'][0]
            data['requester'] = requester
            song = Song(data, requester)
        except Exception:
            traceback.print_exc()
            return None

        if is_text_search and song.url:
            await self._search_cache_put(query, song)
        return song

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
        await self.clear_session(guild_id)
        await self._cleanup_player_message(guild_id)
        # Reset all state for the guild to ensure a fresh start next time.
        self.loop_states.pop(guild_id, None)
        self.song_start_times.pop(guild_id, None)
        self.current_song.pop(guild_id, None)
        self.autoplay_enabled.pop(guild_id, None)
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

    # --- Vote Tallying ---
    # Outcomes returned by _tally_vote.
    VOTE_ALREADY = "already"   # this user had already voted
    VOTE_PASSED = "passed"     # this vote met the majority threshold
    VOTE_PENDING = "pending"   # vote counted, threshold not yet reached

    @staticmethod
    def _required_votes(vc) -> int:
        """Majority of the non-bot listeners in the bot's voice channel."""
        listeners = [member for member in vc.channel.members if not member.bot]
        return (len(listeners) // 2) + 1

    def _tally_vote(self, vc, voters: set, user_id: int):
        """
        Records a single majority vote and reports the outcome.

        Centralizes the vote-counting arithmetic that every vote-based command
        shares. Returns ``(status, current_votes, required_votes)`` where status
        is one of VOTE_ALREADY, VOTE_PASSED or VOTE_PENDING. On VOTE_PASSED the
        caller is responsible for both performing the action and clearing the
        relevant vote set.
        """
        required = self._required_votes(vc)
        if user_id in voters:
            return self.VOTE_ALREADY, len(voters), required
        voters.add(user_id)
        if len(voters) >= required:
            return self.VOTE_PASSED, len(voters), required
        return self.VOTE_PENDING, len(voters), required

    # --- Database & State Persistence ---
    async def initialize_database(self):
        """Initializes the SQLite database and creates tables if they don't exist."""
        # The guild cache must be populated before load_queues_from_db, otherwise
        # every queue is skipped because bot.get_guild returns None.
        await self.bot.wait_until_ready()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('CREATE TABLE IF NOT EXISTS queues (guild_id INTEGER PRIMARY KEY, queue_data TEXT NOT NULL)')
            await db.execute('CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER PRIMARY KEY, volume INTEGER DEFAULT 50)')
            # Remembers which voice/text channel a guild was using so playback can
            # resume automatically after a restart or redeploy.
            await db.execute('CREATE TABLE IF NOT EXISTS guild_sessions '
                             '(guild_id INTEGER PRIMARY KEY, voice_channel_id INTEGER NOT NULL, text_channel_id INTEGER NOT NULL)')
            # Caches "search text -> resolved YouTube video metadata" so repeated
            # songs skip the YouTube search step.
            await db.execute('CREATE TABLE IF NOT EXISTS search_cache '
                             '(query TEXT PRIMARY KEY, data TEXT NOT NULL, cached_at INTEGER NOT NULL)')
            await db.commit()
        await self.load_queues_from_db()
        await self.load_volumes_from_db()
        await self.resume_sessions()

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
        log.info("Queues loaded from database.")

    async def load_volumes_from_db(self):
        """Loads all guild volume settings from the database into memory on startup."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT guild_id, volume FROM guild_settings") as cursor:
                async for row in cursor:
                    guild_id, volume = row
                    self.guild_volumes[guild_id] = volume
        log.info("Guild volumes loaded from database.")

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

    async def save_session(self, guild_id: int, voice_channel_id: int, text_channel_id: int):
        """Records the voice/text channel in use so playback can resume after a restart."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO guild_sessions (guild_id, voice_channel_id, text_channel_id) VALUES (?, ?, ?)",
                (guild_id, voice_channel_id, text_channel_id))
            await db.commit()

    async def clear_session(self, guild_id: int):
        """Forgets a guild's saved session so it won't try to resume."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM guild_sessions WHERE guild_id = ?", (guild_id,))
            await db.commit()

    async def resume_sessions(self):
        """
        Rejoins voice channels and resumes queued playback after a restart.

        Runs once on startup. A guild is resumed only if it still has a saved
        queue and at least one human is in the saved voice channel; otherwise the
        stale session is discarded. This makes restarts (including the automatic
        yt-dlp update restart) invisible to listeners.
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT guild_id, voice_channel_id, text_channel_id FROM guild_sessions") as cursor:
                sessions = await cursor.fetchall()

        for guild_id, voice_channel_id, text_channel_id in sessions:
            queue = self.queues.get(guild_id)
            guild = self.bot.get_guild(guild_id)
            voice_channel = guild.get_channel(voice_channel_id) if guild else None
            text_channel = guild.get_channel(text_channel_id) if guild else None

            # Discard the session if there's nothing to resume, the channels are
            # gone, or nobody is left listening.
            if (not queue or not isinstance(voice_channel, discord.VoiceChannel)
                    or text_channel is None
                    or not any(not m.bot for m in voice_channel.members)):
                await self.clear_session(guild_id)
                continue

            try:
                vc = await voice_channel.connect(self_deaf=True)
                self.voice_clients[guild_id] = vc
            except Exception:
                log.exception("Failed to rejoin voice for guild %s on resume", guild_id)
                continue

            try:
                await text_channel.send(embed=discord.Embed(
                    description="🔄 Resuming the queue after a restart.", color=discord.Color.blurple()))
            except discord.HTTPException:
                pass
            log.info("Resuming playback for guild %s", guild_id)
            await self.play_next(ResumeContext(guild, text_channel))

    # --- Core Playback Engine ---
    def _make_after(self, ctx, song):
        """
        Builds the ``after`` callback that advances the queue when a song ends.

        Runs in the voice thread, so it schedules onto the event loop. If a seek
        is in progress for the guild it does nothing — seeking stops the player to
        swap in a new source and must not advance the queue.
        """
        guild_id = ctx.guild.id

        def after_playback(error):
            if error:
                log.error("Playback error in guild %s: %s", guild_id, error)
            if guild_id in self.seek_in_progress:
                self.seek_in_progress.discard(guild_id)
                return
            asyncio.run_coroutine_threadsafe(self.on_song_end(ctx, song), self.bot.loop)

        return after_playback

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
            volume = guild_volume / 100.0

            # Placeholder songs (e.g. Spotify playlist tracks) have no URL yet and
            # are resolved here, at play time, unless the prefetcher already did it.
            if not getattr(next_song, 'url', None):
                resolved = False
                try:
                    resolved = await self._refresh_song_data(next_song)
                except Exception:
                    log.exception("Failed to resolve %r", next_song.title)
                if not resolved:
                    await ctx.send(embed=discord.Embed(
                        title="❌ Playback Error",
                        description=f"Could not find a playable video for `{next_song.title}`. Skipping.",
                        color=discord.Color.red()))
                    return await self.on_song_end(ctx, None)

            # Fast path is only safe while the pre-fetched stream URL is fresh;
            # YouTube stream URLs expire and then FFmpeg fails with a 403.
            stream_fresh = self._song_has_fresh_stream(next_song)

            player = None
            try:
                if stream_fresh:
                    player = await YTDLSource.from_data(next_song.data, volume=volume)
                else:
                    # Re-fetch a fresh stream URL from the page URL.
                    player = await YTDLSource.from_url(
                        next_song.url, loop=self.bot.loop, stream=True,
                        requester=next_song.requester, volume=volume
                    )
            except Exception:
                log.exception("Failed to create player for %r", next_song.title)
                # If the cached data was the problem, retry once with a fresh extraction.
                if stream_fresh and next_song.url:
                    try:
                        player = await YTDLSource.from_url(
                            next_song.url, loop=self.bot.loop, stream=True,
                            requester=next_song.requester, volume=volume
                        )
                    except Exception:
                        log.exception("Retry with fresh extraction also failed for %r", next_song.title)

            if player is None:
                await ctx.send(embed=discord.Embed(
                    title="❌ Playback Error",
                    description=f"Could not play `{next_song.title}`.\nIt may be unavailable or restricted.\nSkipping.",
                    color=discord.Color.red()))
                return await self.on_song_end(ctx, None)

            try:
                vc.play(player, after=self._make_after(ctx, next_song))
            except discord.ClientException:
                # This can happen if the bot disconnects while trying to play.
                return

            self.current_song[guild_id] = next_song
            self.song_start_times[guild_id] = time.time()

            # Remember where we're playing so the queue can resume after a restart.
            channel = getattr(ctx, 'channel', None)
            if channel is not None:
                await self.save_session(guild_id, vc.channel.id, channel.id)

            # Warm up the next song in the background for a gapless transition.
            if self.queues.get(guild_id):
                self.bot.loop.create_task(self._prefetch_next(guild_id))

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
            # Autoplay: when the queue empties, try to keep the music going with a
            # related track before declaring the queue finished.
            if self.autoplay_enabled.get(guild_id) and await self._enqueue_autoplay(guild_id):
                return await self.play_next(ctx)

            # If the queue is empty, reset presence and mark the bot as inactive for auto-disconnection.
            self.song_start_times.pop(guild_id, None)
            self.current_song.pop(guild_id, None)
            # Nothing left to resume, so forget the saved session.
            await self.clear_session(guild_id)
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
            await ctx.send(embed=discord.Embed(description="✅ Queue finished.", color=discord.Color.green()))
            self.inactive_since[guild_id] = time.time()

    # --- Event Listeners & Background Tasks ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Listener that triggers on voice state changes to detect if the bot is left alone."""
        # If the bot itself was disconnected (kicked, channel deleted), clean up its state
        # so the next !play starts from a consistent slate.
        if member.id == self.bot.user.id and before.channel is not None and after.channel is None:
            guild_id = member.guild.id
            await self._cleanup_player_message(guild_id)
            await self.clear_session(guild_id)
            self.voice_clients.pop(guild_id, None)
            self.song_start_times.pop(guild_id, None)
            self.inactive_since.pop(guild_id, None)
            return

        # Ignore bots' voice state changes.
        if not member.bot and after.channel is None:
            vc = member.guild.voice_client
            # If the bot is in a channel and there's only 1 member left (the bot itself)...
            if vc and len(vc.channel.members) == 1:
                await self._handle_disconnect(vc)

    def _any_voice_activity(self) -> bool:
        """Returns True if any guild is currently playing or has playback paused."""
        return any(vc.is_playing() or vc.is_paused() for vc in self.bot.voice_clients)

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

        # If a yt-dlp update is waiting and nothing is playing anywhere, exit cleanly.
        # The container's restart policy brings the bot back up on the new version.
        if self.pending_ytdlp_restart and not self._any_voice_activity():
            log.info("Restarting bot to apply the pending yt-dlp update.")
            await self.bot.close()

    @auto_disconnect.before_loop
    async def before_auto_disconnect(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def ytdlp_update(self):
        """
        Periodically upgrades yt-dlp.

        YouTube regularly changes its player in ways that break older yt-dlp
        releases, so staying current is the single most important thing for
        keeping playback working 24/7. If pip installs a new version, a restart
        is scheduled for the next moment the bot is idle.
        """
        try:
            updated = await asyncio.get_running_loop().run_in_executor(None, _upgrade_ytdlp_blocking)
        except Exception:
            log.exception("yt-dlp update check failed")
            return
        if updated:
            log.info("A new yt-dlp version was installed; will restart when idle to apply it.")
            self.pending_ytdlp_restart = True

    @ytdlp_update.before_loop
    async def before_ytdlp_update(self):
        await self.bot.wait_until_ready()

    # --- User-Facing Commands ---
    @commands.hybrid_command(name='play', aliases=['p'])
    async def play(self, ctx, *, query: str = None):
        """Plays a song from a URL or search query, or resumes playback."""
        if not ctx.author.voice:
            return await ctx.send(embed=discord.Embed(description="❌ You are not in a voice channel.", color=discord.Color.red()))

        # Acknowledge early: connecting to voice and searching can exceed the 3s
        # window a slash interaction allows before it expires. Harmless (shows
        # typing) for prefix invocations.
        await ctx.defer()

        vc = ctx.voice_client
        was_playing = vc and (vc.is_playing() or vc.is_paused())

        if not vc:
            vc = await ctx.author.voice.channel.connect(self_deaf=True)
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
        placeholder_songs = []
        is_playlist = False
        try:
            # A more robust check for spotify links.
            parsed_url = urllib.parse.urlparse(query)
            is_spotify_link = "spotify.com" in parsed_url.netloc
            # Treat a link as a YouTube playlist only when it's a pure playlist URL
            # (has 'list' but no 'v'); a normal 'watch?v=…&list=…' plays the single
            # video, which also avoids expanding autogenerated radio/mix lists.
            query_params = urllib.parse.parse_qs(parsed_url.query)
            is_youtube = any(h in parsed_url.netloc for h in ("youtube.com", "youtu.be"))
            is_youtube_playlist = is_youtube and "list" in query_params and "v" not in query_params
        except Exception:
            is_spotify_link = False
            is_youtube_playlist = False

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
                            # Queued as unresolved placeholders; the YouTube search
                            # happens lazily just before each track plays.
                            placeholder_songs.append(Song.from_spotify_track(track, ctx.author, album_info))
            except Exception:
                traceback.print_exc()
                return await processing_embed.edit(embed=discord.Embed(title="❌ Spotify Error", description="Could not process the Spotify link. It may be invalid or private.", color=discord.Color.red()))
        # YouTube playlist processing logic
        elif is_youtube_playlist:
            await processing_embed.edit(embed=discord.Embed(description="🎶 Found a YouTube playlist, getting info...", color=discord.Color.green()))
            try:
                placeholder_songs = await self._youtube_playlist_songs(query, ctx.author)
                is_playlist = True
            except Exception:
                traceback.print_exc()
                return await processing_embed.edit(embed=discord.Embed(title="❌ YouTube Error", description="Could not process the YouTube playlist. It may be private or unavailable.", color=discord.Color.red()))
        else:
            search_queries.append(query)

        if not search_queries and not placeholder_songs:
            return await processing_embed.edit(embed=discord.Embed(title="❌ Nothing Found", description="Could not find any songs to add from the provided link.", color=discord.Color.red()))

        queue = self.queues.setdefault(ctx.guild.id, [])

        # Playlist handling: all tracks are queued instantly as placeholders and a
        # single database write covers the whole batch. Tracks that turn out to
        # have no playable YouTube match are reported (and skipped) at play time.
        if is_playlist:
            queue.extend(placeholder_songs)
            await self.save_queue_to_db(ctx.guild.id)
            await processing_embed.edit(embed=discord.Embed(
                description=f"✅ Added **{len(placeholder_songs)}** tracks to the queue.",
                color=discord.Color.green()))
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

    @commands.hybrid_command(name='volume', aliases=['vol'])
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

    @commands.hybrid_command(name='nowplaying', aliases=['np', 'now'])
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

    @staticmethod
    def _parse_timestamp(text: str) -> Union[int, None]:
        """Parses 'SS', 'MM:SS' or 'HH:MM:SS' into seconds. Returns None if invalid."""
        if not text:
            return None
        try:
            parts = [int(p) for p in text.strip().split(":")]
        except ValueError:
            return None
        if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds

    @commands.hybrid_command(name='seek')
    async def seek(self, ctx, *, position: str = None):
        """Jumps to a position in the current song (e.g. `!seek 1:30`)."""
        vc = ctx.voice_client
        if not vc or not vc.source or not (vc.is_playing() or vc.is_paused()):
            return await ctx.send(embed=discord.Embed(description="❌ I'm not playing anything to seek.", color=discord.Color.red()))

        if position is None:
            return await ctx.send(embed=discord.Embed(description="ℹ️ Usage: `!seek <seconds | MM:SS | HH:MM:SS>`.", color=discord.Color.blue()))

        seconds = self._parse_timestamp(position)
        if seconds is None:
            return await ctx.send(embed=discord.Embed(description="❌ Invalid position. Try `90`, `1:30` or `1:02:03`.", color=discord.Color.red()))

        source = vc.source
        song = self.current_song.get(ctx.guild.id)
        seek_url = song.url if song else getattr(source, 'url', None)
        if source.duration and seconds >= source.duration:
            return await ctx.send(embed=discord.Embed(description=f"❌ That's past the end of the song ({source.duration}s).", color=discord.Color.red()))
        if not seek_url:
            return await ctx.send(embed=discord.Embed(description="❌ I can't seek this source.", color=discord.Color.red()))

        # Re-extracting the stream can exceed the slash 3s window.
        await ctx.defer()

        try:
            new_source = await YTDLSource.from_url(
                seek_url, loop=self.bot.loop, stream=True,
                requester=source.requester, volume=source.volume, seek=seconds)
        except Exception:
            log.exception("Seek failed to build source")
            return await ctx.send(embed=discord.Embed(description="❌ Could not seek — the stream may have expired.", color=discord.Color.red()))

        # Suppress the queue-advance that vc.stop() would otherwise trigger.
        self.seek_in_progress.add(ctx.guild.id)
        vc.stop()
        try:
            vc.play(new_source, after=self._make_after(ctx, song))
        except discord.ClientException:
            self.seek_in_progress.discard(ctx.guild.id)
            return await ctx.send(embed=discord.Embed(description="❌ Could not resume after seeking.", color=discord.Color.red()))

        self.current_song[ctx.guild.id] = song
        # Back-date the start time so the nowplaying progress bar stays accurate.
        self.song_start_times[ctx.guild.id] = time.time() - seconds
        await ctx.send(embed=discord.Embed(
            description=f"⏩ Seeked to **{time.strftime('%M:%S', time.gmtime(seconds))}**.",
            color=discord.Color.green()))

    @commands.hybrid_command(name='autoplay', aliases=['radio'])
    async def autoplay(self, ctx):
        """Toggles autoplay: when the queue empties, related tracks keep playing."""
        guild_id = ctx.guild.id
        enabled = not self.autoplay_enabled.get(guild_id, False)
        self.autoplay_enabled[guild_id] = enabled
        if enabled:
            await ctx.send(embed=discord.Embed(description="📻 Autoplay **enabled** — I'll keep the music going with related tracks.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description="⏹️ Autoplay **disabled**.", color=discord.Color.blue()))

    @commands.hybrid_command(name='loop')
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

            guild_votes = self.loop_votes.setdefault(ctx.guild.id, {})
            voters = guild_votes.setdefault(mode, set())
            status, votes, required_votes = self._tally_vote(vc, voters, ctx.author.id)

            if status == self.VOTE_ALREADY:
                return await ctx.send(embed=discord.Embed(description=f"ℹ️ You have already voted to set loop to **{mode}**.", color=discord.Color.yellow()))
            if status == self.VOTE_PASSED:
                self.loop_states[ctx.guild.id] = mode
                self.loop_votes.pop(ctx.guild.id, None)
                await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! Loop mode set to **{mode}**.", color=discord.Color.green()))
            else:
                await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to set loop to **{mode}** added. **{votes}/{required_votes}** votes now.", color=discord.Color.blue()))
            return

        # If no mode is provided, show the interactive view.
        current_mode = self.loop_states.get(ctx.guild.id, 'off')
        embed = discord.Embed(title="🔁 Loop Settings", description=f"Select a loop mode below. The current mode is **{current_mode}**.", color=discord.Color.purple())
        view = LoopControlsView(music_cog=self, ctx=ctx)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

    @commands.hybrid_command(name='disconnect', aliases=['leave', 'dc'])
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

        voters = self.disconnect_votes.setdefault(ctx.guild.id, set())
        status, votes, required_votes = self._tally_vote(vc, voters, ctx.author.id)

        if status == self.VOTE_ALREADY:
            return await ctx.send(embed=discord.Embed(description="ℹ️ You have already voted to disconnect.", color=discord.Color.yellow()))
        if status == self.VOTE_PASSED:
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
            await self._handle_disconnect(vc)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! ({votes}/{required_votes}). Disconnecting.", color=discord.Color.blue()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to disconnect added. **{votes}/{required_votes}** votes now.", color=discord.Color.blue()))

    @commands.hybrid_command(name='queue', aliases=['q'])
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

    @commands.hybrid_command(name='shuffle', aliases=['shuf'])
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

        voters = self.shuffle_votes.setdefault(ctx.guild.id, set())
        status, votes, required_votes = self._tally_vote(vc, voters, ctx.author.id)

        if status == self.VOTE_ALREADY:
            return await ctx.send(embed=discord.Embed(description="ℹ️ You have already voted to shuffle.", color=discord.Color.yellow()))
        if status == self.VOTE_PASSED:
            random.shuffle(guild_queue)
            await self.save_queue_to_db(ctx.guild.id)
            self.shuffle_votes.pop(ctx.guild.id, None)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! ({votes}/{required_votes}). The queue has been shuffled.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to shuffle added. **{votes}/{required_votes}** votes now, need **{required_votes}** to pass.", color=discord.Color.blue()))

    @commands.hybrid_command(name='remove', aliases=['rm'])
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

        guild_votes = self.remove_votes.setdefault(ctx.guild.id, {})
        voters = guild_votes.setdefault(number, set())
        status, votes, required_votes = self._tally_vote(vc, voters, ctx.author.id)

        if status == self.VOTE_ALREADY:
            return await ctx.send(embed=discord.Embed(description=f"ℹ️ You have already voted to remove song #{number}.", color=discord.Color.yellow()))
        if status == self.VOTE_PASSED:
            removed_song = guild_queue.pop(number - 1)
            await self.save_queue_to_db(ctx.guild.id)
            self.remove_votes.pop(ctx.guild.id, None)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! Removed **{removed_song.title}** from the queue.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to remove song #{number} added. **{votes}/{required_votes}** votes now.", color=discord.Color.blue()))

    @commands.hybrid_command(name='clear')
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

        voters = self.clear_votes.setdefault(ctx.guild.id, set())
        status, votes, required_votes = self._tally_vote(vc, voters, ctx.author.id)

        if status == self.VOTE_ALREADY:
            return await ctx.send(embed=discord.Embed(description="ℹ️ You have already voted to clear the queue.", color=discord.Color.yellow()))
        if status == self.VOTE_PASSED:
            guild_queue.clear()
            await self.save_queue_to_db(ctx.guild.id)
            self.clear_votes.pop(ctx.guild.id, None)
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote passed! ({votes}/{required_votes}). The queue has been cleared.", color=discord.Color.green()))
        else:
            await ctx.send(embed=discord.Embed(description=f"🗳️ Vote to clear added. **{votes}/{required_votes}** votes now, need **{required_votes}** to pass.", color=discord.Color.blue()))

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
        # Stop means stop: don't let autoplay resurrect the queue.
        self.autoplay_enabled.pop(guild.id, None)
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
        voters = self.stop_votes.setdefault(guild.id, set())
        status, votes, required_votes = self._tally_vote(vc, voters, author.id)

        if status == self.VOTE_ALREADY:
            return await self._send_response(context_data,
                                             embed=discord.Embed(description="ℹ️ You have already voted to stop.",
                                                                 color=discord.Color.yellow()), ephemeral=True)
        if status == self.VOTE_PASSED:
            self.stop_votes.pop(guild.id, None)
            msg = f"🗳️ Vote passed! ({votes}/{required_votes}). Music has been stopped and queue cleared."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.red()))
        else:
            msg = f"🗳️ Vote to stop added. **{votes}/{required_votes}** votes now."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.blue()),
                                      ephemeral=True)

    @commands.hybrid_command(name='stop')
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

        voters = self.pause_votes.setdefault(guild.id, set())
        status, votes, required_votes = self._tally_vote(vc, voters, author.id)

        if status == self.VOTE_ALREADY:
            return await self._send_response(context_data, embed=discord.Embed(description="ℹ️ You have already voted to pause this song.", color=discord.Color.yellow()), ephemeral=True)

        if status == self.VOTE_PASSED:
            vc.pause()
            self.inactive_since[guild.id] = time.time()
            self.pause_votes.pop(guild.id, None)
            msg = f"🗳️ Vote passed! ({votes}/{required_votes}). Pausing song."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.orange()))
        else:
            msg = f"🗳️ Vote to pause added. **{votes}/{required_votes}** votes now."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.blue()), ephemeral=True)

    @commands.hybrid_command(name='pause')
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

        voters = self.skip_votes.setdefault(guild.id, set())
        status, votes, required_votes = self._tally_vote(vc, voters, author.id)

        if status == self.VOTE_ALREADY:
            return await self._send_response(context_data, embed=discord.Embed(description="ℹ️ You have already voted to skip this song.", color=discord.Color.yellow()), ephemeral=True)

        if status == self.VOTE_PASSED:
            vc.stop()
            msg = f"🗳️ Vote passed! ({votes}/{required_votes}). Skipping song."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.green()))
        else:
            msg = f"🗳️ Vote to skip added. **{votes}/{required_votes}** votes now."
            await self._send_response(context_data, embed=discord.Embed(description=msg, color=discord.Color.blue()), ephemeral=True)

    @commands.hybrid_command(name='skip', aliases=['s'])
    async def skip(self, ctx):
        """Skips the current song, via a vote."""
        await self._skip_logic(ctx)

    # --- Web dashboard integration ---
    # These methods let the optional web "now playing" screen read state and,
    # when enabled, drive playback. Permission mirrors the bot: an admin, or a
    # user currently in the bot's voice channel, may control.
    def web_snapshot(self, guild) -> dict:
        """A JSON-serializable snapshot of the guild's player for the web UI."""
        gid = guild.id
        vc = guild.voice_client
        playing = bool(vc and vc.is_playing())
        paused = bool(vc and vc.is_paused())

        now = None
        song = self.current_song.get(gid)
        if song and (playing or paused):
            start = self.song_start_times.get(gid)
            elapsed = 0
            if start:
                elapsed = max(0.0, time.time() - start)
                if song.duration:
                    elapsed = min(elapsed, song.duration)
            now = {
                "title": song.title,
                "url": song.url,
                "thumbnail": song.thumbnail,
                "uploader": song.uploader,
                "duration": song.duration,
                "elapsed": round(elapsed),
                "requester": getattr(song.requester, "display_name", None),
            }

        queue = [{
            "title": s.title, "url": s.url, "thumbnail": s.thumbnail,
            "duration": s.duration, "uploader": s.uploader,
        } for s in self.queues.get(gid, [])[:25]]

        return {
            "connected": bool(vc and vc.is_connected()),
            "playing": playing,
            "paused": paused,
            "now": now,
            "queue": queue,
            "queue_length": len(self.queues.get(gid, [])),
            "volume": self.guild_volumes.get(gid, 50),
            "loop": self.loop_states.get(gid, "off"),
        }

    def web_can_control(self, guild, member) -> bool:
        """Whether a member may control playback from the web (admin or in-channel)."""
        vc = guild.voice_client
        if not vc or not vc.is_connected() or member is None:
            return False
        if member.guild_permissions.administrator:
            return True
        return bool(member.voice and member.voice.channel == vc.channel)

    async def web_control(self, guild, member, action: str, value=None) -> dict:
        """Applies a control action from the web UI, after checking permission."""
        if not self.web_can_control(guild, member):
            return {"ok": False, "error": "You must be an admin or in the bot's voice channel."}

        gid = guild.id
        vc = guild.voice_client
        if action == "pause":
            if vc.is_playing():
                vc.pause()
                self.inactive_since[gid] = time.time()
        elif action == "resume":
            if vc.is_paused():
                vc.resume()
                self.inactive_since.pop(gid, None)
                self.pause_votes.pop(gid, None)
        elif action == "skip":
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        elif action == "stop":
            await self._web_stop(guild)
        elif action == "previous":
            if not await self._web_previous(guild):
                return {"ok": False, "error": "There's no previous song."}
        elif action == "volume":
            try:
                v = max(0, min(200, int(value)))
            except (TypeError, ValueError):
                return {"ok": False, "error": "Invalid volume."}
            self.guild_volumes[gid] = v
            if vc.source:
                vc.source.volume = v / 100
            await self.save_volume_to_db(gid, v)
        else:
            return {"ok": False, "error": "Unknown action."}
        return {"ok": True}

    async def _web_stop(self, guild):
        """Stops playback and clears the queue (no vote, no chat response)."""
        gid = guild.id
        self.queues[gid] = []
        self.loop_states.pop(gid, None)
        self.autoplay_enabled.pop(gid, None)
        await self.save_queue_to_db(gid)
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await self._cleanup_player_message(gid)
        await self.bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=f"{self.bot.command_prefix}play"))
        self.inactive_since[gid] = time.time()

    async def _web_previous(self, guild) -> bool:
        """Requeues the previous song. Mirrors the ⏮️ player button."""
        gid = guild.id
        history = self.song_history.get(gid)
        if not history or len(history) < 2:
            return False
        current_song = history.pop()
        previous_song = history.pop()
        queue = self.queues.setdefault(gid, [])
        queue.insert(0, current_song)
        queue.insert(0, previous_song)
        vc = guild.voice_client
        if vc:
            vc.stop()
        return True


async def setup(bot):
    """The entry point for loading the Music cog."""
    await bot.add_cog(Music(bot))