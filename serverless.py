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
import importlib
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote

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
        # A different warm Vercel instance may have just selected a custom DB
        # through Telegram. setup_status() above reapplies its saved credentials;
        # adopt the selected store without copying stale state over the new DB.
        try:
            _bot.sync_store_from_config()
        except Exception as exc:                  # pragma: no cover - defensive
            log.warning("could not refresh the configured state store: %s", exc)
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
    if persistent:
        # The status page and JSON API are public. Report the backend/key but
        # never publish a custom Redis host (the bot's private /database view
        # can show admins more specific connection context).
        detail = f"redis REST (key {getattr(store, 'key', storage.DEFAULT_KEY)})"
    else:
        detail = store.describe()
    status = {"backend": backend, "detail": detail, "persistent": persistent}
    if not persistent:
        status["warning"] = (
            "no persistent store: Vercel has no writable disk, so the group, the "
            "merchants and the prices are forgotten as soon as this instance is "
            "recycled. Connect a Redis/KV REST store (KV_REST_API_URL + "
            "KV_REST_API_TOKEN — e.g. the Upstash or Vercel KV integration)."
        )
    return status


# ── is the state database really there? ───────────────────────────────────
# ``RedisStore.load()`` returns ``None`` both for "nothing saved yet" and for
# "the database is gone" — and the bot reads that as an empty state, so the
# group, the merchants and the prices quietly vanish while the setup checklist
# keeps saying "connected".  Probing the store is the only way to tell the two
# apart, so the answer is cached for a short while and never raises: a failed
# probe is *reported*, not fatal.  Off Vercel a database is not a hard
# requirement, so the probe is skipped there rather than billed to every page.
STORE_PROBE_TTL = 60.0          # a healthy answer is trusted for a minute …
STORE_PROBE_FAIL_TTL = 20.0     # … a failure less, so a blip costs seconds, not minutes
STORE_PROBE_TIMEOUT = 4.0
_store_probe: dict[str, Any] = {"at": 0.0, "key": "", "ok": True, "detail": ""}


def reset_store_probe() -> None:
    """Forget the cached probe result (tests, and right after a settings save)."""
    _store_probe.update(at=0.0, key="")


def _store_key() -> str:
    """What the selected store depends on — cheap enough to compare per request.

    Building the store just to describe it would log a line (and touch the disk)
    on every request, so the cache is keyed on the configuration instead.
    """
    return "|".join([storage.state_backend(), str(storage.redis_config() or ""),
                     env("P2P_STATE_KEY"), env("P2P_STATE_FILE"), env("P2P_DATA_DIR")])


def store_health(force: bool = False) -> dict:
    """Reachability of the selected state store: ``{ok, detail, backend}``."""
    key = _store_key()
    now = time.monotonic()
    if not force and _store_probe["key"] == key:
        age = now - _store_probe["at"]
        if age < (STORE_PROBE_TTL if _store_probe["ok"] else STORE_PROBE_FAIL_TTL):
            return {"ok": bool(_store_probe["ok"]), "detail": str(_store_probe["detail"]),
                    "backend": str(_store_probe.get("backend") or "none"), "cached": True}
    store = storage.build_store(ROOT)
    ok, detail = storage.probe_store(store, timeout=STORE_PROBE_TIMEOUT)
    if not ok and not force:
        # One failure is not an outage: a single dropped connection must not
        # take a working deployment down for the whole cache window.
        ok, detail = storage.probe_store(store, timeout=STORE_PROBE_TIMEOUT)
    _store_probe.update(at=now, key=key, ok=ok, detail=detail,
                        backend=getattr(store, "backend", "none"))
    return {"ok": bool(ok), "detail": str(detail),
            "backend": getattr(store, "backend", "none"), "cached": False}


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


# ── can Telegram actually reach this deployment? ───────────────────────────
# A *registered* webhook is not a *working* webhook, and the difference is the
# silence nobody can explain: Telegram's POST can be refused before it ever
# reaches the function.  Vercel Deployment Protection (Vercel Authentication,
# on by default) gates preview deployments **and the auto-generated deployment
# URLs** (``project-abc123-owner.vercel.app``) with a 401 login page that
# Telegram cannot pass; a custom domain whose DNS record is missing answers 404;
# a domain that was never added answers nothing at all.  In every one of those
# cases the status page still says "webhook registered", Telegram retries with
# backoff and finally gives up, and the bot simply looks dead.
#
# The only way to notice is to knock on the public URL from the outside, which
# is what ``/api/health`` exists for: a static, side-effect-free answer that
# proves the request reached *this* deployment (the marker below) or names what
# stopped it.  Nothing here is on the update path — the probe runs when the
# status page or the cron asks for it, the answer is cached, and it is switched
# off unless the deployment looks like Vercel (or ``P2P_WEBHOOK_PROBE=1``
# says so explicitly), so a local ``uvicorn`` never calls out to the internet.
HEALTH_PATH = "/api/health"
HEALTH_MARKER = "p2p-price-bot"
PROBE_TTL = 300.0               # a public deployment stays public for a while …
PROBE_FAIL_TTL = 60.0           # … a failure is re-checked sooner
PROBE_TIMEOUT = 5.0             # room for the relay hop and one cold start
_probe: dict[str, Any] = {"key": "", "at": 0.0, "result": {}}


def reset_reachability_probe() -> None:
    """Forget the cached answer (tests, and right after the webhook is registered)."""
    _probe.update(key="", at=0.0)


def _probe_allowed() -> bool:
    """Whether *this* host should spend a request on its own public URL."""
    explicit = (os.getenv("P2P_WEBHOOK_PROBE") or "").strip()
    if explicit:
        return truthy(explicit)
    return _looks_like_vercel()


async def _probe_request(url: str):
    """Perform the knock itself (kept apart so tests can answer it offline).

    Asynchronous on purpose: the probe runs inside the same event loop that
    serves Telegram's requests, and a blocking call would stall that loop — on a
    single Vercel instance it would even deadlock against its own URL, because
    the wait could never be answered while the loop sits still.
    """
    import httpx

    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
        return await client.get(url, headers={"User-Agent": "p2p-price-bot/webhook-probe",
                                             "Cache-Control": "no-cache"})


async def _probe_once(url: str) -> dict:
    """Knock on the health page once and classify the answer — never raises."""
    try:
        reply = await _probe_request(url)
    except Exception as exc:                          # DNS, TLS, timeout, offline …
        return {"checked": True, "reachable": False, "status": 0, "url": url,
                "protected": False,
                "detail": f"the deployment could not be reached at all "
                          f"({type(exc).__name__}: {exc})",
                "hint": "Telegram cannot deliver a single update to a URL that does not answer. "
                        "Check the domain (Vercel → your project → Settings → Domains) and set "
                        "PUBLIC_URL to the address the bot should be reachable at."}
    marker = ""
    try:
        data = reply.json()
        marker = str((data or {}).get("app") or "") if isinstance(data, dict) else ""
    except Exception:                                 # pragma: no cover - non-JSON (a login page)
        marker = ""
    status = int(reply.status_code)
    if status == 200 and marker == HEALTH_MARKER:
        return {"checked": True, "reachable": True, "status": status, "url": url,
                "protected": False, "detail": "the public URL reaches this deployment",
                "hint": ""}
    if status in (401, 403):
        return {"checked": True, "reachable": False, "status": status, "url": url,
                "protected": True,
                "detail": f"the deployment answers HTTP {status} to anonymous requests — Vercel "
                          "Deployment Protection (or a firewall rule) stands in front of it",
                "hint": "Vercel Authentication gates the auto-generated *-<hash>.vercel.app "
                        "addresses and every preview URL; Telegram cannot log in, so all updates "
                        "are refused. Point the bot at the project's public production domain "
                        "(Vercel → Settings → Domains) with PUBLIC_URL=https://that-domain, keep "
                        "Deployment Protection on *Standard Protection*, then re-register at "
                        "/api/webhook?register=1."}
    if status == 404:
        return {"checked": True, "reachable": False, "status": status, "url": url,
                "protected": False,
                "detail": "the URL answers HTTP 404 — it does not reach this deployment",
                "hint": "The domain is not connected to this project (Vercel → Settings → Domains) "
                        "or PUBLIC_URL names the wrong host. Telegram posts to that 404 and the bot "
                        "never sees an update."}
    return {"checked": True, "reachable": False, "status": status, "url": url,
            "protected": False,
            "detail": f"the public URL answered HTTP {status} instead of the bot's health page",
            "hint": "Something else answers on this URL (a proxy, a firewall rule, another "
                    "deployment). Check Vercel → your project → Settings → Domains and open the "
                    "deployment's Logs."}


