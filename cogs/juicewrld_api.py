# cogs/juicewrld_api.py
"""
Client for the Juice WRLD API (https://juicewrldapi.com).

A fan-run catalogue of roughly 2,800 Juice WRLD songs — released, unreleased,
unsurfaced and recording sessions — with metadata, lyrics, cover art and, for
most entries, a directly streamable audio file.

Unlike every other source in this bot, these tracks need no yt-dlp at all: the
API serves plain MP3s over HTTP with range support, so FFmpeg can open the URL
directly. The URLs are also permanent rather than signed and expiring, which is
why the songs are built with ``permanent_stream`` set (see ``Song``): the queue
never has to re-resolve them, not even after a restart.

Entries in the ``unsurfaced`` category have no file — the song is documented but
has never leaked — so they carry an empty ``path`` and are filtered out here.

This module is a plain helper, not a cog: it has no setup() and is skipped by
the extension loader.
"""
import asyncio
import logging
import os
import random
import urllib.parse
from typing import Optional

import aiohttp

from cogs.sources import Resolution, Song

log = logging.getLogger(__name__)

# Overridable so a self-hoster can point at a mirror if the domain moves.
DEFAULT_API_BASE = "https://juicewrldapi.com"

USER_AGENT = "Lambda-Discord-Bot"
REQUEST_TIMEOUT_SECONDS = 20

# The API's own categories, in the order a plain search should prefer them: the
# officially released cut of a song is what someone asking for it by name means,
# and a recording session is the rawest, least listenable version.
CATEGORIES = ("released", "unreleased", "recording_session", "unsurfaced")
PLAYABLE_CATEGORIES = ("released", "unreleased", "recording_session")
RANDOM_DEFAULT_CATEGORY = "unreleased"

# The API caps page_size at 100.
MAX_PAGE_SIZE = 100
SEARCH_RESULTS = 25
# A random pick can land on an entry with no file; try again rather than give up.
RANDOM_ATTEMPTS = 5


def api_base() -> str:
    """The API root, without a trailing slash."""
    return os.getenv("JUICEWRLD_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _endpoint(path: str) -> str:
    return f"{api_base()}/juicewrld/{path.lstrip('/')}"


def stream_url(path: str) -> str:
    """The direct audio URL for a catalogue file path."""
    return f"{_endpoint('files/download/')}?{urllib.parse.urlencode({'path': path})}"


def cover_url(path: str) -> str:
    """The cover art URL for a catalogue file path."""
    return f"{_endpoint('files/cover-art/')}?{urllib.parse.urlencode({'path': path})}"


def parse_length(text: str) -> Optional[int]:
    """
    Turns the API's ``"m:ss"`` or ``"h:mm:ss"`` length into seconds.

    Returns None for the blank lengths that unsurfaced and session entries carry,
    which is what the rest of the bot already uses for "duration unknown".
    """
    parts = (text or "").strip().split(":")
    if not parts or not all(part.strip().isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds or None


def is_playable(entry: dict) -> bool:
    """Whether this catalogue entry has an audio file behind it."""
    return bool((entry or {}).get("path"))


def era_name(entry: dict) -> str:
    """The entry's era label, or an empty string when it has none."""
    return ((entry or {}).get("era") or {}).get("name") or ""


def describe(entry: dict) -> str:
    """A one-line ``Name — category · era · length`` summary for listings."""
    bits = [(entry.get("category") or "").replace("_", " ") or "unknown"]
    era = era_name(entry)
    if era:
        bits.append(era)
    length = (entry.get("length") or "").strip()
    if length:
        bits.append(length)
    return f"{entry.get('name') or 'Untitled'} — {' · '.join(bits)}"


def song_data(entry: dict) -> Optional[dict]:
    """
    Builds the ``Song`` data dict for a catalogue entry, or None if unplayable.

    ``webpage_url`` is deliberately the audio URL too: it is permanent and public,
    so it is both a sensible link to show in chat and a URL that still resolves if
    anything ever does fall back to yt-dlp's generic extractor.
    """
    if not is_playable(entry):
        return None
    audio = stream_url(entry["path"])
    artists = (entry.get("credited_artists") or "").strip()
    return {
        "webpage_url": audio,
        "url": audio,
        "title": entry.get("name") or "Untitled",
        "uploader": artists or "Juice WRLD",
        "duration": parse_length(entry.get("length")),
        "thumbnail": cover_url(entry["path"]),
        # Tells the queue this stream never expires (see Song.permanent_stream).
        "permanent_stream": True,
    }


def build_song(entry: dict, requester) -> Optional[Song]:
    """Turns a catalogue entry into a queueable Song, or None if unplayable."""
    data = song_data(entry)
    if not data:
        return None
    # The player reads the requester back off this dict (YTDLSource.__init__), and
    # these songs are played straight from it without ever going through yt-dlp.
    data['requester'] = requester
    return Song(data, requester)


# --- Match ranking ---

def _name_score(entry: dict, query: str) -> int:
    """
    How well an entry's title matches the query, highest first.

    The API searches lyrics and credits as well as titles, so a query like
    "righteous" comes back with songs that merely mention the word. Ranking on
    the title first keeps the obvious answer at the top.
    """
    query = (query or "").strip().lower()
    if not query:
        return 0
    names = [(entry.get("name") or "").lower()]
    names += [str(title).lower() for title in (entry.get("track_titles") or [])]
    if any(name == query for name in names):
        return 4
    if any(name.startswith(query) for name in names):
        return 3
    if any(query in name for name in names):
        return 2
    return 1


def _category_rank(entry: dict) -> int:
    """Position in CATEGORIES, used only to break a tie on title match."""
    try:
        return CATEGORIES.index(entry.get("category"))
    except ValueError:
        return len(CATEGORIES)


def rank_matches(entries, query: str) -> list:
    """Playable entries, best title match first."""
    playable = [entry for entry in (entries or []) if is_playable(entry)]
    return sorted(playable, key=lambda entry: (-_name_score(entry, query),
                                               _category_rank(entry),
                                               entry.get("name") or ""))


# --- HTTP ---

async def _get(path: str, params: dict = None) -> Optional[dict]:
    """
    GETs a JSON endpoint, or returns None.

    Never raises: a catalogue lookup that fails is reported to the listener as
    "nothing found", which is also what a network error amounts to for them.
    """
    url = _endpoint(path)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT},
                                         timeout=timeout) as session:
            async with session.get(url, params=params or {}) as response:
                if response.status != 200:
                    log.debug("Juice WRLD API %s returned %s", path, response.status)
                    return None
                return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        log.debug("Juice WRLD API request to %s failed", path, exc_info=True)
        return None
    except Exception:
        log.exception("Unexpected failure calling the Juice WRLD API (%s)", path)
        return None


