import pytest
from unittest.mock import MagicMock, AsyncMock

from cogs.Help import Help

pytestmark = pytest.mark.asyncio


def _make_ctx():
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.interaction = None
    return ctx


async def test_help_overview_sends_embed():
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.is_owner = AsyncMock(return_value=False)
    bot.cogs = {}
    bot.get_cog.return_value = None

    cog = Help(bot)
    ctx = _make_ctx()
    await cog.help_command.callback(cog, ctx, command=None)

    ctx.send.assert_awaited_once()
    embed = ctx.send.call_args.kwargs["embed"]
    assert "Commands" in embed.title


async def test_help_unknown_command():
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.is_owner = AsyncMock(return_value=False)
    bot.get_command.return_value = None

    cog = Help(bot)
    ctx = _make_ctx()
    await cog.help_command.callback(cog, ctx, command="nope")

    embed = ctx.send.call_args.kwargs["embed"]
    assert "no command" in embed.description.lower()


async def test_help_hidden_command_hidden_from_non_owner():
    hidden_cmd = MagicMock()
    hidden_cmd.hidden = True

    bot = MagicMock()
    bot.command_prefix = "!"
    bot.is_owner = AsyncMock(return_value=False)
    bot.get_command.return_value = hidden_cmd

    cog = Help(bot)
    ctx = _make_ctx()
    await cog.help_command.callback(cog, ctx, command="reload")

    # A non-owner asking about a hidden command is told it doesn't exist.
    embed = ctx.send.call_args.kwargs["embed"]
    assert "no command" in embed.description.lower()
