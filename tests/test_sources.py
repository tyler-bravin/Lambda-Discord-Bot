import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.sources import (
    Song,
    identify,
    is_soundcloud_set,
    is_youtube_playlist,
    parse_apple_url,
    resolve_apple,
    resolve_query,
    resolve_spotify,
)


# --- Platform detection ---

@pytest.mark.parametrize("query,expected", [
    ("https://open.spotify.com/playlist/abc", "spotify"),
    ("https://music.apple.com/gb/album/x/123", "apple"),
    ("https://soundcloud.com/artist/track", "soundcloud"),
    ("https://www.youtube.com/watch?v=abc", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("never gonna give you up", "search"),
])
def test_identify(query, expected):
    assert identify(query) == expected


def test_youtube_playlist_detection():
    # A pure playlist URL expands; a video-within-a-playlist plays just the video.
    assert is_youtube_playlist("https://www.youtube.com/playlist?list=PL123") is True
    assert is_youtube_playlist("https://www.youtube.com/watch?v=abc&list=RD123") is False


def test_soundcloud_set_detection():
    assert is_soundcloud_set("https://soundcloud.com/artist/sets/my-mix") is True
    assert is_soundcloud_set("https://soundcloud.com/artist/a-track") is False


@pytest.mark.parametrize("url,kind,item_id", [
    ("https://music.apple.com/gb/album/some-album/1234?i=5678", "track", "5678"),
    ("https://music.apple.com/gb/album/some-album/1234", "album", "1234"),
    ("https://music.apple.com/gb/song/some-song/9999", "track", "9999"),
    ("https://music.apple.com/gb/playlist/chill/pl.abc123", "playlist", None),
    ("https://music.apple.com/gb/artist/someone/42", None, None),
])
def test_parse_apple_url(url, kind, item_id):
    assert parse_apple_url(url) == (kind, item_id)


# --- Apple Music ---

@pytest.mark.asyncio
async def test_resolve_apple_playlist_is_unsupported():
    result = await resolve_apple("https://music.apple.com/gb/playlist/x/pl.123", MagicMock(), None)
    assert result.songs == []
    assert "playlists" in result.error.lower()


@pytest.mark.asyncio
async def test_resolve_apple_album_builds_placeholders():
    payload = {"results": [
        {"wrapperType": "collection", "collectionName": "An Album"},
        {"wrapperType": "track", "trackName": "First", "artistName": "Band",
         "trackTimeMillis": 210000, "artworkUrl100": "http://a/100x100.jpg"},
        {"wrapperType": "track", "trackName": "Second", "artistName": "Band"},
    ]}
    requester = MagicMock()

    with patch("cogs.sources.aiohttp.ClientSession") as mock_session_cls:
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value=payload)
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolve_apple(
            "https://music.apple.com/gb/album/an-album/1234", requester, None)

    assert result.is_playlist is True
    assert len(result.songs) == 2
    first = result.songs[0]
    # Apple Music is DRM'd, so tracks are placeholders resolved via YouTube search.
    assert first.url is None and first.stream_url is None
    assert first.search_query == "First Band"
    assert first.duration == 210
    # The 100px artwork is upgraded to something usable.
    assert first.thumbnail == "http://a/600x600.jpg"


# --- Spotify ---

@pytest.mark.asyncio
async def test_resolve_spotify_without_credentials_explains_itself():
    """Without credentials the link must fail loudly, not fall through to YouTube."""
    result = await resolve_spotify("https://open.spotify.com/playlist/x", MagicMock(), None, None)
    assert result.songs == [] and result.query is None
    assert "isn't configured" in result.error


@pytest.mark.asyncio
async def test_resolve_query_routes_soundcloud_set_to_flat_extraction():
    requester = MagicMock()
    entries = [
        {"id": "1", "url": "https://soundcloud.com/a/one", "title": "One"},
        {"id": "2", "url": "https://soundcloud.com/a/two", "title": "Two"},
    ]
    with patch("cogs.sources.extract_flat_playlist_blocking", return_value=entries):
        result = await resolve_query("https://soundcloud.com/a/sets/mix", requester)

    assert result.is_playlist is True
    assert [s.url for s in result.songs] == [
        "https://soundcloud.com/a/one", "https://soundcloud.com/a/two"]
    # SoundCloud streams natively, so these keep their real page URLs.
    assert all(s.stream_url is None for s in result.songs)


@pytest.mark.asyncio
async def test_resolve_query_passes_plain_search_through():
    result = await resolve_query("never gonna give you up", MagicMock())
    assert result.query == "never gonna give you up"
    assert result.songs == []
    assert result.is_playlist is False


@pytest.mark.asyncio
async def test_resolve_query_passes_single_track_url_through():
    """A single SoundCloud/YouTube link goes straight to yt-dlp."""
    result = await resolve_query("https://soundcloud.com/artist/a-track", MagicMock())
    assert result.query == "https://soundcloud.com/artist/a-track"
    assert result.is_playlist is False


def test_song_from_itunes_track_skips_entries_without_a_name():
    assert Song.from_itunes_track({"artistName": "Band"}, MagicMock()) is None
