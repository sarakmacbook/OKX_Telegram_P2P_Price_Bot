"""Serverless (Vercel) mode for the P2P price bot.

The polling installs (systemd, Docker, local) keep one long-lived process that
talks to Telegram and schedules its own jobs.  Vercel is the opposite: every
request is a separate Python invocation, so this module supplies what
``bot.py`` cannot do there:

``POST /api/webhook``   Telegram pushes each update here (verified with the
                        webhook secret token) and it is handed to the very same
                        handlers the polling bot uses.
``GET  /api/webhook``   First-start setup UI when configuration is missing;
                        otherwise a status page. It registers the webhook when it
                        is missing, so a fresh deployment starts working the
                        moment you open it.
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
    """Import ``bot.py`` (once) and turn a broken configuration into a readable error.

    The preflight also stops a Vercel cron/webhook request from silently using an
    ephemeral file store when the owner forgot to connect KV/Redis.  Local
    ``config.json`` installs remain supported by the exception below.
    """
    global _bot
    setup = setup_status()
    if not setup["ready"] and not (not setup["serverless"] and
                                     _local_config_has_credentials()):
        raise ConfigError(setup["message"] or CONFIG_HINT)
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
        """Whether the caller is a browser that can render the setup/status UI."""
        return "text/html" in self.header("accept", "").lower()

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


CONFIG_HINT = ("Set BOT_TOKEN, ADMIN_IDS and a KV/Redis store "
               "(KV_REST_API_URL + KV_REST_API_TOKEN) in the project's environment "
               "variables, then redeploy.")

# Keep these checks in the serverless layer instead of importing bot.py first.  A
# missing environment variable makes bot.py intentionally call sys.exit() (that
# is useful for a polling install), but doing that before rendering the first-run
# page would make the browser see an opaque platform 500.  The setup UI can be
# rendered without importing python-telegram-bot or touching the state store.
_REDIS_ENV_PAIRS = (
    ("KV_REST_API_URL", "KV_REST_API_TOKEN", "Vercel KV / Upstash"),
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "Upstash Redis"),
    ("REDIS_REST_URL", "REDIS_REST_TOKEN", "Redis REST"),
)


def _looks_like_vercel() -> bool:
    """Return whether this request is running in a Vercel deployment.

    ``VERCEL`` is the normal flag.  The URL/environment fallbacks make the
    preflight useful in a locally emulated Vercel function as well, without
    making a normal polling install require Redis.
    """
    return bool(env("VERCEL", "VERCEL_ENV", "VERCEL_URL",
                    "VERCEL_PROJECT_PRODUCTION_URL"))


def _local_config_has_credentials() -> bool:
    """Preserve the config.json path for local/VM deployments.

    Vercel deployments build their configuration from environment variables, but
    ``uvicorn api.webhook:app`` is also documented as a local debugging option.
    Do not replace a valid local config.json with the Vercel-only env wizard.
    """
    if _looks_like_vercel():
        return False
    candidate = Path(env("P2P_CONFIG_FILE") or (ROOT / "config.json")).expanduser()
    try:
        data = json.loads(candidate.read_text())
    except Exception:
        return False
    return bool(data.get("token") and data.get("admins")) if isinstance(data, dict) else False


def _setup_check(name: str, label: str, required: bool, ok: bool,
                 detail: str, variables: tuple[str, ...]) -> dict:
    """Create a public, secret-free item for the setup checklist."""
    return {
        "name": name,
        "label": label,
        "required": required,
        "ok": bool(ok),
        "status": "ready" if ok else ("required" if required else "recommended"),
        "detail": detail,
        "variables": list(variables),
    }


def setup_status() -> dict:
    """Inspect first-start requirements without importing the bot.

    The returned object deliberately contains no token, admin id, Redis URL, or
    other secret.  It is safe to include in the JSON diagnostics response and in
    the browser UI.  KV is a hard requirement only when the code is actually
    running on Vercel; file state remains the correct default for VPS/Docker.
    """
    token = env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN")
    admins = env("ADMIN_IDS", "ADMINS")
    on_vercel = _looks_like_vercel()

    token_ok = bool(token and token.count(":") == 1 and token.split(":", 1)[0].isdigit())
    if token_ok:
        token_detail = "BOT_TOKEN is present."
    elif token:
        token_detail = "BOT_TOKEN is present but does not look like a Telegram token (it should contain a numeric id followed by a colon)."
    else:
        token_detail = "Add the token from @BotFather → /newbot."

    admin_parts = [part.strip() for part in admins.split(",") if part.strip()]
    admins_ok = bool(admin_parts) and all(part.isdigit() for part in admin_parts)
    if admins_ok:
        admins_detail = "ADMIN_IDS is present."
    elif admins:
        admins_detail = "Use one or more numeric Telegram IDs separated by commas."
    else:
        admins_detail = "Add your numeric ID from @userinfobot."

    complete_pairs = []
    partial_pairs = []
    for url_name, token_name, provider in _REDIS_ENV_PAIRS:
        has_url = bool(env(url_name))
        has_token = bool(env(token_name))
        if has_url and has_token:
            complete_pairs.append(provider)
        elif has_url or has_token:
            partial_pairs.append((url_name, token_name))
    if complete_pairs:
        redis_ok = True
        redis_detail = f"Connected through {complete_pairs[0]}."
    elif partial_pairs:
        redis_ok = False
        redis_detail = "Both the REST URL and REST token are required."
    else:
        redis_ok = False
        redis_detail = "Connect Upstash for Redis or Vercel KV so group, merchant, and price state survives requests."

    checks = [
        _setup_check("bot_token", "Telegram bot token", True, token_ok,
                     token_detail, ("BOT_TOKEN",)),
        _setup_check("admin_ids", "Telegram admin ID", True, admins_ok,
                     admins_detail, ("ADMIN_IDS",)),
        _setup_check("state_store", "Persistent state store", on_vercel, redis_ok,
                     redis_detail, ("KV_REST_API_URL", "KV_REST_API_TOKEN")),
    ]
    blocking = [check for check in checks if check["required"] and not check["ok"]]
    if blocking:
        names = ", ".join(check["variables"][0] for check in blocking)
        if any(check["name"] in ("bot_token", "admin_ids") for check in blocking):
            message = ("bot.py refused to start — set BOT_TOKEN and ADMIN_IDS in the "
                       "deployment's environment variables (see .env.example) and redeploy. "
                       "A KV/Redis store is required too: without it Vercel forgets the group, "
                       "the merchants and the prices between requests.")
        else:
            message = ("The deployment is missing a persistent KV/Redis store — set "
                       "KV_REST_API_URL and KV_REST_API_TOKEN by connecting Upstash for Redis "
                       "or Vercel KV, then redeploy.")
    else:
        names = ""
        message = ""

    # PUBLIC_URL is not normally needed on Vercel: VERCEL_URL is supplied by the
    # platform.  Keep it as a non-blocking diagnostic so custom/local deployments
    # get a useful explanation instead of a mysterious webhook error later.
    has_public_url = bool(env("PUBLIC_URL", "P2P_PUBLIC_URL", "WEBHOOK_URL",
                              "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"))
    checks.append(_setup_check(
        "public_url", "Webhook public URL", False, has_public_url,
        "A public URL was detected." if has_public_url else
        "Optional on Vercel; set PUBLIC_URL when running behind a custom or local URL.",
        ("PUBLIC_URL",)))
    checks.append(_setup_check(
        "cron_secret", "Cron endpoint secret", False, bool(env("CRON_SECRET")),
        "CRON_SECRET protects /api/tick." if env("CRON_SECRET") else
        "Recommended: set CRON_SECRET so nobody else can trigger a price post.",
        ("CRON_SECRET",)))

    return {
        "ready": not blocking,
        "serverless": on_vercel,
        "checks": checks,
        "missing": names,
        "required_missing": [check["name"] for check in blocking],
        "message": message,
    }


SETUP_STEPS = [
    "1. Vercel dashboard → your project → Settings → Environment Variables: set BOT_TOKEN "
    "(from @BotFather) and ADMIN_IDS (your Telegram id, from @userinfobot).",
    "2. Storage → add Upstash for Redis (or Vercel KV) → Connect to this project — this sets "
    "KV_REST_API_URL + KV_REST_API_TOKEN so the bot remembers its group and merchants.",
    "3. Redeploy (Deployments → ⋯ → Redeploy — changed variables only apply to new deploys), "
    "then reopen this page: it registers the Telegram webhook by itself.",
]


def _setup_page(message: str, status: dict) -> str:
    """Render a polished, actionable first-start web UI.

    This intentionally does not contain a form for secrets. Vercel environment
    variables are immutable from inside a function, and accepting a bot token in
    a public page would be unsafe. The UI links to the place where the owner can
    set them, gives a redacted readiness checklist, and can be refreshed after a
    redeploy.
    """
    checks = status.get("checks") or []
    cards = []
    for check in checks:
        if check.get("ok"):
            icon, badge, css = "✓", "Ready", "ready"
        elif check.get("required"):
            icon, badge, css = "!", "Needs action", "required"
        else:
            icon, badge, css = "·", "Recommended", "optional"
        variables = " · ".join(f"<code>{html.escape(str(item))}</code>"
                              for item in check.get("variables") or [])
        cards.append(
            f'<article class="check {css}">'
            f'<span class="check-icon" aria-hidden="true">{icon}</span>'
            f'<div class="check-copy"><h3>{html.escape(str(check.get("label", "")))}</h3>'
            f'<p>{html.escape(str(check.get("detail", "")))}</p>'
            f'<small>{variables}</small></div>'
            f'<span class="badge">{badge}</span></article>')

    checks_markup = "\n".join(cards)
    missing = status.get("missing") or "the required environment variables"
    deployment_note = (
        "This deployment is running on Vercel, so persistent storage is required."
        if status.get("serverless") else
        "The checklist also works locally; file storage is supported outside Vercel."
    )
    # All links are fixed, platform/documentation links or same-origin paths.
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0b1020">
  <title>Setup needed · P2P Price Bot</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #090d19; --panel: #11182a; --panel-2: #17213a;
      --text: #f4f7ff; --muted: #aab6d1; --line: #2a3655;
      --accent: #7c8cff; --accent-2: #a78bfa; --green: #35d399;
      --amber: #f7b955; --red: #ff8c8c; --shadow: 0 24px 70px #0007;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; color: var(--text);
      background: radial-gradient(circle at 80% -10%, #3346a833, transparent 45%), var(--bg);
      font: 15px/1.6 Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    a {{ color: #c7ceff; }}
    .shell {{ width: min(960px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0 56px; }}
    .brand {{ display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }}
    .logo {{ display: grid; place-items: center; width: 45px; height: 45px; border-radius: 14px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2)); font-size: 24px;
      box-shadow: 0 10px 30px #7c8cff44; }}
    .eyebrow {{ margin: 0; color: #aab5ff; font-size: 11px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }}
    .brand h1 {{ margin: 2px 0 0; font-size: clamp(20px, 3vw, 27px); letter-spacing: -.02em; }}
    .hero, .panel {{ border: 1px solid var(--line); border-radius: 22px; background: #11182af2; box-shadow: var(--shadow); }}
    .hero {{ display: grid; grid-template-columns: auto 1fr; gap: 20px; padding: clamp(22px, 5vw, 38px); margin-bottom: 18px; }}
    .hero-symbol {{ display: grid; place-items: center; width: 62px; height: 62px; border-radius: 18px;
      background: #f7b95518; border: 1px solid #f7b95555; font-size: 31px; }}
    .hero h2 {{ margin: 0 0 8px; font-size: clamp(23px, 4vw, 34px); line-height: 1.15; letter-spacing: -.035em; }}
    .hero p {{ margin: 0; color: var(--muted); max-width: 700px; }}
    .hero strong {{ color: var(--text); }}
    .panel {{ padding: clamp(20px, 4vw, 30px); margin-top: 18px; }}
    .panel-heading {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }}
    h2 {{ margin: 0; font-size: 20px; letter-spacing: -.02em; }}
    .subtle {{ color: var(--muted); margin: 4px 0 0; }}
    .checks {{ display: grid; gap: 10px; }}
    .check {{ display: flex; align-items: flex-start; gap: 13px; padding: 15px; border-radius: 15px; background: var(--panel-2); border: 1px solid var(--line); }}
    .check-icon {{ flex: 0 0 auto; display: grid; place-items: center; width: 27px; height: 27px; border-radius: 50%; font-weight: 900; }}
    .ready .check-icon {{ color: #06261a; background: var(--green); }}
    .required .check-icon {{ color: #35100e; background: var(--red); }}
    .optional .check-icon {{ color: #3b2700; background: var(--amber); }}
    .check-copy {{ min-width: 0; flex: 1; }}
    .check h3 {{ margin: 0; font-size: 15px; }}
    .check p {{ color: var(--muted); margin: 2px 0 4px; }}
    .check small {{ color: #c9d0e5; }}
    .badge {{ flex: 0 0 auto; margin-top: 2px; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 750; white-space: nowrap; }}
    .ready .badge {{ color: #73f0bf; background: #35d3991a; }}
    .required .badge {{ color: #ffabab; background: #ff8c8c1a; }}
    .optional .badge {{ color: #ffd27c; background: #f7b9551a; }}
    code {{ color: #d8dcff; background: #090d1977; border: 1px solid #3b4770; border-radius: 6px; padding: 1px 5px; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .steps {{ display: grid; gap: 11px; margin-top: 18px; }}
    .step {{ display: grid; grid-template-columns: 34px 1fr; gap: 13px; padding: 17px; border: 1px solid var(--line); border-radius: 15px; background: #11182a99; }}
    .number {{ display: grid; place-items: center; width: 30px; height: 30px; border-radius: 10px; color: white; background: linear-gradient(135deg, var(--accent), var(--accent-2)); font-weight: 850; }}
    .step h3 {{ margin: 0 0 4px; font-size: 15px; }}
    .step p {{ margin: 0; color: var(--muted); }}
    .step a {{ display: inline-block; margin-top: 9px; font-weight: 700; text-decoration: none; }}
    .why {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 11px; margin-top: 18px; }}
    .why article {{ padding: 17px; border: 1px solid var(--line); border-radius: 15px; background: #11182a99; }}
    .why h3 {{ margin: 0 0 5px; font-size: 14px; }}
    .why p {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }}
    .button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 42px; padding: 9px 15px; border-radius: 11px; color: white; background: linear-gradient(135deg, #6676f5, #8a67df); font-weight: 800; text-decoration: none; box-shadow: 0 8px 22px #6676f533; }}
    .button.secondary {{ color: #d4daff; background: #1b2642; border: 1px solid #3b4770; box-shadow: none; }}
    .foot {{ color: #7f8ba8; margin: 24px 2px 0; font-size: 13px; }}
    @media (max-width: 650px) {{ .shell {{ width: min(100% - 22px, 960px); padding-top: 24px; }} .hero {{ grid-template-columns: 1fr; gap: 13px; }} .panel-heading {{ display: block; }} .badge {{ margin-left: auto; }} .why {{ grid-template-columns: 1fr; }} .check {{ flex-wrap: wrap; }} .check-copy {{ min-width: calc(100% - 42px); }} .check .badge {{ margin-left: 40px; }} }}
    @media (prefers-reduced-motion: no-preference) {{ .hero {{ animation: rise .35s ease-out both; }} @keyframes rise {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: none; }} }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="brand"><div class="logo" aria-hidden="true">🤖</div><div><p class="eyebrow">First deployment</p><h1>P2P Price Bot</h1></div></header>
    <section class="hero" aria-labelledby="setup-title">
      <div class="hero-symbol" aria-hidden="true">⚙️</div>
      <div><h2 id="setup-title">Setup needed</h2>
        <p><strong>What happened:</strong> the bot is not running yet. {html.escape(message)}</p>
        <p style="margin-top:10px"><strong>Fix:</strong> set the variables shown below in the deployment, then redeploy.</p>
        <p style="margin-top:10px">{html.escape(deployment_note)} Missing: <code>{html.escape(str(missing))}</code></p>
      </div>
    </section>

    <section class="panel" aria-labelledby="check-title">
      <div class="panel-heading"><div><h2 id="check-title">Environment checklist</h2><p class="subtle">Values are checked without displaying any secrets.</p></div></div>
      <div class="checks">{checks_markup}</div>
    </section>

    <section class="panel" aria-labelledby="steps-title">
      <h2 id="steps-title">Finish setup in Vercel</h2>
      <div class="steps">
        <article class="step"><span class="number">1</span><div><h3>Set the Telegram credentials</h3><p>In <b>Vercel dashboard → your project → Settings → Environment Variables</b>, set <code>BOT_TOKEN</code> from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> and <code>ADMIN_IDS</code> from <a href="https://t.me/userinfobot" target="_blank" rel="noopener">@userinfobot</a>.</p><a href="https://vercel.com/dashboard" target="_blank" rel="noopener">Open Vercel dashboard ↗</a></div></article>
        <article class="step"><span class="number">2</span><div><h3>Connect persistent storage</h3><p>Open <b>Storage</b> → add <b>Upstash for Redis</b> (or Vercel KV) → <b>Connect to this project</b>. This sets <code>KV_REST_API_URL</code> + <code>KV_REST_API_TOKEN</code> so the bot remembers its group, merchants, and prices between requests.</p></div></article>
        <article class="step"><span class="number">3</span><div><h3>Redeploy, then return here</h3><p>Go to <b>Deployments → ⋯ → Redeploy</b>. Changed variables only apply to new deployments. Reopen this page after the redeploy; it registers the Telegram webhook automatically.</p></div></article>
      </div>
    </section>

    <section class="why" aria-label="Why these settings are needed">
      <article><h3>🔐 Bot credentials</h3><p>Telegram uses the token to deliver updates and the admin ID to protect the control panel.</p></article>
      <article><h3>💾 Persistent state</h3><p>Vercel functions are short-lived. Redis keeps the group, merchants, settings, and prices available on every request.</p></article>
      <article><h3>🔗 Automatic webhook</h3><p>Once the variables are present, opening this page checks the token and registers <code>/api/webhook</code>.</p></article>
    </section>

    <div class="actions"><a class="button" href="/api/webhook">↻ Check setup again</a><a class="button secondary" href="/api/webhook?register=0">View diagnostics</a><a class="button secondary" href="https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/blob/main/VERCEL.md" target="_blank" rel="noopener">Deployment guide ↗</a></div>
    <p class="foot">Secrets are read from the deployment environment only; this page never asks you to paste a bot token into the browser.</p>
  </main>
</body>
</html>"""