async def search(query: str, *, category: str = None,
                 limit: int = SEARCH_RESULTS) -> list:
    """Searches the catalogue, returning playable entries best-match first."""
    if not (query or "").strip():
        return []
    params = {"search": query, "page_size": min(max(limit, 1), MAX_PAGE_SIZE)}
    if category:
        params["category"] = category
    payload = await _get("songs/", params)
    if not payload:
        return []
    return rank_matches(payload.get("results"), query)[:limit]


async def fetch_song(song_id: int) -> Optional[dict]:
    """Looks up one catalogue entry by its id."""
    payload = await _get(f"songs/{int(song_id)}/")
    return payload if payload and payload.get("id") else None


async def stats() -> Optional[dict]:
    """Catalogue totals, including the per-category counts."""
    return await _get("stats/")


async def random_entry(category: str = RANDOM_DEFAULT_CATEGORY) -> Optional[dict]:
    """
    Picks a random playable entry, optionally within one category.

    Rather than page through thousands of songs, this reads the result count and
    jumps straight to a random single-song page.
    """
    params = {"page_size": 1}
    if category:
        params["category"] = category
    first = await _get("songs/", params)
    total = (first or {}).get("count") or 0
    if not total:
        return None

    for _ in range(RANDOM_ATTEMPTS):
        page = random.randint(1, total)
        payload = await _get("songs/", {**params, "page": page})
        results = (payload or {}).get("results") or []
        if results and is_playable(results[0]):
            return results[0]
    return None


# --- Link handling ---

def is_api_url(query: str) -> bool:
    """True for a link into the Juice WRLD API or its web player."""
    try:
        netloc = urllib.parse.urlparse(query).netloc.lower()
    except Exception:
        return False
    host = netloc.split("@")[-1].split(":")[0]
    base_host = urllib.parse.urlparse(api_base()).netloc.lower().split(":")[0]
    if not base_host or not host:
        return False
    # Also matches the project's web player on player.juicewrldapi.com.
    return host == base_host or host.endswith(f".{base_host}")


def parse_api_url(query: str):
    """
    Pulls what is playable out of a Juice WRLD API link.

    Returns ``("path", <file path>)`` for a download or cover-art link,
    ``("id", <song id>)`` for a song link, or ``(None, None)``.
    """
    try:
        parsed = urllib.parse.urlparse(query)
    except Exception:
        return None, None
    params = urllib.parse.parse_qs(parsed.query)
    path = (params.get("path") or [None])[0]
    if path:
        return "path", path
    # Both /juicewrld/songs/123/ and the player's /song/123 style links.
    for segment in reversed([part for part in parsed.path.split("/") if part]):
        if segment.isdigit():
            return "id", int(segment)
    song_id = (params.get("song") or params.get("id") or [None])[0]
    if song_id and str(song_id).isdigit():
        return "id", int(song_id)
    return None, None


async def resolve_link(query: str, requester) -> Resolution:
    """Turns a Juice WRLD API link into a queueable song."""
    kind, value = parse_api_url(query)

    if kind == "path":
        # A download link already carries everything needed to play the file. The
        # catalogue is searched anyway so the queue shows the real title, with the
        # filename as the fallback when the entry can't be found.
        name = os.path.basename(value).rsplit(".", 1)[0]
        entry = next((found for found in await search(name)
                      if found.get("path") == value), None)
        return Resolution(songs=[build_song(entry or {"path": value, "name": name},
                                            requester)])

    if kind == "id":
        entry = await fetch_song(value)
        if entry is None:
            return Resolution(error=f"No Juice WRLD catalogue entry with the id `{value}`.")
        song = build_song(entry, requester)
        if song is None:
            return Resolution(error=f"**{entry.get('name')}** is {entry.get('category')} — "
                                    "there is no audio file for it in the catalogue.")
        return Resolution(songs=[song])

    return Resolution(error="That Juice WRLD API link doesn't point at a song.")
