import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.lyrics import (
    MAX_DURATION_DRIFT_SECONDS,
    clean_track_title,
    fetch_lyrics,
    pick_best_match,
    strip_timestamps,
)


def _candidate(name, duration, *, synced="[00:01.00] la", plain="la"):
    return {"trackName": name, "artistName": "Artist", "duration": duration,
            "syncedLyrics": synced, "plainLyrics": plain}


# --- Title cleaning ---

def test_clean_title_strips_brackets_and_parens():
    assert clean_track_title("Song (Official Video) [HD]") == "Song"


def test_clean_title_keeps_the_artist_separator():
    assert clean_track_title("Artist - Song (Audio)") == "Artist - Song"


def test_clean_title_handles_an_empty_title():
    assert clean_track_title(None) == ""


# --- Match selection ---

def test_best_match_prefers_the_closest_duration():
    results = [_candidate("far", 300), _candidate("close", 212)]
    assert pick_best_match(results, 210)["trackName"] == "close"


def test_best_match_ignores_entries_with_no_lyrics():
    """A closer duration doesn't win if the entry carries no lyrics at all."""
    results = [_candidate("empty", 210, synced=None, plain=None), _candidate("ok", 215)]
    assert pick_best_match(results, 210)["trackName"] == "ok"


def test_best_match_rejects_a_wildly_different_length():
    """An extended mix or an hour-long loop is a different recording."""
    results = [_candidate("extended", 210 + MAX_DURATION_DRIFT_SECONDS + 5)]
    assert pick_best_match(results, 210) is None


def test_best_match_accepts_a_small_drift():
    results = [_candidate("radio edit", 210 + MAX_DURATION_DRIFT_SECONDS - 1)]
    assert pick_best_match(results, 210) is not None


def test_best_match_takes_the_text_match_when_duration_is_unknown():
    results = [_candidate("whatever", 9999)]
    assert pick_best_match(results, None)["trackName"] == "whatever"


def test_best_match_on_nothing():
    assert pick_best_match([], 210) is None
    assert pick_best_match(None, 210) is None


# --- LRC parsing ---

def test_strip_timestamps_removes_lrc_prefixes():
    synced = "[00:12.34] first line\n[00:15.00] second line"
    assert strip_timestamps(synced) == "first line\nsecond line"


def test_strip_timestamps_drops_blank_and_metadata_only_lines():
    assert strip_timestamps("[00:01.00]\n[00:02.00] kept\n\n") == "kept"


def test_strip_timestamps_handles_repeated_stamps():
    assert strip_timestamps("[00:01.00][00:30.00] chorus") == "chorus"


def test_strip_timestamps_on_empty_input():
    assert strip_timestamps("") == ""
    assert strip_timestamps(None) == ""


# --- Fetching ---

def _session_returning(payload, status=200):
    """A stand-in aiohttp.ClientSession whose GET returns `payload`."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=response)
    get_cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


@pytest.mark.asyncio
async def test_fetch_lyrics_returns_the_best_match():
    session_cm, session = _session_returning([_candidate("Song", 212)])
    with patch("cogs.lyrics.aiohttp.ClientSession", return_value=session_cm):
        result = await fetch_lyrics("Song (Official Video)", 212)

    assert result["plain"] == "la"
    assert result["matched"] == "Artist — Song"
    # The cleaned title is what gets searched, not the raw YouTube one.
    assert session.get.call_args.kwargs["params"] == {"q": "Song"}


@pytest.mark.asyncio
async def test_fetch_lyrics_returns_none_on_an_http_error():
    session_cm, _ = _session_returning([], status=500)
    with patch("cogs.lyrics.aiohttp.ClientSession", return_value=session_cm):
        assert await fetch_lyrics("Song", 212) is None


@pytest.mark.asyncio
async def test_fetch_lyrics_swallows_network_errors():
    """A listener can't act on a traceback, so a failed lookup reads as 'no lyrics'."""
    with patch("cogs.lyrics.aiohttp.ClientSession", side_effect=OSError("offline")):
        assert await fetch_lyrics("Song", 212) is None


@pytest.mark.asyncio
async def test_fetch_lyrics_skips_the_request_for_an_unusable_title():
    with patch("cogs.lyrics.aiohttp.ClientSession") as session:
        assert await fetch_lyrics("(Official Video)", 212) is None
    session.assert_not_called()
