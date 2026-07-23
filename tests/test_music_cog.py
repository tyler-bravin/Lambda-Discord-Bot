# test_music_cog.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import time
import discord

# Assuming your music cog is in cogs/Music.py
from cogs.Music import Music, YTDLSource, Song, PlayerControls, QueuePaginator
from cogs.sources import Resolution

# --- Test Data ---
full_song_data = {
    'webpage_url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'title': 'Never Gonna Give You Up',
    'uploader': 'Rick Astley',
    'thumbnail': 'http://example.com/thumb.jpg',
    'duration': 212,
    'url': 'http://example.com/stream.mp3',
    'requester': MagicMock()
}

playlist_entry_1 = {
    'title': 'Song 1', 'webpage_url': 'http://googleusercontent.com/youtube.com/1', 'uploader': 'Artist 1',
    'duration': 180, 'thumbnail': '...', 'url': 'http://example.com/stream1.mp3', 'requester': MagicMock()
}
playlist_entry_2 = {
    'title': 'Song 2', 'webpage_url': 'http://googleusercontent.com/youtube.com/2', 'uploader': 'Artist 2',
    'duration': 240, 'thumbnail': '...', 'url': 'http://example.com/stream2.mp3', 'requester': MagicMock()
}

# --- Fixtures ---

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.change_presence = AsyncMock()
    bot.loop = asyncio.new_event_loop()
    bot.loop.create_task = MagicMock()
    yield bot
    bot.loop.close()

@pytest.fixture
def music_cog(mock_bot):
    with patch('discord.ext.tasks.Loop.start'):
        cog = Music(bot=mock_bot)
        cog.save_queue_to_db = AsyncMock()
        # Stub the session-persistence DB writes so tests don't touch sqlite.
        cog.save_session = AsyncMock()
        cog.clear_session = AsyncMock()
        return cog

@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.defer = AsyncMock()
    # None marks a classic (prefix) invocation so hybrid command checks use the
    # non-interaction path.
    ctx.interaction = None
    ctx.guild.id = 12345
    ctx.author = MagicMock()
    ctx.author.voice.channel.connect = AsyncMock(return_value=AsyncMock())
    ctx.channel.send = AsyncMock()
    return ctx

@pytest.fixture
def mock_player():
    player = MagicMock(spec=YTDLSource)
    player.title = "Mock Song"
    player.url = "http://example.com/song"
    player.thumbnail = "http://example.com/thumb.jpg"
    player.duration = 180
    player.uploader = "Mock Uploader"
    player.requester = MagicMock()
    player.requester.display_name = "TestUser"
    player.requester.display_avatar.url = "http://example.com/avatar.jpg"
    return player

# --- Core Logic Tests ---

@pytest.mark.asyncio
@patch('cogs.Music.YTDLSource.from_data', new_callable=AsyncMock)
async def test_play_next_with_prefetched_data(mock_from_data, music_cog, mock_ctx, mock_player):
    song = Song(full_song_data, mock_ctx.author)
    music_cog.queues[mock_ctx.guild.id] = [song]
    mock_ctx.guild.voice_client = MagicMock()
    mock_ctx.guild.voice_client.is_connected.return_value = True
    mock_from_data.return_value = mock_player

    await music_cog.play_next(mock_ctx)

    mock_from_data.assert_awaited_once()
    mock_ctx.guild.voice_client.play.assert_called_once()
    music_cog.save_queue_to_db.assert_awaited()

@pytest.mark.asyncio
@patch('cogs.Music.YTDLSource.from_url', new_callable=AsyncMock)
async def test_play_next_with_database_loaded_song(mock_from_url, music_cog, mock_ctx, mock_player):
    db_song_data = {k: v for k, v in full_song_data.items() if k != 'url'}
    song = Song(db_song_data, mock_ctx.author)
    delattr(song, 'stream_url')
    delattr(song, 'data') # FIX: Fully simulate a DB load by removing .data as well

    music_cog.queues[mock_ctx.guild.id] = [song]
    mock_ctx.guild.voice_client = MagicMock()
    mock_ctx.guild.voice_client.is_connected.return_value = True
    mock_from_url.return_value = mock_player

    await music_cog.play_next(mock_ctx)

    mock_from_url.assert_awaited_once()
    mock_ctx.guild.voice_client.play.assert_called_once()

# --- Command Tests ---