async def webhook_reachability(base: str = "", force: bool = False) -> dict:
    """Whether an anonymous request reaches this deployment: ``{checked, reachable, hint, …}``.

    ``reachable`` is ``None`` when the check was not possible (no public URL, or
    the probe is switched off), so a page can say "not checked" instead of
    pretending everything is fine.  The answer is cached, because a page view or
    a cron run is not a reason to hammer the deployment's own URL.
    """
    if not _probe_allowed():
        return {"checked": False, "reachable": None, "url": "", "protected": False,
                "detail": "the public-URL check is off on this host "
                          "(set P2P_WEBHOOK_PROBE=1 to enable it)", "hint": ""}
    try:
        url = (base or public_base_url()).rstrip("/") + HEALTH_PATH
    except ConfigError as exc:
        return {"checked": False, "reachable": None, "url": "", "protected": False,
                "detail": str(exc), "hint": ""}

    now = time.monotonic()
    if not force and _probe["key"] == url:
        age = now - float(_probe["at"])
        if age < (PROBE_TTL if _probe["result"].get("reachable") else PROBE_FAIL_TTL):
            return {**_probe["result"], "cached": True}
    result = await _probe_once(url)
    _probe.update(key=url, at=now, result=result)
    return {**result, "cached": False}


# Telegram reports the last delivery failure to ``getWebhookInfo``.  These are
# the messages seen in practice, each with the one thing that fixes it.
_WEBHOOK_ERROR_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("401", "403", "unauthorized", "forbidden"),
     "Telegram's request is refused before it reaches the bot — that is Vercel Deployment "
     "Protection (or a firewall rule). Use the public production domain with PUBLIC_URL, keep "
     "protection on *Standard*, then re-register at /api/webhook?register=1."),
    (("404", "not found"),
     "Telegram's request lands on a 404: the domain is not connected to this project, or "
     "PUBLIC_URL names the wrong host. Fix the domain, then re-register at "
     "/api/webhook?register=1."),
    (("timeout", "timed out", "504", "gateway"),
     "The function takes too long to answer Telegram (usually a cold start). Keep maxDuration as "
     "configured in vercel.json and check the deployment's Logs."),
    (("ssl", "certificate", "tls", "cert"),
     "The domain's TLS certificate is not valid yet — Vercel issues it a few minutes after the "
     "domain is added. Re-register once the domain loads over HTTPS."),
    (("429", "too many requests", "rate limit"),
     "Telegram is rate-limiting the webhook, usually a retry storm after an earlier failure. "
     "Set WEBHOOK_DROP_PENDING=1 once to clear the backlog and re-register."),
    (("500", "502", "503", "internal", "error"),
     "The function answered an error to Telegram and the update will be retried with backoff. "
     "The traceback is in the deployment's Logs (Vercel → your project → Logs)."),
)


def webhook_error_hint(message: str) -> str:
    """One actionable sentence for Telegram's ``last_error_message`` (``""`` if unknown)."""
    text = (message or "").lower()
    if not text:
        return ""
    for needles, hint in _WEBHOOK_ERROR_HINTS:
        if any(needle in text for needle in needles):
            return hint
    return ""


# ── the cron is the deployment's heartbeat ─────────────────────────────────
# On Vercel nothing runs between requests: the cron drives the price posts, and
# on the free plan it runs **once a day**.  A quiet group therefore has two very
# different explanations — "nothing changed" and "nothing is running it" — and
# only a recorded timestamp can tell them apart.
TICK_KEY = "tick"
TICK_STALE_SECONDS = 25 * 3600        # daily cron + Vercel's ±59 min precision


