import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import juicewrld_api as api
from cogs.JuiceWRLD import JuiceWRLD

GUILD_ID = 12345
PATH = "Compilation/2. Unreleased Discography/1. JUICED UP THE EP (Sessions)/2MININHELL.mp3"


def _entry(name="2MININHELL", song_id=94316, **overrides):
    entry = {
        "id": song_id,
        "name": name,
        "category": "unreleased",
        "era": {"id": 101, "name": "jute"},
        "path": PATH,
        "track_titles": [name],
        "credited_artists": "JuiceTheKidd",
        "producers": "J Knight",
        "length": "3:42",
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def music():
    cog = MagicMock()
    cog.queues = {}
    cog.voice_clients = {}
    cog.save_queue_to_db = AsyncMock()
    cog.play_next = AsyncMock()
    return cog


@pytest.fixture
def cog(music):
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.get_cog.return_value = music
    return JuiceWRLD(bot)


@pytest.fixture
def ctx():
    context = MagicMock()
    context.send = AsyncMock()
    context.defer = AsyncMock()
    context.interaction = None
    context.guild.id = GUILD_ID
    context.author.id = 777
    context.author.display_name = "tyler"
    context.voice_client = None
    context.author.voice.channel.connect = AsyncMock(return_value=MagicMock())
    return context


def _embed(ctx):
    return ctx.send.await_args.kwargs["embed"]


def _text(ctx):
    """Everything the last embed put in front of the user."""
    embed = _embed(ctx)
    parts = [embed.title or "", embed.description or "", getattr(embed.footer, "text", "") or ""]
    parts += [f"{field.name} {field.value}" for field in embed.fields]
    return "\n".join(parts)


def _searching(*entries):
    return patch("cogs.JuiceWRLD.api.search", new_callable=AsyncMock, return_value=list(entries))


# --- Queueing by name ---

@pytest.mark.asyncio
async def test_juice_queues_the_best_match(cog, music, ctx):
    with _searching(_entry()):
        await cog.juice.callback(cog, ctx, query="2mininhell")

    assert [song.title for song in music.queues[GUILD_ID]] == ["2MININHELL"]
    music.save_queue_to_db.assert_awaited_once()
    music.play_next.assert_awaited_once()
    assert "Added to Queue" in _text(ctx)


@pytest.mark.asyncio
async def test_the_queued_track_streams_straight_from_the_api(cog, music, ctx):
    """No yt-dlp lookup: the song goes in already carrying a playable URL."""
    with _searching(_entry()):
        await cog.juice.callback(cog, ctx, query="2mininhell")

    song = music.queues[GUILD_ID][0]
    assert song.permanent_stream is True
    assert song.stream_url == api.stream_url(PATH)


@pytest.mark.asyncio
async def test_the_queued_track_belongs_to_whoever_asked(cog, music, ctx):
    """Skip and remove votes key off the requester."""
    with _searching(_entry()):
        await cog.juice.callback(cog, ctx, query="2mininhell")

    song = music.queues[GUILD_ID][0]
    assert song.requester_id == ctx.author.id
    # The Now Playing embed reads it off the data dict rather than the Song.
    assert song.data["requester"] is ctx.author


@pytest.mark.asyncio
async def test_other_matches_are_offered_with_their_ids(cog, music, ctx):
    """The catalogue holds many versions of a song, so the runners-up matter."""
    matches = [_entry("Righteous", 1), _entry("Righteous (v1)", 2),
               _entry("Righteous (v2)", 3)]
    with _searching(*matches):
        await cog.juice.callback(cog, ctx, query="righteous")

    shown = _text(ctx)
    assert "Righteous (v1)" in shown and "`2`" in shown
    assert "!juice <id>" in shown


@pytest.mark.asyncio
async def test_a_single_match_offers_no_alternatives(cog, music, ctx):
    with _searching(_entry()):
        await cog.juice.callback(cog, ctx, query="2mininhell")
    assert "Other matches" not in _text(ctx)


@pytest.mark.asyncio
async def test_juice_reports_when_nothing_matches(cog, music, ctx):
    with _searching():
        await cog.juice.callback(cog, ctx, query="not a juice wrld song")

    assert GUILD_ID not in music.queues
    assert "Nothing playable" in _text(ctx)
    music.play_next.assert_not_awaited()


# --- Queueing by id ---

@pytest.mark.asyncio
async def test_a_numeric_query_is_treated_as_a_catalogue_id(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.fetch_song", new_callable=AsyncMock,
               return_value=_entry()) as fetch, _searching() as search:
        await cog.juice.callback(cog, ctx, query="94316")

    fetch.assert_awaited_once_with(94316)
    search.assert_not_called()
    assert [song.title for song in music.queues[GUILD_ID]] == ["2MININHELL"]


@pytest.mark.asyncio
async def test_an_unknown_id_is_reported(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.fetch_song", new_callable=AsyncMock, return_value=None):
        await cog.juice.callback(cog, ctx, query="1")

    assert "no catalogue entry" in _text(ctx).lower()
    music.play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unsurfaced_id_explains_there_is_no_audio(cog, music, ctx):
    """The catalogue documents songs that have never leaked; they can't be played."""
    entry = _entry(path="", category="unsurfaced")
    with patch("cogs.JuiceWRLD.api.fetch_song", new_callable=AsyncMock, return_value=entry):
        await cog.juice.callback(cog, ctx, query="1")

    assert "no audio file" in _text(ctx)
    assert GUILD_ID not in music.queues


# --- Voice handling ---

@pytest.mark.asyncio
async def test_juice_requires_a_voice_channel(cog, ctx):
    ctx.author.voice = None
    with _searching(_entry()) as search:
        await cog.juice.callback(cog, ctx, query="2mininhell")
    assert "not in a voice channel" in _text(ctx)
    search.assert_not_called()


@pytest.mark.asyncio
async def test_juice_connects_when_not_already_in_voice(cog, music, ctx):
    with _searching(_entry()):
        await cog.juice.callback(cog, ctx, query="2mininhell")

    ctx.author.voice.channel.connect.assert_awaited_once()
    assert GUILD_ID in music.voice_clients


@pytest.mark.asyncio
async def test_juice_does_not_restart_playback_in_progress(cog, music, ctx):
    vc = MagicMock()
    vc.is_playing.return_value = True
    vc.is_paused.return_value = False
    ctx.voice_client = vc

    with _searching(_entry()):
        await cog.juice.callback(cog, ctx, query="2mininhell")

    assert len(music.queues[GUILD_ID]) == 1
    music.play_next.assert_not_awaited()


# --- Bare invocation ---

@pytest.mark.asyncio
async def test_juice_on_its_own_shows_the_catalogue_totals(cog, ctx):
    totals = {"category_stats": {"unreleased": 1533, "released": 326}}
    with patch("cogs.JuiceWRLD.api.stats", new_callable=AsyncMock, return_value=totals):
        await cog.juice.callback(cog, ctx, query=None)

    shown = _text(ctx)
    assert "1533" in shown and "unreleased" in shown
    assert "!juicesearch" in shown


@pytest.mark.asyncio
async def test_the_usage_message_survives_the_api_being_down(cog, ctx):
    with patch("cogs.JuiceWRLD.api.stats", new_callable=AsyncMock, return_value=None):
        await cog.juice.callback(cog, ctx, query=None)
    assert "!juice <song name or id>" in _text(ctx)


# --- Search listing ---

@pytest.mark.asyncio
async def test_search_lists_matches_with_ids(cog, ctx):
    with _searching(_entry("Righteous", 1), _entry("Righteous (v1)", 2)):
        await cog.juice_search.callback(cog, ctx, query="righteous")

    shown = _text(ctx)
    assert "`1`" in shown and "`2`" in shown
    assert "Showing 2 of 2" in shown


@pytest.mark.asyncio
async def test_search_caps_a_long_result_list(cog, ctx):
    matches = [_entry(f"Song {i}", i) for i in range(30)]
    with _searching(*matches):
        await cog.juice_search.callback(cog, ctx, query="song")

    assert f"Showing {JuiceWRLD.SEARCH_RESULTS_SHOWN} of 30" in _text(ctx)


@pytest.mark.asyncio
async def test_search_reports_when_nothing_matches(cog, ctx):
    with _searching():
        await cog.juice_search.callback(cog, ctx, query="nope")
    assert "Nothing playable" in _text(ctx)


@pytest.mark.asyncio
async def test_search_needs_no_voice_channel(cog, ctx):
    """Browsing the catalogue shouldn't require being in a call."""
    ctx.author.voice = None
    with _searching(_entry()):
        await cog.juice_search.callback(cog, ctx, query="2mininhell")
    assert "`94316`" in _text(ctx)


# --- Random ---

@pytest.mark.asyncio
async def test_random_queues_an_unreleased_track_by_default(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock,
               return_value=_entry()) as pick:
        await cog.juice_random.callback(cog, ctx)

    pick.assert_awaited_once_with("unreleased")
    assert [song.title for song in music.queues[GUILD_ID]] == ["2MININHELL"]
    assert "Random Pick" in _text(ctx)


@pytest.mark.asyncio
async def test_random_can_span_the_whole_catalogue(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock,
               return_value=_entry()) as pick:
        await cog.juice_random.callback(cog, ctx, category="any")
    pick.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_random_normalises_the_category(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock,
               return_value=_entry()) as pick:
        await cog.juice_random.callback(cog, ctx, category="  Released ")
    pick.assert_awaited_once_with("released")


@pytest.mark.asyncio
async def test_random_rejects_an_unknown_category(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock) as pick:
        await cog.juice_random.callback(cog, ctx, category="bangers")

    assert "`unreleased`" in _text(ctx)
    pick.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["unsurfaced", "recording_session", "Recording Session"])
async def test_random_turns_away_the_categories_with_no_audio(cog, music, ctx, category):
    """
    The catalogue documents 523 sessions and 401 unsurfaced songs without serving
    audio for any of them, so a pick from either could only ever fail.
    """
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock) as pick:
        await cog.juice_random.callback(cog, ctx, category=category)

    pick.assert_not_called()
    assert "no audio" in _text(ctx)


