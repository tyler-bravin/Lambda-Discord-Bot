import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.Playlists import Playlists
from cogs.sources import Song

GUILD_ID = 12345

song_data = {
    'webpage_url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'title': 'Never Gonna Give You Up',
    'uploader': 'Rick Astley',
    'thumbnail': 'http://example.com/thumb.jpg',
    'duration': 212,
    'url': 'http://example.com/stream.mp3',
}


@pytest.fixture
def music(tmp_path):
    """A stand-in Music cog backed by a real database file."""
    cog = MagicMock()
    cog.db_path = str(tmp_path / "music_queue.db")
    cog.queues = {}
    cog.current_song = {}
    cog.voice_clients = {}
    cog.save_queue_to_db = AsyncMock()
    cog.play_next = AsyncMock()
    return cog


@pytest.fixture
def cog(music):
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.get_cog.return_value = music
    # __init__ schedules the table creation; capture and close that coroutine so
    # the tests can set the table up themselves via _ready().
    scheduled = []
    bot.loop.create_task = MagicMock(side_effect=scheduled.append)
    playlists = Playlists(bot)
    for coroutine in scheduled:
        coroutine.close()
    return playlists


@pytest.fixture
def ctx():
    context = MagicMock()
    context.send = AsyncMock()
    context.defer = AsyncMock()
    context.interaction = None
    context.guild.id = GUILD_ID
    context.author.id = 777
    context.author.guild_permissions.administrator = False
    context.voice_client = None
    context.author.voice.channel.connect = AsyncMock(return_value=MagicMock())
    return context


async def _ready(cog):
    """Creates the table, as the startup task does in production."""
    cog.bot.wait_until_ready = AsyncMock()
    await cog._initialize_database()


def _song(title="Never Gonna Give You Up"):
    return Song({**song_data, 'title': title}, MagicMock(id=777))


def _description(ctx):
    return ctx.send.await_args.kwargs['embed'].description


# --- Saving ---

@pytest.mark.asyncio
async def test_save_stores_the_current_song_and_queue(cog, music, ctx):
    await _ready(cog)
    music.current_song[GUILD_ID] = _song("playing")
    music.queues[GUILD_ID] = [_song("queued one"), _song("queued two")]

    await cog.save_playlist.callback(cog, ctx, name="Chill")

    stored = json.loads((await cog._fetch(GUILD_ID, "Chill"))[0])
    # The playing song comes first, so loading reproduces what you were hearing.
    assert [s['title'] for s in stored] == ["playing", "queued one", "queued two"]


@pytest.mark.asyncio
async def test_save_refuses_an_empty_queue(cog, ctx):
    await _ready(cog)

    await cog.save_playlist.callback(cog, ctx, name="Empty")

    assert await cog._fetch(GUILD_ID, "Empty") is None
    assert "nothing playing or queued" in _description(ctx)


@pytest.mark.asyncio
async def test_save_rejects_an_unusable_name(cog, music, ctx):
    await _ready(cog)
    music.queues[GUILD_ID] = [_song()]

    await cog.save_playlist.callback(cog, ctx, name="   ")

    assert not await cog._list(GUILD_ID)
    assert "1-50 characters" in _description(ctx)


@pytest.mark.asyncio
async def test_save_overwrites_the_same_name_case_insensitively(cog, music, ctx):
    await _ready(cog)
    music.queues[GUILD_ID] = [_song("first")]
    await cog.save_playlist.callback(cog, ctx, name="Chill")

    music.queues[GUILD_ID] = [_song("second")]
    await cog.save_playlist.callback(cog, ctx, name="chill")

    assert len(await cog._list(GUILD_ID)) == 1
    stored = json.loads((await cog._fetch(GUILD_ID, "CHILL"))[0])
    assert [s['title'] for s in stored] == ["second"]
    assert "was replaced" in _description(ctx)


@pytest.mark.asyncio
async def test_save_enforces_the_per_guild_limit(cog, music, ctx):
    await _ready(cog)
    music.queues[GUILD_ID] = [_song()]
    for i in range(Playlists.MAX_PER_GUILD):
        await cog._store(GUILD_ID, f"list {i}", [_song().to_dict()], 777)

    await cog.save_playlist.callback(cog, ctx, name="one too many")

    assert await cog._fetch(GUILD_ID, "one too many") is None
    assert "already has 25 playlists" in _description(ctx)


@pytest.mark.asyncio
async def test_save_overwrite_is_allowed_at_the_limit(cog, music, ctx):
    """Replacing an existing playlist doesn't add one, so it must not be blocked."""
    await _ready(cog)
    music.queues[GUILD_ID] = [_song("new")]
    for i in range(Playlists.MAX_PER_GUILD):
        await cog._store(GUILD_ID, f"list {i}", [_song().to_dict()], 777)

    await cog.save_playlist.callback(cog, ctx, name="list 0")

    stored = json.loads((await cog._fetch(GUILD_ID, "list 0"))[0])
    assert [s['title'] for s in stored] == ["new"]


@pytest.mark.asyncio
async def test_save_trims_an_oversized_queue(cog, music, ctx):
    await _ready(cog)
    music.queues[GUILD_ID] = [_song(f"t{i}") for i in range(Playlists.MAX_SONGS + 10)]

    await cog.save_playlist.callback(cog, ctx, name="Huge")

    stored = json.loads((await cog._fetch(GUILD_ID, "Huge"))[0])
    assert len(stored) == Playlists.MAX_SONGS
    assert "first 500 tracks" in _description(ctx)


