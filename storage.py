"""State persistence for the P2P price bot.

Two backends, chosen automatically from the environment:

``redis``  a Redis-compatible REST API (e.g. Upstash) — for hosts without a
           writable disk, or when several bot instances share one state.
           Enabled by any of::

               KV_REST_API_URL      + KV_REST_API_TOKEN
               UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN
               REDIS_REST_URL       + REDIS_REST_TOKEN

``file``   the classic ``data.json`` next to the bot — default for
           systemd / Docker / local installs.  If the directory is not
           writable the store falls back to ``$TMPDIR`` and says so.

Nothing here ever raises: a failed write is logged and the in-memory state
stays the source of truth for the running process.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("p2p-bot.store")

DEFAULT_KEY = "p2p-price-bot:state"
REDIS_ENV_PAIRS = (
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ("REDIS_REST_URL", "REDIS_REST_TOKEN"),
)


def _redis_config() -> tuple[str, str] | None:
    for url_var, token_var in REDIS_ENV_PAIRS:
        url = (os.getenv(url_var) or "").strip().rstrip("/")
        token = (os.getenv(token_var) or "").strip()
        if url and token:
            return url, token
    return None


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
    """Read-only, nothing persisted — used when no location is writable."""

    backend = "none"

    def load(self) -> dict | None:
        return None

    def save(self, data: dict) -> None:                            # pragma: no cover
        pass

    def describe(self) -> str:
        return "none (state lives only in memory)"


def _writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".p2p-write-test"
        probe.write_text("")
        probe.unlink()
        return True
    except Exception:
        return False


def build_store(base_dir: str | Path, default_name: str = "data.json"):
    """Pick the best available backend for this environment."""
    key = (os.getenv("P2P_STATE_KEY") or DEFAULT_KEY).strip() or DEFAULT_KEY

    redis = _redis_config()
    if redis:
        store = RedisStore(redis[0], redis[1], key=key)
        log.info("State backend: %s", store.describe())
        return store

    explicit = (os.getenv("P2P_STATE_FILE") or "").strip()
    data_dir = (os.getenv("P2P_DATA_DIR") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    elif data_dir:
        path = Path(data_dir).expanduser() / default_name
    else:
        path = Path(base_dir) / default_name

    if _writable(path):
        store = FileStore(path)
    else:
        fallback = Path(tempfile.gettempdir()) / default_name
        store = FileStore(fallback)
        if _writable(fallback):
            log.warning("State file %s is not writable — falling back to %s "
                        "(ephemeral: add KV_REST_API_URL / UPSTASH_REDIS_REST_URL for "
                        "persistent state)", path, fallback)
        else:                                                      # pragma: no cover
            store = NullStore()
            log.warning("No writable state location found — state will not persist")
    if store.backend != "redis" and os.getenv("VERCEL"):
        log.error("Vercel gives every request an ephemeral filesystem %s: the group, the "
                  "merchants and the prices will not be shared between instances and are lost "
                  "on the next cold start. Connect a Redis/KV REST store (KV_REST_API_URL + "
                  "KV_REST_API_TOKEN — e.g. the Upstash or Vercel KV integration).",
                  "(/tmp on this deployment)" if store.backend == "file" else "")
    log.info("State backend: %s", store.describe())
    return store
