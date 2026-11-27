# cogs/lyrics.py
"""
Synced-lyrics lookups against LRCLIB.

Shared by the web player (which scrolls the synced lines alongside playback) and
the ``!lyrics`` command, so both resolve a track the same way.

LRCLIB is free and needs no key, but it matches on text, so a YouTube title like
"Artist - Song (Official Video) [HD]" has to be cleaned up first and the result
sanity-checked against the track's duration — otherwise a cover or a remix with
a similar name gets returned confidently.

This module is a plain helper, not a cog: it has no setup() and is skipped by the
extension loader.
"""
import logging
import re
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

LRCLIB_SEARCH = "https://lrclib.net/api/search"
LRCLIB_UA = "Lambda-Discord-Bot (https://github.com/tyler-bravin/Lambda-Discord-Bot)"

# How far a candidate's length may differ from ours before it is treated as a
# different recording. Generous enough for a radio edit, tight enough to reject
# an extended mix or an hour-long loop.
MAX_DURATION_DRIFT_SECONDS = 20


def clean_track_title(title: str) -> str:
    """Strips the bracketed noise YouTube titles carry so lookups match better."""
    cleaned = re.sub(r"\[[^\]]*\]", "", title or "")
    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    return cleaned.strip(" -")


def pick_best_match(results, duration: Optional[int]) -> Optional[dict]:
    """
    Chooses the candidate whose length is closest to ours, ignoring any with no
    lyrics at all. Returns None when nothing is close enough to trust.
    """
    best, best_drift = None, None
    for item in results or []:
        if not (item.get("syncedLyrics") or item.get("plainLyrics")):
            continue
        drift = abs((item.get("duration") or 0) - (duration or 0))
        if best is None or drift < best_drift:
            best, best_drift = item, drift

    if best is None:
        return None
    # With no duration of our own there's nothing to check against, so take the
    # best text match and let the caller present it as approximate.
    if duration and best_drift > MAX_DURATION_DRIFT_SECONDS:
        return None
    return best


async def fetch_lyrics(title: str, duration: Optional[int] = None) -> Optional[dict]:
    """
    Looks up lyrics for a track title.

    Returns ``{"synced", "plain", "matched"}`` for the best match, or None if the
    lookup failed or nothing matched closely enough. Never raises: callers treat
    a missing result as "no lyrics", which is also what a network error means to
    the listener.
    """
    query = clean_track_title(title)
    if not query:
        return None

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": LRCLIB_UA}) as session:
            async with session.get(LRCLIB_SEARCH, params={"q": query}) as response:
                results = await response.json() if response.status == 200 else []
    except Exception:
        log.debug("LRCLIB lookup failed for %r", query, exc_info=True)
        return None

    best = pick_best_match(results, duration)
    if best is None:
        return None
    return {
        "synced": best.get("syncedLyrics"),
        "plain": best.get("plainLyrics"),
        "matched": f"{best.get('artistName', '')} — {best.get('trackName', '')}".strip(" —"),
    }


def strip_timestamps(synced: str) -> str:
    """Turns an LRC body into plain text, dropping the ``[mm:ss.xx]`` prefixes."""
    lines = []
    for line in (synced or "").splitlines():
        text = re.sub(r"^\s*(\[\d+:\d+(?:\.\d+)?\]\s*)+", "", line).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)
