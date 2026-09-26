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
``GET|POST /api/setup`` The same first-start page, plus the form that stores
                        what is missing (``runtime_config``) without a redeploy —
                        the browser twin of ``python setup_cli.py``.

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

import runtime_config                              # noqa: E402  (setup page + setup_cli.py)
import storage                                     # noqa: E402

log = logging.getLogger("p2p-bot.serverless")

WEBHOOK_PATH = "/api/webhook"
TICK_PATH = "/api/tick"
SETUP_PATH = "/api/setup"
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
        # serverless has no JobQueue: expired anti-scam checks are swept here
        result["captcha_expired"] = int(await bot_module.sweep_captcha(app.bot))
        result["auto"] = bool(bot_module.state.get("auto"))
        result["group"] = bot_module.state.get("group")
        result["channel"] = bot_module.state.get("channel")
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

    def form(self) -> dict:
        """A ``application/x-www-form-urlencoded`` body — the setup page's form."""
        if "application/x-www-form-urlencoded" not in self.header("content-type", "").lower():
            return {}
        return {key: values[-1] for key, values
                in parse_qs(self.body.decode("utf-8", "replace"),
                            keep_blank_values=True).items()}

    def submitted(self) -> dict:
        """Everything the client sent as data (form fields win over a JSON body)."""
        return {**self.json(), **self.form()}


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
                 detail: str, variables: tuple[str, ...], source: str = "") -> dict:
    """Create a public, secret-free item for the setup checklist."""
    return {
        "name": name,
        "label": label,
        "required": required,
        "ok": bool(ok),
        "status": "ready" if ok else ("required" if required else "recommended"),
        "detail": detail,
        "variables": list(variables),
        "source": source,
    }


def setup_status() -> dict:
    """Inspect first-start requirements without importing the bot.

    Settings that were saved on ``/api/setup`` or by ``python setup_cli.py`` count
    as present: ``runtime_config.apply`` copies them into the environment before
    anything reads it, which is also what makes the deployment work without a
    redeploy.

    The returned object deliberately contains no token, admin id, Redis URL, or
    other secret.  It is safe to include in the JSON diagnostics response and in
    the browser UI.  KV is a hard requirement only when the code is actually
    running on Vercel; file state remains the correct default for VPS/Docker.
    """
    stored = runtime_config.load(ROOT)
    runtime_config.apply(ROOT)                  # environment variables still win
    from_store = runtime_config.injected()

    def source(name: str) -> str:
        """Where a value came from — the dashboard, or the setup page/wizard."""
        return "setup" if name in stored and name in from_store else "environment"

    token = env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN")
    admins = env("ADMIN_IDS", "ADMINS")
    on_vercel = _looks_like_vercel()

    token_ok = bool(token and token.count(":") == 1 and token.split(":", 1)[0].isdigit())
    if token_ok:
        token_detail = ("BOT_TOKEN is present (saved through the setup page)."
                        if source("BOT_TOKEN") == "setup" else "BOT_TOKEN is present.")
    elif token:
        token_detail = "BOT_TOKEN is present but does not look like a Telegram token (it should contain a numeric id followed by a colon)."
    else:
        token_detail = "Add the token from @BotFather → /newbot."

    admin_parts = [part.strip() for part in admins.split(",") if part.strip()]
    admins_ok = bool(admin_parts) and all(part.isdigit() for part in admin_parts)
    if admins_ok:
        admins_detail = ("ADMIN_IDS is present (saved through the setup page)."
                         if source("ADMIN_IDS") == "setup" else "ADMIN_IDS is present.")
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

    # A complete KV pair is not enough when the owner explicitly chose the file
    # database: honour that choice rather than silently switching databases.
    # Vercel has no durable file system, so a file choice there is a blocking
    # misconfiguration even when Redis credentials happen to be available.
    selected_backend = storage.state_backend()
    if selected_backend == "redis":
        state_ok = redis_ok
        state_detail = (redis_detail if redis_ok else
                        "P2P_STATE_BACKEND=redis is selected, but " + redis_detail)
    elif selected_backend == "file":
        state_ok = not on_vercel
        state_detail = ("P2P_STATE_BACKEND=file is selected; state is kept in data.json."
                        if state_ok else
                        "P2P_STATE_BACKEND=file is selected, but Vercel discards file state. "
                        "Choose redis and connect a KV/Redis REST store.")
    else:
        state_ok = redis_ok if on_vercel else True
        state_detail = (redis_detail if redis_ok else
                        ("Auto mode will use data.json on this host. Set "
                         "P2P_STATE_BACKEND=redis and connect a KV store to share state."))

    checks = [
        _setup_check("bot_token", "Telegram bot token", True, token_ok,
                     token_detail, ("BOT_TOKEN",), source("BOT_TOKEN")),
        _setup_check("admin_ids", "Telegram admin ID", True, admins_ok,
                     admins_detail, ("ADMIN_IDS",), source("ADMIN_IDS")),
        _setup_check("state_store", "State database", on_vercel, state_ok,
                     state_detail, ("KV_REST_API_URL", "KV_REST_API_TOKEN", "P2P_STATE_BACKEND")),
    ]
    blocking = [check for check in checks if check["required"] and not check["ok"]]
    if blocking:
        names = ", ".join(check["variables"][0] for check in blocking)
        if any(check["name"] in ("bot_token", "admin_ids") for check in blocking):
            message = ("bot.py refused to start — BOT_TOKEN and ADMIN_IDS are missing. Add them "
                       f"in the form on {SETUP_PATH}, or run  python setup_cli.py  in a terminal, "
                       "or set them in the deployment's environment variables and redeploy. "
                       "A KV/Redis store is required too: without it Vercel forgets the group, "
                       "the merchants and the prices between requests.")
        else:
            message = ("The deployment is missing a persistent KV/Redis store — set "
                       "KV_REST_API_URL and KV_REST_API_TOKEN by connecting Upstash for Redis "
                       f"or Vercel KV (the form on {SETUP_PATH} accepts them as well, but only a "
                       "redeploy makes them apply to every instance).")
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
        "Recommended: set CRON_SECRET so nobody else can trigger a price post "
        "(environment variable only — Vercel's cron sends it from there).",
        ("CRON_SECRET",)))

    configured = bool(token) and bool(admins)
    locked = bool(env("SETUP_SECRET")) or not blocking
    return {
        "ready": not blocking,
        "serverless": on_vercel,
        "checks": checks,
        "missing": names,
        "required_missing": [check["name"] for check in blocking],
        "message": message,
        # what the browser form may do, and what it already holds (redacted)
        "setup_path": SETUP_PATH,
        "runtime": runtime_config.summary(ROOT),
        "setup_secret": bool(env("SETUP_SECRET")),
        "configured": configured,
        "locked": locked,
    }


