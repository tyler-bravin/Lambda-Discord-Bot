# cogs/WebServer.py
"""
An optional, self-hostable web dashboard for the TaskBoard.

This runs a small aiohttp server *inside the bot process*, so it shares the
TaskBoard cog's live in-memory state — edits made on the web appear in Discord
immediately and vice-versa, with no file/DB desync.

It is disabled unless ``WEB_ENABLED=1``. Users sign in with Discord (OAuth2) and
only ever see and manage their own tasks, in the servers they share with the bot.

Required environment variables when enabled:
- DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET — the bot application's OAuth2 creds.
- WEB_REDIRECT_URI — must exactly match an OAuth2 redirect registered for the app,
  e.g. https://tasks.example.com/callback

Optional:
- WEB_HOST (default 0.0.0.0), WEB_PORT (default 8080)
- WEB_SECURE=1 — set when served over HTTPS, to mark cookies Secure.
- WEB_SECRET_KEY — signs session cookies; set a stable random value so sessions
  survive restarts (a random per-process key is used if unset).
"""
import base64
import datetime
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

from cogs.TaskBoard import Task

LRCLIB_SEARCH = "https://lrclib.net/api/search"
LRCLIB_UA = "Lambda-Discord-Bot (https://github.com/tyler-bravin/Lambda-Discord-Bot)"

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
SESSION_COOKIE = "lambda_session"
STATE_COOKIE = "lambda_oauth_state"
SESSION_TTL = 7 * 24 * 3600


def _truthy(value: str) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