@pytest.mark.asyncio
async def test_playlists_are_scoped_to_one_guild(cog, music, ctx):
    await _ready(cog)
    music.queues[GUILD_ID] = [_song()]
    await cog.save_playlist.callback(cog, ctx, name="Chill")

    assert await cog._fetch(99999, "Chill") is None


# --- Loading ---

@pytest.mark.asyncio
async def test_load_appends_to_the_queue_and_starts_playback(cog, music, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song("a").to_dict(), _song("b").to_dict()], 777)
    music.queues[GUILD_ID] = [_song("already queued")]

    await cog.load_playlist.callback(cog, ctx, name="Chill")

    assert [s.title for s in music.queues[GUILD_ID]] == ["already queued", "a", "b"]
    music.save_queue_to_db.assert_awaited()
    music.play_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_does_not_restart_playback_already_in_progress(cog, music, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song("a").to_dict()], 777)
    vc = MagicMock()
    vc.is_playing.return_value = True
    vc.is_paused.return_value = False
    ctx.voice_client = vc

    await cog.load_playlist.callback(cog, ctx, name="Chill")

    music.play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_loaded_tracks_belong_to_whoever_loaded_them(cog, music, ctx):
    """Skip and remove votes key off the requester, so it must be the loader."""
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song("a").to_dict()], 999)

    await cog.load_playlist.callback(cog, ctx, name="Chill")

    assert music.queues[GUILD_ID][0].requester_id == ctx.author.id


@pytest.mark.asyncio
async def test_load_reports_an_unknown_playlist(cog, music, ctx):
    await _ready(cog)

    await cog.load_playlist.callback(cog, ctx, name="nope")

    assert "No playlist called" in _description(ctx)
    music.play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_requires_a_voice_channel(cog, ctx):
    await _ready(cog)
    ctx.author.voice = None

    await cog.load_playlist.callback(cog, ctx, name="Chill")

    assert "not in a voice channel" in _description(ctx)


@pytest.mark.asyncio
async def test_load_survives_corrupt_stored_json(cog, music, ctx):
    await _ready(cog)
    async with __import__("aiosqlite").connect(music.db_path) as db:
        await db.execute(
            "INSERT INTO saved_playlists (guild_id, name, songs, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (GUILD_ID, "Broken", "{not json", 777, 0))
        await db.commit()

    await cog.load_playlist.callback(cog, ctx, name="Broken")

    assert "couldn't be read" in _description(ctx)
    music.play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_connects_when_not_already_in_voice(cog, music, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song("a").to_dict()], 777)

    await cog.load_playlist.callback(cog, ctx, name="Chill")

    ctx.author.voice.channel.connect.assert_awaited_once()
    assert GUILD_ID in music.voice_clients


# --- Listing and deleting ---

@pytest.mark.asyncio
async def test_list_shows_names_and_track_counts(cog, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song().to_dict()] * 3, 777)
    ctx.guild.get_member.return_value = None

    await cog.list_playlists.callback(cog, ctx)

    assert "**Chill** — 3 track(s)" in _description(ctx)


@pytest.mark.asyncio
async def test_list_when_there_are_none(cog, ctx):
    await _ready(cog)

    await cog.list_playlists.callback(cog, ctx)

    assert "No saved playlists yet" in _description(ctx)


@pytest.mark.asyncio
async def test_owner_can_delete_their_playlist(cog, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song().to_dict()], 777)

    await cog.delete_playlist.callback(cog, ctx, name="Chill")

    assert await cog._fetch(GUILD_ID, "Chill") is None


@pytest.mark.asyncio
async def test_other_members_cannot_delete_someone_elses_playlist(cog, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song().to_dict()], 999)

    await cog.delete_playlist.callback(cog, ctx, name="Chill")

    assert await cog._fetch(GUILD_ID, "Chill") is not None
    assert "Only an admin" in _description(ctx)


@pytest.mark.asyncio
async def test_admins_can_delete_any_playlist(cog, ctx):
    await _ready(cog)
    await cog._store(GUILD_ID, "Chill", [_song().to_dict()], 999)
    ctx.author.guild_permissions.administrator = True

    await cog.delete_playlist.callback(cog, ctx, name="Chill")

    assert await cog._fetch(GUILD_ID, "Chill") is None


@pytest.mark.asyncio
async def test_delete_reports_an_unknown_playlist(cog, ctx):
    await _ready(cog)

    await cog.delete_playlist.callback(cog, ctx, name="nope")

    assert "No playlist called" in _description(ctx)


# --- Degraded mode ---

@pytest.mark.asyncio
async def test_commands_degrade_when_music_is_unavailable(cog, ctx):
    await _ready(cog)
    cog.bot.get_cog.return_value = None

    for call in (cog.save_playlist.callback(cog, ctx, name="x"),
                 cog.load_playlist.callback(cog, ctx, name="x"),
                 cog.list_playlists.callback(cog, ctx),
                 cog.delete_playlist.callback(cog, ctx, name="x")):
        await call
        assert "isn't available" in _description(ctx)
