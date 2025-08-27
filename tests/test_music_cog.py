# test_music_cog.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import discord

# Assuming your music cog is in cogs/Music.py
from cogs.Music import Music, YTDLSource, Song, PlayerControls, QueuePaginator

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
        return cog

@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.send = AsyncMock()
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
@patch('cogs.music.YTDLSource.from_data', new_callable=AsyncMock)
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
@patch('cogs.music.YTDLSource.from_url', new_callable=AsyncMock)
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
    mock_ctx.channel.send.assert_awaited_once()

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