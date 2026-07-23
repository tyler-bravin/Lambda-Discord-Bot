# cogs/sources.py
"""
Audio source resolution for the music cog.

This module owns everything about *where a song comes from*: the yt-dlp
configuration and extraction helpers, the `Song` model, and one resolver per
supported platform. The cog itself only needs `resolve_query`, which turns a
user's input into either a list of queued songs or a single search query.

Supported inputs:
- YouTube links, playlists, and free-text searches (native streaming).
- SoundCloud tracks and sets (native streaming).
- Spotify tracks, albums, and playlists (metadata -> YouTube search).
- Apple Music songs and albums (metadata -> YouTube search).

Spotify and Apple Music are DRM-protected, so their audio can never be streamed
directly. For those we read the track metadata and play the matching YouTube
video instead, which is why their tracks are queued as unresolved placeholders.
"""
import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import Optional

import aiohttp
import yt_dlp

log = logging.getLogger(__name__)

# Directory for persistent data (database, cookies, cache). On Docker/Coolify this
# is set to a mounted volume (e.g. /data) so state survives redeploys.
DATA_DIR = os.getenv("DATA_DIR", ".")

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

# Suppress yt-dlp's default bug report message on console errors.
yt_dlp.utils.bug_reports_message = lambda **kwargs: ''


def _find_cookie_file() -> Optional[str]:
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

# Configuration for FFmpeg, the audio processing library.
ffmpeg_options = {
    # Arguments passed to FFmpeg before the input, useful for reconnection on stream interruptions.
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    # Core arguments; '-vn' specifies no video processing, which saves resources.
    'options': '-vn'
}


# --- yt-dlp helpers ---

def create_ytdl() -> yt_dlp.YoutubeDL:
    """Creates a fresh YoutubeDL instance."""
    return yt_dlp.YoutubeDL(ytdl_format_options)


# All extractions go through one shared YoutubeDL instance behind a lock. This
# serializes yt-dlp (whose instances are not thread-safe) and, crucially, keeps a
# single cookie jar alive: YouTube *rotates* cookies during use, and saving the
# jar after every extraction writes the rotated values back to cookies.txt so the
# file never goes stale and never needs to be re-exported by hand.
_ytdl_lock = threading.Lock()
_shared_ytdl: Optional[yt_dlp.YoutubeDL] = None


def _save_cookies(ydl: yt_dlp.YoutubeDL):
    """Persists rotated YouTube cookies back to the cookie file, if one is in use."""
    try:
        jar = ydl.cookiejar
        if getattr(jar, 'filename', None):
            jar.save()
    except Exception as e:
        log.debug("Could not save rotated cookies: %s", e)


def extract_info_blocking(query: str, download: bool = False) -> dict:
    """Thread-safe yt-dlp extraction. Blocking — run in an executor."""
    global _shared_ytdl
    with _ytdl_lock:
        if _shared_ytdl is None:
            _shared_ytdl = create_ytdl()
        data = _shared_ytdl.extract_info(query, download=download)
        _save_cookies(_shared_ytdl)
        return data


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


def upgrade_ytdlp_blocking() -> bool:
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


# --- Song model ---