def short_age(seconds: int) -> str:
    """``3700`` → ``"1h 1m"`` — short enough for a Telegram panel line."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def tick_status(bot_module=None) -> dict:
    """When the cron last ran and how often: ``{at, count, age, …}`` (``at=0`` before the first)."""
    bot_module = bot_module or get_bot()
    record = bot_module.state.get(TICK_KEY)
    record = record if isinstance(record, dict) else {}
    at = int(record.get("at") or 0)
    return {"at": at, "count": int(record.get("count") or 0),
            "age": (max(0, int(time.time()) - at) if at else None),
            "serverless": _looks_like_vercel()}


def tick_note(status: dict | None = None, bot_module=None) -> str:
    """One readable line about the cron, for the status page and the Telegram panel."""
    status = status if isinstance(status, dict) else tick_status(bot_module)
    if not status.get("at"):
        return ("never — on Vercel the cron posts the prices, and on the free plan it runs once a "
                "day (see VERCEL.md §5 for a five-minute cadence)")
    return (f"{short_age(int(status.get('age') or 0))} ago — run "
            f"{int(status.get('count') or 0)}, next one within a day on the free plan")


def tick_warning(status: dict | None = None, bot_module=None) -> str:
    """Why the group can be quiet even though everything looks configured."""
    status = status if isinstance(status, dict) else tick_status(bot_module)
    if not status.get("serverless"):
        return ""                                  # a polling install has its own JobQueue
    if not status.get("at"):
        return ("The cron has not run yet, so it has never posted a price. It replaces the "
                "polling bot's JobQueue: open /api/webhook to check the webhook and give the cron "
                "its first run at /api/tick, or set up an external scheduler (VERCEL.md §5).")
    if int(status.get("age") or 0) > TICK_STALE_SECONDS:
        return (f"The last cron run was {short_age(int(status['age']))} ago, so prices are only as "
                "fresh as that run. Vercel's free plan runs a cron once a day (with up to ±59 "
                "minutes of jitter); for five-minute prices let an external scheduler call "
                "/api/tick — VERCEL.md §5 has a ready-made recipe.")
    return ""


def webhook_diagnostics(summary: dict | None = None, reachability: dict | None = None) -> dict:
    """The public-URL probe, Telegram's last error and its fix, in one place.

    Used by the status page, its JSON twin and the cron answer, so all three say
    the same thing about why Telegram may not be reaching the bot.
    """
    summary = summary if isinstance(summary, dict) else {}
    reachability = reachability if isinstance(reachability, dict) else {}
    last_error = str(summary.get("last_error") or "")
    hint = webhook_error_hint(last_error)
    warning = ""
    if reachability and reachability.get("checked") and not reachability.get("reachable"):
        warning = f"Telegram cannot reach this deployment: {reachability.get('detail')}. " \
                  f"{reachability.get('hint')}".strip()
    elif hint:
        warning = f"Telegram reports a webhook problem ({last_error}): {hint}"
    elif int(summary.get("pending_updates") or 0) > 20:
        warning = (f"Telegram still holds {int(summary['pending_updates'])} undelivered updates — "
                   "the webhook is registered but the deployment is not answering them. Check the "
                   "deployment's Logs and open /api/webhook?register=1.")
    return {"public": reachability or {"checked": False, "reachable": None},
            "last_error": last_error or None, "hint": hint, "warning": warning}


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
    # /setup hands the admins the one-time link that reopens the setup form.
    # It lives here rather than in bot.py because a polling install configures
    # itself from config.json and needs no browser round-trip.
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("setup", setup_command), group=0)
    # Bot.initialize() calls getMe once: it verifies the token and caches the
    # bot's own user, so /start links work without a second API call.
    await app.initialize()
    # Unlike polling, Application.initialize() does not run PTB's post_init
    # hook, so publish the Telegram private-chat command menu explicitly here.
    await bot_module.configure_bot_commands(app.bot, include_setup=True)
    try:
        bot_module.BOT_USERNAME = app.bot.username
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
        drop_pending_updates=truthy(env("WEBHOOK_DROP_PENDING", default="0")),
    )
    bot_module.state[MARKER_KEY] = {"url": target, "secret_fp": _fingerprint(secret)}
    bot_module.save()
    reset_reachability_probe()      # the address just changed — knock on it afresh
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
    """One round of the periodic work the polling bot does in its JobQueue.

    Every step is isolated.  On the free plan this may be the only run in 24
    hours, so a failing webhook check must not cost the day's price post: each
    step keeps going, and what failed is listed in ``errors`` rather than
    swallowed.  The run itself is remembered in the state store (``tick``), so
    the status page and the Telegram panel can tell "prices didn't change" apart
    from "nothing is running this deployment".
    """
    bot_module = get_bot()
    loop = asyncio.get_running_loop()
    result: dict = {"errors": []}
    async with _lock_for(loop):
        app = await _application_locked(loop)
        bot_module.refresh_state()

        # 1. keep the webhook registered — the one step whose failure would
        #    silence the bot completely, so it is reported in full.
        try:
            result["webhook"] = await ensure_webhook(app=app)
        except ConfigError:
            raise
        except Exception as exc:                      # pragma: no cover - network
            log.warning("webhook check failed: %s", exc)
            result["webhook"] = {"error": f"{type(exc).__name__}: {exc}"}
            result["errors"].append(f"webhook: {exc}")

        # 2. post the prices when they changed
        try:
            result["posted"] = bool(await bot_module.auto_post_task(app.bot))
        except Exception as exc:                      # pragma: no cover - Telegram
            log.warning("auto post failed: %s", exc)
            result["posted"] = False
            result["errors"].append(f"post prices: {exc}")

        # 3. delete the stale group message
        try:
            result["deleted_stale_message"] = bool(await bot_module.cleanup_task(app.bot))
        except Exception as exc:                      # pragma: no cover - Telegram
            log.warning("cleanup failed: %s", exc)
            result["deleted_stale_message"] = False
            result["errors"].append(f"cleanup: {exc}")

        # serverless has no JobQueue: expired anti-scam checks are swept here
        try:
            result["captcha_expired"] = int(await bot_module.sweep_captcha(app.bot))
        except Exception as exc:                      # pragma: no cover - Telegram
            log.warning("anti-scam sweep failed: %s", exc)
            result["captcha_expired"] = 0
            result["errors"].append(f"anti-scam sweep: {exc}")

        # 🧹 …and so are the group-cleanup warnings still standing in the group
        result["cleanup"] = bot_module.cleanup_summary()
        try:
            result["cleanup_warnings_removed"] = int(await bot_module.sweep_cleanup_notices(app.bot))
        except Exception as exc:                      # pragma: no cover - Telegram
            log.warning("group cleanup sweep failed: %s", exc)
            result["cleanup_warnings_removed"] = 0
            result["errors"].append(f"group cleanup sweep: {exc}")

        result["auto"] = bool(bot_module.state.get("auto"))
        result["group"] = bot_module.state.get("group")
        result["channel"] = bot_module.state.get("channel")
        # ↪️ the chats whose messages are relayed into the group
        result["forward_from"] = bot_module.forward_source_summary()
        result["merchants"] = len(bot_module.state.get("merchants") or {})

        # Remember that the cron ran, and how often: the group's silence is
        # otherwise indistinguishable from a deployment nobody starts.
        previous = bot_module.state.get(TICK_KEY)
        previous = previous if isinstance(previous, dict) else {}
        bot_module.state[TICK_KEY] = {"at": int(time.time()),
                                      "count": int(previous.get("count") or 0) + 1}
        try:
            bot_module.save()
        except Exception as exc:                      # pragma: no cover - stores never raise
            log.debug("tick bookkeeping could not be saved: %s", exc)
        result["tick"] = dict(bot_module.state[TICK_KEY])

    result["state"] = state_status()
    # The cron may be the only chance in 24 hours (free plan) to notice that
    # Telegram cannot reach the deployment at all, so ask the public URL too.
    result["public"] = await webhook_reachability()
    result["diagnostics"] = webhook_diagnostics(
        result.get("webhook") if isinstance(result.get("webhook"), dict) else {},
        result["public"])
    if result["diagnostics"]["warning"]:
        result["errors"].append(result["diagnostics"]["warning"])
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
    (storage.CUSTOM_REDIS_ENV_PAIR[0], storage.CUSTOM_REDIS_ENV_PAIR[1],
     "custom Redis REST (Telegram)"),
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


# ── read-only database detection (Vercel storage + this deployment's env) ──
# Two places know about the database. The deployment environment holds a
# complete KV/Redis REST pair — the only thing the bot can actually use. Vercel
# itself reports a store connected to the project, even while its credentials
# have not reached this deployment yet (the "connected, but not redeployed"
# case). This is diagnostic only: the browser setup page reports database
# status but never accepts credentials, selects a backend, or shows a connect
# button. Initial Vercel storage is configured through Vercel Storage/env;
# custom Redis REST credentials are managed privately in Telegram.
#
# The Vercel look-up is read-only, best-effort and never fatal: no token means
# no call (and a note saying so), a refused token or an offline host is reported
# rather than raised, and the answer is cached because a page view is not a
# reason to spend two API round-trips.
VERCEL_API = "https://api.vercel.com"
DB_DETECT_TIMEOUT = 3.0
DB_DETECT_TTL = 300.0            # a database that is there stays there for a while …
DB_DETECT_FAIL_TTL = 45.0        # … a "none found" answer is re-checked sooner
_REDIS_STORE_TYPES = ("redis", "kv", "valkey", "upstash", "redis-rest")
_KV_URL_VARS = tuple(url_name for url_name, _token_name, _label in _REDIS_ENV_PAIRS)
_db_detect: dict[str, Any] = {"at": 0.0, "key": "", "result": {}}


def reset_database_detection() -> None:
    """Forget the cached answer (tests, and right after a settings save)."""
    _db_detect.update(at=0.0, key="")


def _vercel_api_token() -> str:
    """A credential for the Vercel REST API, when this deployment offers one.

    ``VERCEL_TOKEN`` is an access token the owner adds to the environment
    themselves; ``VERCEL_OIDC_TOKEN`` is the short-lived token Vercel issues to
    the function.  Both are tried on a best-effort basis — "not authorized" is
    *reported*, never raised.
    """
    return env("VERCEL_TOKEN", "P2P_VERCEL_TOKEN", "VERCEL_OIDC_TOKEN")


def _vercel_project() -> tuple[str, str]:
    """``(project id or name, team id)`` for the Vercel API — ``("", "")`` if unknown."""
    return (env("VERCEL_PROJECT_ID", "P2P_VERCEL_PROJECT_ID", "VERCEL_PROJECT_NAME"),
            env("VERCEL_TEAM_ID", "P2P_VERCEL_TEAM_ID"))


def _vercel_get(path: str, token: str, params: dict | None = None) -> tuple[int, Any]:
    """One read-only Vercel API call: ``(status, payload)`` — it never raises.

    ``status`` is ``0`` when the call itself failed (offline, timeout, DNS); the
    payload then holds the reason instead of a parsed body.
    """
    import httpx

    query = {name: value for name, value in (params or {}).items() if value}
    try:
        reply = httpx.get(f"{VERCEL_API}{path}", params=query, timeout=DB_DETECT_TIMEOUT,
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/json"})
    except Exception as exc:                              # offline, timeout, DNS …
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return int(reply.status_code), reply.json()
    except Exception:                                     # pragma: no cover - non-JSON reply
        return int(reply.status_code), {}


def _vercel_error(status: int, body: Any, what: str) -> str:
    """Why a Vercel look-up did not answer — public, so it holds no secret."""
    if status == 0:
        return f"the Vercel API could not be reached ({body})"
    if status in (401, 403):
        return f"the Vercel API refused the token (HTTP {status}) — {what} was not checked"
    detail = ""
    if isinstance(body, dict):
        error = body.get("error") or {}
        detail = str(error.get("message") or error.get("code") or "")
    return f"the Vercel API answered HTTP {status} for {what}" + (f" ({detail})" if detail else "")


def _store_is_redis(store: dict) -> bool:
    """Whether a store Vercel lists is the KV/Redis database this bot needs.

    A Blob or Postgres store is *not* an answer here: the bot keeps one small
    JSON document in a Redis-compatible REST store.
    """
    kind = str(store.get("type") or store.get("kind") or "").strip().lower()
    if kind:
        return kind in _REDIS_STORE_TYPES or "redis" in kind
    # Older payloads carry no type — fall back to the name and the product.
    haystack = " ".join(str(store.get(key) or "")
                        for key in ("name", "productId", "integrationProductId", "slug")).lower()
    return any(word in haystack for word in ("redis", "kv", "upstash"))


def _vercel_connected_stores(token: str, project: str,
                             team: str) -> tuple[list[str], str, int]:
    """Redis/KV stores Vercel reports for this project: ``(names, error, status)``."""
    status, body = _vercel_get("/v1/storage/stores", token,
                               {"projectId": project, "teamId": team})
    if status != 200:
        return [], _vercel_error(status, body, "the project's stores"), status
    stores = (body or {}).get("stores") if isinstance(body, dict) else None
    if not isinstance(stores, list):
        return [], "", status
    names: list[str] = []
    for store in stores:
        if isinstance(store, dict) and _store_is_redis(store):
            name = str(store.get("name") or store.get("id") or "").strip()
            if name and name not in names:
                names.append(name)
    return names, "", status


def _vercel_env_database(token: str, project: str, team: str) -> tuple[str, str]:
    """``(variable, store)`` when the *project* itself holds a KV/Redis variable.

    The second look-up, used when the store list does not answer: the project's
    environment variables are listed without their values, so a connected
    database shows up here even when the store list is not readable.
    """
    status, body = _vercel_get(f"/v1/projects/{quote(project, safe='')}/env", token,
                               {"teamId": team})
    if status != 200:
        return "", ""
    items = (body or {}).get("envs") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return "", ""
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("key") or "").strip().upper()
        hint = item.get("contentHint") or {}
        redis_hint = str(hint.get("type") or "").strip().lower() in (
            "redis-url", "redis_rest_url", "redis")
        if name in _KV_URL_VARS or redis_hint:
            return (name or "KV_REST_API_URL"), str(hint.get("storeId") or "").strip()
    return "", ""


def detect_database(force: bool = False) -> dict:
    """Find the database the bot should use — and say where it was detected.

    The answer is public (no URL, token or secret ever leaves this function):

    ``found``    a database exists: a complete KV/Redis REST pair in this
                 deployment's environment, *or* a store Vercel reports as
                 connected to the project.
    ``wired``    this deployment can use it — the credentials are in *its* own
                 environment.  ``found`` without ``wired`` is one redeploy away,
                 not a missing database.
    ``source``   ``environment`` | ``vercel-storage`` | ``vercel-env`` | ``""``
    ``where``    one human sentence naming *where* it was detected.
    ``skipped``  legacy status metadata, true when ``found`` is set; it does not
                 hide or disable database controls because the browser page has
                 no database editor.

    Off Vercel nothing is queried over the network: a polling install is allowed
    to keep its state in ``data.json``, so "no credentials" is a valid answer
    there rather than a problem to investigate.
    """
    custom_pair = storage.custom_redis_config()
    pair = custom_pair or storage.environment_redis_config()
    provider = ("custom Redis REST (Telegram)" if custom_pair else
                next((label for url_name, token_name, label in _REDIS_ENV_PAIRS[1:]
                      if env(url_name) and env(token_name)), ""))
    token = _vercel_api_token()
    project, team = _vercel_project()
    on_vercel = _looks_like_vercel()
    key = "|".join([str(pair or ""), provider, str(on_vercel), project, team,
                    "token" if token else ""])
    now = time.monotonic()
    if not force and _db_detect["key"] == key:
        age = now - float(_db_detect["at"])
        if age < (DB_DETECT_TTL if _db_detect["result"].get("found") else DB_DETECT_FAIL_TTL):
            return dict(_db_detect["result"])

    result: dict[str, Any] = {"found": False, "wired": False, "usable": False, "source": "",
                              "provider": provider, "where": "", "detail": "", "stores": [],
                              "checked": False, "error": "", "skipped": False}

    if pair:
        # The configured pair is the answer that counts: it is what the bot
        # reads, so there is nothing to look up on Vercel and no call to spend.
        custom = custom_pair is not None
        result.update(
            found=True, wired=True, usable=True,
            source="telegram-custom" if custom else "environment",
            provider=provider or "KV / Redis",
            where=("a Telegram-selected custom Redis REST database" if custom else
                   f"a complete {provider or 'KV / Redis'} pair in this deployment's environment"),
            detail=("A custom Redis REST database selected in Telegram is active." if custom else
                    "A KV/Redis database is already connected through "
                    f"{provider or 'KV / Redis'} — the bot uses it automatically."))
    elif not on_vercel:
        result.update(detail="No KV/Redis credentials in the environment; this host keeps "
                             "its state in data.json.")
    elif not project or not token:
        result.update(detail="No KV/Redis credentials in this deployment's environment.",
                      error=("Vercel could not be asked which database is connected: "
                             "VERCEL_PROJECT_ID (or VERCEL_PROJECT_NAME) is not exposed to "
                             "this deployment"
                             if not project else
                             "no Vercel credential in the environment — add VERCEL_TOKEN "
                             "(or P2P_VERCEL_TOKEN) to enable the optional read-only check"))
    else:
        names, error, status = _vercel_connected_stores(token, project, team)
        variable, store = ("", "")
        if not names and status not in (401, 403):
            # The store list is not always readable (a token without the storage
            # scope, an older project).  The project's own variables are the
            # second answer, and they are listed without their values.
            variable, store = _vercel_env_database(token, project, team)
        result["checked"] = True
        if names:
            result.update(found=True, error="", source="vercel-storage", stores=list(names),
                          where=f"Vercel → Storage: {', '.join(names)} (connected to this "
                                "project)")
        elif variable:
            result.update(found=True, error="", source="vercel-env",
                          where=f"{variable} on the Vercel project"
                                + (f" (store {store})" if store else ""))
        else:
            result.update(
                error=error,
                detail=("No KV/Redis credentials in this deployment's environment. "
                        + ("" if error else
                           "Vercel reports no KV/Redis store connected to this project, so "
                           "connect one through Vercel Storage or the deployment environment.")))

    if result["found"]:
        result["skipped"] = True
        if not result["wired"]:
            # Connected on Vercel, invisible to this deployment: a redeploy is
            # the fix, and creating a second database is the wrong one.
            result["detail"] = (
                f"Detected on {result['where']} — this deployment has no "
                "KV_REST_API_URL + KV_REST_API_TOKEN yet, so it cannot use it yet. "
                "No new database is needed: press Connect to this project again in "
                "Vercel → Storage (that is what writes the two variables) and redeploy.")

    _db_detect.update(at=now, key=key, result=result)
    return dict(result)


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
                 detail: str, variables: tuple[str, ...], source: str = "",
                 broken: bool = False, detected: str = "") -> dict:
    """Create a public, secret-free item for the setup checklist.

    ``broken`` marks something that *is* configured but does not work — a
    database whose credentials are present yet unreachable.  It is shown with
    its own badge so it is not mistaken for "never set up".

    ``detected`` names *where* a value was found (the deployment environment, or
    Vercel itself), so the read-only checklist can explain the detected status.
    """
    return {
        "name": name,
        "label": label,
        "required": required,
        "ok": bool(ok),
        "broken": bool(broken) and not ok,
        "status": ("broken" if broken and not ok else
                   "ready" if ok else ("required" if required else "recommended")),
        "detail": detail,
        "variables": list(variables),
        "source": source,
        "detected": detected,
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

    # Is a database already there?  In this deployment's environment, or — one
    # step earlier — on Vercel, which knows which store is connected to the
    # project even before its credentials reach the environment.
    database = detect_database()
    detected_elsewhere = bool(database["found"] and not database["wired"])
    if detected_elsewhere:
        # "Connected but not redeployed" is not "nothing is connected": it is
        # one redeploy away, and the page must not talk the owner into a second
        # database they do not need.
        redis_detail = database["detail"]

    # A complete KV pair is not enough when the owner explicitly chose the file
    # database: honour that choice rather than silently switching databases.
    # Vercel has no durable file system, so a file choice there is a blocking
    # misconfiguration even when Redis credentials happen to be available.
    selected_backend = storage.state_backend()
    wants_redis = selected_backend != "file"
    health = store_health() if (on_vercel and redis_ok and wants_redis) else None
    state_broken = bool(health is not None and not health["ok"])
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
        state_detail = (redis_detail if (redis_ok or detected_elsewhere) else
                        ("Auto mode will use data.json on this host. Set "
                         "P2P_STATE_BACKEND=redis and connect a KV store to share state."))

    if state_broken:
        # Credentials that are present but dead are the failure you never see:
        # every page reports a healthy database while the bot has already
        # forgotten its group, merchants and prices.  Say so — and keep the
        # deployment "not ready" so the form that repairs it stays open.
        state_ok = False
        state_detail = (f"The database credentials are set, but the store does not answer "
                        f"({health['detail']}). The bot forgets its group, merchants and "
                        "prices until it is reachable again. Restore the database credentials "
                        "through the deployment environment; database credentials are not "
                        "accepted on this web page.")

    checks = [
        _setup_check("bot_token", "Telegram bot token", True, token_ok,
                     token_detail, ("BOT_TOKEN",), source("BOT_TOKEN")),
        _setup_check("admin_ids", "Telegram admin ID", True, admins_ok,
                     admins_detail, ("ADMIN_IDS",), source("ADMIN_IDS")),
        _setup_check("state_store", "State database", on_vercel, state_ok,
                     state_detail, ("KV_REST_API_URL", "KV_REST_API_TOKEN", "P2P_STATE_BACKEND"),
                     broken=state_broken,
                     detected=database["where"] if database["found"] else ""),
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
        elif state_broken:
            names = "a reachable KV/Redis database"
            message = ("The bot cannot work until its database answers again: the credentials "
                       "are set, but the store does not respond, so the group, the merchants "
                       "and the prices are being forgotten. Restore the KV / Redis credentials "
                       "in the deployment environment and redeploy. Database credentials are "
                       "not accepted on this web page; /database in Telegram manages a custom "
                       "store after the bot is running.")
        elif detected_elsewhere:
            names = "KV_REST_API_URL, KV_REST_API_TOKEN"
            message = (f"This project already has a database — detected on {database['where']} "
                       "— but this deployment cannot see its credentials yet. No new database "
                       "is needed: press Connect to this project again in Vercel → Storage "
                       "(that is what writes KV_REST_API_URL + KV_REST_API_TOKEN into the "
                       "environment) and redeploy. Opening this page again then registers the "
                       "Telegram webhook by itself.")
        else:
            message = ("The deployment is missing a persistent KV/Redis store — connect Upstash "
                       "for Redis or Vercel KV through the hosting provider's Storage / "
                       "environment settings and redeploy. The Web UI does not accept database "
                       "credentials; once the bot is running, /database in Telegram can select "
                       "a custom REST database.")
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
    # "configured but not working" — the state that used to hide behind a green
    # checklist and a locked form.  The setup page stays editable while it is
    # set, because repairing the deployment is exactly what the form is for.
    broken = [check["name"] for check in checks if check.get("broken")]
    return {
        "ready": not blocking,
        "healthy": not blocking and not broken,
        "broken": broken,
        "serverless": on_vercel,
        "checks": checks,
        "missing": names,
        "required_missing": [check["name"] for check in blocking],
        "message": message,
        "store_health": health or ({"ok": None, "detail": "not checked",
                                    "backend": selected_backend} if on_vercel else None),
        # read-only database detection — no credentials or editable controls
        "database": database,
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
    "2. For Vercel's initial persistent store, connect Upstash for Redis or Vercel KV in "
    "Vercel → Storage, or set the Redis REST pair in the deployment environment and redeploy. "
    "The Web UI does not accept database credentials. After the bot starts, use /database in "
    "a private Telegram chat to add or switch a custom Redis REST database.",
    "3. Redeploy (Deployments → ⋯ → Redeploy — changed *environment variables* only apply to new "
    f"deploys), then reopen this page: it registers the Telegram webhook by itself. Settings saved "
    f"on {SETUP_PATH} are applied immediately, no redeploy needed.",
]


def setup_steps(status: dict | None = None) -> list[str]:
    """Return first-start guidance with read-only information about existing storage."""
    steps = list(SETUP_STEPS)
    database = (status or {}).get("database") or {}
    if database.get("found"):
        where = str(database.get("where") or "the connected store")
        if database.get("wired"):
            steps[1] = (f"2. Database detected and available: {where}. The Web UI only reports "
                        "database status; use /database in a private Telegram chat to add or "
                        "switch a custom Redis REST database.")
        else:
            steps[1] = (f"2. Database detected on {where}, but this deployment does not have "
                        "its credentials yet. Connect the existing store to the project in "
                        "Vercel → Storage and redeploy; the Web UI cannot enter credentials. "
                        "After startup, use /database in Telegram to manage a custom store.")
    return steps


# ── saving the required settings (browser form + terminal wizard) ──────────
def _bearer(request: "Request") -> str:
    header = request.header("authorization")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


SETUP_LINK_KEY = "setup_link"
SETUP_LINK_TTL = 15 * 60.0          # a reconfigure link is valid for 15 minutes
SETUP_LINK_COOLDOWN = 60.0          # …and at most one is handed out per minute
SETUP_LINK_FIELD = "action"
SETUP_LINK_ACTION = "send-link"

# The link that is currently out, kept in memory as well: if the state store is
# the very thing that is broken, the record cannot be persisted — and that is
# exactly when the owner needs the form.  A cold start simply starts over.
_pending_setup_link: dict[str, Any] = {"token": "", "expires": 0.0}


def _setup_link_record(bot_module) -> dict:
    """The persisted one-time link record (``{}`` when there is none)."""
    record = (bot_module.state.get(SETUP_LINK_KEY) if bot_module is not None else None)
    return record if isinstance(record, dict) else {}


def _active_setup_link() -> tuple[str, float]:
    """``(token, expires)`` of a link this instance handed out and still honours."""
    token = str(_pending_setup_link.get("token") or "")
    expires = float(_pending_setup_link.get("expires") or 0.0)
    if token and time.time() < expires:
        return token, expires
    return "", 0.0


def issue_setup_link_token(bot_module=None) -> tuple[str, float, bool]:
    """Create the one-time token a reconfigure link carries.

    Returns ``(token, expires, fresh)``.  ``fresh`` is ``False`` while the
    cooldown from the previous link is still running: the button is public, so
    without that a stranger could make the bot message the admins in a loop.
    """
    if bot_module is None:
        bot_module = sys.modules.get("bot")
    record = _setup_link_record(bot_module)
    issued_at = max(float(record.get("issued_at") or 0.0),
                    float(_pending_setup_link.get("issued_at") or 0.0))
    token, expires = _active_setup_link()
    if token and time.time() - issued_at < SETUP_LINK_COOLDOWN:
        return token, expires, False

    token = secrets.token_urlsafe(32)
    expires = time.time() + SETUP_LINK_TTL
    _pending_setup_link.update(token=token, expires=expires, issued_at=time.time())
    if bot_module is not None:
        try:
            # Only the fingerprint is stored: anybody who can read the database
            # must not be able to rebuild the link from it.
            bot_module.state[SETUP_LINK_KEY] = {
                "fp": hashlib.sha256(token.encode()).hexdigest(),
                "expires": expires,
                "issued_at": time.time(),
            }
            bot_module.save()
        except Exception as exc:                     # pragma: no cover - defensive
            log.warning("the reconfigure link could not be stored: %s", exc)
    return token, expires, True


def setup_link_valid(token: str, bot_module=None) -> bool:
    """Whether ``token`` is a live one-time reconfigure link."""
    candidate = str(token or "").strip()
    if not candidate:
        return False
    if bot_module is None:
        bot_module = sys.modules.get("bot")
    record = _setup_link_record(bot_module)
    stored = str(record.get("fp") or "")
    if stored:
        if not secrets.compare_digest(stored, hashlib.sha256(candidate.encode()).hexdigest()):
            return False
        return time.time() < float(record.get("expires") or 0.0)
    # Nothing persisted (the store is the thing that is broken): fall back to
    # what this instance handed out, which is all one instance can honour.
    active, expires = _active_setup_link()
    return bool(active) and secrets.compare_digest(active, candidate) and time.time() < expires


def consume_setup_link(token: str, bot_module=None) -> bool:
    """Spend a one-time reconfigure link so it cannot be replayed."""
    if not setup_link_valid(token, bot_module):
        return False
    if bot_module is None:
        bot_module = sys.modules.get("bot")
    if bot_module is not None:
        try:
            bot_module.state.pop(SETUP_LINK_KEY, None)
            bot_module.save()
        except Exception as exc:                     # pragma: no cover - defensive
            log.warning("the reconfigure link could not be spent: %s", exc)
    _pending_setup_link.update(token="", expires=0.0)
    return True


async def issue_setup_link(app=None) -> dict:
    """One button worth of work: hand the owner a link that reopens the form.

    The setup page is public, so it cannot simply unlock itself — anybody who
    guessed ``/api/setup`` would be able to point the bot at their own admin id.
    Instead the link is delivered **to the admins in Telegram**: only they can
    receive it, it is single use and expires.  Returns a secret-free report.
    """
    bot_module = get_bot()
    app = app or await get_application()
    token, expires, fresh = issue_setup_link_token(bot_module)
    try:
        url = public_base_url() + SETUP_PATH + "?secret=" + token
    except ConfigError as exc:                       # no PUBLIC_URL and no Vercel host
        url = SETUP_PATH + "?secret=" + token
        log.warning("reconfigure link without a public base URL: %s", exc)

    if not fresh:
        # A link is already out and the admins already have it — messaging them
        # again on every click would turn a public button into a spam lever.
        return {"ok": True, "url": url, "delivered": [], "errors": [], "resent": False,
                "expires_in": int(max(0, expires - time.time()))}

    delivered: list[str] = []
    errors: list[str] = []
    minutes = max(1, int(round((expires - time.time()) / 60.0)))
    text = ("🔧 <b>Reconfigure bot settings</b>\n\n"
            "Tap the link to change its token, admins, asset/fiat pair or interval. "
            "Database settings are managed separately with /database in Telegram. It works "
            f"once and expires in {minutes} minutes — request a new one any time from "
            f"{SETUP_PATH}.")
    for admin in sorted(getattr(bot_module, "ADMINS", ()) or ()):
        try:
            await app.bot.send_message(chat_id=int(admin), text=text, parse_mode="HTML",
                                       reply_markup={"inline_keyboard":
                                                     [[{"text": "🔧 Reconfigure the bot",
                                                        "url": url}]]},
                                       disable_web_page_preview=True)
            delivered.append(str(admin))
        except Exception as exc:
            errors.append(f"admin {admin}: {type(exc).__name__}: {exc}")
    return {"ok": bool(delivered), "url": url, "delivered": delivered, "errors": errors,
            "resent": True, "expires_in": int(max(0, expires - time.time()))}


async def setup_command(update, context) -> None:
    """``/setup`` in Telegram — the same link the web UI's button sends."""
    user = getattr(update, "effective_user", None)
    bot_module = get_bot()
    if not user or not bot_module.is_admin(update):
        return
    try:
        report = await issue_setup_link(app=getattr(context, "application", None))
    except Exception as exc:                      # pragma: no cover - network, config
        log.warning("/setup could not issue a link: %s", exc)
        report = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"], "expires_in": 0}
    if report["ok"]:
        await update.message.reply_html(
            "🔧 Here is your reconfigure link — it works once and expires in "
            f"{max(1, report['expires_in'] // 60)} minutes:\n\n{html.escape(report['url'])}")
    else:
        await update.message.reply_html(
            "⚠️ I could not send you the reconfigure link: "
            f"{html.escape('; '.join(report['errors']) or 'unknown error')}")


