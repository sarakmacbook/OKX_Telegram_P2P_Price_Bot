"""Serverless (Vercel) mode for the P2P price bot.

The polling installs (systemd, Docker, local) keep one long-lived process that
talks to Telegram and schedules its own jobs.  Vercel is the opposite: every
request is a separate Python invocation, so this module supplies what
``bot.py`` cannot do there:

``POST /api/webhook``   Telegram pushes each update here (verified with the
                        webhook secret token) and it is handed to the very same
                        handlers the polling bot uses.
``GET  /api/webhook``   Status page.  It also registers the webhook when it is
                        missing, so a fresh deployment starts working the moment
                        you open it.
``GET  /api/tick``      The Vercel Cron entry point that replaces PTB's
                        in-process JobQueue: post prices when they changed,
                        delete stale group messages, keep the webhook alive.

It also keeps one initialized PTB ``Application`` per warm container and re-reads
the shared state store before every request, because Vercel may run several
instances of the same deployment at once.

Only the ``api/*`` functions import this module — polling installs never touch it.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:                     # api/*.py lives one level down
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("p2p-bot.serverless")

WEBHOOK_PATH = "/api/webhook"
TICK_PATH = "/api/tick"
TELEGRAM_SECRET_HEADER = "x-telegram-bot-api-secret-token"
MARKER_KEY = "webhook"

Handler = Callable[["Request"], Awaitable["Response"]]


class ConfigError(RuntimeError):
    """The deployment is missing something it cannot work without."""


def env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names`` (Vercel offers several aliases)."""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return default


def truthy(value: str) -> bool:
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


# ── the bot module ─────────────────────────────────────────────────────────
_bot: Any = None


def get_bot():
    """Import ``bot.py`` (once) and turn a broken configuration into a readable error."""
    global _bot
    if _bot is not None:
        return _bot
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import bot as bot_module
    except SystemExit as exc:                     # bot.py calls sys.exit() on bad config
        detail = "" if exc.code in (None, 0, 1) else f": {exc.code}"
        raise ConfigError(
            f"bot.py refused to start{detail} — set BOT_TOKEN and ADMIN_IDS in the "
            "deployment's environment variables (see .env.example) and redeploy. "
            "A KV/Redis store is required too: without it Vercel forgets the group, "
            "the merchants and the prices between requests."
        ) from None
    except Exception as exc:                      # pragma: no cover - defensive
        raise ConfigError(f"bot.py could not be imported: {exc!r}") from exc
    _bot = bot_module
    return bot_module


def state_status(store: Any = None) -> dict:
    """What persists the group / merchants / settings — and whether that survives a deploy."""
    bot_module = get_bot()
    store = store if store is not None else bot_module.STORE
    backend = getattr(store, "backend", "none")
    persistent = backend == "redis"
    status = {"backend": backend, "detail": store.describe(), "persistent": persistent}
    if not persistent:
        status["warning"] = (
            "no persistent store: Vercel has no writable disk, so the group, the "
            "merchants and the prices are forgotten as soon as this instance is "
            "recycled. Connect a Redis/KV REST store (KV_REST_API_URL + "
            "KV_REST_API_TOKEN — e.g. the Upstash or Vercel KV integration)."
        )
    return status


def delete_webhook(bot_module=None) -> None:
    """Forget the registration bookkeeping (used when a webhook is removed)."""
    bot_module = bot_module or get_bot()
    try:
        bot_module.state.pop(MARKER_KEY, None)
        bot_module.save()
    except Exception:                             # pragma: no cover - best effort
        pass


# ── public URL / secrets ───────────────────────────────────────────────────
def public_base_url() -> str:
    """Public https URL of this deployment.

    Only platform-provided values are used — **never** the request's ``Host``
    header: a forged header must not be able to point your Telegram updates at a
    server somebody else controls (whoever answers the webhook sees every update
    the bot receives).
    """
    url = env("PUBLIC_URL", "P2P_PUBLIC_URL", "WEBHOOK_URL").rstrip("/")
    if not url:
        for var in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
            host = env(var).split("/")[0].strip()
            if host:
                url = f"https://{host}"
                break
    if not url:
        raise ConfigError(
            "Cannot tell where this deployment is reachable, so the webhook cannot be "
            "registered. Set PUBLIC_URL (e.g. https://my-bot.vercel.app) in the project's "
            "environment variables."
        )
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def webhook_secret() -> str:
    """Telegram's ``secret_token`` for the webhook.

    ``WEBHOOK_SECRET`` when you set one, otherwise a stable hash of the bot
    token — unguessable without the token, so there is nothing to configure.
    """
    explicit = env("WEBHOOK_SECRET")
    if explicit:
        return explicit
    return hashlib.sha256(f"p2p-price-bot:{get_bot().TOKEN}".encode()).hexdigest()[:32]


