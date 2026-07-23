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
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

from cogs.TaskBoard import Task

# HTML/CSS lives in cogs/templates so this module stays Python.
TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")



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
        # access_log=None: the now-playing page polls every couple of seconds per
        # viewer, which otherwise floods the bot's logs with one line per request.
        self._runner = web.AppRunner(app, access_log=None)
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
        return _render_page((
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
        return _render_page(header + banner + "".join(sections))

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


_PAGE = _load_template("dashboard.html")
_PLAYER_PAGE = _load_template("player.html")


def _render_page(body: str) -> str:
    """Wraps rendered body HTML in the shared dashboard shell."""
    return _PAGE.replace("__BODY__", body)





async def setup(bot: commands.Bot):
    """The entry point for loading the WebServer cog."""
    await bot.add_cog(WebServer(bot))
