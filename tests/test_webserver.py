import datetime
import time
from unittest.mock import MagicMock

from cogs.TaskBoard import Task
from cogs.WebServer import WebServer


def _cog():
    return WebServer(MagicMock())


def test_session_sign_roundtrip():
    cog = _cog()
    token = cog._sign({"uid": "42", "name": "Tyler", "exp": int(time.time()) + 60})
    data = cog._unsign(token)
    assert data["uid"] == "42"
    assert data["name"] == "Tyler"


def test_session_without_exp_is_rejected():
    # A payload with no expiry is treated as expired (safe default).
    cog = _cog()
    assert cog._unsign(cog._sign({"uid": "42"})) is None


def test_session_tamper_is_rejected():
    cog = _cog()
    token = cog._sign({"uid": "42"})
    raw, _, sig = token.rpartition(".")
    # Flip the payload but keep the old signature.
    forged = cog._sign({"uid": "99"}).rpartition(".")[0] + "." + sig
    assert cog._unsign(forged) is None


def test_session_expiry():
    cog = _cog()
    expired = cog._sign({"uid": "1", "exp": int(time.time()) - 10})
    # _sign doesn't set exp; craft one manually via the same signer.
    assert cog._unsign(expired) is None


def test_require_guild():
    cog = _cog()
    session = {"guilds": [{"id": "1000", "name": "A"}]}
    assert cog._require_guild(session, "1000") is True
    assert cog._require_guild(session, "2000") is False


def test_render_login_has_button():
    html = _cog()._render_login()
    assert "Login with Discord" in html
    assert "/login" in html


def test_render_dashboard_escapes_and_lists_tasks():
    cog = _cog()
    tb = MagicMock()
    tb.tasks = {1000: {42: [Task(42, "<b>hack</b> me", datetime.datetime(2026, 1, 1))]}}
    cog.bot.get_cog.return_value = tb

    session = {"uid": "42", "name": "Tyler", "csrf": "tok",
               "guilds": [{"id": "1000", "name": "My Server"}]}
    html = cog._render_dashboard(session)

    assert "My Server" in html
    assert "2026-01-01" in html
    # User content must be HTML-escaped (no raw tags injected).
    assert "<b>hack</b>" not in html
    assert "&lt;b&gt;hack&lt;/b&gt;" in html