SETUP_STEPS = [
    f"1. Add BOT_TOKEN (from @BotFather) and ADMIN_IDS (your Telegram id, from @userinfobot) — "
    f"in the form on {SETUP_PATH}, with  python setup_cli.py  in a terminal, or in the Vercel "
    "dashboard (Settings → Environment Variables).",
    f"2. Connect a database ({storage.database_link()}): Storage → add Upstash for Redis (or "
    "Vercel KV) → Connect to this project — this sets KV_REST_API_URL + KV_REST_API_TOKEN so the "
    "bot remembers its group and merchants.",
    "3. Redeploy (Deployments → ⋯ → Redeploy — changed *environment variables* only apply to new "
    f"deploys), then reopen this page: it registers the Telegram webhook by itself. Settings saved "
    f"on {SETUP_PATH} are applied immediately, no redeploy needed.",
]


# ── saving the required settings (browser form + terminal wizard) ──────────
def _bearer(request: "Request") -> str:
    header = request.header("authorization")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def setup_write_allowed(request: "Request", status: dict | None = None) -> tuple[bool, str]:
    """Who may store settings through ``POST /api/setup``.

    The page is public, so the rule is deliberately blunt:

    * ``SETUP_SECRET`` set → the request must carry it (form field, ``?secret=``
      or ``Authorization: Bearer``).  This is the recommended way to lock the
      form *before* the first deployment.
    * the deployment is **not** ready yet → anybody may finish the setup.  It is
      unusable until then, and stored values never overrule environment
      variables, so there is nothing to take over.
    * the deployment **is** ready → refused.  An anonymous form must not be able
      to point a running bot at somebody else's admin id.
    """
    status = status or setup_status()
    secret = env("SETUP_SECRET")
    if secret:
        given = request.form().get("SETUP_SECRET") or request.query.get("secret") or _bearer(request)
        if matches(given, secret):
            return True, ""
        return False, ("SETUP_SECRET is set for this deployment — send it with the request "
                       "(form field, ?secret= or 'Authorization: Bearer') to change the settings.")
    if status["ready"]:
        return False, ("The deployment is configured already, so the form is locked. Set "
                       "SETUP_SECRET in the environment to allow changes from the browser, or "
                       "run  python setup_cli.py  in a terminal — it writes to the same store.")
    return True, ""