def webhook_secret_note() -> str:
    """Human readable answer to "where does the secret come from?" (for the status page)."""
    return ("WEBHOOK_SECRET environment variable" if env("WEBHOOK_SECRET")
            else "derived from BOT_TOKEN — set WEBHOOK_SECRET to use your own")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def matches(given: str, expected: str) -> bool:
    """Constant-time comparison of a shared secret (empty values never match)."""
    return bool(expected) and secrets.compare_digest(given.encode(), expected.encode())


# ── one PTB application per warm container ─────────────────────────────────
_apps: dict[int, Any] = {}
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    """A lock per event loop, dropping state that belongs to a dead loop."""
    key = id(loop)
    for stale in [k for k in _locks if k != key]:
        _locks.pop(stale, None)
        _apps.pop(stale, None)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


async def _application_locked(loop: asyncio.AbstractEventLoop):
    """Build/initialize the application — caller already holds ``_lock_for(loop)``."""
    app = _apps.get(id(loop))
    if app is not None:
        return app
    bot_module = get_bot()
    app = bot_module.build_application(polling=False)
    # Bot.initialize() calls getMe once: it verifies the token and caches the
    # bot's own user, so /start links work without a second API call.
    await app.initialize()
    try:
        bot_module.BOT_USERNAME = app.bot.bot.username
    except Exception as exc:                      # pragma: no cover - defensive
        log.warning("could not read the bot's own user: %s", exc)
    _apps[id(loop)] = app
    log.info("Serverless mode ready · %s/%s · state: %s", bot_module.ASSET, bot_module.FIAT,
             bot_module.STORE.describe())
    return app


async def get_application():
    """An initialized PTB ``Application`` bound to the current event loop.

    Vercel keeps a warm container — and its event loop — alive between
    invocations, so this normally builds the bot once.  Should a request arrive
    on a new loop, the application is rebuilt instead of failing with
    "attached to a different loop".
    """
    loop = asyncio.get_running_loop()
    async with _lock_for(loop):
        return await _application_locked(loop)


async def shutdown() -> None:
    """Close this container's application (and its HTTP client)."""
    for key, app in list(_apps.items()):
        _apps.pop(key, None)
        try:
            await app.shutdown()
        except Exception as exc:                  # pragma: no cover - best effort
            log.warning("shutdown failed: %s", exc)


# ── Telegram work ──────────────────────────────────────────────────────────
async def process_update(payload: dict) -> bool:
    """Hand one Telegram update (the decoded webhook body) to the bot handlers."""
    bot_module = get_bot()
    loop = asyncio.get_running_loop()
    async with _lock_for(loop):                   # one update at a time per container
        app = await _application_locked(loop)
        bot_module.refresh_state()                # another instance may have written meanwhile
        update = bot_module.Update.de_json(payload, app.bot)
        if update is None:
            log.warning("Ignoring update without an update_id: %s", payload)
            return False
        await app.process_update(update)
    return True


async def _webhook_target() -> str:
    return public_base_url() + WEBHOOK_PATH


def _webhook_summary(info: Any) -> dict:
    return {
        "url": getattr(info, "url", "") or "",
        "pending_updates": getattr(info, "pending_update_count", 0) or 0,
        "last_error": getattr(info, "last_error_message", None) or None,
    }


async def set_webhook(app=None) -> dict:
    """Point Telegram at this deployment (idempotent) and remember it did."""
    bot_module = get_bot()
    app = app or await get_application()
    target = await _webhook_target()
    secret = webhook_secret()
    await app.bot.set_webhook(
        url=target,
        secret_token=secret,
        allowed_updates=list(bot_module.Update.ALL_TYPES),
        drop_pending_updates=truthy(env("WEBHOOK_DROP_PENDING", default="1")),
    )
    bot_module.state[MARKER_KEY] = {"url": target, "secret_fp": _fingerprint(secret)}
    bot_module.save()
    log.info("Webhook registered: %s", target)
    return _webhook_summary(await app.bot.get_webhook_info())


async def ensure_webhook(force: bool = False, app=None) -> dict:
    """Re-register the webhook when it is missing, moved or registered with another secret.

    This is what makes the deployment self-healing: a new ``vercel.app`` domain
    after re-deploying, a rotated ``BOT_TOKEN`` or a deleted webhook are all
    fixed by the next cron run / status page visit.
    """
    bot_module = get_bot()
    app = app or await get_application()
    want = await _webhook_target()
    marker = bot_module.state.get(MARKER_KEY)
    marker = marker if isinstance(marker, dict) else {}
    info = await app.bot.get_webhook_info()
    stale = ((info.url or "").rstrip("/") != want
             or marker.get("secret_fp") != _fingerprint(webhook_secret()))
    if force or stale:
        return await set_webhook(app=app)
    return _webhook_summary(info)


