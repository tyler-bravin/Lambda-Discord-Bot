# cogs/Backup.py
"""
Dated snapshots of the persistent data directory.

Everything the bot remembers across restarts lives in ``DATA_DIR`` — saved
queues, guild volumes, resume sessions, the search cache and the TaskBoard. On
Docker/Coolify that is a named volume, so a redeploy keeps it but recreating the
application does not.

This cog writes a ``tar.gz`` of that directory on a schedule and keeps the most
recent few. Be clear about what that buys: a snapshot stored *inside* the volume
protects against file corruption, a bad migration or an accidental deletion, but
it disappears along with the volume itself. ``!backup`` exists for that case — it
builds a snapshot and uploads it to Discord, putting a copy off the box.

``cookies.txt`` is deliberately excluded. It holds a live YouTube session and is
regenerable, so there is no reason to copy it around (least of all into a Discord
channel).

Environment:
    BACKUP_ENABLED          "0" disables the scheduled snapshots (default on).
    BACKUP_INTERVAL_HOURS   Hours between snapshots (default 24).
    BACKUP_KEEP             How many snapshots to retain (default 7).
"""
import asyncio
import glob
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from cogs.sources import DATA_DIR

log = logging.getLogger(__name__)

# Fallback upload cap for contexts with no guild (a DM), matching Discord's
# baseline attachment limit.
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Reads a positive integer setting, falling back on anything unusable."""
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        log.warning("%s is not a number; using %s", name, default)
        return default
    if value < minimum:
        log.warning("%s must be at least %s; using %s", name, minimum, minimum)
        return minimum
    return value


def _copy_sqlite(src: str, dst: str):
    """
    Copies a live SQLite database using the online backup API.

    A plain file copy of a database that is being written to can capture a torn
    page and restore as corrupt. The backup API walks the database properly and
    folds in any write-ahead log, without blocking the bot's own writes.
    """
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


class Backup(commands.Cog):
    """Takes and prunes snapshots of the bot's data directory."""

    PREFIX = "lambda-data-"
    # The snapshot directory itself (never recurse into it) and the live YouTube
    # session, which is a credential and is regenerable.
    EXCLUDED_NAMES = {"backups", "cookies.txt"}
    # SQLite sidecars: _copy_sqlite already folds these into the database copy.
    EXCLUDED_SUFFIXES = ("-wal", "-shm", "-journal")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data_dir = DATA_DIR
        self.backup_dir = os.path.join(DATA_DIR, "backups")
        self.keep = _int_env("BACKUP_KEEP", 7)
        self.interval_hours = _int_env("BACKUP_INTERVAL_HOURS", 24)
        self.last_snapshot: str = None

        if os.getenv("BACKUP_ENABLED", "1") in ("0", "false", "False"):
            log.info("Scheduled data backups are disabled (BACKUP_ENABLED=0).")
            return
        self.scheduled_backup.change_interval(hours=self.interval_hours)
        self.scheduled_backup.start()

    def cog_unload(self):
        self.scheduled_backup.cancel()

    # --- Snapshot building ---

    def _should_copy(self, name: str) -> bool:
        return not (
            name in self.EXCLUDED_NAMES
            or name.startswith(".")  # the yt-dlp cache and other scratch state
            or name.endswith(self.EXCLUDED_SUFFIXES)
        )

    def _write_snapshot(self, out_path: str):
        """
        Stages a consistent copy of the data directory, then tars it. Blocking:
        always call this in an executor.
        """
        with tempfile.TemporaryDirectory() as staging:
            for name in sorted(os.listdir(self.data_dir)):
                if not self._should_copy(name):
                    continue
                src = os.path.join(self.data_dir, name)
                dst = os.path.join(staging, name)
                try:
                    if name.endswith(".db"):
                        _copy_sqlite(src, dst)
                    elif os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except Exception:
                    # One unreadable file shouldn't cost us the whole snapshot.
                    log.exception("Skipping %s while backing up", name)
            with tarfile.open(out_path, "w:gz") as tar:
                tar.add(staging, arcname="data")

    def _prune(self):
        """Deletes the oldest snapshots beyond the retention limit."""
        # Names are UTC timestamps, so sorting by name sorts by age.
        snapshots = sorted(glob.glob(os.path.join(self.backup_dir, f"{self.PREFIX}*.tar.gz")))
        for stale in snapshots[:-self.keep]:
            try:
                os.remove(stale)
                log.info("Pruned old backup %s", os.path.basename(stale))
            except OSError:
                log.exception("Could not remove old backup %s", stale)

    def _next_path(self) -> str:
        """
        A fresh snapshot path, stamped to the second in UTC.

        Two snapshots inside the same second (``!backup`` run twice) would
        otherwise overwrite each other, so a counter is appended. ``_`` sorts
        after ``.``, which keeps the filenames in age order for _prune.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.backup_dir, f"{self.PREFIX}{stamp}.tar.gz")
        suffix = 2
        while os.path.exists(path):
            path = os.path.join(self.backup_dir, f"{self.PREFIX}{stamp}_{suffix:02d}.tar.gz")
            suffix += 1
        return path

    async def create_snapshot(self) -> str:
        """Writes a new snapshot, prunes old ones, and returns the new path."""
        os.makedirs(self.backup_dir, exist_ok=True)
        out_path = self._next_path()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_snapshot, out_path)
        await loop.run_in_executor(None, self._prune)
        self.last_snapshot = out_path
        return out_path

    # --- Scheduled task ---

    @tasks.loop(hours=24)
    async def scheduled_backup(self):
        try:
            path = await self.create_snapshot()
        except Exception:
            log.exception("Scheduled data backup failed")
            return
        log.info("Wrote data backup %s (%.1f KiB)",
                 os.path.basename(path), os.path.getsize(path) / 1024)

    @scheduled_backup.before_loop
    async def before_scheduled_backup(self):
        await self.bot.wait_until_ready()

    # --- Commands ---

    @commands.command(name="backup", hidden=True)
    @commands.is_owner()
    async def backup_now(self, ctx: commands.Context):
        """(Bot Owner Only) Snapshots the data directory and uploads it to this channel."""
        message = await ctx.send(embed=discord.Embed(
            description="⏳ Building a snapshot of the data directory…",
            color=discord.Color.yellow()))

        try:
            path = await self.create_snapshot()
        except Exception as exc:
            log.exception("Manual backup failed")
            return await message.edit(embed=discord.Embed(
                title="❌ Backup failed", description=f"`{exc}`", color=discord.Color.red()))

        size = os.path.getsize(path)
        limit = ctx.guild.filesize_limit if ctx.guild else DEFAULT_UPLOAD_LIMIT
        embed = discord.Embed(title="💾 Backup created", color=discord.Color.green())
        embed.add_field(name="File", value=f"`{os.path.basename(path)}`", inline=False)
        embed.add_field(name="Size", value=f"{size / 1024:.1f} KiB", inline=True)
        embed.add_field(name="Retained", value=f"{self.keep} snapshots", inline=True)

        if size > limit:
            embed.description = (f"Saved to `{path}` inside the volume. It is too large to "
                                 f"upload here ({limit / 1024 / 1024:.0f} MiB limit), so copy it "
                                 "off the host yourself if you want a remote copy.")
            return await message.edit(embed=embed)

        embed.description = ("Saved inside the volume and attached here, which keeps a copy "
                             "off the host. Excludes `cookies.txt`.")
        await message.edit(embed=embed)
        try:
            await ctx.send(file=discord.File(path))
        except discord.HTTPException as exc:
            await ctx.send(embed=discord.Embed(
                description=f"⚠️ Snapshot saved to `{path}` but the upload failed: `{exc}`",
                color=discord.Color.orange()))

    @commands.command(name="backups", hidden=True)
    @commands.is_owner()
    async def list_backups(self, ctx: commands.Context):
        """(Bot Owner Only) Lists the snapshots currently retained."""
        snapshots = sorted(
            glob.glob(os.path.join(self.backup_dir, f"{self.PREFIX}*.tar.gz")), reverse=True)
        if not snapshots:
            return await ctx.send(embed=discord.Embed(
                description="ℹ️ No snapshots yet.", color=discord.Color.blue()))

        lines = [f"`{os.path.basename(p)}` — {os.path.getsize(p) / 1024:.1f} KiB"
                 for p in snapshots]
        embed = discord.Embed(title="💾 Retained backups", description="\n".join(lines),
                              color=discord.Color.blue())
        embed.set_footer(text=f"In {self.backup_dir} · every {self.interval_hours}h · "
                              f"keeping {self.keep}")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))
