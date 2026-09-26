"""State persistence for the P2P price bot.

Choose the state backend with ``P2P_STATE_BACKEND`` (``P2P_STORAGE_BACKEND`` is
accepted as an alias):

``auto``   the default.  Use Redis when a complete Redis REST credential pair is
           available; otherwise use the JSON file.
``redis``  require a Redis-compatible REST API (for example Upstash or Vercel
           KV).  A missing credential pair leaves state in memory rather than
           silently writing it to a different database.
``file``   always use the classic JSON ``data.json`` file, even when Redis
           credentials happen to be present.  This is useful for a local or
           Docker installation that should not share its data with another bot.

Redis is configured by any of::

    KV_REST_API_URL      + KV_REST_API_TOKEN
    UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN
    REDIS_REST_URL       + REDIS_REST_TOKEN

Nothing here ever raises: a failed write is logged and the in-memory state
stays the source of truth for the running process.

This module is also the single source of the "connect a database" link the bot
panel, the setup page and the status page show when no shared database is
connected (:func:`database_link`) — the destination is deployment specific, so
it is resolved in one place instead of being spelled out in three UIs.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("p2p-bot.store")

DEFAULT_KEY = "p2p-price-bot:state"
BACKENDS = ("auto", "file", "redis")
_BACKEND_ALIASES = {
    "auto": "auto",
    "file": "file",
    "json": "file",
    "redis": "redis",
    "kv": "redis",
    "upstash": "redis",
}
REDIS_ENV_PAIRS = (
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ("REDIS_REST_URL", "REDIS_REST_TOKEN"),
)

# Where the owner connects a database: Vercel's Storage page, which is where
# "Connect to this project" writes KV_REST_API_URL + KV_REST_API_TOKEN into the
# deployment environment.  P2P_DATABASE_LINK overrides it for a team- or
# project-specific storage page, an Upstash console, or a self-hosted Redis.
DEFAULT_DATABASE_LINK = "https://vercel.com/dashboard/stores"
DATABASE_LINK_ENV = "P2P_DATABASE_LINK"


def normalize_backend(value: str | None) -> str | None:
    """Return the canonical backend name, or ``None`` for an invalid value.

    ``json``, ``kv`` and ``upstash`` are friendly aliases for values users tend
    to type in a command line or a setup form.  The public configuration and UI
    always write the canonical names.
    """
    return _BACKEND_ALIASES.get(str(value or "").strip().lower())


def state_backend() -> str:
    """Configured backend, canonicalised; invalid/missing values mean ``auto``.

    ``P2P_STATE_BACKEND`` wins over the older, more generic
    ``P2P_STORAGE_BACKEND`` alias.  Logging is done by :func:`build_store` so
    callers such as the setup page can inspect the preference without producing
    a warning on every request.
    """
    raw = os.getenv("P2P_STATE_BACKEND")
    if raw is None or not raw.strip():
        raw = os.getenv("P2P_STORAGE_BACKEND", "")
    return normalize_backend(raw) or "auto"


def configured_backend_value() -> str:
    """The raw configured value, useful for an actionable invalid-value warning."""
    return (os.getenv("P2P_STATE_BACKEND") or os.getenv("P2P_STORAGE_BACKEND") or "").strip()


def redis_config() -> tuple[str, str] | None:
    """The first complete ``(url, token)`` pair in the environment, if any.

    Public because ``runtime_config.py`` stores the settings the setup page and
    ``setup_cli.py`` collected in the same Redis, under a key of its own.
    """
    for url_var, token_var in REDIS_ENV_PAIRS:
        url = (os.getenv(url_var) or "").strip().rstrip("/")
        token = (os.getenv(token_var) or "").strip()
        if url and token:
            return url, token
    return None


def database_link() -> str:
    """Where to connect a shared database (Vercel's Storage page by default).

    One link, used by every surface that tells the owner how to make the bot
    remember things: the panel in Telegram, the first-start page and the status
    page.  ``P2P_DATABASE_LINK`` points it at your own storage page, an Upstash
    console or a self-hosted Redis; a value that is not an http(s) URL is
    ignored, so a typo cannot turn a button into a broken link.
    """
    explicit = (os.getenv(DATABASE_LINK_ENV) or "").strip()
    if explicit.startswith(("http://", "https://")) and len(explicit) > len("https://"):
        return explicit
    return DEFAULT_DATABASE_LINK


def database_connected(store=None) -> bool:
    """Whether state is kept in a shared Redis-compatible database.

    Pass the store in use (``bot.STORE``) for the verified answer; without one
    this reports whether a complete credential pair exists in the environment,
    which is what the setup page can know before the bot may import.
    """
    if store is not None:
        return getattr(store, "backend", "none") == "redis"
    return redis_config() is not None


class RedisStore:
    """State kept in a Redis-compatible REST service (e.g. Upstash)."""

    backend = "redis"

    def __init__(self, url: str, token: str, key: str = DEFAULT_KEY, timeout: float = 6.0):
        self.url, self.token, self.key, self.timeout = url, token, key, timeout

    # -- low level ---------------------------------------------------------
    def _command(self, *args):
        import httpx
        r = httpx.post(self.url, json=list(args), timeout=self.timeout,
                       headers={"Authorization": f"Bearer {self.token}",
                                "Content-Type": "application/json"})
        r.raise_for_status()
        return r.json().get("result")

    # -- api ---------------------------------------------------------------
    def load(self) -> dict | None:
        try:
            raw = self._command("GET", self.key)
        except Exception as e:                                    # pragma: no cover - network
            log.warning("Redis load failed (%s) — starting from current defaults", e)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception as e:
            log.warning("Redis state is not valid JSON (%s) — ignoring it", e)
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict) -> None:
        try:
            self._command("SET", self.key, json.dumps(data, separators=(",", ":")))
        except Exception as e:                                    # pragma: no cover - network
            log.warning("Redis save failed (%s) — state kept in memory only", e)

    def describe(self) -> str:
        host = self.url.split("//", 1)[-1].split("/", 1)[0]
        return f"redis ({host}, key {self.key})"


class FileStore:
    """State kept in a JSON file (``data.json`` by default)."""

    backend = "file"

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- api ---------------------------------------------------------------
    def load(self) -> dict | None:
        try:
            if not self.path.exists():
                return None
            data = json.loads(self.path.read_text())
        except Exception as e:
            log.warning("State file %s unreadable (%s) — starting fresh", self.path, e)
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
        except Exception as e:
            log.warning("Could not write %s (%s)", self.path, e)

    def describe(self) -> str:
        return f"file ({self.path})"


class NullStore:
    """Read-only, nothing persisted — used when no selected location is usable."""

    backend = "none"

    def __init__(self, reason: str = "state lives only in memory"):
        self.reason = reason

    def load(self) -> dict | None:
        return None

    def save(self, data: dict) -> None:                            # pragma: no cover
        pass

    def describe(self) -> str:
        return f"none ({self.reason})"


def writable(path: Path) -> bool:
    """Whether ``path``'s directory can be created and written to."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".p2p-write-test"
        probe.write_text("")
        probe.unlink()
        return True
    except Exception:
        return False


def _file_store(base_dir: str | Path, default_name: str):
    """Build the file backend, including the old writable-directory fallback."""
    explicit = (os.getenv("P2P_STATE_FILE") or "").strip()
    data_dir = (os.getenv("P2P_DATA_DIR") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    elif data_dir:
        path = Path(data_dir).expanduser() / default_name
    else:
        path = Path(base_dir) / default_name

    if writable(path):
        return FileStore(path)

    fallback = Path(tempfile.gettempdir()) / default_name
    store = FileStore(fallback)
    if writable(fallback):
        log.warning("State file %s is not writable — falling back to %s "
                    "(ephemeral: add KV_REST_API_URL / UPSTASH_REDIS_REST_URL for "
                    "persistent state)", path, fallback)
        return store
    log.warning("No writable state location found — state will not persist")
    return NullStore()


def build_store(base_dir: str | Path, default_name: str = "data.json"):
    """Build the explicitly selected state backend.

    ``auto`` preserves the historical behaviour: Redis wins whenever a complete
    credential pair exists, otherwise the JSON file is used.  An explicit
    ``redis`` choice never falls back to the file database; that prevents an
    outage or typo from splitting a bot's state across two stores.
    """
    key = (os.getenv("P2P_STATE_KEY") or DEFAULT_KEY).strip() or DEFAULT_KEY
    raw_choice = configured_backend_value()
    choice = state_backend()
    if raw_choice and normalize_backend(raw_choice) is None:
        log.warning("Unknown P2P_STATE_BACKEND=%r; using auto (choose: %s)",
                    raw_choice, ", ".join(BACKENDS))

    redis = redis_config()
    if choice == "redis":
        if redis:
            store = RedisStore(redis[0], redis[1], key=key)
            log.info("State backend (selected redis): %s", store.describe())
            return store
        log.error("P2P_STATE_BACKEND=redis but no complete Redis credential pair was found "
                  "(set KV_REST_API_URL + KV_REST_API_TOKEN, or choose file/auto)")
        return NullStore("Redis was selected but is not configured")

    if choice == "auto" and redis:
        store = RedisStore(redis[0], redis[1], key=key)
        log.info("State backend (auto): %s", store.describe())
        return store

    # ``file`` deliberately reaches this branch even if Redis credentials exist.
    store = _file_store(base_dir, default_name)
    if store.backend != "redis" and os.getenv("VERCEL"):
        log.error("Vercel gives every request an ephemeral filesystem %s: the group, the "
                  "merchants and the prices will not be shared between instances and are lost "
                  "on the next cold start. Select Redis and connect a KV REST store "
                  "(KV_REST_API_URL + KV_REST_API_TOKEN — e.g. the Upstash or Vercel KV "
                  "integration).",
                  "(/tmp on this deployment)" if store.backend == "file" else "")
    log.info("State backend (%s): %s", choice, store.describe())
    return store