@pytest.mark.asyncio
async def test_play_single_song_empty_queue(music_cog, mock_ctx):
    mock_ctx.voice_client = None
    song = Song(full_song_data, mock_ctx.author)
    message_mock = AsyncMock()
    mock_ctx.send.return_value = message_mock

    with patch.object(music_cog, '_search_and_create_song', new_callable=AsyncMock) as mock_search:
        mock_search.return_value = song
        with patch.object(music_cog, 'play_next', new_callable=AsyncMock) as mock_play_next:
            await music_cog.play.callback(music_cog, mock_ctx, query="some song")

            mock_search.assert_awaited_with("some song", mock_ctx.author)
            assert music_cog.queues[mock_ctx.guild.id] == [song]
            mock_play_next.assert_awaited_once()
            message_mock.delete.assert_awaited_once()

@pytest.mark.asyncio
async def test_play_song_while_already_playing(music_cog, mock_ctx):
    mock_ctx.voice_client = MagicMock()
    mock_ctx.voice_client.is_playing.return_value = True
    song = Song(full_song_data, mock_ctx.author)
    message_mock = AsyncMock()
    mock_ctx.send.return_value = message_mock

    with patch.object(music_cog, '_search_and_create_song', new_callable=AsyncMock) as mock_search:
        mock_search.return_value = song
        with patch.object(music_cog, 'play_next', new_callable=AsyncMock) as mock_play_next:
            await music_cog.play.callback(music_cog, mock_ctx, query="a new song")

            mock_search.assert_awaited_with("a new song", mock_ctx.author)
            assert len(music_cog.queues[mock_ctx.guild.id]) == 1
            mock_play_next.assert_not_awaited()
            sent_embed = message_mock.edit.call_args.kwargs['embed']
            assert "Added to Queue" in sent_embed.title

@pytest.mark.asyncio
async def test_play_playlist(music_cog, mock_bot, mock_ctx):
    mock_ctx.voice_client = None
    message_mock = AsyncMock()
    mock_ctx.send.return_value = message_mock

    song1 = Song(playlist_entry_1, mock_ctx.author)
    song2 = Song(playlist_entry_2, mock_ctx.author)

    with patch.object(music_cog, '_search_and_create_song', new_callable=AsyncMock) as mock_search:
        mock_search.side_effect = [song1, song2]
        with patch.object(music_cog, 'play_next', new_callable=AsyncMock) as mock_play_next:
            await music_cog.play.callback(music_cog, mock_ctx, query="spotify playlist link")

            # --- FIX: Adjust assertions ---
            # 1. Assert the "Processing..." message was sent
            mock_ctx.send.assert_awaited_once()

            # 2. Assert the first song was queued
            assert len(music_cog.queues[mock_ctx.guild.id]) == 1
            assert music_cog.queues[mock_ctx.guild.id][0].title == "Song 1"

            # 3. Assert playback was started
            mock_play_next.assert_awaited_once()

            # 4. Assert the background task was created
            mock_bot.loop.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_stop_command(music_cog, mock_ctx):
    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    mock_vc.is_playing.return_value = True
    mock_vc.is_paused.return_value = False
    mock_vc.disconnect = AsyncMock()

    mock_ctx.guild.voice_client = mock_vc

    mock_ctx.author.guild_permissions.administrator = True
    music_cog.queues[mock_ctx.guild.id] = [Song(full_song_data, mock_ctx.author)]

    await music_cog.stop.callback(music_cog, mock_ctx)

    assert not music_cog.queues.get(mock_ctx.guild.id)
    mock_vc.stop.assert_called_once()
    # Responses go through ctx.send so 'ephemeral' is handled correctly.
    mock_ctx.send.assert_awaited_once()

# --- Lazy Playlist Resolution Tests ---

spotify_track_data = {
    'name': 'Bohemian Rhapsody',
    'duration_ms': 354000,
    'artists': [{'name': 'Queen'}],
    'album': {'images': [{'url': 'http://example.com/cover.jpg'}]},
}


def test_song_from_spotify_track_is_unresolved_placeholder():
    requester = MagicMock()
    song = Song.from_spotify_track(spotify_track_data, requester)

    assert song.url is None
    assert song.stream_url is None
    assert song.search_query == 'Bohemian Rhapsody Queen'
    assert song.duration == 354
    assert song.thumbnail == 'http://example.com/cover.jpg'

    # Placeholders must survive a database round-trip still unresolved.
    restored = Song(song.to_dict(), requester)
    assert restored.url is None
    assert restored.search_query == 'Bohemian Rhapsody Queen'