def config_error_response(request: Request, message: str,
                          status: dict | None = None) -> Response:
    """A broken deployment: JSON for scripts, a setup guide for browsers."""
    status = status or setup_status()
    if request.wants_html:
        return Response.html(_setup_page(message, status), status=500)
    return Response.json({"ok": False, "error": message, "hint": CONFIG_HINT,
                          "status": "setup_required", "setup": status}, 500)


def internal_error_response(request: Request, message: str) -> Response:
    """An unexpected crash: JSON for scripts, a readable page for browsers."""
    if request.wants_html:
        return Response.html(
            page("❌ Something went wrong · P2P Price Bot",
                 [("Error", message)],
                 notes=["Check the Runtime Logs (Vercel dashboard → your project → Logs) for the "
                        "traceback, then open /api/webhook again — it re-registers the webhook "
                        "by itself."]),
            status=500)
    return Response.json({"ok": False, "error": message}, 500)


async def asgi_dispatch(scope, receive, send, handler: Handler) -> None:
    """Glue between Vercel's ASGI runtime and a per-endpoint ``handle()``."""
    if scope.get("type") == "lifespan":
        await _lifespan(receive, send)
        return
    if scope.get("type") != "http":
        await send_response(send, Response.json({"ok": False, "error": "unsupported request type"}, 400))
        return
    try:
        body = await read_body(receive)
    except Exception:
        body = b""
    request = Request(scope, body)
    try:
        response = await handler(request)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        response = config_error_response(request, str(exc))
    except Exception as exc:                      # pragma: no cover - defensive
        log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"))
        response = internal_error_response(request, f"{type(exc).__name__}: {exc}")
    await send_response(send, response)


# ── self-contained HTML pages (the status endpoint, opened in a browser) ───
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
    warn = "".join(f'\n    <p class="warn">⚠️ {html.escape(w)}</p>' for w in warnings or [])
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
  .warn {{ border-left: 4px solid #d97706; background: #d9770622; padding: .5rem .7rem; border-radius: 0 .4rem .4rem 0; }}
</style>
<h1>{html.escape(title)}</h1>{warn}
<table>
{body}
</table>{extra}
</html>
"""
