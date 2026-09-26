"""The required settings, kept outside the deployment's environment variables.

A Vercel deployment reads ``BOT_TOKEN`` / ``ADMIN_IDS`` from its environment, and
changing them means editing the dashboard and redeploying.  That is fine, but it
is not the only way: the same values can be handed over

* in the **browser**, on the first-start setup page (``POST /api/setup``), or
* in the **terminal**, with ``python setup_cli.py`` (which you can also skip and
  finish in the browser later — ``--skip`` prints the link and exits).

Both write into this module's store, and :func:`apply` copies whatever the
environment does not already provide into ``os.environ`` **before** ``bot.py`` is
imported, so the very same configuration code (``env_or_cli``) serves both paths.

Where the values live follows ``storage.py``:

``redis``  the connected KV/Redis REST store, under the key ``P2P_CONFIG_KEY``
           (default ``p2p-price-bot:config`` — separate from the bot's state key,
           so ``python setup_cli.py`` and the setup page never touch groups,
           merchants or prices).  This is the persistent option on Vercel.
``file``   ``runtime_config.json`` next to ``data.json`` (override with
           ``P2P_RUNTIME_CONFIG_FILE``).  Persistent on a VPS/Docker/local
           install; **ephemeral** on Vercel, whose filesystem is thrown away with
           the instance — the setup page says so when it saves there.

Nothing here ever raises: an unreadable store logs a warning and yields ``{}``,
because a deployment that cannot read its settings must still be able to render
the page that asks for them.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import storage

log = logging.getLogger("p2p-bot.config")

#: Settings the browser form and the terminal wizard may store.  Everything else
#: stays environment-only on purpose — ``CRON_SECRET`` for example is sent by
#: Vercel's own cron from the *deployment* environment, so a stored copy would
#: lock the cron out instead of protecting it.
SUPPORTED = ("BOT_TOKEN", "ADMIN_IDS", "ASSET", "FIAT", "INTERVAL")
REQUIRED = ("BOT_TOKEN", "ADMIN_IDS")
SECRETS = ("BOT_TOKEN",)

CONFIG_FILE = "runtime_config.json"
DEFAULT_CONFIG_KEY = "p2p-price-bot:config"

#: How long a warm container reuses the stored values without re-reading them
#: (one Redis round-trip per request is the alternative; the bot already reads
#: its state on every request, so this keeps setup off that path).
CACHE_SECONDS = 30.0

_cache: dict[str, Any] = {"at": 0.0, "values": {}}
_injected: set[str] = set()          # names apply() wrote into os.environ


# ── where the settings are kept ────────────────────────────────────────────
def config_key() -> str:
    """Redis key the settings live under (never the bot's state key)."""
    return (os.getenv("P2P_CONFIG_KEY") or "").strip() or DEFAULT_CONFIG_KEY


def config_path(base_dir: str | Path | None = None) -> Path:
    """File the settings live in when there is no Redis (``runtime_config.json``)."""
    explicit = (os.getenv("P2P_RUNTIME_CONFIG_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_file = (os.getenv("P2P_STATE_FILE") or "").strip()
    data_dir = (os.getenv("P2P_DATA_DIR") or "").strip()
    if state_file:
        parent = Path(state_file).expanduser().parent
    elif data_dir:
        parent = Path(data_dir).expanduser()
    else:
        parent = Path(base_dir or Path(__file__).resolve().parent)
    path = parent / CONFIG_FILE
    if storage.writable(path):
        return path
    return Path(tempfile.gettempdir()) / CONFIG_FILE      # read-only code dir


def config_store(base_dir: str | Path | None = None):
    """The store object for these settings (Redis key, or the JSON file)."""
    redis = storage.redis_config()
    if redis:
        return storage.RedisStore(redis[0], redis[1], key=config_key())
    return storage.FileStore(config_path(base_dir))


# ── read / write ───────────────────────────────────────────────────────────
def load(base_dir: str | Path | None = None) -> dict[str, str]:
    """Stored settings as ``{ENV_NAME: value}`` — empty when there are none."""
    try:
        data = config_store(base_dir).load()
    except Exception as exc:                              # pragma: no cover - defensive
        log.warning("Could not read the stored settings (%s)", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: str(value).strip() for name, value in data.items()
            if name in SUPPORTED and str(value).strip()}


def save(values: dict, base_dir: str | Path | None = None) -> dict[str, str]:
    """Merge ``values`` into the store and return what it now holds.

    The returned dict is read back from the store, so a caller can compare it
    with what it asked for and notice a store that silently refused the write
    (``storage`` never raises).
    """
    merged = load(base_dir)
    for name, value in (values or {}).items():
        name = str(name).upper()
        if name not in SUPPORTED:
            continue
        text = str(value or "").strip()
        if text:
            merged[name] = text
    store = config_store(base_dir)
    store.save(merged)
    written = load(base_dir)
    if any(written.get(name) != merged[name] for name in merged):
        log.warning("Stored settings were not written to %s", store.describe())
    return written


def clear(base_dir: str | Path | None = None) -> bool:
    """Forget every stored setting (the environment variables are untouched)."""
    invalidate()
    store = config_store(base_dir)
    store.save({})
    return not load(base_dir)


def describe(base_dir: str | Path | None = None) -> dict:
    """Public, secret-free description of where the settings would be kept."""
    store = config_store(base_dir)
    return {"store": store.describe(), "backend": store.backend,
            "persistent": store.backend == "redis"}


# ── into the process environment ───────────────────────────────────────────
def invalidate() -> None:
    """Drop the cached copy (after a write, so the next read sees it)."""
    _cache.update(at=0.0, values={})


def apply(base_dir: str | Path | None = None, force: bool = False) -> dict[str, str]:
    """Copy the stored settings into ``os.environ`` where nothing is set yet.

    Real environment variables always win: a value in the Vercel dashboard is the
    owner's decision, and the setup page cannot overrule it.  Called before
    ``bot.py`` is imported (``serverless.setup_status``) and again from
    ``bot.load_config``, so a polling install sees the same values.
    """
    now = time.monotonic()
    if _cache["values"] and now - float(_cache["at"]) < CACHE_SECONDS:
        values = dict(_cache["values"])
    else:
        values = load(base_dir)
        _cache.update(at=now, values=values)
    applied: dict[str, str] = {}
    for name, value in values.items():
        if force or not (os.getenv(name) or "").strip():
            os.environ[name] = value
            applied[name] = value
            _injected.add(name)
    return applied


def mark(names) -> None:
    """Record that these values came from the store (right after writing them)."""
    _injected.update(str(name).upper() for name in names or ())


def injected() -> set[str]:
    """Names this process took from the store (so a page can say where a value came from)."""
    return set(_injected)


# ── what the pages may show ────────────────────────────────────────────────
def mask(value: str) -> str:
    """A value that proves it is there without revealing it."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return "•" * len(text)
    return f"{text[:3]}{'•' * 6}{text[-2:]}"


def summary(base_dir: str | Path | None = None) -> dict:
    """What is stored, where, and how it looks redacted (safe for the browser)."""
    values = load(base_dir)
    info = describe(base_dir)
    return {**info, "saved": sorted(values),
            "masked": {name: mask(value) for name, value in sorted(values.items())},
            "missing": [name for name in REQUIRED if name not in values]}


# ── validation (shared by the browser form and the terminal wizard) ────────
_IDENT = re.compile(r"^[A-Z0-9]{2,12}$")


def _clean_token(value: str) -> tuple[str, str]:
    """``(clean, error)`` for a ``BOT_TOKEN`` — Telegram sends ``<digits>:<secret>``."""
    token = str(value or "").strip()
    if token.count(":") != 1 or not token.split(":", 1)[0].isdigit():
        return "", ("BOT_TOKEN does not look like a Telegram token — @BotFather sends "
                    "<numeric id>:<secret> (one colon).")
    return token, ""


def _clean_admins(value: str) -> tuple[str, str]:
    admins = str(value or "").replace(" ", "").strip().strip(",")
    parts = [part for part in admins.split(",") if part]
    if not parts or not all(part.isdigit() for part in parts):
        return "", ("ADMIN_IDS must be one or more numeric Telegram IDs, comma-separated "
                    "(send /start to @userinfobot to get yours).")
    return ",".join(parts), ""


def validate(values: dict) -> tuple[dict[str, str], list[str]]:
    """Split a submission into ``(usable values, human readable problems)``.

    Empty fields are ignored rather than rejected: both the browser form and the
    terminal wizard are allowed to fill in one setting at a time.  Keys are
    matched case-insensitively, so ``curl -d asset=usdt`` works as well.
    """
    values = {str(name).upper(): value for name, value in (values or {}).items()}
    cleaned: dict[str, str] = {}
    errors: list[str] = []

    token = str(values.get("BOT_TOKEN") or "").strip()
    if token:
        clean, error = _clean_token(token)
        if error:
            errors.append(error)
        else:
            cleaned["BOT_TOKEN"] = clean

    admins = str(values.get("ADMIN_IDS") or "").strip()
    if admins:
        clean, error = _clean_admins(admins)
        if error:
            errors.append(error)
        else:
            cleaned["ADMIN_IDS"] = clean

    for name in ("ASSET", "FIAT"):
        raw = str(values.get(name) or "").strip().upper()
        if not raw:
            continue
        if _IDENT.match(raw):
            cleaned[name] = raw
        else:
            errors.append(f"{name} should be a short code such as USDT or USD.")

    interval = str(values.get("INTERVAL") or "").strip()
    if interval:
        if interval.isdigit() and 5 <= int(interval) <= 86400:
            cleaned["INTERVAL"] = str(int(interval))
        else:
            errors.append("INTERVAL must be a number of seconds between 5 and 86400.")

    return cleaned, errors


def verify_bot_token(token: str, timeout: float = 8.0) -> tuple[bool | None, str]:
    """Ask Telegram whether the token works before storing it.

    ``(True, …)`` the token is good, ``(False, …)`` Telegram rejected it (do not
    store it), ``(None, …)`` Telegram could not be reached — the token is kept,
    because an offline host must still be able to finish its setup.
    """
    import httpx

    token = str(token or "").strip()
    try:
        reply = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=timeout)
    except Exception as exc:                              # offline, DNS, timeout …
        return None, (f"Telegram could not be reached ({type(exc).__name__}) — "
                      "the token was stored without being verified.")
    try:
        data = reply.json()
    except Exception:                                     # pragma: no cover - non-JSON reply
        data = {}
    if reply.status_code == 200 and data.get("ok"):
        who = (data.get("result") or {}).get("username") or "no username"
        return True, f"Telegram accepted the token (@{who})."
    why = data.get("description") or f"HTTP {reply.status_code}"
    return False, f"Telegram rejected the token: {why}"
