import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import juicewrld_api as api
from cogs.sources import Song, identify

PATH = "Compilation/2. Unreleased Discography/1. JUICED UP THE EP (Sessions)/2MININHELL.mp3"


def _entry(name="2MININHELL", **overrides):
    entry = {
        "id": 94316,
        "name": name,
        "category": "unreleased",
        "era": {"id": 101, "name": "jute", "description": "JUICED UP THE EP"},
        "path": PATH,
        "track_titles": [name],
        "credited_artists": "JuiceTheKidd",
        "producers": "J Knight",
        "length": "3:42",
    }
    entry.update(overrides)
    return entry


def _requester():
    member = MagicMock()
    member.id = 777
    member.display_name = "tyler"
    return member


# --- Length parsing ---

def test_length_parses_minutes_and_seconds():
    assert api.parse_length("3:42") == 222


def test_length_parses_hours():
    assert api.parse_length("1:02:03") == 3723


def test_length_is_none_when_the_api_has_none():
    """Unsurfaced and session entries carry a blank length, not a zero."""
    assert api.parse_length("") is None
    assert api.parse_length(None) is None
    assert api.parse_length("0:00") is None


def test_length_ignores_junk():
    assert api.parse_length("about 4 min") is None


# --- Playability ---

def test_unsurfaced_entries_are_not_playable():
    """A documented song that never leaked has no file behind it."""
    assert api.is_playable(_entry(path="", category="unsurfaced")) is False
    assert api.is_playable(_entry()) is True


def test_build_song_refuses_an_unplayable_entry():
    assert api.build_song(_entry(path=""), _requester()) is None
    assert api.song_data(_entry(path="")) is None


# --- Song construction ---

def test_song_streams_the_file_directly():
    song = api.build_song(_entry(), _requester())
    assert song.stream_url.startswith(api.api_base() + "/juicewrld/files/download/")
    assert "2MININHELL.mp3" in song.stream_url
    # No yt-dlp is involved, so FFmpeg plays data['url'] as-is.
    assert song.data["url"] == song.stream_url


def test_song_is_marked_as_a_permanent_stream():
    """These URLs don't expire, which exempts them from the queue's refresh cycle."""
    assert api.build_song(_entry(), _requester()).permanent_stream is True
    assert Song({"webpage_url": "x", "url": "y"}, _requester()).permanent_stream is False


def test_song_carries_metadata_and_cover_art():
    song = api.build_song(_entry(), _requester())
    assert song.title == "2MININHELL"
    assert song.uploader == "JuiceTheKidd"
    assert song.duration == 222
    assert "cover-art" in song.thumbnail


def test_song_falls_back_to_juice_wrld_as_the_artist():
    song = api.build_song(_entry(credited_artists=""), _requester())
    assert song.uploader == "Juice WRLD"


def test_song_data_carries_the_requester():
    """The Now Playing embed reads the requester off the data dict, not the Song."""
    member = _requester()
    song = api.build_song(_entry(), member)
    assert song.data["requester"] is member


def test_url_encoding_survives_awkward_paths():
    """Real paths contain spaces and ampersands, which must not split the query."""
    url = api.stream_url("Compilation/1. Released/7. Goodbye & Good Riddance/Lucid Dreams.mp3")
    assert "&" not in url.split("?", 1)[1].replace("%26", "")
    assert url.count("?") == 1


# --- Database round trip ---

def test_a_permanent_stream_survives_a_restart():
    """Nothing to re-resolve, so the queue should come back playable as stored."""
    original = api.build_song(_entry(), _requester())
    restored = Song(original.to_dict(), _requester())

    assert restored.permanent_stream is True
    assert restored.stream_url == original.stream_url
    # from_data plays data['url'], so the restored data must point at the stream.
    assert restored.data["url"] == original.stream_url
    assert restored.data["requester"] is not None


def test_an_expiring_stream_is_still_dropped_on_restart():
    """YouTube URLs expire, so a restored song must be re-resolved as before."""
    song = Song({"webpage_url": "https://youtu.be/abc", "url": "https://stream",
                 "title": "t"}, _requester())
    restored = Song(song.to_dict(), _requester())
    assert restored.stream_url is None
    assert "stream_url" not in song.to_dict()


# --- Match ranking ---

def test_ranking_prefers_an_exact_title_match():
    """The API also matches on lyrics, so the obvious answer has to be promoted."""
    results = [_entry("On Time"), _entry("Righteous")]
    assert api.rank_matches(results, "righteous")[0]["name"] == "Righteous"