def setup_write_allowed(request: "Request", status: dict | None = None) -> tuple[bool, str]:
    """Who may store settings through ``POST /api/setup``.

    The page is public, so the rule is deliberately blunt:

    * ``SETUP_SECRET`` set → the request must carry it (form field, ``?secret=``
      or ``Authorization: Bearer``).  This is the recommended way to lock the
      form *before* the first deployment.
    * a **one-time reconfigure link** (the ⚙️/🔧 button) → accepted.  Only the
      admins receive one, in Telegram, so proving the secret proves the admin.
    * the deployment is **not** ready, or something it needs is **broken** →
      anybody may finish or repair the setup.  It is unusable until then, and
      stored values never overrule environment variables, so there is nothing
      to take over.
    * the deployment **is** ready and healthy → refused.  An anonymous form must
      not be able to point a running bot at somebody else's admin id.
    """
    status = status or setup_status()
    given = (request.form().get("SETUP_SECRET") or request.query.get("secret")
             or _bearer(request))
    secret = env("SETUP_SECRET")
    if secret and matches(given, secret):
        return True, ""
    if setup_link_valid(given):
        return True, ""
    if secret:
        return False, ("SETUP_SECRET is set for this deployment — send it with the request "
                       "(form field, ?secret= or 'Authorization: Bearer') to change the settings, "
                       "or press 🔧 Reconfigure on the setup page to get a link in Telegram.")
    if status["ready"] and not status.get("broken"):
        return False, ("locked: this deployment is configured and working, so the form is "
                       "closed — an open page must not be able to change a running bot. "
                       "Press 🔧 Reconfigure on the setup page to get a one-time link in "
                       "Telegram, set SETUP_SECRET in the environment, or run "
                       " python setup_cli.py  in a terminal — it writes to the same store.")
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