def _probe_redis(url: str, token: str) -> tuple[bool, str]:
    """Whether a KV/Redis REST endpoint answers — before anything is written to it."""
    import httpx

    try:
        reply = httpx.post(url, json=["GET", runtime_config.config_key()], timeout=6.0,
                           headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"})
        reply.raise_for_status()
        return True, "the store answered"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _refresh_store() -> None:
    """Rebuild the selected store after setup changes it.

    A browser submission can add Redis credentials or switch the explicit
    ``P2P_STATE_BACKEND`` choice after ``bot.py`` was imported.  Carry the
    in-memory state into the newly selected database so changing stores does
    not make an already configured group/merchant list disappear.  Saving is
    best-effort, like every store write in ``storage.py``.
    """
    if _bot is None:
        return
    try:
        previous = _bot.STORE
        replacement = storage.build_store(ROOT)
        _bot.STORE = replacement
        if previous.describe() != replacement.describe():
            replacement.save(dict(_bot.state))
            log.info("state store switched from %s to %s", previous.describe(),
                     replacement.describe())
        else:
            log.info("state store rebuilt: %s", replacement.describe())
    except Exception as exc:                      # pragma: no cover - defensive
        log.warning("could not rebuild the state store: %s", exc)


def _next_steps(status: dict) -> list[str]:
    """What to do after a save — the page and the JSON answer say the same thing."""
    if not status["ready"]:
        return [f"Still missing: {status['missing']} — add it here, or in a terminal "
                f"(python setup_cli.py), then reopen {WEBHOOK_PATH}."]
    steps = [f"Open {WEBHOOK_PATH} — it registers the Telegram webhook and shows the status page."]
    if not status["runtime"]["persistent"] and status["serverless"]:
        steps.append("Connect Upstash for Redis (or Vercel KV) and redeploy, or everything — "
                     "including these settings — is forgotten with this instance.")
    steps.append("In Telegram: /start → 👥 Set group → paste a merchant URL → 🟢 Auto: ON.")
    return steps


def save_setup_values(values: dict, verify: bool = True) -> dict:
    """Store what the browser form (or ``setup_cli.py``) submitted.

    Returns a public, secret-free report: what was saved (redacted), where it was
    saved, what is still missing, and what to do next.  Values that are already
    set in the deployment environment are **not** overruled — the report says so
    instead, because that is a surprise worth naming.
    """
    cleaned, errors = runtime_config.validate(values or {})
    notes: list[str] = []
    warnings: list[str] = []

    kv_url = str((values or {}).get("KV_REST_API_URL") or "").strip().rstrip("/")
    kv_token = str((values or {}).get("KV_REST_API_TOKEN") or "").strip()
    if bool(kv_url) != bool(kv_token):
        errors.append("The KV REST URL and its token belong together — send both, or neither.")
    elif kv_url and not kv_url.startswith(("http://", "https://")):
        errors.append("The KV REST URL must start with https:// (Upstash shows the full endpoint).")

    requested_backend = cleaned.get("P2P_STATE_BACKEND", storage.state_backend())
    if requested_backend == "redis" and not (storage.redis_config() or (kv_url and kv_token)):
        errors.append("Redis was selected as the state database. Add a KV/Redis REST URL and token, "
                      "or choose auto or file.")

    if errors:
        return _save_report(False, errors=errors, warnings=warnings, notes=notes)

    if verify and cleaned.get("BOT_TOKEN"):
        ok, detail = runtime_config.verify_bot_token(cleaned["BOT_TOKEN"])
        if ok is False:
            return _save_report(False, errors=[detail + " Nothing was saved."],
                                warnings=warnings, notes=notes)
        notes.append(detail)

    if kv_url and kv_token:
        reachable, detail = _probe_redis(kv_url, kv_token)
        if not reachable:
            return _save_report(False, errors=[f"The KV/Redis store did not accept the "
                                               f"connection ({detail}). Nothing was saved."],
                                warnings=warnings, notes=notes)
        if not storage.redis_config() and cleaned:
            # The environment has no KV pair, so this instance cannot read that
            # store on a cold start.  Keep a copy there anyway: as soon as the two
            # variables are added and the project redeployed, everything is in place.
            storage.RedisStore(kv_url, kv_token, key=runtime_config.config_key()).save(cleaned)
            notes.append("A copy was written to that store as well — add KV_REST_API_URL + "
                         "KV_REST_API_TOKEN to the deployment environment and redeploy, and the "
                         "settings survive every cold start.")

    if not cleaned and not kv_url:
        return _save_report(False, errors=["Nothing to save — the form was empty."],
                            warnings=warnings, notes=notes)

    store_before = runtime_config.describe(ROOT)
    saved = runtime_config.save(cleaned, ROOT)
    if any(saved.get(name) != value for name, value in cleaned.items()):
        warnings.append(f"The settings could not be written to {store_before['store']} — "
                        "the deployment may have no writable storage at all.")

    # Environment variables keep priority, so name the ones this save cannot beat.
    shadowed = [name for name in cleaned
                if (os.getenv(name) or "").strip()
                and name not in runtime_config.injected()
                and (os.getenv(name) or "").strip() != cleaned[name]]
    for name, value in cleaned.items():
        os.environ[name] = value                  # this instance uses the new value right away
    runtime_config.mark(saved)                    # the checklist now says where they came from
    if kv_url and kv_token:
        os.environ["KV_REST_API_URL"], os.environ["KV_REST_API_TOKEN"] = kv_url, kv_token
        notes.append("This instance now talks to that store; other instances and cold starts "
                     "need KV_REST_API_URL + KV_REST_API_TOKEN in the deployment environment.")
    runtime_config.invalidate()
    _refresh_store()

    for name in shadowed:
        warnings.append(f"{name} is also set in the deployment environment — other instances keep "
                        f"using that value until you change or remove it there.")
    if store_before["backend"] != "redis" and _looks_like_vercel():
        warnings.append("No KV/Redis is connected, so these settings live in this instance's "
                        "temporary storage only: connect a store (form field above, or "
                        "Vercel → Storage → Upstash for Redis) and redeploy to keep them.")

    status = setup_status()
    return _save_report(True, errors=[], warnings=warnings, notes=notes,
                        saved=[name for name in runtime_config.SUPPORTED if name in cleaned],
                        status=status)


def _save_report(ok: bool, errors: list[str] | None = None, warnings: list[str] | None = None,
                 notes: list[str] | None = None, saved: list[str] | None = None,
                 status: dict | None = None) -> dict:
    """The uniform answer of a save — rendered as HTML or JSON by api/setup.py."""
    status = status or setup_status()
    return {
        "ok": bool(ok),
        "saved": list(saved or []),
        "errors": [str(item) for item in errors or []],
        "warnings": [str(item) for item in warnings or []],
        "notes": [str(item) for item in notes or []],
        "store": status["runtime"],
        "status": status,
        "next": _next_steps(status),
    }


def _banner(banner: tuple[str, str] | None) -> str:
    """The result of the last form submission (``(kind, text)``)."""
    if not banner:
        return ""
    kind, text = banner
    css = {"ok": "ok", "error": "error"}.get(kind, "info")
    icon = {"ok": "✅", "error": "⚠️"}.get(kind, "ℹ️")
    return (f'<div class="banner {css}" role="status"><span aria-hidden="true">{icon}</span>'
            f'<p>{html.escape(str(text))}</p></div>')


def _field(name: str, label: str, hint: str, *, secret: bool = False, value: str = "",
           placeholder: str = "", stored: str = "") -> str:
    """One labelled input of the setup form (``name`` is the environment variable)."""
    kind = "password" if secret else "text"
    note = f'<small>{html.escape(hint)}</small>'
    if stored:
        note = (f'<small>{html.escape(hint)} <b>stored:</b> '
                f'<code>{html.escape(stored)}</code></small>')
    return (f'<label class="field"><span>{html.escape(label)} '
            f'<code>{html.escape(name)}</code></span>'
            f'<input type="{kind}" name="{html.escape(name)}" value="{html.escape(value)}" '
            f'placeholder="{html.escape(placeholder)}" autocomplete="off" '
            f'spellcheck="false">{note}</label>')


def _select_field(name: str, label: str, hint: str, options: tuple[tuple[str, str], ...],
                  value: str = "") -> str:
    """A labelled select control for a non-secret setup choice."""
    choices = "".join(
        '<option value="{}"{}>{}</option>'.format(
            html.escape(key), " selected" if key == value else "", html.escape(title))
        for key, title in options
    )
    return (f'<label class="field"><span>{html.escape(label)} '
            f'<code>{html.escape(name)}</code></span>'
            f'<select name="{html.escape(name)}">{choices}</select>'
            f'<small>{html.escape(hint)}</small></label>')


def _setup_form(status: dict, values: dict | None = None) -> str:
    """The browser half of the setup: add the required settings, no redeploy.

    The values go to ``POST /api/setup``, which stores them where ``storage.py``
    can keep them (the connected KV/Redis, or ``runtime_config.json``) and applies
    them to this instance immediately.  ``SETUP_SECRET`` locks the form; without
    it the form closes as soon as the deployment is ready.
    """
    values = values or {}
    runtime = status.get("runtime") or {}
    masked = runtime.get("masked") or {}
    needs_secret = bool(status.get("setup_secret")) or bool(status["ready"])
    selected_backend = str(values.get("P2P_STATE_BACKEND") or
                           runtime.get("state_backend") or storage.state_backend())
    redis_connected = bool(storage.redis_config())

    if status["ready"] and not status.get("setup_secret"):
        return (
            '<section class="panel" aria-labelledby="form-title">'
            '<div class="panel-heading"><div><h2 id="form-title">Add the required settings</h2>'
            '<p class="subtle">Locked — this deployment is configured.</p></div></div>'
            f'<p class="locked">🔒 Everything is in place, so the form is closed: an open page '
            f'must not be able to change a running bot. To change a setting from the browser, '
            f'set <code>SETUP_SECRET</code> in the deployment environment and reopen '
            f'<code>{html.escape(SETUP_PATH)}</code>. In a terminal, '
            f'<code>python setup_cli.py</code> writes to the same store.</p></section>')

    fields = [
        _field("BOT_TOKEN", "Telegram bot token", "From @BotFather → /newbot.",
               secret=True, placeholder="123456:ABCdef…", stored=masked.get("BOT_TOKEN", "")),
        _field("ADMIN_IDS", "Your Telegram ID(s)",
               "Numeric, comma-separated — @userinfobot tells you yours.",
               value=values.get("ADMIN_IDS", ""), placeholder="123456789",
               stored=masked.get("ADMIN_IDS", "")),
    ]
    fields.append(_select_field(
        "P2P_STATE_BACKEND", "State database",
        ("A Redis / KV database is already connected; no credentials need to be entered."
         if redis_connected else
         "Auto chooses Redis when credentials exist, otherwise data.json. File always uses data.json; Redis requires the URL and token below."),
        (("auto", "Auto — Redis when configured, otherwise file"),
         ("file", "File — always use local data.json"),
         ("redis", "Redis / KV — shared persistent database")),
        selected_backend))
    if redis_connected:
        fields.append(
            '<p class="where">✓ A KV / Redis database is already connected through the '
            'deployment environment. The bot will use it automatically; no database '
            'URL or token input is needed.</p>')
    else:
        fields += [
            _field("KV_REST_API_URL", "KV / Redis REST URL",
                   "Required when Redis is selected. Upstash for Redis → REST API → endpoint.",
                   value=values.get("KV_REST_API_URL", ""),
                   placeholder="https://eu1-….upstash.io"),
            _field("KV_REST_API_TOKEN", "KV / Redis REST token",
                   "The token that belongs to the URL above.", secret=True,
                   placeholder="A…"),
        ]
    advanced = "".join([
        _field("ASSET", "Asset", "What is traded.", value=values.get("ASSET", ""),
               placeholder="USDT"),
        _field("FIAT", "Fiat", "The currency prices are shown in.",
               value=values.get("FIAT", ""), placeholder="USD"),
        _field("INTERVAL", "Check every N seconds", "Polling installs only; Vercel uses its cron.",
               value=values.get("INTERVAL", ""), placeholder="60"),
    ])
    if needs_secret:
        fields.append(_field("SETUP_SECRET", "Setup secret",
                             "This deployment protects the form with SETUP_SECRET.",
                             secret=True, placeholder="the value of SETUP_SECRET"))

    store = runtime.get("store") or "the connected store"
    persistent = runtime.get("persistent")
    where = ("Stored in the connected KV/Redis — it survives restarts and redeploys."
             if persistent else
             "Stored next to the bot's state file. On Vercel that is temporary storage: "
             "connect a KV/Redis store to keep it across restarts.")
    return f"""
    <section class="panel" aria-labelledby="form-title">
      <div class="panel-heading">
        <div><h2 id="form-title">Add the required settings here</h2>
          <p class="subtle">Or do it in a terminal — both write to the same place.</p></div>
        <span class="badge {'ready' if persistent else 'optional'}">{'persistent store' if persistent else 'temporary store'}</span>
      </div>
      <form method="post" action="{html.escape(SETUP_PATH)}" class="form">
        {''.join(fields)}
        <details class="advanced"><summary>Optional: asset, fiat and interval</summary>{advanced}</details>
        <p class="where">💾 {html.escape(str(store))} — {html.escape(where)} Values are never
          shown again; only a redacted copy appears in the checklist.</p>
        <div class="actions"><button class="button" type="submit">💾 Save settings</button>
          <a class="button secondary" href="{html.escape(WEBHOOK_PATH)}">Open the status page</a></div>
      </form>
    </section>"""


def _terminal_panel(status: dict) -> str:
    """The terminal half of the setup — including the way to skip it."""
    runtime = status.get("runtime") or {}
    missing = ", ".join(status.get("required_missing") or []) or "nothing required"
    return f"""
    <section class="panel" aria-labelledby="terminal-title">
      <div class="panel-heading"><div><h2 id="terminal-title">…or do it in a terminal</h2>
        <p class="subtle">Same settings store, same values — pick whichever you prefer.</p></div></div>
      <pre class="shell"><code># asks for what is missing; press Enter on a question to skip it
python setup_cli.py

# nothing to enter now — the wizard prints this page's address and exits
python setup_cli.py --skip

# one line, no prompts
python setup_cli.py --token 123456:ABC-your-token --admins 123456789 --yes

# what is stored right now (redacted), and where
python setup_cli.py --show</code></pre>
      <p class="subtle">Currently missing: <code>{html.escape(missing)}</code> · stored in
        <code>{html.escape(str(runtime.get('store') or '—'))}</code>. The wizard verifies the token
        with Telegram before saving, and <code>--vercel-env</code> additionally writes the values
        into the deployment's environment variables with the Vercel CLI.</p>
    </section>"""


def _db_guide_panel() -> str:
    """Step-by-step instructions for inserting the KV / Redis database.

    The form above asks for ``KV_REST_API_URL`` and ``KV_REST_API_TOKEN`` —
    this panel explains where those two values come from and what to do with
    them: create an Upstash database and paste its REST pair into the form,
    or let Vercel's Storage integration write the environment pair itself.
    It opens with a direct link to the Vercel Storage page
    (:func:`storage.database_link`) — the screen *Connect to this project*
    lives on.
    """
    connect_url = html.escape(storage.database_link(), quote=True)
    return f"""
    <section class="panel" aria-labelledby="db-guide-title">
      <div class="panel-heading"><div><h2 id="db-guide-title">How to insert the database</h2>
        <p class="subtle">Where the KV / Redis URL and token come from — and what to do with them.</p></div>
        <span class="badge optional">KV / Redis</span></div>

      <div class="actions">
        <a class="button" href="{connect_url}" target="_blank" rel="noopener">🔌 Connect database ↗</a>
        <a class="button secondary" href="https://console.upstash.com" target="_blank" rel="noopener">Create an Upstash database ↗</a>
      </div>
      <p class="where">The first link opens <b>Vercel → Storage</b> (or <code>P2P_DATABASE_LINK</code>
        when it is set): create <b>Upstash for Redis</b> / <b>Vercel KV</b> there and press
        <b>Connect to this project</b> — Vercel then writes <code>KV_REST_API_URL</code> +
        <code>KV_REST_API_TOKEN</code> into the environment itself. Then redeploy — or paste the
        pair into the form above and save, which needs no redeploy.</p>

      <h3 class="guide-option">Option A — create a free Upstash database, paste the pair into the form</h3>
      <div class="steps">
        <article class="step"><span class="number">1</span><div><h3>Open the Upstash console</h3><p>Sign in at <a href="https://console.upstash.com" target="_blank" rel="noopener">console.upstash.com</a> — a free account is enough, the bot only stores one small JSON document.</p></div></article>
        <article class="step"><span class="number">2</span><div><h3>Create the database</h3><p><b>Create Database</b> → give it any name → pick the region closest to the deployment (e.g. <code>eu-central-1</code> for the <code>fra1</code> region pinned in <code>vercel.json</code>) → <b>Create</b>.</p></div></article>
        <article class="step"><span class="number">3</span><div><h3>Copy the REST credentials</h3><p>Open the new database and scroll to <b>REST API</b>. <code>UPSTASH_REDIS_REST_URL</code> is the <b>KV / Redis REST URL</b> (<code>KV_REST_API_URL</code>) in the form above, <code>UPSTASH_REDIS_REST_TOKEN</code> is the <b>KV / Redis REST token</b> (<code>KV_REST_API_TOKEN</code>).</p></div></article>
        <article class="step"><span class="number">4</span><div><h3>Paste them into the form and save</h3><p>Leave <b>State database</b> (<code>P2P_STATE_BACKEND</code>) on <code>auto</code> or set it to <code>redis</code>, paste both values, then press <b>💾 Save settings</b>. This instance starts using the database immediately — no redeploy.</p></div></article>
      </div>

      <h3 class="guide-option">Option B — let Vercel connect the storage for you</h3>
      <div class="steps">
        <article class="step"><span class="number">1</span><div><h3>Vercel dashboard → your project → Storage</h3><p><b>Create Database</b> → choose <b>Upstash for Redis</b> (or <b>Vercel KV</b>) → confirm the region → <b>Create</b>.</p><a href="https://vercel.com/dashboard" target="_blank" rel="noopener">Open Vercel dashboard ↗</a></div></article>
        <article class="step"><span class="number">2</span><div><h3>Connect it to this project</h3><p><b>Connect to this project</b> → keep the proposed variable names <code>KV_REST_API_URL</code> + <code>KV_REST_API_TOKEN</code>. Vercel writes the pair into the environment variables for you — nothing to copy-paste.</p></div></article>
        <article class="step"><span class="number">3</span><div><h3>Redeploy</h3><p>Environment variables only apply to new deployments: <b>Deployments → ⋯ → Redeploy</b>. After that every cold start reads the database pair from the environment — the durable path on Vercel.</p></div></article>
      </div>

      <p class="subtle" style="margin-top:13px">ℹ️ Values saved in the form live in this deployment's
        own state store. On Vercel, also add <code>KV_REST_API_URL</code> +
        <code>KV_REST_API_TOKEN</code> to <b>Settings → Environment Variables</b> (Option B does it
        automatically) so other instances and cold starts read the same database.</p>
    </section>"""


def _setup_page(message: str, status: dict, banner: tuple[str, str] | None = None,
                form_values: dict | None = None) -> str:
    """Render a polished, actionable first-start web UI.

    Three ways to finish, all on one page: the form (``POST /api/setup`` →
    ``runtime_config``, applied without a redeploy), a terminal
    (``python setup_cli.py``, which can be skipped with ``--skip`` and writes to
    the very same store), and the deployment's environment variables — the only
    option for ``CRON_SECRET``, which Vercel's cron reads from there.

    When no KV/Redis credentials are connected, a "How to insert the database"
    panel walks through getting the pair: create an Upstash database and paste
    its REST URL + token into the form, or connect storage in the Vercel dashboard
    (which writes ``KV_REST_API_URL`` + ``KV_REST_API_TOKEN`` by itself). When a
    complete pair is already present, the form skips those inputs and the guide.

    Secrets are accepted over HTTPS and stored in the deployment's own state
    store; they are never echoed back, and the form closes (or requires
    ``SETUP_SECRET``) as soon as the deployment is ready.
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
        source = (f' · <i>from the setup page</i>' if check.get("source") == "setup" else "")
        cards.append(
            f'<article class="check {css}">'
            f'<span class="check-icon" aria-hidden="true">{icon}</span>'
            f'<div class="check-copy"><h3>{html.escape(str(check.get("label", "")))}</h3>'
            f'<p>{html.escape(str(check.get("detail", "")))}</p>'
            f'<small>{variables}{source}</small></div>'
            f'<span class="badge">{badge}</span></article>')

    checks_markup = "\n".join(cards)
    banner_markup = _banner(banner)
    form_markup = _setup_form(status, form_values)
    database_connected = storage.database_connected()
    db_guide_markup = "" if database_connected else _db_guide_panel()
    connect_link_markup = (
        "" if database_connected else
        f'<a class="button" href="{html.escape(storage.database_link(), quote=True)}" '
        f'target="_blank" rel="noopener">🔌 Connect database ↗</a>')
    terminal_markup = _terminal_panel(status)
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
    .banner {{ display: grid; grid-template-columns: 30px 1fr; gap: 12px; align-items: start; padding: 16px 18px; margin-bottom: 18px; border-radius: 15px; border: 1px solid var(--line); background: var(--panel-2); }}
    .banner p {{ margin: 0; }}
    .banner.ok {{ border-color: #35d39955; background: #35d39914; }}
    .banner.error {{ border-color: #ff8c8c55; background: #ff8c8c14; }}
    .form {{ display: grid; gap: 13px; }}
    .field {{ display: grid; gap: 6px; }}
    .field span {{ font-weight: 700; }}
    .field input, .field select {{ min-height: 44px; padding: 10px 13px; border-radius: 11px; border: 1px solid var(--line); background: #0b1122; color: var(--text); font: 14px ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .field input:focus, .field select:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
    .field small {{ color: var(--muted); }}
    .advanced summary {{ cursor: pointer; color: #c7ceff; font-weight: 700; }}
    .advanced[open] {{ display: grid; gap: 13px; padding-top: 13px; }}
    .guide-option {{ margin: 20px 0 10px; font-size: 15px; letter-spacing: -.01em; }}
    .where {{ color: var(--muted); margin: 2px 0 0; font-size: 13px; }}
    .locked {{ color: var(--muted); margin: 0; }}
    .shell-code, pre.shell {{ overflow-x: auto; margin: 0; padding: 16px; border-radius: 15px; border: 1px solid var(--line); background: #080c17; }}
    pre.shell code {{ color: #d9e2ff; background: none; border: 0; padding: 0; font: 12.5px/1.75 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre; }}
    .badge.ready {{ color: #73f0bf; background: #35d3991a; }}
    .badge.optional {{ color: #ffd27c; background: #f7b9551a; }}
    .check i {{ color: #a9b4d6; font-style: normal; }}
    @media (max-width: 650px) {{ .shell {{ width: min(100% - 22px, 960px); padding-top: 24px; }} .hero {{ grid-template-columns: 1fr; gap: 13px; }} .panel-heading {{ display: block; }} .badge {{ margin-left: auto; }} .why {{ grid-template-columns: 1fr; }} .check {{ flex-wrap: wrap; }} .check-copy {{ min-width: calc(100% - 42px); }} .check .badge {{ margin-left: 40px; }} }}
    @media (prefers-reduced-motion: no-preference) {{ .hero {{ animation: rise .35s ease-out both; }} @keyframes rise {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: none; }} }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="brand"><div class="logo" aria-hidden="true">🤖</div><div><p class="eyebrow">First deployment</p><h1>P2P Price Bot</h1></div></header>
    {banner_markup}
    <section class="hero" aria-labelledby="setup-title">
      <div class="hero-symbol" aria-hidden="true">⚙️</div>
      <div><h2 id="setup-title">Setup needed</h2>
        <p><strong>What happened:</strong> the bot is not running yet. {html.escape(message)}</p>
        <p style="margin-top:10px"><strong>Fix:</strong> add what is missing below — in the browser form, in a terminal (<code>python setup_cli.py</code>), or in the deployment's environment variables.</p>
        <p style="margin-top:10px">{html.escape(deployment_note)} Missing: <code>{html.escape(str(missing))}</code></p>
      </div>
    </section>
    {form_markup}
    {db_guide_markup}

    <section class="panel" aria-labelledby="check-title">
      <div class="panel-heading"><div><h2 id="check-title">Environment checklist</h2><p class="subtle">Values are checked without displaying any secrets.</p></div></div>
      <div class="checks">{checks_markup}</div>
    </section>
    {terminal_markup}

    <section class="panel" aria-labelledby="steps-title">
      <h2 id="steps-title">Finish setup in Vercel</h2>
      <div class="steps">
        <article class="step"><span class="number">1</span><div><h3>Set the Telegram credentials</h3><p>Use the form above, or <b>Vercel dashboard → your project → Settings → Environment Variables</b>: <code>BOT_TOKEN</code> from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> and <code>ADMIN_IDS</code> from <a href="https://t.me/userinfobot" target="_blank" rel="noopener">@userinfobot</a>.</p><a href="https://vercel.com/dashboard" target="_blank" rel="noopener">Open Vercel dashboard ↗</a></div></article>
        <article class="step"><span class="number">2</span><div><h3>Connect persistent storage</h3><p>Open <b>Storage</b> → add <b>Upstash for Redis</b> (or Vercel KV) → <b>Connect to this project</b>. This sets <code>KV_REST_API_URL</code> + <code>KV_REST_API_TOKEN</code> so the bot remembers its group, merchants, and prices between requests.</p></div></article>
        <article class="step"><span class="number">3</span><div><h3>Redeploy only if you changed variables</h3><p>Settings saved on this page apply at once. Environment variables need <b>Deployments → ⋯ → Redeploy</b>, because they only apply to new deployments. Then reopen this page: it registers the Telegram webhook automatically.</p></div></article>
      </div>
    </section>

    <section class="why" aria-label="Why these settings are needed">
      <article><h3>🔐 Bot credentials</h3><p>Telegram uses the token to deliver updates and the admin ID to protect the control panel.</p></article>
      <article><h3>💾 Persistent state</h3><p>Vercel functions are short-lived. Redis keeps the group, merchants, settings, and prices available on every request.</p></article>
      <article><h3>🔗 Automatic webhook</h3><p>Once the variables are present, opening this page checks the token and registers <code>/api/webhook</code>.</p></article>
    </section>

    <div class="actions">{connect_link_markup}<a class="button secondary" href="/api/setup">↻ Check setup again</a><a class="button secondary" href="/api/webhook?register=0">View diagnostics</a><a class="button secondary" href="https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/blob/main/VERCEL.md" target="_blank" rel="noopener">Deployment guide ↗</a></div>
    <p class="foot">The form posts over HTTPS to <code>/api/setup</code> and stores what you enter in this deployment's own state store — the same place <code>python setup_cli.py</code> writes to. Values are never shown again (the checklist shows a redacted copy), environment variables always win over stored ones, and the form asks for <code>SETUP_SECRET</code> — or closes — once the deployment is configured.</p>
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
                          "status": "setup_required", "setup": status,
                          "steps": SETUP_STEPS, "setup_page": SETUP_PATH,
                          "connect_database": storage.database_link()}, 500)


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
         warnings: list[str] | None = None,
         links: list[tuple[str, str]] | None = None) -> str:
    """A self-contained status page — no CSS files, no JavaScript, dark-mode aware.

    ``links`` are ``(label, url)`` pairs rendered as buttons; only fixed,
    platform/documentation URLs are ever passed in here — never a value that came
    from a request.
    """
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
    buttons = "".join(
        f'\n    <a class="button" href="{html.escape(url, quote=True)}" target="_blank" '
        f'rel="noopener">{html.escape(label)}</a>'
        for label, url in links or [])
    actions = f'\n    <p class="actions">{buttons}</p>' if buttons else ""
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
  .actions {{ display: flex; flex-wrap: wrap; gap: .6rem; margin: 1.1rem 0 0; }}
  .button {{ display: inline-block; padding: .5rem .8rem; border-radius: .45rem; border: 1px solid #8886; text-decoration: none; font-weight: 600; }}
</style>
<h1>{html.escape(title)}</h1>{warn}{actions}
<table>
{body}
</table>{extra}
</html>
"""