class Song:
    """
    A data class representing a song in the queue.

    Standardizes song information whatever its origin. Songs are either
    *resolved* (they carry a playable stream URL) or *placeholders* (they carry
    only a page URL or a search query, resolved to a stream just before playing).
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
        # Set for unresolved placeholder songs (e.g. Spotify tracks): the YouTube
        # lookup for this query is deferred until just before playback.
        self.search_query = data.get('search_query')
        # When the stream URL was fetched; stream URLs expire after a few hours.
        self.fetched_at = time.time() if self.stream_url else 0.0

    @classmethod
    def from_search(cls, title: str, artist: str, requester, *,
                    duration: int = None, thumbnail: str = None) -> 'Song':
        """
        Creates an unresolved placeholder from plain title/artist metadata.

        Used for DRM-protected sources (Spotify, Apple Music) where we can only
        read the track details and must play the matching YouTube video instead.
        The search is deferred until just before the song plays, which makes
        adding large playlists instant.
        """
        data = {
            'title': f"{title} - {artist}" if artist else title,
            'uploader': artist or None,
            'duration': duration,
            'thumbnail': thumbnail,
            'search_query': f"{title} {artist}".strip(),
        }
        return cls(data, requester)

    @classmethod
    def from_spotify_track(cls, track: dict, requester, album_info: dict = None) -> 'Song':
        """Creates a placeholder Song from Spotify track metadata."""
        artists = ", ".join(a['name'] for a in track.get('artists', []) if a.get('name'))
        if not artists and album_info:
            artists = ", ".join(a['name'] for a in album_info.get('artists', []) if a.get('name'))
        images = (track.get('album') or {}).get('images') or (album_info or {}).get('images') or []
        duration_ms = track.get('duration_ms')
        return cls.from_search(
            track['name'], artists, requester,
            duration=(duration_ms // 1000) if duration_ms else None,
            thumbnail=images[0].get('url') if images else None,
        )

    @classmethod
    def from_itunes_track(cls, item: dict, requester) -> Optional['Song']:
        """Creates a placeholder Song from an iTunes/Apple Music lookup result."""
        name = item.get('trackName')
        if not name:
            return None
        duration_ms = item.get('trackTimeMillis')
        artwork = item.get('artworkUrl100')
        if artwork:
            # The API returns a 100px thumbnail; ask for a larger one.
            artwork = artwork.replace('100x100', '600x600')
        return cls.from_search(
            name, item.get('artistName') or '', requester,
            duration=(duration_ms // 1000) if duration_ms else None,
            thumbnail=artwork,
        )

    @classmethod
    def from_flat_entry(cls, entry: dict, requester) -> Optional['Song']:
        """
        Creates a placeholder Song from a flat playlist entry (YouTube or SoundCloud).

        The entry already carries the track's page URL, so only the stream needs
        resolving later.
        """
        video_id = entry.get('id')
        webpage_url = entry.get('url')
        if webpage_url and not webpage_url.startswith('http'):
            webpage_url = None  # 'url' was just a bare id; rebuild it below.
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
        Serializes the Song for database storage.

        Only non-expiring metadata is saved; the full 'data' blob and the
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


# --- Resolution result ---

class Resolution:
    """
    The outcome of turning a user's query into something playable.

    Exactly one of these is meaningful:
    - ``songs``: tracks to queue directly (playlists, albums, multi-track links).
    - ``query``: a single search term or URL still to be looked up.
    - ``error``: a user-facing message explaining why nothing could be resolved.
    """
    def __init__(self, *, songs=None, query=None, error=None, is_playlist=False, notice=None):
        self.songs = songs or []
        self.query = query
        self.error = error
        self.is_playlist = is_playlist
        # Optional progress message to show while a slow lookup runs.
        self.notice = notice


# --- Platform detection ---

def _netloc(query: str) -> str:
    try:
        return urllib.parse.urlparse(query).netloc.lower()
    except Exception:
        return ""


def identify(query: str) -> str:
    """Classifies a query into a source name: spotify, apple, soundcloud, youtube or search."""
    netloc = _netloc(query)
    if "spotify.com" in netloc:
        return "spotify"
    if "music.apple.com" in netloc:
        return "apple"
    if "soundcloud.com" in netloc:
        return "soundcloud"
    if any(host in netloc for host in ("youtube.com", "youtu.be")):
        return "youtube"
    return "search"


# --- Spotify ---

def _spotify_items_blocking(spotify, query: str) -> tuple:
    """Fetches every track of a Spotify playlist or album. Blocking — use an executor."""
    items = []
    is_album = "album" in query
    if is_album:
        results = spotify.album_tracks(query, limit=50)
    else:
        results = spotify.playlist_items(query, limit=100)
    items.extend(results['items'])
    while results.get('next'):
        results = spotify.next(results)
        items.extend(results['items'])
    album_info = spotify.album(query) if is_album else None
    return items, album_info, is_album


async def resolve_spotify(query: str, requester, spotify, loop) -> Resolution:
    """Resolves a Spotify track, album or playlist into placeholder songs."""
    if spotify is None:
        return Resolution(error="Spotify support isn't configured on this bot "
                                "(`SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` are missing).")
    try:
        if "track" in query:
            track = await loop.run_in_executor(None, lambda: spotify.track(query))
            return Resolution(query=Song.from_spotify_track(track, requester).search_query)

        if "playlist" not in query and "album" not in query:
            return Resolution(error="That Spotify link isn't a track, album or playlist.")

        items, album_info, is_album = await loop.run_in_executor(
            None, lambda: _spotify_items_blocking(spotify, query))
    except Exception:
        log.exception("Spotify lookup failed")
        return Resolution(error="Could not read that Spotify link. If it's a playlist, make sure "
                                "it's **public** — private and collaborative playlists can't be read.")

    songs = []
    for item in items:
        track = item if is_album else item.get('track')
        if track and track.get('name'):
            songs.append(Song.from_spotify_track(track, requester, album_info))
    if not songs:
        return Resolution(error="That Spotify link didn't contain any playable tracks.")
    return Resolution(songs=songs, is_playlist=True, notice="🎶 Found a Spotify link, getting info…")


# --- Apple Music ---

def parse_apple_url(query: str) -> tuple:
    """
    Extracts (kind, id) from an Apple Music URL.

    Returns ('track', id), ('album', id), ('playlist', None) or (None, None).
    Apple Music song links are albums with an ``?i=`` track parameter.
    """
    parsed = urllib.parse.urlparse(query)
    path = parsed.path
    if "/playlist/" in path:
        return "playlist", None
    track_id = urllib.parse.parse_qs(parsed.query).get("i", [None])[0]
    if track_id:
        return "track", track_id
    match = re.search(r"/(?:album|song)/[^/]+/(\d+)", path)
    if match:
        return ("track" if "/song/" in path else "album"), match.group(1)
    return None, None


async def resolve_apple(query: str, requester, loop) -> Resolution:
    """
    Resolves an Apple Music song or album into placeholder songs.

    Apple Music audio is DRM-protected and can never be streamed, so this reads
    the track metadata from the free iTunes Lookup API and plays the matching
    YouTube video instead. Playlists need the paid Apple Music API, so they are
    reported as unsupported.
    """
    kind, item_id = parse_apple_url(query)
    if kind == "playlist":
        return Resolution(error="Apple Music **playlists** aren't supported (they need Apple's paid "
                                "developer API). Songs and albums work fine.")
    if not kind or not item_id:
        return Resolution(error="That doesn't look like an Apple Music song or album link.")

    params = {"id": item_id}
    if kind == "album":
        params["entity"] = "song"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ITUNES_LOOKUP, params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"iTunes lookup returned {resp.status}")
                # The API responds as text/javascript, so don't enforce the type.
                payload = await resp.json(content_type=None)
    except Exception:
        log.exception("Apple Music lookup failed")
        return Resolution(error="Could not read that Apple Music link.")

    results = [r for r in payload.get("results", []) if r.get("wrapperType") == "track"]
    if not results:
        return Resolution(error="That Apple Music link didn't contain any tracks.")

    if kind == "track":
        song = Song.from_itunes_track(results[0], requester)
        return Resolution(query=song.search_query) if song else Resolution(
            error="Could not read that Apple Music track.")

    songs = [s for s in (Song.from_itunes_track(r, requester) for r in results) if s]
    return Resolution(songs=songs, is_playlist=True, notice="🍎 Found an Apple Music album, getting info…")


# --- SoundCloud & YouTube ---

async def resolve_flat_playlist(query: str, requester, loop, notice: str) -> Resolution:
    """Flat-extracts a YouTube/SoundCloud playlist into placeholder songs."""
    try:
        entries = await loop.run_in_executor(None, lambda: extract_flat_playlist_blocking(query))
    except Exception:
        log.exception("Flat playlist extraction failed for %s", query)
        return Resolution(error="Could not read that playlist. It may be private or unavailable.")
    songs = [s for s in (Song.from_flat_entry(e, requester) for e in entries if e) if s]
    if not songs:
        return Resolution(error="That playlist didn't contain any playable tracks.")
    return Resolution(songs=songs, is_playlist=True, notice=notice)


def is_youtube_playlist(query: str) -> bool:
    """
    True for a pure YouTube playlist URL (has 'list' but no 'v').

    A normal 'watch?v=…&list=…' link plays the single video, which also avoids
    expanding autogenerated radio/mix lists.
    """
    params = urllib.parse.parse_qs(urllib.parse.urlparse(query).query)
    return "list" in params and "v" not in params


def is_soundcloud_set(query: str) -> bool:
    """True for a SoundCloud set (playlist) URL."""
    return "/sets/" in urllib.parse.urlparse(query).path


# --- Dispatcher ---

async def resolve_query(query: str, requester, *, spotify=None, loop=None) -> Resolution:
    """
    Turns a user's input into playable songs.

    Returns a :class:`Resolution` holding either songs to queue, a single query
    still to search, or a user-facing error.
    """
    loop = loop or asyncio.get_running_loop()
    source = identify(query)

    if source == "spotify":
        return await resolve_spotify(query, requester, spotify, loop)
    if source == "apple":
        return await resolve_apple(query, requester, loop)
    if source == "soundcloud" and is_soundcloud_set(query):
        return await resolve_flat_playlist(query, requester, loop,
                                           "🎧 Found a SoundCloud set, getting info…")
    if source == "youtube" and is_youtube_playlist(query):
        return await resolve_flat_playlist(query, requester, loop,
                                           "🎶 Found a YouTube playlist, getting info…")
    # Everything else (single YouTube/SoundCloud links and free-text searches)
    # is handled natively by yt-dlp.
    return Resolution(query=query)