def _reload_bot() -> bool:
    """Re-run ``bot.py`` so a changed token, admin id or pair take effect.

    ``bot.TOKEN``, ``bot.ADMINS``, ``bot.ASSET`` and ``bot.STORE`` are set once,
    at import time, and a warm container keeps that module for its whole life.
    Without this a saved setting would be written to the store and then ignored
    until the next cold start — the reconfigure form would look like it did
    nothing.  ``importlib.reload`` re-executes the module in place, so the
    references other modules already hold stay valid.
    """
    global _bot
    module = sys.modules.get("bot")
    if module is None:                            # nothing imported yet — nothing to redo
        return False
    _apps.clear()                                 # the cached PTB app holds the old token
    _locks.clear()
    try:
        importlib.reload(module)
    except Exception as exc:                      # pragma: no cover - defensive
        log.warning("bot.py could not be reloaded after a settings change: %s", exc)
        return False
    _bot = module
    try:
        module.refresh_state()
    except Exception as exc:                      # pragma: no cover - defensive
        log.debug("state refresh after reload failed: %s", exc)
    return True


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
    """Store non-database settings submitted by the browser setup form.

    Database credentials/backend selection are intentionally excluded from the
    Web UI; admins manage custom REST credentials in Telegram with /database.
    The browser may still set the bot token, admins, pair and polling interval.
    """
    values = {str(name).upper(): value for name, value in (values or {}).items()}
    notes: list[str] = []
    warnings: list[str] = []
    database_fields = {
        "P2P_STATE_BACKEND", "P2P_STORAGE_BACKEND",
        "KV_REST_API_URL", "KV_REST_API_TOKEN",
        "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
        "REDIS_REST_URL", "REDIS_REST_TOKEN",
        storage.CUSTOM_REDIS_ENV_PAIR[0], storage.CUSTOM_REDIS_ENV_PAIR[1],
    }
    if any(str(values.get(name) or "").strip() for name in database_fields):
        return _save_report(
            False,
            errors=["Database credentials and backend selection are not accepted on the Web UI. "
                    "Use /database in a private Telegram chat to manage a custom database."],
            warnings=warnings, notes=notes)

    browser_fields = {name: values.get(name) for name in
                      ("BOT_TOKEN", "ADMIN_IDS", "ASSET", "FIAT", "INTERVAL")
                      if name in values}
    cleaned, errors = runtime_config.validate(browser_fields)
    if errors:
        return _save_report(False, errors=errors, warnings=warnings, notes=notes)
    if not cleaned:
        return _save_report(False, errors=["Nothing to save — the form was empty."],
                            warnings=warnings, notes=notes)

    if verify and cleaned.get("BOT_TOKEN"):
        ok, detail = runtime_config.verify_bot_token(cleaned["BOT_TOKEN"])
        if ok is False:
            return _save_report(False, errors=[detail + " Nothing was saved."],
                                warnings=warnings, notes=notes)
        notes.append(detail)

    store_before = runtime_config.describe(ROOT)
    saved = runtime_config.save(cleaned, ROOT)
    if any(saved.get(name) != value for name, value in cleaned.items()):
        warnings.append(f"The settings could not be written to {store_before['store']} — "
                        "the deployment may have no writable storage at all.")

    # Environment variables keep priority on other instances. The current
    # function uses the new values immediately, and a warm bot reloads them.
    shadowed = [name for name in cleaned
                if (os.getenv(name) or "").strip()
                and name not in runtime_config.injected()
                and (os.getenv(name) or "").strip() != cleaned[name]]
    for name, value in cleaned.items():
        os.environ[name] = value
    runtime_config.mark(saved)
    runtime_config.invalidate()
    reset_store_probe()
    reset_database_detection()
    try:
        if _reload_bot():
            notes.append("This instance picked up the new settings at once — no redeploy needed.")
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("applying the new settings to this instance failed: %s", exc)
    _refresh_store()

    for name in shadowed:
        warnings.append(f"{name} is also set in the deployment environment — other instances keep "
                        f"using that value until you change or remove it there.")
    if _looks_like_vercel() and not runtime_config.describe(ROOT)["persistent"]:
        warnings.append("These settings are in temporary storage and will be lost on a cold start. "
                        "Connect a KV/Redis store in the deployment environment and redeploy.")

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
    # A configured deployment is editable in the browser when the owner has
    # supplied SETUP_SECRET, or arrived with a one-time reconfigure link.
    # ``edit_authorized`` is set only after the GET request proves one of them;
    # it is deliberately not persisted in status.
    if status["ready"] and not status.get("setup_secret") and not status.get("edit_authorized"):
        broken_marks = status.get("broken") or []
        if broken_marks:
            lead = ("🔧 <b>Something needs repairing</b> — the form below is open so you can "
                    "fix it without a redeploy.")
        else:
            lead = ("🔒 <b>Everything is in place, so the form is closed</b> — an open page "
                    "must not be able to change a running bot.")
        return (
            '<section class="panel" aria-labelledby="form-title">'
            '<div class="panel-heading"><div><h2 id="form-title">Reconfigure the bot</h2>'
            f'<p class="subtle">{"Needs repair" if broken_marks else "Locked"} — this '
            f'deployment is configured.</p></div></div>'
            f'<p class="locked">{lead} Press the button and the bot sends you a '
            f'one-time link in Telegram — it works once and expires, so it is the only thing '
            f'that can reopen this form.</p>'
            f'<form method="post" action="{html.escape(SETUP_PATH)}" class="actions">'
            f'<input type="hidden" name="{SETUP_LINK_FIELD}" value="{SETUP_LINK_ACTION}">'
            f'<button class="button" type="submit">🔧 Reconfigure — send me a link</button>'
            f'</form>'
            f'<p class="where">The link goes to the admin(s) listed in '
            f'<code>ADMIN_IDS</code> — if Telegram cannot be reached, run '
            f'<code>python setup_cli.py</code> in a terminal (same store), or set '
            f'<code>SETUP_SECRET</code> in the deployment environment and open '
            f'<code>{html.escape(SETUP_PATH)}?secret=…</code>.</p></section>')

    if status.get("edit_authorized"):
        # Keep the authorization across the form submit without putting the
        # secret in the URL again. The value is password-like and never echoed
        # into the page; POST authorization still validates it server-side.
        fields_hidden = '<input type="hidden" name="SETUP_SECRET" value="' + html.escape(
            str(status.get("edit_secret", ""))) + '">'
    else:
        fields_hidden = ""


    fields = [
        _field("BOT_TOKEN", "Telegram bot token", "From @BotFather → /newbot.",
               secret=True, placeholder="123456:ABCdef…", stored=masked.get("BOT_TOKEN", "")),
        _field("ADMIN_IDS", "Your Telegram ID(s)",
               "Numeric, comma-separated — @userinfobot tells you yours.",
               value=values.get("ADMIN_IDS", ""), placeholder="123456789",
               stored=masked.get("ADMIN_IDS", "")),
    ]
    advanced = "".join([
        _field("ASSET", "Asset", "What is traded.", value=values.get("ASSET", ""),
               placeholder="USDT"),
        _field("FIAT", "Fiat", "The currency prices are shown in.",
               value=values.get("FIAT", ""), placeholder="USD"),
        _field("INTERVAL", "Check every N seconds", "Polling installs only; Vercel uses its cron.",
               value=values.get("INTERVAL", ""), placeholder="60"),
    ])
    if needs_secret and not status.get("edit_authorized"):
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
          <p class="subtle">Bot settings are also available through the terminal wizard.</p></div>
        <span class="badge {'ready' if persistent else 'optional'}">{'persistent store' if persistent else 'temporary store'}</span>
      </div>
      <form method="post" action="{html.escape(SETUP_PATH)}" class="form">
        {fields_hidden}{''.join(fields)}
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
        <p class="subtle">Bot settings use the same store; the terminal also supports backend configuration.</p></div></div>
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
        with Telegram before saving. It can select the state backend and set a KV REST pair;
        <code>--vercel-env</code> writes environment variables to Vercel and requires a redeploy.
        For a custom database after startup, use Telegram <code>/database</code>.</p>
    </section>"""


def _setup_page(message: str, status: dict, banner: tuple[str, str] | None = None,
                form_values: dict | None = None) -> str:
    """Render a polished, actionable first-start web UI.

    The page presents the bot-settings form alongside terminal and deployment
    alternatives. The form (``POST /api/setup``) stores bot settings in
    ``runtime_config`` and applies them without a redeploy. The terminal wizard
    shares that store but also supports backend/REST configuration; deployment
    environment variables are required for ``CRON_SECRET`` and Vercel's initial
    persistent database.

    Database credentials and backend selection are intentionally not part of
    the browser UI. Vercel's first persistent store is connected in deployment
    settings; after startup, admins can add or switch a custom REST database in
    the private Telegram ``/database`` menu.

    Secrets are accepted over HTTPS and stored in the deployment's own state
    store; they are never echoed back, and the form closes (or requires
    ``SETUP_SECRET``) as soon as the deployment is ready.
    """
    checks = status.get("checks") or []
    cards = []
    for check in checks:
        if check.get("ok"):
            icon, badge, css = "✓", "Ready", "ready"
        elif check.get("broken"):
            # Configured, but not working — an outage, not a missing setting.
            icon, badge, css = "✕", "Not responding", "broken"
        elif check.get("required"):
            icon, badge, css = "!", "Needs action", "required"
        else:
            icon, badge, css = "·", "Recommended", "optional"
        variables = " · ".join(f"<code>{html.escape(str(item))}</code>"
                              for item in check.get("variables") or [])
        source = (f' · <i>from the setup page</i>' if check.get("source") == "setup" else "")
        detected = (f' · <i>detected: {html.escape(str(check.get("detected", "")))}</i>'
                    if check.get("detected") else "")
        cards.append(
            f'<article class="check {css}">'
            f'<span class="check-icon" aria-hidden="true">{icon}</span>'
            f'<div class="check-copy"><h3>{html.escape(str(check.get("label", "")))}</h3>'
            f'<p>{html.escape(str(check.get("detail", "")))}</p>'
            f'<small>{variables}{source}{detected}</small></div>'
            f'<span class="badge">{badge}</span></article>')

    checks_markup = "\n".join(cards)
    banner_markup = _banner(banner)
    form_markup = _setup_form(status, form_values)
    detection = status.get("database") or {}
    terminal_markup = _terminal_panel(status)
    missing = status.get("missing") or "the required environment variables"
    if detection.get("found") and detection.get("wired"):
        database_step = ("A persistent KV / Redis store is already available to this "
                         "deployment.")
    elif detection.get("found"):
        database_step = ("A KV / Redis store was detected, but its credentials are not in this "
                         "deployment yet. Connect it through Vercel Storage and redeploy.")
    else:
        database_step = ("Before the bot can start on Vercel, connect a KV / Redis store through "
                         "Vercel Storage or add its REST URL and token to the deployment "
                         "environment, then redeploy.")
    storage_step_markup = (
        '<article class="step"><span class="number">2</span><div>'
        '<h3>Provide persistent storage</h3><p>' + html.escape(database_step) + ' '
        'This web UI does not accept database credentials. Once the bot is running, use '
        '<code>/database</code> in a private Telegram chat to add or switch a custom Redis '
        'REST database.</p></div></article>')
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
    .broken .check-icon {{ color: #35100e; background: var(--red); }}
    .optional .check-icon {{ color: #3b2700; background: var(--amber); }}
    .check-copy {{ min-width: 0; flex: 1; }}
    .check h3 {{ margin: 0; font-size: 15px; }}
    .check p {{ color: var(--muted); margin: 2px 0 4px; }}
    .check small {{ color: #c9d0e5; }}
    .badge {{ flex: 0 0 auto; margin-top: 2px; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 750; white-space: nowrap; }}
    .ready .badge {{ color: #73f0bf; background: #35d3991a; }}
    .required .badge {{ color: #ffabab; background: #ff8c8c1a; }}
    .broken .badge {{ color: #ffabab; background: #ff8c8c1a; }}
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

    <section class="panel" aria-labelledby="check-title">
      <div class="panel-heading"><div><h2 id="check-title">Environment checklist</h2><p class="subtle">Values are checked without displaying any secrets.</p></div></div>
      <div class="checks">{checks_markup}</div>
    </section>
    {terminal_markup}

    <section class="panel" aria-labelledby="steps-title">
      <h2 id="steps-title">Finish setup in Vercel</h2>
      <div class="steps">
        <article class="step"><span class="number">1</span><div><h3>Set the Telegram credentials</h3><p>Use the form above, or <b>Vercel dashboard → your project → Settings → Environment Variables</b>: <code>BOT_TOKEN</code> from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> and <code>ADMIN_IDS</code> from <a href="https://t.me/userinfobot" target="_blank" rel="noopener">@userinfobot</a>.</p><a href="https://vercel.com/dashboard" target="_blank" rel="noopener">Open Vercel dashboard ↗</a></div></article>
        {storage_step_markup}
        <article class="step"><span class="number">3</span><div><h3>Redeploy only if you changed variables</h3><p>Settings saved on this page apply at once. Environment variables need <b>Deployments → ⋯ → Redeploy</b>, because they only apply to new deployments. Then reopen this page: it registers the Telegram webhook automatically.</p></div></article>
      </div>
    </section>

    <section class="why" aria-label="Why these settings are needed">
      <article><h3>🔐 Bot credentials</h3><p>Telegram uses the token to deliver updates and the admin ID to protect the control panel.</p></article>
      <article><h3>💾 Persistent state</h3><p>Vercel functions are short-lived. Redis keeps the group, merchants, settings, and prices available on every request.</p></article>
      <article><h3>🔗 Automatic webhook</h3><p>Once the variables are present, opening this page checks the token and registers <code>/api/webhook</code>.</p></article>
    </section>

    <div class="actions"><a class="button secondary" href="/api/setup">↻ Check setup again</a><a class="button secondary" href="/api/webhook?register=0">View diagnostics</a><a class="button secondary" href="https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/blob/main/VERCEL.md" target="_blank" rel="noopener">Deployment guide ↗</a></div>
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
                          "steps": setup_steps(status), "setup_page": SETUP_PATH,
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
    # Same-origin links (the setup page, a re-check) stay in this tab; only the
    # fixed platform/documentation URLs open a new one.
    rendered = []
    for label, url in links or []:
        same_origin = str(url).startswith("/")
        target = "" if same_origin else ' target="_blank" rel="noopener"'
        rendered.append(f'\n    <a class="button" href="{html.escape(url, quote=True)}"'
                        f'{target}>{html.escape(label)}</a>')
    buttons = "".join(rendered)
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