@pytest.mark.asyncio
async def test_random_reports_an_unreachable_catalogue(cog, music, ctx):
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock, return_value=None):
        await cog.juice_random.callback(cog, ctx)

    assert "Couldn't reach" in _text(ctx)
    assert GUILD_ID not in music.queues


@pytest.mark.asyncio
async def test_random_requires_a_voice_channel(cog, ctx):
    ctx.author.voice = None
    with patch("cogs.JuiceWRLD.api.random_entry", new_callable=AsyncMock) as pick:
        await cog.juice_random.callback(cog, ctx)
    assert "not in a voice channel" in _text(ctx)
    pick.assert_not_called()


# --- Shuffle ---

def _shuffling(*entries):
    return patch("cogs.JuiceWRLD.api.random_entries", new_callable=AsyncMock,
                 return_value=list(entries))


def _batch(n):
    return [_entry(f"Track {i}", 1000 + i) for i in range(n)]


@pytest.mark.asyncio
async def test_shuffle_fills_the_queue_with_unreleased_tracks(cog, music, ctx):
    with _shuffling(*_batch(20)) as pick:
        await cog.juice_shuffle.callback(cog, ctx)

    pick.assert_awaited_once_with(api.SHUFFLE_DEFAULT, "unreleased")
    assert len(music.queues[GUILD_ID]) == 20
    music.save_queue_to_db.assert_awaited_once()
    music.play_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_shuffle_takes_a_count(cog, music, ctx):
    with _shuffling(*_batch(5)) as pick:
        await cog.juice_shuffle.callback(cog, ctx, count=5)

    pick.assert_awaited_once_with(5, "unreleased")
    assert len(music.queues[GUILD_ID]) == 5