async def run_tick() -> dict:
    """One round of the periodic work the polling bot does in its JobQueue."""
    bot_module = get_bot()
    loop = asyncio.get_running_loop()
    result: dict = {}
    async with _lock_for(loop):
        app = await _application_locked(loop)
        bot_module.refresh_state()
        try:
            result["webhook"] = await ensure_webhook(app=app)
        except ConfigError:
            raise
        except Exception as exc:                  # pragma: no cover - network
            result["webhook"] = {"error": str(exc)}
        result["posted"] = bool(await bot_module.auto_post_task(app.bot))
        result["deleted_stale_message"] = bool(await bot_module.cleanup_task(app.bot))
        result["auto"] = bool(bot_module.state.get("auto"))
        result["group"] = bot_module.state.get("group")
        result["merchants"] = len(bot_module.state.get("merchants") or {})
    result["state"] = state_status()
    return result


# ── ASGI plumbing (Vercel loads the ``app`` of every api/*.py) ─────────────
class Request:
    """The parts of an ASGI request these endpoints care about."""

    def __init__(self, scope: dict, body: bytes = b""):
        self.scope = scope
        self.body = body
        self.method = (scope.get("method") or "GET").upper()
        self.path = scope.get("path") or "/"
        self.headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                        for key, value in scope.get("headers") or []}
        self.query = {key: values[-1] for key, values
                      in parse_qs((scope.get("query_string") or b"").decode()).items()}

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def wants_html(self) -> bool:
        return "text/html" in self.header("accept", "")

    def json(self) -> dict:
        try:
            data = json.loads(self.body or b"{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}


class Response:
    """A minimal ASGI response."""

    def __init__(self, body: str | bytes = "", status: int = 200,
                 content_type: str = "application/json; charset=utf-8",
                 headers: dict | None = None):
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = {"content-type": content_type, "cache-control": "no-store",
                        **(headers or {})}

    @classmethod
    def json(cls, data: dict, status: int = 200) -> "Response":
        return cls(json.dumps(data, indent=1, default=str), status=status)

    @classmethod
    def html(cls, markup: str, status: int = 200) -> "Response":
        return cls(markup, status=status, content_type="text/html; charset=utf-8")


async def read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        chunks.append(message.get("body") or b"")
        if not message.get("more_body"):
            break
    return b"".join(chunks)


async def send_response(send, response: Response) -> None:
    await send({"type": "http.response.start", "status": response.status,
                "headers": [(k.encode(), v.encode()) for k, v in response.headers.items()]})
    await send({"type": "http.response.body", "body": response.body})


async def _lifespan(receive, send) -> None:
    """Answer the runtime's lifespan messages (startup warm-up / graceful close)."""
    while True:
        message = await receive()
        if message.get("type") == "lifespan.startup":
            try:
                await get_application()           # fail early, and warm the bot up
                log.info("startup complete")
            except Exception as exc:
                # Answer anyway: the per-request error page tells the user what is missing
                log.error("startup failed: %s", exc)
            await send({"type": "lifespan.startup.complete"})
        elif message.get("type") == "lifespan.shutdown":
            await shutdown()
            await send({"type": "lifespan.shutdown.complete"})
            return


async def asgi_dispatch(scope, receive, send, handler: Handler) -> None:
    """Glue between Vercel's ASGI runtime and a per-endpoint ``handle()``."""
    if scope.get("type") == "lifespan":
        await _lifespan(receive, send)
        return
    if scope.get("type") != "http":
        await send_response(send, Response.json({"ok": False, "error": "unsupported request type"}, 400))
        return
    try:
        response = await handler(Request(scope, await read_body(receive)))
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        response = Response.json(
            {"ok": False, "error": str(exc),
             "hint": ("Set BOT_TOKEN, ADMIN_IDS and a KV/Redis store "
                      "(KV_REST_API_URL + KV_REST_API_TOKEN) in the project's environment "
                      "variables, then redeploy.")}, 500)
    except Exception as exc:                      # pragma: no cover - defensive
        log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"))
        response = Response.json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
    await send_response(send, response)


# ── tiny HTML page (the status endpoint, opened in a browser) ──────────────
def page(title: str, rows: list[tuple[str, Any]], notes: list[str] | None = None,
         warnings: list[str] | None = None) -> str:
    """A self-contained status page — no CSS files, no JavaScript, dark-mode aware."""
    def cell(value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if value is None or value == "":
            return "—"
        return html.escape(str(value))

    body = "\n".join(
        f"    <tr><th>{html.escape(str(name))}</th><td>{cell(value)}</td></tr>"
        for name, value in rows)
    warn = "".join(f'\n    <p class="warn">{html.escape(w)}</p>' for w in warnings or [])
    extra = "".join(f"\n    <p>{html.escape(n)}</p>" for n in notes or [])
    return f"""<!DOCTYPE html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 46rem; padding: 0 1rem; }}
  h1 {{ font-size: 1.35rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #8883; vertical-align: top; }}
  th {{ width: 12rem; font-weight: 600; opacity: .75; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .warn {{ border-left: 4px solid #d97706; padding-left: .7rem; }}
</style>
<h1>{html.escape(title)}</h1>
<table>
{body}
</table>{warn}{extra}
</html>
"""