class WebServer(commands.Cog):
    """Serves the optional TaskBoard web dashboard."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = _truthy(os.getenv("WEB_ENABLED", "0"))
        self.client_id = os.getenv("DISCORD_CLIENT_ID")
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET")
        self.redirect_uri = os.getenv("WEB_REDIRECT_URI")
        self.host = os.getenv("WEB_HOST", "0.0.0.0")
        self.port = int(os.getenv("WEB_PORT", "8080"))
        self.secure = _truthy(os.getenv("WEB_SECURE", "0"))
        # Whether the now-playing screen exposes playback controls.
        self.controls = _truthy(os.getenv("WEB_CONTROLS", "0"))
        # Public base URL, used to build shareable now-playing links. Derived from
        # the OAuth redirect URI if not set explicitly.
        self.base_url = os.getenv("WEB_BASE_URL")
        if not self.base_url and self.redirect_uri:
            self.base_url = self.redirect_uri.rsplit("/callback", 1)[0]
        key = os.getenv("WEB_SECRET_KEY")
        self._secret = key.encode() if key else secrets.token_bytes(32)
        self._runner = None

    async def cog_load(self):
        if not self.enabled:
            return
        if not (self.client_id and self.client_secret and self.redirect_uri):
            log.warning("WEB_ENABLED is set but DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET / "
                        "WEB_REDIRECT_URI are missing; the web dashboard was not started.")
            return
        await self._start()

    async def cog_unload(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _start(self):
        app = web.Application()
        app.add_routes([
            web.get("/", self.handle_index),
            web.get("/login", self.handle_login),
            web.get("/callback", self.handle_callback),
            web.get("/logout", self.handle_logout),
            web.get("/health", self.handle_health),
            web.post("/tasks/add", self.handle_add),
            web.post("/tasks/done", self.handle_done),
            web.post("/tasks/remove", self.handle_remove),
            # Now-playing screen (public read-only view + JSON API).
            web.get("/np/{guild_id}", self.handle_np_page),
            web.get("/api/np/{guild_id}", self.handle_np_data),
            web.get("/api/np/{guild_id}/lyrics", self.handle_np_lyrics),
            web.post("/api/np/{guild_id}/control", self.handle_np_control),
        ])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("TaskBoard web dashboard listening on %s:%s", self.host, self.port)

    # --- Signed-cookie sessions (stdlib HMAC, no extra dependency) ---
    def _sign(self, payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        sig = hmac.new(self._secret, raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def _unsign(self, token: str):
        if not token or "." not in token:
            return None
        raw, _, sig = token.rpartition(".")
        expected = hmac.new(self._secret, raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode()))
        except Exception:
            return None
        if data.get("exp", 0) < time.time():
            return None
        return data

    def _session(self, request: web.Request):
        return self._unsign(request.cookies.get(SESSION_COOKIE, ""))

    def _set_session(self, resp: web.Response, data: dict):
        data = {**data, "exp": int(time.time()) + SESSION_TTL}
        resp.set_cookie(SESSION_COOKIE, self._sign(data), max_age=SESSION_TTL,
                        httponly=True, samesite="Lax", secure=self.secure)

    # --- OAuth2 flow ---
    @staticmethod
    def _safe_next(value: str) -> str:
        """Only allow same-site relative redirects (prevents open-redirect abuse)."""
        if value and value.startswith("/") and not value.startswith("//"):
            return value
        return "/"

    async def handle_login(self, request: web.Request) -> web.Response:
        state = secrets.token_urlsafe(16)
        next_url = self._safe_next(request.query.get("next", "/"))
        params = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        })
        resp = web.HTTPFound(f"{AUTHORIZE_URL}?{params}")
        resp.set_cookie(STATE_COOKIE,
                        self._sign({"state": state, "next": next_url, "exp": int(time.time()) + 600}),
                        max_age=600, httponly=True, samesite="Lax", secure=self.secure)
        return resp

    async def handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        saved = self._unsign(request.cookies.get(STATE_COOKIE, ""))
        if not code or not saved or saved.get("state") != state:
            return web.Response(status=400, text="Invalid OAuth state. Please try logging in again.")

        token_data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{DISCORD_API}/oauth2/token", data=token_data) as resp:
                if resp.status != 200:
                    return web.Response(status=502, text="Discord token exchange failed.")
                token = await resp.json()
            headers = {"Authorization": f"Bearer {token['access_token']}"}
            async with session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
                user = await resp.json()
            async with session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
                guilds = await resp.json()

        bot_guild_ids = {g.id for g in self.bot.guilds}
        shared = [{"id": str(g["id"]), "name": g["name"]}
                  for g in guilds if int(g["id"]) in bot_guild_ids]

        session_data = {
            "uid": str(user["id"]),
            "name": user.get("global_name") or user.get("username", "user"),
            "guilds": shared,
            "csrf": secrets.token_urlsafe(16),
        }
        resp = web.HTTPFound(self._safe_next(saved.get("next", "/")))
        self._set_session(resp, session_data)
        resp.del_cookie(STATE_COOKIE)
        return resp

    async def handle_logout(self, request: web.Request) -> web.Response:
        resp = web.HTTPFound("/")
        resp.del_cookie(SESSION_COOKIE)
        return resp

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    # --- Task mutations (validated) ---
    def _require_guild(self, session: dict, guild_id: str):
        """Ensures the logged-in user is a member of the target guild."""
        return any(g["id"] == guild_id for g in session.get("guilds", []))

    async def _guarded_post(self, request: web.Request):
        """Returns (session, form_data) or raises a redirect/forbidden response."""
        session = self._session(request)
        if not session:
            raise web.HTTPFound("/")
        data = await request.post()
        if data.get("csrf") != session.get("csrf"):
            raise web.HTTPForbidden(text="Invalid CSRF token.")
        return session, data

    async def handle_add(self, request: web.Request) -> web.Response:
        session, data = await self._guarded_post(request)
        guild_id = data.get("guild_id", "")
        if not self._require_guild(session, guild_id):
            return web.HTTPForbidden(text="You are not a member of that server.")

        description = (data.get("description") or "").strip()
        try:
            due_date = datetime.datetime.strptime(data.get("due_date", ""), "%Y-%m-%d")
        except ValueError:
            return web.HTTPFound("/?e=date")
        if not description:
            return web.HTTPFound("/?e=empty")

        tb = self.bot.get_cog("TaskBoard")
        gid, uid = int(guild_id), int(session["uid"])
        tb.tasks.setdefault(gid, {}).setdefault(uid, []).append(Task(uid, description[:500], due_date))
        await tb.file_manager.save_tasks(tb.tasks)
        return web.HTTPFound("/")

    async def handle_done(self, request: web.Request) -> web.Response:
        session, data = await self._guarded_post(request)
        return await self._mutate_by_index(session, data, action="done")

    async def handle_remove(self, request: web.Request) -> web.Response:
        session, data = await self._guarded_post(request)
        return await self._mutate_by_index(session, data, action="remove")

    async def _mutate_by_index(self, session: dict, data, action: str) -> web.Response:
        guild_id = data.get("guild_id", "")
        if not self._require_guild(session, guild_id):
            return web.HTTPForbidden(text="You are not a member of that server.")
        try:
            index = int(data.get("index", ""))
        except ValueError:
            return web.HTTPFound("/")

        tb = self.bot.get_cog("TaskBoard")
        gid, uid = int(guild_id), int(session["uid"])
        user_tasks = tb.tasks.get(gid, {}).get(uid)
        if not user_tasks or not 1 <= index <= len(user_tasks):
            return web.HTTPFound("/")

        if action == "done":
            user_tasks[index - 1].mark_done()
        else:  # remove
            user_tasks.pop(index - 1)
            # Prune empty containers, mirroring the Discord command.
            if not tb.tasks.get(gid, {}).get(uid):
                tb.tasks[gid].pop(uid, None)
            if not tb.tasks.get(gid):
                tb.tasks.pop(gid, None)
        await tb.file_manager.save_tasks(tb.tasks)
        return web.HTTPFound("/")

    # --- Now-playing screen ---
    async def handle_np_page(self, request: web.Request) -> web.Response:
        guild_id = request.match_info["guild_id"]
        guild = self.bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
        if guild is None:
            return web.Response(status=404, text="Unknown server.")
        return web.Response(text=self._render_player(guild_id, guild.name), content_type="text/html")

    def _np_context(self, request: web.Request):
        """Resolves (guild, music_cog) from the request, or None if unavailable."""
        guild_id = request.match_info["guild_id"]
        if not guild_id.isdigit():
            return None, None
        guild = self.bot.get_guild(int(guild_id))
        music = self.bot.get_cog("Music")
        return guild, music

    async def handle_np_data(self, request: web.Request) -> web.Response:
        guild, music = self._np_context(request)
        if guild is None or music is None:
            return web.json_response({"error": "unknown guild"}, status=404)

        data = music.web_snapshot(guild)
        data["controls_enabled"] = self.controls
        session = self._session(request)
        data["logged_in"] = bool(session)
        data["can_control"] = False
        if self.controls and session:
            member = guild.get_member(int(session["uid"]))
            data["can_control"] = music.web_can_control(guild, member)
            if data["can_control"]:
                data["csrf"] = session.get("csrf")
        return web.json_response(data)

    async def handle_np_control(self, request: web.Request) -> web.Response:
        guild, music = self._np_context(request)
        if guild is None or music is None:
            return web.json_response({"ok": False, "error": "unknown guild"}, status=404)
        if not self.controls:
            return web.json_response({"ok": False, "error": "Controls are disabled."}, status=403)
        session = self._session(request)
        if not session:
            return web.json_response({"ok": False, "error": "Not logged in."}, status=401)
        data = await request.post()
        if data.get("csrf") != session.get("csrf"):
            return web.json_response({"ok": False, "error": "Invalid CSRF token."}, status=403)

        member = guild.get_member(int(session["uid"]))
        result = await music.web_control(guild, member, data.get("action", ""), data.get("value"))
        return web.json_response(result, status=200 if result.get("ok") else 400)

    @staticmethod
    def _clean_track_title(title: str) -> str:
        """Strips common YouTube noise so lyrics lookups match better."""
        cleaned = re.sub(r"\[[^\]]*\]", "", title or "")
        cleaned = re.sub(r"\([^)]*\)", "", cleaned)
        return cleaned.strip(" -")

    async def handle_np_lyrics(self, request: web.Request) -> web.Response:
        guild, music = self._np_context(request)
        if guild is None or music is None:
            return web.json_response({"error": "unknown guild"}, status=404)
        song = music.current_song.get(guild.id)
        if not song:
            return web.json_response({"synced": None, "plain": None})

        query = self._clean_track_title(song.title)
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": LRCLIB_UA}) as session:
                async with session.get(LRCLIB_SEARCH, params={"q": query}) as resp:
                    results = await resp.json() if resp.status == 200 else []
        except Exception:
            log.debug("LRCLIB lookup failed", exc_info=True)
            results = []

        # Prefer a result with lyrics and the closest duration to our track.
        best, best_diff = None, None
        for item in results:
            if not (item.get("syncedLyrics") or item.get("plainLyrics")):
                continue
            diff = abs((item.get("duration") or 0) - (song.duration or 0))
            if best is None or diff < best_diff:
                best, best_diff = item, diff

        # Reject wildly mismatched durations when we know ours.
        if best and (not song.duration or best_diff <= 20):
            return web.json_response({
                "synced": best.get("syncedLyrics"),
                "plain": best.get("plainLyrics"),
                "matched": f"{best.get('artistName', '')} — {best.get('trackName', '')}".strip(" —"),
            })
        return web.json_response({"synced": None, "plain": None})

    # --- Rendering ---
    async def handle_index(self, request: web.Request) -> web.Response:
        session = self._session(request)
        if not session:
            return web.Response(text=self._render_login(), content_type="text/html")
        return web.Response(text=self._render_dashboard(session, request.query.get("e")),
                            content_type="text/html")

    def _render_login(self) -> str:
        return _PAGE.format(body=(
            '<div class="card center">'
            '<h1>📋 Lambda TaskBoard</h1>'
            '<p>Sign in with Discord to view and manage your tasks.</p>'
            '<a class="btn" href="/login">Login with Discord</a>'
            '</div>'))

    def _render_dashboard(self, session: dict, error: str = None) -> str:
        tb = self.bot.get_cog("TaskBoard")
        name = html.escape(session.get("name", "user"))
        csrf = html.escape(session.get("csrf", ""))
        today = datetime.date.today().isoformat()

        banners = {
            "date": "Invalid date — use YYYY-MM-DD.",
            "empty": "Task description can't be empty.",
        }
        banner = f'<div class="banner">{html.escape(banners[error])}</div>' if error in banners else ""

        sections = []
        for guild in session.get("guilds", []):
            gid, uid = int(guild["id"]), int(session["uid"])
            gname = html.escape(guild["name"])
            gid_attr = html.escape(guild["id"])
            user_tasks = (tb.tasks.get(gid, {}).get(uid, []) if tb else [])

            rows = ""
            for i, task in enumerate(user_tasks, 1):
                status = (f'<span class="done">✅ {html.escape(str(task.finished))}</span>'
                          if task.is_finished else '<span class="todo">❌ Not done</span>')
                done_btn = "" if task.is_finished else (
                    f'<form method="post" action="/tasks/done" class="inline">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="guild_id" value="{gid_attr}">'
                    f'<input type="hidden" name="index" value="{i}">'
                    f'<button class="mini">Mark done</button></form>')
                rows += (
                    f'<tr><td>{html.escape(task.task)}</td>'
                    f'<td>{html.escape(task.due_date.strftime("%Y-%m-%d"))}</td>'
                    f'<td>{status}</td>'
                    f'<td class="actions">{done_btn}'
                    f'<form method="post" action="/tasks/remove" class="inline">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="guild_id" value="{gid_attr}">'
                    f'<input type="hidden" name="index" value="{i}">'
                    f'<button class="mini danger">Remove</button></form></td></tr>')

            if not rows:
                rows = '<tr><td colspan="4" class="empty">No tasks yet.</td></tr>'

            sections.append(
                f'<div class="card"><h2>{gname}</h2>'
                f'<table><thead><tr><th>Task</th><th>Due</th><th>Status</th><th></th></tr></thead>'
                f'<tbody>{rows}</tbody></table>'
                f'<form method="post" action="/tasks/add" class="addform">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="guild_id" value="{gid_attr}">'
                f'<input type="text" name="description" placeholder="New task…" maxlength="500" required>'
                f'<input type="date" name="due_date" value="{today}" required>'
                f'<button class="btn">Add</button></form></div>')

        if not session.get("guilds"):
            sections.append('<div class="card"><p>You don\'t share any servers with the bot yet.</p></div>')

        header = (f'<div class="topbar"><span>Signed in as <b>{name}</b></span>'
                  f'<a class="btn ghost" href="/logout">Log out</a></div>')
        return _PAGE.format(body=header + banner + "".join(sections))

    def _render_player(self, guild_id: str, guild_name: str) -> str:
        return (_PLAYER_PAGE
                .replace("__GID__", html.escape(guild_id))
                .replace("__GNAME__", html.escape(guild_name)))

    # --- Command ---
    @commands.hybrid_command(name="player", aliases=["screen", "webnp"])
    async def player(self, ctx: commands.Context):
        """Posts a link to the live web 'now playing' screen for this server."""
        if not self.enabled or self._runner is None:
            return await ctx.send(embed=discord.Embed(
                description="ℹ️ The web dashboard isn't enabled on this bot.", color=discord.Color.blurple()))
        if not self.base_url:
            return await ctx.send(embed=discord.Embed(
                description="⚠️ Set `WEB_BASE_URL` (or `WEB_REDIRECT_URI`) so I can build the link.",
                color=discord.Color.orange()))
        url = f"{self.base_url.rstrip('/')}/np/{ctx.guild.id}"
        await ctx.send(embed=discord.Embed(
            title="🎧 Live Player",
            description=f"[Open the now-playing screen for **{ctx.guild.name}**]({url})",
            color=discord.Color.blurple()))


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lambda TaskBoard</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0e0f13; --card: rgba(255,255,255,.045); --stroke: rgba(255,255,255,.08);
    --text: #eef0f3; --muted: #a6adbb; --dim: #6b7280;
    --accent1: #7c5cff; --accent2: #4ea1ff;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: var(--font); color: var(--text); padding: 40px 20px 60px;
          background: radial-gradient(90% 60% at 50% -10%, rgba(90,80,255,.16), transparent 60%), var(--bg);
          min-height: 100vh; -webkit-font-smoothing: antialiased; }}
  .topbar {{ max-width: 840px; margin: 0 auto 20px; display: flex; justify-content: space-between;
             align-items: center; font-size: .92rem; color: var(--muted); }}
  .card {{ max-width: 840px; margin: 0 auto 20px; background: var(--card); border: 1px solid var(--stroke);
           border-radius: 20px; padding: 26px 28px; backdrop-filter: blur(20px);
           box-shadow: 0 20px 50px rgba(0,0,0,.35); animation: rise .45s ease both; }}
  @keyframes rise {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: none; }} }}
  .center {{ text-align: center; }}
  h1 {{ margin: 0 0 8px; font-size: 1.55rem; letter-spacing: -.02em; }}
  h2 {{ margin: 0 0 16px; font-size: 1.15rem; color: #fff; letter-spacing: -.01em; }}
  p {{ color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 12px 8px; border-bottom: 1px solid var(--stroke); font-size: .92rem;
            vertical-align: middle; }}
  th {{ color: var(--muted); font-weight: 600; font-size: .72rem; letter-spacing: .08em; text-transform: uppercase; }}
  tr:last-child td {{ border-bottom: none; }}
  .empty {{ color: var(--muted); text-align: center; padding: 22px 0; }}
  .done {{ color: #46e5a0; font-weight: 600; }} .todo {{ color: #f0b232; }}
  .actions {{ white-space: nowrap; text-align: right; }}
  .inline {{ display: inline; }}
  .btn {{ display: inline-flex; align-items: center; gap: 6px; color: #fff; border: none; border-radius: 11px;
          padding: 10px 18px; font-size: .9rem; font-weight: 600; text-decoration: none; cursor: pointer;
          background: linear-gradient(135deg, var(--accent1), var(--accent2));
          box-shadow: 0 8px 20px rgba(90,80,255,.32); transition: transform .12s ease, box-shadow .2s ease; }}
  .btn:hover {{ transform: translateY(-2px); box-shadow: 0 12px 26px rgba(90,80,255,.42); }}
  .btn:active {{ transform: scale(.97); }}
  .btn.ghost {{ background: transparent; border: 1px solid var(--stroke); box-shadow: none; color: var(--muted); font-weight: 500; }}
  .btn.ghost:hover {{ color: #fff; border-color: rgba(255,255,255,.25); box-shadow: none; }}
  .mini {{ background: rgba(255,255,255,.08); color: #fff; border: 1px solid var(--stroke); border-radius: 8px;
           padding: 6px 12px; font-size: .8rem; cursor: pointer; margin-left: 6px; transition: background .2s ease; }}
  .mini:hover {{ background: rgba(255,255,255,.16); }}
  .mini.danger {{ background: rgba(218,55,60,.18); border-color: rgba(218,55,60,.4); color: #ff9a9d; }}
  .mini.danger:hover {{ background: rgba(218,55,60,.32); }}
  .addform {{ display: flex; gap: 10px; margin-top: 18px; flex-wrap: wrap; }}
  .addform input[type=text] {{ flex: 1; min-width: 180px; }}
  input {{ background: rgba(0,0,0,.25); border: 1px solid var(--stroke); color: var(--text); border-radius: 11px;
           padding: 11px 14px; font-size: .9rem; font-family: inherit; transition: border .2s ease; }}
  input:focus {{ outline: none; border-color: var(--accent2); }}
  .banner {{ max-width: 840px; margin: 0 auto 18px; background: rgba(218,55,60,.14); border: 1px solid rgba(218,55,60,.4);
             color: #ffb3b5; border-radius: 12px; padding: 12px 16px; font-size: .9rem; }}
</style></head>
<body>{body}</body></html>"""


