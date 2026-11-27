import glob
import os
import sqlite3
import tarfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.Backup import Backup, _copy_sqlite, _int_env

def _make_cog(data_dir, *, keep=7) -> Backup:
    """A Backup cog pointed at a throwaway data directory, with no scheduled task."""
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    with patch("discord.ext.tasks.Loop.start"):
        cog = Backup(bot)
    cog.data_dir = str(data_dir)
    cog.backup_dir = os.path.join(str(data_dir), "backups")
    cog.keep = keep
    return cog


def _seed(data_dir):
    """Writes a representative data directory: a database, a JSON file, secrets, cache."""
    db_path = data_dir / "music_queue.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE queues (guild_id INTEGER PRIMARY KEY, queue_data TEXT)")
    conn.execute("INSERT INTO queues VALUES (1, '[]')")
    conn.commit()
    conn.close()

    (data_dir / "tasks.json").write_text('{"1": ["write tests"]}', encoding="utf-8")
    (data_dir / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    (data_dir / "music_queue.db-wal").write_text("junk", encoding="utf-8")
    cache = data_dir / ".yt-dlp-cache"
    cache.mkdir()
    (cache / "blob").write_text("cached", encoding="utf-8")
    return db_path


def _members(archive) -> set:
    with tarfile.open(archive) as tar:
        return {os.path.relpath(m.name, "data") for m in tar.getmembers() if m.isfile()}


# --- Settings ---

def test_int_env_reads_a_value(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "3")
    assert _int_env("BACKUP_KEEP", 7) == 3


def test_int_env_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "lots")
    assert _int_env("BACKUP_KEEP", 7) == 7


def test_int_env_clamps_to_the_minimum(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "0")
    assert _int_env("BACKUP_KEEP", 7) == 1


def test_scheduled_backup_can_be_disabled(monkeypatch):
    monkeypatch.setenv("BACKUP_ENABLED", "0")
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start") as start:
        Backup(bot)
    start.assert_not_called()


def test_configured_interval_is_applied_to_the_loop(monkeypatch):
    monkeypatch.setenv("BACKUP_ENABLED", "1")
    monkeypatch.setenv("BACKUP_INTERVAL_HOURS", "6")
    bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start"):
        cog = Backup(bot)
    assert cog.scheduled_backup.hours == 6


# --- Snapshot contents ---

@pytest.mark.asyncio
async def test_snapshot_contains_the_data_files(tmp_path):
    _seed(tmp_path)
    cog = _make_cog(tmp_path)

    archive = await cog.create_snapshot()

    assert tarfile.is_tarfile(archive)
    assert _members(archive) == {"music_queue.db", "tasks.json"}


@pytest.mark.asyncio
async def test_snapshot_excludes_cookies_and_scratch_state(tmp_path):
    """Cookies are a live credential; the cache and SQLite sidecars are noise."""
    _seed(tmp_path)
    cog = _make_cog(tmp_path)

    members = _members(await cog.create_snapshot())

    assert not any("cookies" in m for m in members)
    assert not any("yt-dlp-cache" in m for m in members)
    assert not any(m.endswith("-wal") for m in members)


@pytest.mark.asyncio
async def test_snapshot_lands_in_the_backups_directory(tmp_path):
    _seed(tmp_path)
    cog = _make_cog(tmp_path)

    archive = await cog.create_snapshot()

    assert os.path.dirname(archive) == cog.backup_dir
    assert os.path.basename(archive).startswith(Backup.PREFIX)
    # The snapshot directory must not end up inside the next snapshot.
    assert "backups" not in _members(await cog.create_snapshot())


@pytest.mark.asyncio
async def test_unreadable_file_does_not_abort_the_snapshot(tmp_path):
    """One bad file is skipped; the rest of the directory is still captured."""
    _seed(tmp_path)
    cog = _make_cog(tmp_path)

    real_copy = Backup._write_snapshot
    with patch("cogs.Backup.shutil.copy2", side_effect=OSError("boom")):
        archive = await cog.create_snapshot()

    assert real_copy is Backup._write_snapshot  # sanity: nothing monkeypatched the cog
    # tasks.json failed to copy, but the database went through _copy_sqlite.
    assert _members(archive) == {"music_queue.db"}


# --- SQLite consistency ---

def test_copy_sqlite_produces_a_readable_database(tmp_path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('kept')")
    conn.commit()

    dst = tmp_path / "dst.db"
    # Copied while the source connection is still open, as it is in production.
    _copy_sqlite(str(src), str(dst))
    conn.close()

    copied = sqlite3.connect(dst)
    assert copied.execute("SELECT v FROM t").fetchall() == [("kept",)]
    copied.close()


@pytest.mark.asyncio
async def test_backed_up_database_survives_a_round_trip(tmp_path):
    """The archived database is restorable, not just present."""
    _seed(tmp_path)
    cog = _make_cog(tmp_path)
    archive = await cog.create_snapshot()

    restored = tmp_path / "restored"
    with tarfile.open(archive) as tar:
        tar.extractall(restored)

    conn = sqlite3.connect(restored / "data" / "music_queue.db")
    assert conn.execute("SELECT queue_data FROM queues").fetchall() == [("[]",)]
    conn.close()


# --- Retention ---

@pytest.mark.asyncio
async def test_old_snapshots_are_pruned(tmp_path):
    _seed(tmp_path)
    cog = _make_cog(tmp_path, keep=2)
    os.makedirs(cog.backup_dir, exist_ok=True)
    # Names are UTC timestamps, so lexical order is age order.
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        open(os.path.join(cog.backup_dir, f"{Backup.PREFIX}{stamp}.tar.gz"), "wb").close()

    await cog.create_snapshot()

    kept = sorted(os.path.basename(p) for p in
                  glob.glob(os.path.join(cog.backup_dir, f"{Backup.PREFIX}*.tar.gz")))
    # Only the two newest survive: the newest placeholder and the one just written.
    assert len(kept) == 2
    assert kept[0] == f"{Backup.PREFIX}20200103-000000.tar.gz"
    assert kept[1] == os.path.basename(cog.last_snapshot)


@pytest.mark.asyncio
async def test_pruning_keeps_everything_when_under_the_limit(tmp_path):
    _seed(tmp_path)
    cog = _make_cog(tmp_path, keep=5)

    await cog.create_snapshot()
    await cog.create_snapshot()

    assert len(glob.glob(os.path.join(cog.backup_dir, "*.tar.gz"))) == 2


@pytest.mark.asyncio
async def test_two_snapshots_in_the_same_second_do_not_collide(tmp_path):
    """Running !backup twice quickly must not overwrite the first archive."""
    _seed(tmp_path)
    cog = _make_cog(tmp_path)

    first = await cog.create_snapshot()
    second = await cog.create_snapshot()

    assert first != second
    assert os.path.exists(first) and os.path.exists(second)