def test_ranking_prefers_a_title_prefix_over_a_mere_mention():
    results = [_entry("Something Else"), _entry("Righteous (v1)")]
    assert api.rank_matches(results, "righteous")[0]["name"] == "Righteous (v1)"


def test_ranking_matches_alternate_track_titles():
    results = [_entry("Nothing"), _entry("2MININHELL",
                                         track_titles=["2 Minutes In Hell (Pt. 1)"])]
    assert api.rank_matches(results, "2 minutes in hell")[0]["name"] == "2MININHELL"


def test_ranking_breaks_ties_on_category():
    """Asked for a song by name, the released cut is what someone means."""
    results = [_entry("Righteous", category="recording_session"),
               _entry("Righteous", category="released"),
               _entry("Righteous", category="unreleased")]
    assert [e["category"] for e in api.rank_matches(results, "righteous")] == [
        "released", "unreleased", "recording_session"]


def test_ranking_drops_unplayable_entries():
    results = [_entry("Righteous", path="", category="unsurfaced"), _entry("Other")]
    assert [e["name"] for e in api.rank_matches(results, "righteous")] == ["Other"]


def test_ranking_on_nothing():
    assert api.rank_matches(None, "x") == []
    assert api.rank_matches([], "x") == []


# --- Describe ---

def test_describe_summarises_an_entry():
    assert api.describe(_entry()) == "2MININHELL — unreleased · jute · 3:42"


def test_describe_omits_what_is_missing():
    assert api.describe(_entry(era=None, length="")) == "2MININHELL — unreleased"


# --- Link handling ---

def test_api_urls_are_recognised():
    assert identify("https://juicewrldapi.com/juicewrld/songs/96445/") == "juicewrld"
    assert identify("https://player.juicewrldapi.com/song/96445") == "juicewrld"
    assert identify("https://www.youtube.com/watch?v=abc") == "youtube"
    assert identify("lucid dreams") == "search"


def test_a_lookalike_domain_is_not_the_api():
    assert api.is_api_url("https://juicewrldapi.com.evil.test/x") is False


def test_a_download_link_yields_its_file_path():
    assert api.parse_api_url(api.stream_url(PATH)) == ("path", PATH)


def test_a_song_link_yields_its_id():
    assert api.parse_api_url("https://juicewrldapi.com/juicewrld/songs/96445/") == ("id", 96445)
    assert api.parse_api_url("https://juicewrldapi.com/?song=123") == ("id", 123)


def test_an_unusable_link_yields_nothing():
    assert api.parse_api_url("https://juicewrldapi.com/about") == (None, None)


# --- HTTP ---

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
async def test_search_returns_ranked_playable_results():
    payload = {"count": 2, "results": [_entry("Other"), _entry("Righteous")]}
    session_cm, session = _session_returning(payload)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        results = await api.search("righteous", category="unreleased")

    assert [e["name"] for e in results] == ["Righteous", "Other"]
    assert session.get.call_args.kwargs["params"]["search"] == "righteous"
    assert session.get.call_args.kwargs["params"]["category"] == "unreleased"


@pytest.mark.asyncio
async def test_search_skips_the_request_for_an_empty_query():
    with patch("cogs.juicewrld_api.aiohttp.ClientSession") as session:
        assert await api.search("   ") == []
    session.assert_not_called()


@pytest.mark.asyncio
async def test_search_respects_the_api_page_size_cap():
    session_cm, session = _session_returning({"results": []})
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        await api.search("x", limit=1000)
    assert session.get.call_args.kwargs["params"]["page_size"] == api.MAX_PAGE_SIZE


@pytest.mark.asyncio
async def test_a_failed_lookup_reads_as_nothing_found():
    """A listener can't act on a traceback, so an outage means 'no results'."""
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", side_effect=OSError("offline")):
        assert await api.search("righteous") == []
        assert await api.fetch_song(1) is None
        assert await api.random_entry() is None


@pytest.mark.asyncio
async def test_an_http_error_reads_as_nothing_found():
    session_cm, _ = _session_returning({}, status=500)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        assert await api.search("righteous") == []


@pytest.mark.asyncio
async def test_fetch_song_rejects_an_empty_response():
    session_cm, _ = _session_returning({})
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        assert await api.fetch_song(94316) is None


