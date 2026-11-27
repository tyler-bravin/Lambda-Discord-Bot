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
# Only these two have audio behind them. The catalogue documents its 523 recording
# sessions and 401 unsurfaced songs in detail — stem counts, engineers, studios —
# but serves no file for any of them, so asking for a random one can only fail.
NO_AUDIO_CATEGORIES = ("recording_session", "unsurfaced")
PLAYABLE_CATEGORIES = ("released", "unreleased")
RANDOM_DEFAULT_CATEGORY = "unreleased"

# The API caps page_size at 100.
MAX_PAGE_SIZE = 100
SEARCH_RESULTS = 25
# A random pick can land on an entry with no file; sample more rather than give up.
SHUFFLE_DEFAULT = 20
SHUFFLE_MAX = 50
# Request budget for one shuffle. Tied to the size of the ask so a small one stays
# cheap, with slack for the no-file entries that `any` includes, and a hard ceiling
# so no single command can hammer the API.
SAMPLE_BUDGET_PER_TRACK = 3
SAMPLE_BUDGET_SLACK = 10
MAX_SAMPLE_REQUESTS = 120
# Sampling is one request per track, so they run in waves rather than one by one.
SAMPLE_CONCURRENCY = 8


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

def _open_session() -> aiohttp.ClientSession:
    """A session configured for the API, to be used as an async context manager."""
    return aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS))


async def _get_json(session, path: str, params: dict = None) -> Optional[dict]:
    """
    One GET against an already-open session, or None.

    Never raises: a catalogue lookup that fails is reported to the listener as
    "nothing found", which is also what a network error amounts to for them.
    """
    try:
        async with session.get(_endpoint(path), params=params or {}) as response:
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


async def _get(path: str, params: dict = None) -> Optional[dict]:
    """GETs a JSON endpoint on a session of its own, or returns None."""
    try:
        async with _open_session() as session:
            return await _get_json(session, path, params)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        log.debug("Juice WRLD API request to %s failed", path, exc_info=True)
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


async def count_entries(category: str = None) -> int:
    """How many entries a category holds, or 0 if the catalogue can't be reached."""
    params = {"page_size": 1}
    if category:
        params["category"] = category
    payload = await _get("songs/", params)
    return (payload or {}).get("count") or 0


async def random_entries(count: int, category: str = RANDOM_DEFAULT_CATEGORY) -> list:
    """
    Picks up to ``count`` distinct playable entries at random.

    The API has no random ordering and no lightweight listing — a full page of 100
    entries is about 700KB, because every entry carries its own lyrics — so pulling
    the category down to shuffle it locally is out. Instead this reads the entry
    count once and then fetches single-entry pages at random offsets, a wave at a
    time. That is one small request per track and spreads the picks evenly over the
    whole category, where sampling from one page would land them all in one era.

    Offsets are drawn without replacement, so no two requests can return the same
    track, and a wave that lands on entries with no audio file is topped up rather
    than counted.
    """
    count = max(1, min(int(count), SHUFFLE_MAX))
    total = await count_entries(category)
    if not total:
        return []

    budget = min(total, MAX_SAMPLE_REQUESTS,
                 count * SAMPLE_BUDGET_PER_TRACK + SAMPLE_BUDGET_SLACK)
    offsets = random.sample(range(1, total + 1), budget)
    params = {"page_size": 1}
    if category:
        params["category"] = category

    def first_result(payload):
        return ((payload or {}).get("results") or [None])[0]

    picked, seen = [], set()
    try:
        async with _open_session() as session:
            while offsets and len(picked) < count:
                wave = [offsets.pop() for _ in range(min(count - len(picked), len(offsets)))]
                for index in range(0, len(wave), SAMPLE_CONCURRENCY):
                    chunk = wave[index:index + SAMPLE_CONCURRENCY]
                    payloads = await asyncio.gather(*(
                        _get_json(session, "songs/", {**params, "page": page})
                        for page in chunk))
                    for entry in map(first_result, payloads):
                        # Distinct offsets mean distinct tracks unless the API's own
                        # ordering shifts mid-shuffle, which the id check absorbs.
                        if is_playable(entry) and entry["id"] not in seen:
                            seen.add(entry["id"])
                            picked.append(entry)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        log.debug("Sampling the Juice WRLD catalogue failed", exc_info=True)

    # The offsets were drawn in random order, but shuffling keeps that true of the
    # queue even if the wave logic ever changes.
    random.shuffle(picked)
    return picked[:count]


async def random_entry(category: str = RANDOM_DEFAULT_CATEGORY) -> Optional[dict]:
    """Picks a single random playable entry, optionally within one category."""
    entries = await random_entries(1, category)
    return entries[0] if entries else None


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