_PLAYER_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__GNAME__ — Now Playing</title>
<style>
  :root {
    color-scheme: dark;
    --glass: rgba(255,255,255,.06);
    --glass-strong: rgba(255,255,255,.10);
    --stroke: rgba(255,255,255,.09);
    --text: #f4f5f7; --muted: #a6adbb; --dim: #6b7280;
    --accent1: #7c5cff; --accent2: #4ea1ff;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; font-family: var(--font); color: var(--text); background: #0c0d10;
         overflow-x: hidden; -webkit-font-smoothing: antialiased; }
  a { color: inherit; }

  /* Blurred album-art ambience */
  #bg { position: fixed; inset: -12%; z-index: -2; background-size: cover; background-position: center;
        filter: blur(70px) saturate(1.5) brightness(.5); transform: scale(1.15);
        transition: background-image .6s ease, opacity .6s ease; opacity: .9; }
  #veil { position: fixed; inset: 0; z-index: -1;
          background: radial-gradient(120% 90% at 50% 0%, transparent 30%, rgba(8,9,12,.55) 75%, rgba(8,9,12,.92) 100%); }

  .wrap { max-width: 1040px; margin: 0 auto; padding: 34px 24px 60px; animation: rise .5s ease both; }
  @keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
  h1 { font-size: .82rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
       font-weight: 600; margin: 0 0 22px; display: flex; align-items: center; gap: 8px; }

  .grid { display: grid; grid-template-columns: 1.05fr 1fr; gap: 22px; align-items: stretch; }
  @media (max-width: 780px) { .grid { grid-template-columns: 1fr; } }

  .card { background: var(--glass); border: 1px solid var(--stroke); border-radius: 24px; padding: 26px;
          backdrop-filter: blur(24px) saturate(1.3); -webkit-backdrop-filter: blur(24px) saturate(1.3);
          box-shadow: 0 24px 60px rgba(0,0,0,.45); }

  .art-wrap { position: relative; width: 100%; max-width: 300px; margin: 0 auto 22px; aspect-ratio: 1; }
  #art { width: 100%; height: 100%; object-fit: cover; border-radius: 18px; background: #1b1d22;
         box-shadow: 0 20px 45px rgba(0,0,0,.5); transition: transform .5s ease, filter .4s ease; }
  body.paused #art { filter: saturate(.5) brightness(.8); transform: scale(.97); }

  .title { font-size: 1.5rem; font-weight: 750; line-height: 1.2; letter-spacing: -.01em; }
  .title a { text-decoration: none; }
  .title a:hover { text-decoration: underline; }
  .sub { color: var(--muted); font-size: .92rem; margin-top: 5px; }
  #requester { color: var(--dim); font-size: .8rem; margin-top: 2px; }

  .bar { height: 6px; background: rgba(255,255,255,.12); border-radius: 99px; margin: 22px 0 7px; overflow: hidden; }
  .fill { height: 100%; width: 0; border-radius: 99px;
          background: linear-gradient(90deg, var(--accent1), var(--accent2));
          transition: width .28s linear; }
  .times { display: flex; justify-content: space-between; font-size: .76rem; color: var(--muted);
           font-variant-numeric: tabular-nums; }

  .controls { display: flex; align-items: center; gap: 12px; margin-top: 22px; flex-wrap: wrap; }
  .controls button { display: grid; place-items: center; background: var(--glass-strong);
                     border: 1px solid var(--stroke); color: #fff; border-radius: 50%;
                     width: 48px; height: 48px; font-size: 1.05rem; cursor: pointer;
                     transition: transform .12s ease, background .2s ease; }
  .controls button:hover { background: rgba(255,255,255,.18); transform: translateY(-2px); }
  .controls button:active { transform: scale(.92); }
  .controls button.primary { width: 60px; height: 60px; font-size: 1.35rem; border: none;
                             background: linear-gradient(135deg, var(--accent1), var(--accent2));
                             box-shadow: 0 10px 24px rgba(90,80,255,.4); }
  .vol { flex: 1; min-width: 120px; appearance: none; height: 5px; border-radius: 99px;
         background: rgba(255,255,255,.14); outline: none; cursor: pointer; }
  .vol::-webkit-slider-thumb { appearance: none; width: 15px; height: 15px; border-radius: 50%; background: #fff;
                               box-shadow: 0 2px 6px rgba(0,0,0,.4); }
  .vol::-moz-range-thumb { width: 15px; height: 15px; border: none; border-radius: 50%; background: #fff; }
  .hint { color: var(--muted); font-size: .84rem; margin-top: 14px; }
  .login { display: inline-flex; align-items: center; gap: 8px; margin-top: 14px; padding: 10px 18px;
           border-radius: 99px; text-decoration: none; font-weight: 600; font-size: .9rem;
           background: linear-gradient(135deg, var(--accent1), var(--accent2)); color: #fff;
           box-shadow: 0 10px 24px rgba(90,80,255,.35); transition: transform .12s ease; }
  .login:hover { transform: translateY(-2px); }

  /* Lyrics */
  .lyrics { position: relative; height: 100%; min-height: 440px; max-height: 560px; overflow-y: auto;
            line-height: 1.35; padding: 20px 6px; scrollbar-width: thin;
            -webkit-mask-image: linear-gradient(transparent, #000 14%, #000 86%, transparent);
            mask-image: linear-gradient(transparent, #000 14%, #000 86%, transparent); }
  .lyrics::-webkit-scrollbar { width: 6px; } .lyrics::-webkit-scrollbar-thumb { background: rgba(255,255,255,.14); border-radius: 9px; }
  .lyrics .line { font-size: 1.24rem; font-weight: 650; color: #fff; opacity: .28; padding: 7px 14px;
                  border-radius: 12px; transition: opacity .35s ease, transform .35s ease, background .35s ease;
                  transform-origin: left center; }
  .lyrics .line.active { opacity: 1; transform: scale(1.03); background: rgba(255,255,255,.06); }
  .lyrics .muted { color: var(--muted); font-size: .95rem; font-weight: 500; opacity: 1; }
  .lyrics .plain { white-space: pre-wrap; font-size: 1rem; font-weight: 500; opacity: .85; color: #e6e8ec; }

  .queue { margin-top: 30px; }
  .queue h2 { font-size: .8rem; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); margin: 0 0 6px; }
  .qrow { display: flex; align-items: center; gap: 14px; padding: 10px 12px; border-radius: 14px;
          transition: background .2s ease; }
  .qrow:hover { background: var(--glass); }
  .qrow img { width: 58px; height: 42px; object-fit: cover; border-radius: 8px; background: #1b1d22; }
  .qrow .qt { font-size: .92rem; font-weight: 550; }
  .qrow .qs { font-size: .76rem; color: var(--muted); margin-top: 1px; }
  .qnum { color: var(--dim); width: 22px; text-align: right; font-size: .8rem; font-variant-numeric: tabular-nums; }
  .idle { color: var(--muted); padding: 34px 0; text-align: center; font-size: .9rem; }
</style></head>
<body>
  <div id="bg"></div><div id="veil"></div>
  <div class="wrap">
  <h1>🎧 __GNAME__</h1>
  <div class="grid">
    <div class="card">
      <div class="art-wrap"><img id="art" alt=""></div>
      <div id="title" class="title">—</div>
      <div id="uploader" class="sub"></div>
      <div class="bar"><div id="fill" class="fill"></div></div>
      <div class="times"><span id="elapsed">0:00</span><span id="dur">0:00</span></div>
      <div id="requester"></div>
      <div id="controls" class="controls"></div>
      <div id="cmsg" class="hint"></div>
    </div>
    <div class="card"><div id="lyrics" class="lyrics"><div class="muted">Lyrics will appear here.</div></div></div>
  </div>
  <div class="queue"><h2>Up Next</h2><div id="queue"></div></div>
</div>
<script>
const GID = "__GID__";
const S = { url:null, duration:0, elapsed:0, syncTs:0, playing:false, paused:false,
            controls:false, canControl:false, loggedIn:false, csrf:null, lyrics:[] };

function fmt(s){ s=Math.max(0,Math.floor(s||0)); const m=Math.floor(s/60); return m+":"+String(s%60).padStart(2,"0"); }

async function poll(){
  try{ const r=await fetch(`/api/np/${GID}`); apply(await r.json()); }catch(e){}
}
function apply(d){
  S.controls=d.controls_enabled; S.canControl=d.can_control; S.loggedIn=d.logged_in; S.csrf=d.csrf||null;
  const now=d.now;
  if(now){
    if(now.url!==S.url){ S.url=now.url; loadLyrics(); }
    S.duration=now.duration||0; S.elapsed=now.elapsed||0; S.syncTs=performance.now();
    S.playing=d.playing; S.paused=d.paused;
    document.getElementById("art").src = now.thumbnail||"";
    document.getElementById("bg").style.backgroundImage = now.thumbnail? `url("${now.thumbnail}")` : "";
    document.body.classList.toggle("paused", !!d.paused);
    const t=document.getElementById("title"); t.textContent="";
    if(now.url){ const a=document.createElement("a"); a.href=now.url; a.target="_blank"; a.rel="noopener"; a.textContent=now.title||"—"; t.appendChild(a); }
    else { t.textContent=now.title||"—"; }
    document.getElementById("uploader").textContent=now.uploader||"";
    document.getElementById("requester").textContent = now.requester? "Requested by "+now.requester : "";
  } else {
    S.url=null; S.playing=false; S.paused=false; S.duration=0; S.elapsed=0;
    document.body.classList.remove("paused");
    document.getElementById("art").src=""; document.getElementById("bg").style.backgroundImage="";
    document.getElementById("title").textContent="Nothing playing";
    document.getElementById("uploader").textContent=""; document.getElementById("requester").textContent="";
    document.getElementById("lyrics").innerHTML='<div class="muted">Nothing playing right now.</div>';
    S.lyrics=[];
  }
  renderQueue(d.queue||[]);
  renderControls(d);
}
function renderQueue(q){
  const el=document.getElementById("queue");
  if(!q.length){ el.innerHTML='<div class="idle">Queue is empty.</div>'; return; }
  el.innerHTML="";
  q.forEach((s,i)=>{
    const row=document.createElement("div"); row.className="qrow";
    const num=document.createElement("div"); num.className="qnum"; num.textContent=(i+1);
    const img=document.createElement("img"); img.src=s.thumbnail||""; img.alt="";
    const box=document.createElement("div");
    const qt=document.createElement("div"); qt.className="qt"; qt.textContent=s.title||"";
    const qs=document.createElement("div"); qs.className="qs"; qs.textContent=s.uploader||"";
    box.appendChild(qt); box.appendChild(qs);
    row.appendChild(num); row.appendChild(img); row.appendChild(box); el.appendChild(row);
  });
}
function renderControls(d){
  const bar=document.getElementById("controls"), msg=document.getElementById("cmsg");
  bar.innerHTML=""; msg.textContent="";
  if(!S.controls) return;
  if(!S.loggedIn){
    const a=document.createElement("a"); a.className="login"; a.href=`/login?next=/np/${GID}`;
    a.textContent="Login with Discord to control"; bar.appendChild(a); return;
  }
  if(!S.canControl){ msg.textContent="Join the bot's voice channel to control playback."; return; }
  const mk=(label,fn,cls)=>{ const b=document.createElement("button"); b.textContent=label; if(cls)b.className=cls; b.onclick=fn; return b; };
  bar.appendChild(mk("⏮", ()=>control("previous")));
  bar.appendChild(mk(S.paused?"▶":"⏸", ()=>control(S.paused?"resume":"pause"), "primary"));
  bar.appendChild(mk("⏭", ()=>control("skip")));
  bar.appendChild(mk("⏹", ()=>control("stop")));
  const vol=document.createElement("input"); vol.type="range"; vol.min=0; vol.max=200; vol.value=d.volume||50;
  vol.className="vol"; vol.onchange=()=>control("volume", vol.value); bar.appendChild(vol);
}
async function control(action, value){
  if(!S.csrf) return;
  const body=new URLSearchParams({csrf:S.csrf, action});
  if(value!==undefined) body.set("value", value);
  try{ await fetch(`/api/np/${GID}/control`, {method:"POST", body}); }catch(e){}
  poll();
}
async function loadLyrics(){
  const box=document.getElementById("lyrics");
  box.innerHTML='<div class="muted">Loading lyrics…</div>'; S.lyrics=[];
  try{
    const r=await fetch(`/api/np/${GID}/lyrics`); const d=await r.json();
    if(d.synced){ S.lyrics=parseLRC(d.synced); renderLyricLines(); }
    else if(d.plain){ box.innerHTML=""; const p=document.createElement("div"); p.className="plain"; p.textContent=d.plain; box.appendChild(p); }
    else { box.innerHTML='<div class="muted">No synced lyrics found. <a href="https://genius.com/search?q='+encodeURIComponent(document.getElementById("title").textContent)+'" target="_blank" rel="noopener">Search Genius →</a></div>'; }
  }catch(e){ box.innerHTML='<div class="muted">Couldn\'t load lyrics.</div>'; }
}
function parseLRC(text){
  const out=[];
  for(const line of text.split("\n")){
    const tags=[...line.matchAll(/\[(\d+):(\d+)(?:\.(\d+))?\]/g)];
    const txt=line.replace(/\[[^\]]*\]/g,"").trim();
    for(const g of tags){ const t=(+g[1])*60+(+g[2])+(g[3]?+("0."+g[3]):0); out.push({t,txt}); }
  }
  return out.sort((a,b)=>a.t-b.t);
}
function renderLyricLines(){
  const box=document.getElementById("lyrics"); box.innerHTML="";
  S.lyrics.forEach((l,i)=>{ const d=document.createElement("div"); d.className="line"; d.id="ly"+i; d.textContent=l.txt||"♪"; box.appendChild(d); });
}
let lastActive=-1;
function tick(){
  let e=S.elapsed;
  if(S.playing && !S.paused && S.syncTs) e+=(performance.now()-S.syncTs)/1000;
  if(S.duration && e>S.duration) e=S.duration;
  document.getElementById("fill").style.width = S.duration? Math.min(100,(e/S.duration)*100)+"%":"0";
  document.getElementById("elapsed").textContent=fmt(e);
  document.getElementById("dur").textContent=fmt(S.duration);
  if(S.lyrics.length){
    let idx=-1; for(let i=0;i<S.lyrics.length;i++){ if(S.lyrics[i].t<=e) idx=i; else break; }
    if(idx!==lastActive){
      if(lastActive>=0){ const p=document.getElementById("ly"+lastActive); if(p)p.classList.remove("active"); }
      const c=document.getElementById("ly"+idx);
      if(c){ c.classList.add("active"); c.scrollIntoView({block:"center", behavior:"smooth"}); }
      lastActive=idx;
    }
  }
}
poll(); setInterval(poll, 2500); setInterval(tick, 250);
</script></body></html>"""


async def setup(bot: commands.Bot):
    """The entry point for loading the WebServer cog."""
    await bot.add_cog(WebServer(bot))