@pytest.mark.asyncio
async def test_random_jumps_to_a_page_instead_of_paging_through():
    """2,800 entries make walking the catalogue a non-starter."""
    session_cm, session = _session_returning({"count": 1533, "results": [_entry()]})
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        entry = await api.random_entry("unreleased")

    assert entry["name"] == "2MININHELL"
    page = session.get.call_args.kwargs["params"]["page"]
    assert 1 <= page <= 1533


@pytest.mark.asyncio
async def test_random_gives_up_on_an_empty_catalogue():
    session_cm, _ = _session_returning({"count": 0, "results": []})
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        assert await api.random_entry() is None


@pytest.mark.asyncio
async def test_random_retries_past_an_entry_with_no_file():
    """A random page can land on an unsurfaced song; that isn't a dead end."""
    unplayable = {"count": 10, "results": [_entry(path="")]}
    playable = {"count": 10, "results": [_entry()]}
    session_cm, session = _session_returning(unplayable)

    calls = {"n": 0}

    async def payload():
        calls["n"] += 1
        # First call is the count probe, then one unplayable page, then a good one.
        return playable if calls["n"] > 2 else unplayable

    session.get.return_value.__aenter__.return_value.json = AsyncMock(side_effect=payload)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        entry = await api.random_entry("unreleased")
    assert entry and entry["path"] == PATH


# --- Batch sampling ---

def _paging_session(total, entries):
    """
    A session whose first GET answers the count probe and whose later GETs return
    single-entry pages, taken from `entries` in turn and then repeated.
    """
    calls = []

    async def payload():
        if not calls:
            calls.append("count")
            return {"count": total, "results": entries[:1]}
        index = (len(calls) - 1) % len(entries)
        calls.append("page")
        return {"count": total, "results": [entries[index]]}

    session_cm, session = _session_returning(None)
    session.get.return_value.__aenter__.return_value.json = AsyncMock(side_effect=payload)
    return session_cm, session, calls


@pytest.mark.asyncio
async def test_sampling_returns_the_requested_number_of_tracks():
    entries = [_entry(f"Track {i}", id=i) for i in range(1, 9)]
    session_cm, _, _ = _paging_session(1533, entries)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        picked = await api.random_entries(8, "unreleased")
    assert len(picked) == 8


@pytest.mark.asyncio
async def test_sampling_never_returns_the_same_track_twice():
    """Distinct offsets should mean distinct tracks, but ids settle it either way."""
    repeated = [_entry("Same", id=1)] * 4
    session_cm, _, _ = _paging_session(1533, repeated)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        picked = await api.random_entries(4, "unreleased")
    assert len(picked) == 1


@pytest.mark.asyncio
async def test_sampling_skips_entries_with_no_file():
    entries = [_entry("Playable", id=1), _entry("No file", id=2, path="")]
    session_cm, _, _ = _paging_session(1533, entries)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        picked = await api.random_entries(1, "unreleased")
    assert [entry["name"] for entry in picked] == ["Playable"]


@pytest.mark.asyncio
async def test_sampling_spreads_over_the_whole_category():
    """
    One page of 100 entries is ~700KB and lands in a single era, so the picks are
    drawn from random offsets across the category instead.
    """
    entries = [_entry(f"Track {i}", id=i) for i in range(1, 21)]
    session_cm, session, _ = _paging_session(1533, entries)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        await api.random_entries(20, "unreleased")

    pages = [call.kwargs["params"].get("page") for call in session.get.call_args_list]
    offsets = [page for page in pages if page is not None]
    assert len(offsets) == len(set(offsets))      # drawn without replacement
    assert all(1 <= page <= 1533 for page in offsets)
    assert max(offsets) - min(offsets) > 100      # not clustered in one page


@pytest.mark.asyncio
async def test_sampling_caps_the_requests_a_small_ask_can_cost():
    """A three-track shuffle shouldn't be allowed to make a hundred requests."""
    session_cm, session, _ = _paging_session(1533, [_entry(path="")])
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        assert await api.random_entries(3, "unreleased") == []

    budget = 3 * api.SAMPLE_BUDGET_PER_TRACK + api.SAMPLE_BUDGET_SLACK
    # Plus the one count probe.
    assert session.get.call_count <= budget + 1


@pytest.mark.asyncio
async def test_sampling_is_capped_at_the_shuffle_maximum():
    entries = [_entry(f"Track {i}", id=i) for i in range(1, api.SHUFFLE_MAX + 20)]
    session_cm, _, _ = _paging_session(1533, entries)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        picked = await api.random_entries(999, "unreleased")
    assert len(picked) == api.SHUFFLE_MAX


