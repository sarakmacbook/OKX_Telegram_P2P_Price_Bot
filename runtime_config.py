"""Bot settings and Telegram-selected custom database credentials, kept outside
the deployment's environment variables.

A Vercel deployment can keep ``BOT_TOKEN`` / ``ADMIN_IDS`` in its environment,
or they can be stored without a redeploy:

* the **browser** form (``POST /api/setup``) handles bot credentials and
  optional asset/fiat/interval settings only;
* the **terminal** wizard (``python setup_cli.py``) handles the same bot settings
  and can additionally select the state backend and configure a deployment KV /
  Redis REST pair;
* custom Redis REST credentials entered in a private Telegram chat are stored
  here separately from the deployment-managed pair; a custom database of **any**
  type (SQLite, MySQL/MariaDB, PostgreSQL, MongoDB, Redis over TCP) entered the
  same way is kept as one connection URI (``P2P_CUSTOM_DB_URL``).

The browser form and terminal wizard write bot settings into this module's
store, and :func:`apply` copies values not already supplied by the environment
into ``os.environ`` **before** ``bot.py`` is imported. This keeps the bot
configuration path consistent without exposing database credentials in the Web UI.

Where the values live follows ``storage.py``:

``redis``     the connected KV/Redis REST store, under the key ``P2P_CONFIG_KEY``
              (default ``p2p-price-bot:config`` — separate from the bot's state
              key, so ``python setup_cli.py`` and the setup page never touch
              groups, merchants or prices).  This is the persistent option on
              Vercel.
``telegram``  one pinned message in an admin's private chat (``telegram_store``).
              A serverless host throws its filesystem away with the instance, and
              an initial KV store used to be a *requirement* before the bot would
              start at all — so the bot can instead bring itself up, and keep the
              few settings that make it work (including a database connected
              later from Telegram with ``/database``) in the chat.  Used when the
              local file cannot survive and ``P2P_TELEGRAM_SETTINGS`` allows it.
``file``      ``runtime_config.json`` next to ``data.json`` (override with
              ``P2P_RUNTIME_CONFIG_FILE``).  It is also used when
              ``P2P_STATE_BACKEND=file`` explicitly selects the JSON state
              database.  Persistent on a VPS/Docker/local install; **ephemeral**
              on Vercel, whose filesystem is thrown away with the instance — that
              is exactly when the chat copy above takes over.

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
from urllib.parse import urlsplit

import storage
import telegram_store

log = logging.getLogger("p2p-bot.config")

#: Runtime settings for the browser form, terminal wizard, and Telegram DB menu.
#: The browser writes only bot settings; the CLI can configure the backend and
#: deployment REST pair, while Telegram stores custom credentials separately.
#: ``CRON_SECRET`` stays environment-only because Vercel Cron reads it there.
CUSTOM_REDIS_URL = "P2P_CUSTOM_KV_REST_API_URL"
CUSTOM_REDIS_TOKEN = "P2P_CUSTOM_KV_REST_API_TOKEN"
#: A custom database of any type (SQLite, MySQL/MariaDB, PostgreSQL, MongoDB,
#: Redis over TCP) selected in Telegram with /database: one connection URI.
#: Its password lives inside the URI, so it is a secret like the REST token.
CUSTOM_DB_URL = "P2P_CUSTOM_DB_URL"
SUPPORTED = ("BOT_TOKEN", "ADMIN_IDS", "ASSET", "FIAT", "INTERVAL", "P2P_STATE_BACKEND",
             CUSTOM_REDIS_URL, CUSTOM_REDIS_TOKEN, CUSTOM_DB_URL)
REQUIRED = ("BOT_TOKEN", "ADMIN_IDS")
SECRETS = ("BOT_TOKEN", CUSTOM_REDIS_TOKEN, CUSTOM_DB_URL)

CONFIG_FILE = "runtime_config.json"
DEFAULT_CONFIG_KEY = "p2p-price-bot:config"

#: Where a deployment can keep the settings while it has no database yet.
#: ``auto``  only where a local file cannot survive (a serverless function);
#: ``on``    always use the pinned chat copy when a token and admin id exist;
#: ``off``   never — keep the settings in the local file, even on Vercel.
TELEGRAM_SETTINGS_ENV = "P2P_TELEGRAM_SETTINGS"
_TOKEN_ENV = ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN")
_ADMIN_ENV = ("ADMIN_IDS", "ADMINS")

#: How long a warm container reuses the stored values without re-reading them
#: (one Redis round-trip per request is the alternative; the bot already reads
#: its state on every request, so this keeps setup off that path).
CACHE_SECONDS = 30.0

_cache: dict[str, Any] = {"at": 0.0, "values": {}}
_injected: set[str] = set()          # names apply() wrote into os.environ
_preferred_chat: dict[str, int | None] = {"id": None}   # the chat the admin is using


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


def _env_value(names: tuple[str, ...]) -> str:
    """First non-empty value among ``names`` — read from the environment only.

    The chat copy needs a token and an admin id *before* it can read anything, so
    those two values must come from the deployment environment: taking them from
    the store would be circular (the settings store cannot authenticate itself).
    """
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def telegram_settings_mode() -> str:
    """``auto`` / ``on`` / ``off`` for the pinned chat copy of the settings."""
    raw = (os.getenv(TELEGRAM_SETTINGS_ENV) or "").strip().lower()
    if raw in ("on", "1", "true", "yes", "always"):
        return "on"
    if raw in ("off", "0", "false", "no", "never"):
        return "off"
    return "auto"


def prefer_chat(chat_id) -> None:
    """Remember the private chat the admin is working in (``None`` clears it).

    The pinned copy lives in a chat, and the chat the admin is *using* is the one
    that will still be there tomorrow — so the caller (``bot.py``) hands it over
    whenever a /database flow starts.  Without a hint the first admin id wins.
    """
    if chat_id is None:
        _preferred_chat["id"] = None
        return
    try:
        chat = int(chat_id)
    except (TypeError, ValueError):
        return
    if chat:
        _preferred_chat["id"] = chat


def preferred_chat() -> int | None:
    """The chat the settings copy should prefer, if the bot knows one yet."""
    return _preferred_chat["id"]


def telegram_config_store(base_dir: str | Path | None = None, timeout: float = 6.0):
    """The pinned-copy store for this deployment, or ``None`` when it does not apply.

    Applies when the mode allows it, the local settings file cannot survive a
    restart, and the process has a bot token plus at least one admin id to keep
    the message in.  Nothing here talks to Telegram: the store is only built, and
    :meth:`telegram_store.TelegramConfigStore.load` does the (cached) reading.
    """
    mode = telegram_settings_mode()
    if mode == "off":
        return None
    if mode == "auto" and not _settings_file_is_ephemeral(base_dir):
        return None
    token = _env_value(_TOKEN_ENV)
    admins: list[int] = []
    for part in _env_value(_ADMIN_ENV).replace(" ", "").split(","):
        if part.isdigit():
            admins.append(int(part))
    if not token or not admins:
        return None
    chat_ids = ([preferred_chat()] if preferred_chat() else []) + \
               [chat for chat in admins if chat != preferred_chat()]
    return telegram_store.TelegramConfigStore(token, chat_ids, timeout=timeout)


def _settings_file_is_ephemeral(base_dir: str | Path | None = None) -> bool:
    """Whether the settings file would be thrown away with this instance.

    On a serverless host it always would: Vercel mounts the code read-only and
    gives ``/tmp`` to a single instance, so ``config_path`` may well be writable
    while still forgetting everything on the next request.
    """
    if storage.serverless_host():
        return True
    return not storage.writable(config_path(base_dir))


def config_store(base_dir: str | Path | None = None):
    """The bootstrap store for settings, independent of Telegram's custom DB.

    A custom state database is selected *from* these settings. Reading this
    configuration from that selected database would create a circular
    dependency on cold starts, so config always uses a deployment-managed KV
    pair when available, then the pinned Telegram copy where a local file cannot
    survive, and otherwise the local runtime-config file. The bot's state store
    still follows ``storage.build_store`` and may be a different DB.
    """
    choice = storage.state_backend()
    redis = storage.environment_redis_config()
    if choice != "file" and redis:
        return storage.RedisStore(redis[0], redis[1], key=config_key())
    telegram = telegram_config_store(base_dir)
    if telegram is not None:
        return telegram
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


def remove(names, base_dir: str | Path | None = None) -> bool:
    """Remove selected stored settings without touching environment variables."""
    selected = {str(name).upper() for name in (names or ())} & set(SUPPORTED)
    if not selected:
        return True
    invalidate()
    values = load(base_dir)
    for name in selected:
        values.pop(name, None)
    store = config_store(base_dir)
    store.save(values)
    written = load(base_dir)
    return all(name not in written for name in selected)


def clear(base_dir: str | Path | None = None) -> bool:
    """Forget every stored setting (the environment variables are untouched)."""
    invalidate()
    store = config_store(base_dir)
    store.save({})
    return not load(base_dir)


def describe(base_dir: str | Path | None = None) -> dict:
    """Public, secret-free description of where the settings would be kept.

    ``persistent`` answers "does this survive the next request?" — true for the
    KV store and for the pinned chat copy, false for a local file on a
    serverless host that throws its filesystem away.
    """
    store = config_store(base_dir)
    backend = store.backend
    return {"store": store.describe(), "backend": backend,
            "state_backend": storage.state_backend(),
            # The chat copy is not a database, but it does outlive the instance.
            "persistent": backend in ("redis", "telegram")}


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


def clean_redis_rest_url(value: str) -> tuple[str, str]:
    """Normalize a Redis-compatible REST endpoint without accepting credentials in its URL."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return "", "A Redis REST URL is required."
    if len(text) > 2048 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "", "The Redis REST URL is too long or contains whitespace."
    try:
        parsed = urlsplit(text)
        port = parsed.port
        valid = (parsed.scheme in ("https", "http") and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        return "", ("Use a valid https:// Redis REST endpoint (http:// is allowed for "
                    "self-hosted/private services); do not include a username, password, "
                    "query or fragment.")
    return text, ""


def clean_redis_rest_token(value: str) -> tuple[str, str]:
    """Validate the token belonging to a custom Redis REST endpoint."""
    text = str(value or "").strip()
    if not text:
        return "", "A Redis REST token is required."
    if len(text) > 4096 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "", "The Redis REST token is too long or contains whitespace."
    return text, ""


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

    backend = str(values.get("P2P_STATE_BACKEND") or "").strip()
    if backend:
        normal = storage.normalize_backend(backend)
        if normal is None:
            errors.append("P2P_STATE_BACKEND must be auto, file, or redis.")
        else:
            cleaned["P2P_STATE_BACKEND"] = normal

    custom_url = str(values.get(CUSTOM_REDIS_URL) or "").strip()
    custom_token = str(values.get(CUSTOM_REDIS_TOKEN) or "").strip()
    custom_db = str(values.get(CUSTOM_DB_URL) or "").strip()
    if custom_db and (custom_url or custom_token):
        errors.append("Provide either a custom database URI or a Redis REST URL + token, "
                      "not both — selecting one database replaces the other.")
    elif bool(custom_url) != bool(custom_token):
        errors.append("The custom Redis REST URL and token must be provided together.")
    elif custom_url and custom_token:
        clean_url, url_error = clean_redis_rest_url(custom_url)
        clean_token, token_error = clean_redis_rest_token(custom_token)
        if url_error:
            errors.append(url_error)
        if token_error:
            errors.append(token_error)
        if not url_error and not token_error:
            cleaned[CUSTOM_REDIS_URL] = clean_url
            cleaned[CUSTOM_REDIS_TOKEN] = clean_token
    elif custom_db:
        from database_adapters import parse_database_uri
        spec, error = parse_database_uri("", custom_db)
        if error:
            errors.append(error)
        else:
            cleaned[CUSTOM_DB_URL] = spec["uri"]

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