@pytest.mark.asyncio
async def test_shuffle_can_span_the_whole_catalogue(cog, music, ctx):
    with _shuffling(*_batch(3)) as pick:
        await cog.juice_shuffle.callback(cog, ctx, count=3, category="any")
    pick.assert_awaited_once_with(3, None)


@pytest.mark.asyncio
async def test_shuffle_previews_a_few_and_counts_the_rest(cog, music, ctx):
    """Naming all fifty would blow past what an embed can show."""
    with _shuffling(*_batch(20)):
        await cog.juice_shuffle.callback(cog, ctx)

    shown = _text(ctx)
    assert "Queued **20** random tracks" in shown
    assert "`1000`" in shown and "`1004`" in shown
    assert "`1005`" not in shown
    assert f"and **{20 - JuiceWRLD.SHUFFLE_PREVIEW}** more" in shown


@pytest.mark.asyncio
async def test_a_small_shuffle_has_nothing_left_over_to_count(cog, music, ctx):
    with _shuffling(*_batch(3)):
        await cog.juice_shuffle.callback(cog, ctx, count=3)
    assert "more." not in _text(ctx)


@pytest.mark.asyncio
async def test_shuffle_refuses_more_than_the_limit(cog, music, ctx):
    """One request per track, so the ask has to stay bounded."""
    with _shuffling(*_batch(1)) as pick:
        await cog.juice_shuffle.callback(cog, ctx, count=api.SHUFFLE_MAX + 1)

    pick.assert_not_called()
    assert str(api.SHUFFLE_MAX) in _text(ctx)