@pytest.mark.asyncio
async def test_sampling_asks_for_at_least_one():
    entries = [_entry(id=1)]
    session_cm, _, _ = _paging_session(1533, entries)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        assert len(await api.random_entries(0, "unreleased")) == 1


@pytest.mark.asyncio
async def test_sampling_cannot_exceed_the_category_size():
    """Offsets are drawn without replacement, so the pool is the hard ceiling."""
    entries = [_entry(f"Track {i}", id=i) for i in range(1, 6)]
    session_cm, _, _ = _paging_session(3, entries)
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        picked = await api.random_entries(20, "unreleased")
    assert 0 < len(picked) <= 3


@pytest.mark.asyncio
async def test_sampling_an_empty_catalogue():
    session_cm, _ = _session_returning({"count": 0, "results": []})
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", return_value=session_cm):
        assert await api.random_entries(10) == []


@pytest.mark.asyncio
async def test_sampling_survives_the_api_going_down():
    with patch("cogs.juicewrld_api.aiohttp.ClientSession", side_effect=OSError("offline")):
        assert await api.random_entries(10) == []


@pytest.mark.asyncio
async def test_the_categories_with_no_audio_are_known():
    """
    523 recording sessions and 401 unsurfaced songs are documented without audio,
    so neither can be shuffled; the commands check this list before sampling.
    """
    assert api.PLAYABLE_CATEGORIES == ("released", "unreleased")
    assert set(api.NO_AUDIO_CATEGORIES) == {"recording_session", "unsurfaced"}
    assert set(api.PLAYABLE_CATEGORIES) | set(api.NO_AUDIO_CATEGORIES) == set(api.CATEGORIES)


# --- Link resolution ---

@pytest.mark.asyncio
async def test_resolving_a_song_link_queues_it():
    with patch("cogs.juicewrld_api.fetch_song", new_callable=AsyncMock,
               return_value=_entry()):
        resolution = await api.resolve_link(
            "https://juicewrldapi.com/juicewrld/songs/94316/", _requester())
    assert [song.title for song in resolution.songs] == ["2MININHELL"]


@pytest.mark.asyncio
async def test_resolving_an_unknown_song_link_explains_itself():
    with patch("cogs.juicewrld_api.fetch_song", new_callable=AsyncMock, return_value=None):
        resolution = await api.resolve_link(
            "https://juicewrldapi.com/juicewrld/songs/1/", _requester())
    assert "no juice wrld catalogue entry" in resolution.error.lower()
    assert not resolution.songs


@pytest.mark.asyncio
async def test_resolving_an_unsurfaced_song_link_explains_itself():
    with patch("cogs.juicewrld_api.fetch_song", new_callable=AsyncMock,
               return_value=_entry(path="", category="unsurfaced")):
        resolution = await api.resolve_link(
            "https://juicewrldapi.com/juicewrld/songs/1/", _requester())
    assert "no audio file" in resolution.error


@pytest.mark.asyncio
async def test_resolving_a_download_link_uses_the_catalogue_title():
    with patch("cogs.juicewrld_api.search", new_callable=AsyncMock,
               return_value=[_entry()]):
        resolution = await api.resolve_link(api.stream_url(PATH), _requester())
    assert resolution.songs[0].title == "2MININHELL"


@pytest.mark.asyncio
async def test_a_download_link_plays_even_when_the_catalogue_lookup_fails():
    """The link alone is enough to play the file; the title is just nicer."""
    with patch("cogs.juicewrld_api.search", new_callable=AsyncMock, return_value=[]):
        resolution = await api.resolve_link(api.stream_url(PATH), _requester())
    song = resolution.songs[0]
    assert song.title == "2MININHELL"
    assert song.stream_url == api.stream_url(PATH)


@pytest.mark.asyncio
async def test_resolving_a_link_that_points_at_no_song():
    resolution = await api.resolve_link("https://juicewrldapi.com/about", _requester())
    assert resolution.error
    assert not resolution.songs


# --- Configuration ---

def test_the_api_base_is_overridable(monkeypatch):
    """So a self-hoster can point at a mirror if the domain moves."""
    monkeypatch.setenv("JUICEWRLD_API_BASE", "https://mirror.test/")
    assert api.api_base() == "https://mirror.test"
    assert api.stream_url("a.mp3").startswith("https://mirror.test/juicewrld/files/download/")
    assert api.is_api_url("https://mirror.test/juicewrld/songs/1/") is True
    assert api.is_api_url("https://juicewrldapi.com/juicewrld/songs/1/") is False
