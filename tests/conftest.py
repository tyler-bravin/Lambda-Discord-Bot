# tests/conftest.py

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock
from discord.ext import commands
import discord

from cogs.TaskBoard import TaskBoard


@pytest.fixture
def mock_bot():
    """A pytest fixture that creates a mock of the discord.py Bot object."""
    bot = MagicMock(spec=commands.Bot)

    # This part is still correct and necessary.
    bot.can_run = AsyncMock(return_value=True)

    return bot


@pytest_asyncio.fixture
async def cog(mock_bot):
    """An async pytest fixture that initializes the TaskBoard cog for testing."""
    task_cog = TaskBoard(mock_bot)
    task_cog.file_manager.save_tasks = AsyncMock()
    await task_cog.cog_load()
    return task_cog


# --- THIS FIXTURE IS THE KEY CHANGE ---
@pytest.fixture
def mock_context(mock_bot):  # <-- 1. ADD mock_bot as a dependency
    """
    A pytest fixture that creates a mock of the discord.py Context (ctx) object.
    It now includes a correctly configured mock_bot.
    """
    ctx = MagicMock()

    # --- 2. EXPLICITLY ATTACH THE BOT ---
    ctx.bot = mock_bot

    ctx.guild = MagicMock(spec=discord.Guild)
    ctx.guild.id = 1000
    ctx.author = MagicMock(spec=discord.Member)
    ctx.author.id = 2000
    ctx.send = AsyncMock()
    return ctx