@pytest.mark.asyncio
async def test_shuffle_refuses_a_nonsense_count(cog, music, ctx):
    with _shuffling(*_batch(1)) as pick:
        await cog.juice_shuffle.callback(cog, ctx, count=0)
    pick.assert_not_called()
    assert "at least one" in _text(ctx)


@pytest.mark.asyncio
async def test_shuffle_turns_away_a_category_with_no_audio(cog, music, ctx):
    with _shuffling(*_batch(1)) as pick:
        await cog.juice_shuffle.callback(cog, ctx, count=5, category="recording_session")
    pick.assert_not_called()
    assert "no audio" in _text(ctx)


@pytest.mark.asyncio
async def test_shuffle_reports_an_unreachable_catalogue(cog, music, ctx):
    with _shuffling():
        await cog.juice_shuffle.callback(cog, ctx)

    assert "Couldn't reach" in _text(ctx)
    assert GUILD_ID not in music.queues
    music.play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_shuffle_requires_a_voice_channel(cog, ctx):
    ctx.author.voice = None
    with _shuffling(*_batch(5)) as pick:
        await cog.juice_shuffle.callback(cog, ctx)
    assert "not in a voice channel" in _text(ctx)
    pick.assert_not_called()


@pytest.mark.asyncio
async def test_shuffle_connects_when_not_already_in_voice(cog, music, ctx):
    with _shuffling(*_batch(5)):
        await cog.juice_shuffle.callback(cog, ctx)

    ctx.author.voice.channel.connect.assert_awaited_once()
    assert GUILD_ID in music.voice_clients


@pytest.mark.asyncio
async def test_shuffle_appends_without_restarting_playback(cog, music, ctx):
    """A shuffle mid-song should extend the queue, not interrupt what's playing."""
    vc = MagicMock()
    vc.is_playing.return_value = True
    vc.is_paused.return_value = False
    ctx.voice_client = vc
    music.queues[GUILD_ID] = ["already queued"]

    with _shuffling(*_batch(4)):
        await cog.juice_shuffle.callback(cog, ctx)

    assert len(music.queues[GUILD_ID]) == 5
    assert music.queues[GUILD_ID][0] == "already queued"
    music.play_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_shuffled_tracks_stream_directly_and_belong_to_the_invoker(cog, music, ctx):
    with _shuffling(*_batch(3)):
        await cog.juice_shuffle.callback(cog, ctx, count=3)

    for song in music.queues[GUILD_ID]:
        assert song.permanent_stream is True
        assert song.requester_id == ctx.author.id


# --- Degraded mode ---

@pytest.mark.asyncio
async def test_commands_degrade_when_music_is_unavailable(cog, ctx):
    cog.bot.get_cog.return_value = None

    for call in (cog.juice.callback(cog, ctx, query="2mininhell"),
                 cog.juice_random.callback(cog, ctx),
                 cog.juice_shuffle.callback(cog, ctx)):
        await call
        assert "isn't available" in _text(ctx)