@pytest.mark.asyncio
@patch('cogs.Music.YTDLSource.from_data', new_callable=AsyncMock)
async def test_play_next_resolves_placeholder(mock_from_data, music_cog, mock_ctx, mock_player):
    placeholder = Song.from_spotify_track(spotify_track_data, mock_ctx.author)
    music_cog.queues[mock_ctx.guild.id] = [placeholder]
    mock_ctx.guild.voice_client = MagicMock()
    mock_ctx.guild.voice_client.is_connected.return_value = True
    mock_from_data.return_value = mock_player

    resolved_song = Song(full_song_data, mock_ctx.author)
    with patch.object(music_cog, '_search_and_create_song', new_callable=AsyncMock) as mock_search:
        mock_search.return_value = resolved_song
        await music_cog.play_next(mock_ctx)

    # The placeholder was resolved via its search query and then played.
    mock_search.assert_awaited_once_with('Bohemian Rhapsody Queen', mock_ctx.author)
    assert placeholder.url == full_song_data['webpage_url']
    mock_from_data.assert_awaited_once()
    mock_ctx.guild.voice_client.play.assert_called_once()


@pytest.mark.asyncio
async def test_play_queues_resolved_playlist_instantly(music_cog, mock_ctx):
    """A resolved playlist is queued wholesale with no up-front YouTube searches."""
    mock_ctx.voice_client = None
    mock_ctx.send.return_value = AsyncMock()

    placeholders = [
        Song.from_spotify_track(spotify_track_data, mock_ctx.author),
        Song.from_spotify_track({'name': 'Song 2', 'artists': [{'name': 'Artist 2'}]}, mock_ctx.author),
    ]
    resolution = Resolution(songs=placeholders, is_playlist=True)

    with patch('cogs.Music.resolve_query', new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = resolution
        with patch.object(music_cog, '_search_and_create_song', new_callable=AsyncMock) as mock_search:
            with patch.object(music_cog, 'play_next', new_callable=AsyncMock) as mock_play_next:
                await music_cog.play.callback(
                    music_cog, mock_ctx, query="https://open.spotify.com/playlist/abc123")

                mock_search.assert_not_awaited()
                queued = music_cog.queues[mock_ctx.guild.id]
                assert len(queued) == 2
                assert all(song.url is None and song.search_query for song in queued)
                mock_play_next.assert_awaited_once()

    # The whole batch is persisted with a single database write.
    music_cog.save_queue_to_db.assert_awaited_once_with(mock_ctx.guild.id)


@pytest.mark.asyncio
async def test_play_reports_resolution_error(music_cog, mock_ctx):
    """A resolver error is surfaced to the user and nothing is queued."""
    mock_ctx.voice_client = None
    message_mock = AsyncMock()
    mock_ctx.send.return_value = message_mock

    with patch('cogs.Music.resolve_query', new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = Resolution(error="Apple Music playlists aren't supported.")
        with patch.object(music_cog, 'play_next', new_callable=AsyncMock) as mock_play_next:
            await music_cog.play.callback(
                music_cog, mock_ctx, query="https://music.apple.com/gb/playlist/x/pl.123")

    embed = message_mock.edit.call_args.kwargs['embed']
    assert "Apple Music playlists aren't supported." in embed.description
    assert not music_cog.queues.get(mock_ctx.guild.id)
    mock_play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_cache_roundtrip(music_cog, tmp_path):
    """A resolved search is cached and returned as a placeholder on the next hit."""
    import aiosqlite
    music_cog.db_path = str(tmp_path / "cache.db")
    async with aiosqlite.connect(music_cog.db_path) as db:
        await db.execute('CREATE TABLE search_cache '
                         '(query TEXT PRIMARY KEY, data TEXT NOT NULL, cached_at INTEGER NOT NULL)')
        await db.commit()

    requester = MagicMock()
    song = Song(full_song_data, requester)

    assert await music_cog._search_cache_get("Never Gonna Give You Up") is None
    await music_cog._search_cache_put("Never Gonna Give You Up", song)

    # Case-insensitive hit returns the cached metadata as an unresolved placeholder.
    cached = await music_cog._search_cache_get("never gonna give you up")
    assert cached is not None
    assert cached['webpage_url'] == full_song_data['webpage_url']
    revived = Song(cached, requester)
    assert revived.url == full_song_data['webpage_url']
    assert revived.stream_url is None


def test_song_from_flat_entry_builds_placeholder():
    requester = MagicMock()
    entry = {'id': 'abc123', 'title': 'A Video', 'duration': 100,
             'channel': 'Some Channel', 'thumbnails': [{'url': 'http://t/1.jpg'}]}
    song = Song.from_flat_entry(entry, requester)

    assert song.url == 'https://www.youtube.com/watch?v=abc123'
    assert song.stream_url is None
    assert song.uploader == 'Some Channel'


@pytest.mark.asyncio
async def test_seek_does_not_advance_queue(music_cog, mock_ctx, mock_player):
    """Seeking must replace the source without popping the next queued song."""
    song = Song(full_song_data, mock_ctx.author)
    music_cog.current_song[mock_ctx.guild.id] = song
    music_cog.queues[mock_ctx.guild.id] = [Song(playlist_entry_1, mock_ctx.author)]

    mock_vc = MagicMock()
    mock_vc.is_playing.return_value = True
    mock_vc.is_paused.return_value = False
    mock_vc.source = mock_player
    mock_player.volume = 0.5
    mock_player.url = full_song_data['webpage_url']
    mock_player.duration = 212
    mock_ctx.voice_client = mock_vc
    mock_ctx.guild.voice_client = mock_vc

    with patch('cogs.Music.YTDLSource.from_url', new_callable=AsyncMock) as mock_from_url:
        mock_from_url.return_value = MagicMock()
        await music_cog.seek.callback(music_cog, mock_ctx, position="1:00")

    # Seek requested 60s into the source.
    assert mock_from_url.await_args.kwargs['seek'] == 60
    # The player was stopped and restarted, but the upcoming song is untouched.
    mock_vc.stop.assert_called_once()
    mock_vc.play.assert_called_once()
    assert len(music_cog.queues[mock_ctx.guild.id]) == 1


@pytest.mark.asyncio
async def test_autoplay_toggle(music_cog, mock_ctx):
    assert not music_cog.autoplay_enabled.get(mock_ctx.guild.id)
    await music_cog.autoplay.callback(music_cog, mock_ctx)
    assert music_cog.autoplay_enabled[mock_ctx.guild.id] is True
    await music_cog.autoplay.callback(music_cog, mock_ctx)
    assert music_cog.autoplay_enabled[mock_ctx.guild.id] is False


# --- Auto-Resume Tests ---

@pytest.mark.asyncio
async def test_play_next_saves_session(music_cog, mock_ctx, mock_player):
    """Playing a song should record the voice/text channel for restart resume."""
    song = Song(full_song_data, mock_ctx.author)
    music_cog.queues[mock_ctx.guild.id] = [song]

    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    mock_vc.channel.id = 999
    mock_ctx.guild.voice_client = mock_vc
    mock_ctx.channel.id = 777

    with patch('cogs.Music.YTDLSource.from_data', new_callable=AsyncMock) as mock_from_data:
        mock_from_data.return_value = mock_player
        await music_cog.play_next(mock_ctx)

    music_cog.save_session.assert_awaited_once_with(mock_ctx.guild.id, 999, 777)


@pytest.mark.asyncio
async def test_play_next_clears_session_when_queue_empty(music_cog, mock_ctx):
    """When the queue drains, the saved session must be cleared."""
    music_cog.queues[mock_ctx.guild.id] = []
    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    mock_ctx.guild.voice_client = mock_vc

    await music_cog.play_next(mock_ctx)

    music_cog.clear_session.assert_awaited_once_with(mock_ctx.guild.id)


@pytest.mark.asyncio
async def test_skip_with_nothing_playing_does_not_crash(music_cog, mock_ctx):
    """
    Prefix commands must not pass 'ephemeral' to a raw channel send.

    Regression: Messageable.send() rejects ephemeral, so !skip raised a TypeError
    instead of replying.
    """
    mock_vc = MagicMock()
    mock_vc.source = None
    mock_ctx.guild.voice_client = mock_vc

    await music_cog.skip.callback(music_cog, mock_ctx)

    # Replied through ctx.send (which tolerates ephemeral), not channel.send.
    mock_ctx.send.assert_awaited_once()
    embed = mock_ctx.send.call_args.kwargs['embed']
    assert "Nothing to skip" in embed.description


@pytest.mark.asyncio
async def test_send_response_strips_ephemeral_without_a_context(music_cog):
    """The raw-channel fallback (e.g. resume contexts) must drop 'ephemeral'."""
    channel = AsyncMock()
    context_data = {"is_interaction": False, "ctx": None, "interaction": None,
                    "channel": channel}

    await music_cog._send_response(context_data, content="hi", ephemeral=True)

    assert "ephemeral" not in channel.send.call_args.kwargs


# --- Play Next / Move Tests ---

@pytest.mark.asyncio
async def test_playnext_inserts_at_front(music_cog, mock_ctx):
    existing = Song(playlist_entry_1, mock_ctx.author)
    music_cog.queues[mock_ctx.guild.id] = [existing]
    mock_ctx.voice_client = MagicMock()
    mock_ctx.voice_client.is_playing.return_value = True
    mock_ctx.send.return_value = AsyncMock()

    jumper = Song(full_song_data, mock_ctx.author)
    with patch.object(music_cog, '_collect_songs', new_callable=AsyncMock) as mock_collect:
        mock_collect.return_value = ([jumper], False, None)
        with patch.object(music_cog, 'play_next', new_callable=AsyncMock):
            await music_cog.playnext.callback(music_cog, mock_ctx, query="a song")

    queue = music_cog.queues[mock_ctx.guild.id]
    assert queue[0] is jumper and queue[1] is existing


@pytest.mark.asyncio
async def test_playnext_keeps_playlist_order_at_front(music_cog, mock_ctx):
    existing = Song(playlist_entry_1, mock_ctx.author)
    music_cog.queues[mock_ctx.guild.id] = [existing]
    mock_ctx.voice_client = MagicMock()
    mock_ctx.voice_client.is_playing.return_value = True
    mock_ctx.send.return_value = AsyncMock()

    first = Song.from_spotify_track({'name': 'A', 'artists': [{'name': 'X'}]}, mock_ctx.author)
    second = Song.from_spotify_track({'name': 'B', 'artists': [{'name': 'X'}]}, mock_ctx.author)
    with patch.object(music_cog, '_collect_songs', new_callable=AsyncMock) as mock_collect:
        mock_collect.return_value = ([first, second], True, None)
        with patch.object(music_cog, 'play_next', new_callable=AsyncMock):
            await music_cog.playnext.callback(music_cog, mock_ctx, query="a playlist")

    queue = music_cog.queues[mock_ctx.guild.id]
    assert [queue[0], queue[1], queue[2]] == [first, second, existing]


@pytest.mark.asyncio
async def test_move_reorders_queue_for_requester(music_cog, mock_ctx):
    mock_ctx.author.id = 4242
    mock_ctx.author.guild_permissions.administrator = False
    songs = []
    for title in ("one", "two", "three"):
        song = Song({**full_song_data, 'title': title}, mock_ctx.author)
        song.requester_id = 4242
        songs.append(song)
    music_cog.queues[mock_ctx.guild.id] = songs

    await music_cog.move.callback(music_cog, mock_ctx, position=3, destination=1)

    assert [s.title for s in music_cog.queues[mock_ctx.guild.id]] == ["three", "one", "two"]
    music_cog.save_queue_to_db.assert_awaited()


@pytest.mark.asyncio
async def test_move_rejects_other_peoples_songs(music_cog, mock_ctx):
    mock_ctx.author.id = 1
    mock_ctx.author.guild_permissions.administrator = False
    songs = [Song(full_song_data, mock_ctx.author) for _ in range(2)]
    for song in songs:
        song.requester_id = 999  # queued by someone else
    music_cog.queues[mock_ctx.guild.id] = list(songs)

    await music_cog.move.callback(music_cog, mock_ctx, position=1, destination=2)

    assert music_cog.queues[mock_ctx.guild.id] == songs  # untouched
    embed = mock_ctx.send.call_args.kwargs['embed']
    assert "admin or the person who queued" in embed.description


@pytest.mark.asyncio
async def test_move_rejects_out_of_range(music_cog, mock_ctx):
    music_cog.queues[mock_ctx.guild.id] = [Song(full_song_data, mock_ctx.author) for _ in range(2)]

    await music_cog.move.callback(music_cog, mock_ctx, position=1, destination=99)

    embed = mock_ctx.send.call_args.kwargs['embed']
    assert "Invalid position" in embed.description


# --- Web Player Tests ---

def _guild_with_vc(playing=True):
    guild = MagicMock()
    guild.id = 777
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    vc.is_paused.return_value = False
    guild.voice_client = vc
    return guild, vc


def test_web_snapshot(music_cog):
    guild, vc = _guild_with_vc()
    song = Song(full_song_data, MagicMock())
    music_cog.current_song[guild.id] = song
    music_cog.song_start_times[guild.id] = time.time()
    music_cog.queues[guild.id] = [Song(playlist_entry_1, MagicMock())]

    snap = music_cog.web_snapshot(guild)
    assert snap["now"]["title"] == full_song_data["title"]
    assert snap["playing"] is True
    assert snap["queue_length"] == 1 and len(snap["queue"]) == 1


def test_web_can_control(music_cog):
    guild, vc = _guild_with_vc()
    channel = MagicMock()
    vc.channel = channel

    admin = MagicMock()
    admin.guild_permissions.administrator = True
    assert music_cog.web_can_control(guild, admin) is True

    listener = MagicMock()
    listener.guild_permissions.administrator = False
    listener.voice.channel = channel
    assert music_cog.web_can_control(guild, listener) is True

    outsider = MagicMock()
    outsider.guild_permissions.administrator = False
    outsider.voice = None
    assert music_cog.web_can_control(guild, outsider) is False
    assert music_cog.web_can_control(guild, None) is False


@pytest.mark.asyncio
async def test_web_control_denied_for_outsider(music_cog):
    guild, vc = _guild_with_vc()
    vc.channel = MagicMock()
    outsider = MagicMock()
    outsider.guild_permissions.administrator = False
    outsider.voice = None

    result = await music_cog.web_control(guild, outsider, "pause")
    assert result["ok"] is False
    vc.pause.assert_not_called()


@pytest.mark.asyncio
async def test_web_control_pause_for_admin(music_cog):
    guild, vc = _guild_with_vc()
    admin = MagicMock()
    admin.guild_permissions.administrator = True

    result = await music_cog.web_control(guild, admin, "pause")
    assert result["ok"] is True
    vc.pause.assert_called_once()


# --- UI and Other Feature Tests ---

@pytest.mark.asyncio
async def test_lyrics_button_creation():
    mock_cog = MagicMock()
    mock_player = MagicMock(spec=YTDLSource)
    mock_player.title = "Bohemian Rhapsody"
    mock_player.uploader = "Queen"

    controls = PlayerControls(music_cog=mock_cog, player=mock_player)

    lyrics_button = next(
        (child for child in controls.children if isinstance(child, discord.ui.Button) and child.label == "Lyrics"), None
    )
    assert lyrics_button is not None
    assert lyrics_button.style == discord.ButtonStyle.link
    assert "genius.com" in lyrics_button.url
    assert "Bohemian+Rhapsody+Queen" in lyrics_button.url

def test_live_player_button_added_when_web_running(mock_player):
    controls = PlayerControls(MagicMock(), mock_player, player_url="https://x.dev/np/1")
    button = next((c for c in controls.children
                   if isinstance(c, discord.ui.Button) and c.label == "Live Player"), None)
    assert button is not None
    assert button.url == "https://x.dev/np/1"


def test_live_player_button_absent_without_web(mock_player):
    controls = PlayerControls(MagicMock(), mock_player)
    assert not any(getattr(c, "label", None) == "Live Player" for c in controls.children)


def test_player_url_none_when_web_disabled(music_cog):
    music_cog.bot.get_cog.return_value = None
    assert music_cog._player_url(123) is None


def test_player_url_built_from_web_base(music_cog):
    web = MagicMock()
    web._runner = object()
    web.base_url = "https://lambda.example.dev/"
    music_cog.bot.get_cog.return_value = web
    assert music_cog._player_url(123) == "https://lambda.example.dev/np/123"


@pytest.mark.asyncio
async def test_queue_command_with_songs(music_cog, mock_ctx, mock_player):
    mock_vc = MagicMock()
    mock_vc.is_playing.return_value = True
    mock_vc.source = mock_player
    mock_ctx.voice_client = mock_vc
    music_cog.queues[mock_ctx.guild.id] = [Song(full_song_data, mock_ctx.author)]

    await music_cog.queue.callback(music_cog, mock_ctx)

    mock_ctx.send.assert_awaited_once()
    sent_view = mock_ctx.send.call_args.kwargs.get('view')
    assert isinstance(sent_view, QueuePaginator)