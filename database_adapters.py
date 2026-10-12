"""Database adapters for every state backend the bot can be pointed at.

The bot's state (group, merchants, settings, prices) is one JSON document, and
this module knows how to keep that document in **any** of the database types an
admin can pick from Telegram's ``/database`` menu:

``sqlite``    a local database file (stdlib ``sqlite3`` — no driver needed)
``mysql``     MySQL / MariaDB (``pymysql``, or ``mysql-connector-python`` / ``mysqlclient`` if present)
``postgres``  PostgreSQL (``psycopg`` / ``psycopg2`` / ``pg8000``, tried in that order)
``mongo``     MongoDB (``pymongo``)
``redis``     Redis / Valkey over TCP (``redis``) — the Redis *REST* endpoint
              (Upstash, Vercel KV) stays in ``storage.RedisStore`` because it
              needs a URL **and** a token instead of one connection URI

Connection details arrive as one URI, the way every database documents itself::

    sqlite:///home/user/p2p-bot.db
    mysql://user:password@db-host:3306/p2p
    mariadb://user:password@db-host:3306/p2p
    postgres://user:password@db-host:5432/p2p
    mongodb://user:password@db-host:27017/p2p
    redis://user:password@db-host:6379/0

Rules of the house (same as ``storage.py``):

* ``probe`` / ``load`` / ``save`` **never raise** — a dead database looks like an
  empty one and is logged, so a broken connection can never crash a chat
  handler.  ``probe`` is the honest check that tells "nothing saved yet" and
  "the database went away" apart.
* :func:`describe` and every error message are secret-free: the password never
  appears in a reply, a log line, or the panel — only the saved URI (which the
  settings store keeps sealed) ever holds it.
* Drivers are imported lazily.  A deployment without a driver gets one clear
  "pip install …" sentence instead of a stack trace, and the base install ships
  every driver so the common case just works.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("p2p-bot.db-adapters")

#: Row / document key the JSON state document lives under (``storage.DEFAULT_KEY``).
DEFAULT_KEY = "p2p-price-bot:state"

#: One table / collection name for every SQL and Mongo adapter.
TABLE = "p2p_state"

#: The supported database types, in menu order.
KINDS = ("sqlite", "mysql", "postgres", "mongo", "redis")

#: Friendly names people type or send as callback data → canonical kind.
KIND_ALIASES = {
    "sqlite": "sqlite", "sql": "sqlite", "file": "sqlite",
    "mysql": "mysql", "mariadb": "mysql", "maria": "mysql", "my": "mysql",
    "postgres": "postgres", "postgresql": "postgres", "pgsql": "postgres", "pg": "postgres",
    "mongo": "mongo", "mongodb": "mongo",
    "redis": "redis", "rediss": "redis", "valkey": "redis", "valkeys": "redis",
}

#: URI scheme → canonical kind (what :func:`kind_from_uri` reads).
SCHEME_TO_KIND = {
    "sqlite": "sqlite", "file": "sqlite",
    "mysql": "mysql", "mariadb": "mysql", "maria": "mysql",
    "postgres": "postgres", "postgresql": "postgres", "pgsql": "postgres",
    "mongodb": "mongo", "mongodb+srv": "mongo",
    "redis": "redis", "rediss": "redis", "valkey": "redis", "valkeys": "redis",
}

#: Accepted URI schemes per kind (what :func:`parse_database_uri` enforces).
KIND_SCHEMES = {
    "sqlite": ("sqlite", "file"),
    "mysql": ("mysql", "mariadb", "maria"),
    "postgres": ("postgres", "postgresql", "pgsql"),
    "mongo": ("mongodb", "mongodb+srv"),
    "redis": ("redis", "rediss", "valkey", "valkeys"),
}

#: Default TCP port per kind (``sqlite`` and ``mongo+srv`` have none).
KIND_PORTS = {"mysql": 3306, "postgres": 5432, "mongo": 27017, "redis": 6379}

#: Shown on the buttons and in the help text.
KIND_LABELS = {
    "sqlite": "SQLite",
    "mysql": "MySQL / MariaDB",
    "postgres": "PostgreSQL",
    "mongo": "MongoDB",
    "redis": "Redis",
}

#: The one example URI each help screen shows.
EXAMPLES = {
    "sqlite": "sqlite:///home/user/p2p-bot.db",
    "mysql": "mysql://user:password@db-host:3306/p2p",
    "postgres": "postgres://user:password@db-host:5432/p2p",
    "mongo": "mongodb://user:password@db-host:27017/p2p",
    "redis": "redis://user:password@db-host:6379/0",
}

#: What to install when a driver is missing (``sqlite`` is stdlib).
DRIVER_PACKAGES = {"mysql": "PyMySQL", "postgres": "pg8000", "mongo": "pymongo", "redis": "redis"}

UNSUPPORTED_URI_ERROR = (
    "That is not a supported connection URI. Send one like "
    + " · ".join(EXAMPLES[kind] for kind in KINDS[:4])
    + " (or a plain file path for SQLite)."
)


class DriverMissing(RuntimeError):
    """The driver for one database type is not importable here."""


def normalize_kind(value: str) -> str | None:
    """``"MariaDB"`` → ``"mysql"`` — or ``None`` for an unknown type."""
    return KIND_ALIASES.get(str(value or "").strip().lower())


def kind_from_uri(text: str) -> str | None:
    """Guess the database type from a URI's scheme (bare paths count as SQLite)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    scheme = urlsplit(raw).scheme.lower()
    if not scheme:                                   # a plain file path
        return "sqlite"
    return SCHEME_TO_KIND.get(scheme)


def driver_hint(kind: str) -> str | None:
    """``None`` when this deployment can connect ``kind``, else one install line."""
    kind = normalize_kind(kind) or ""
    if kind == "sqlite":
        return None
    if kind not in DRIVER_PACKAGES:
        return None
    if _driver_module(kind) is None:
        package = DRIVER_PACKAGES[kind]
        return (f"this deployment has no {KIND_LABELS.get(kind, kind)} driver yet — "
                f"run <code>pip install {package}</code> (then restart the bot) and try again")
    return None


# ── driver loading ─────────────────────────────────────────────────────────
def _import_first(*names):
    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def _driver_module(kind: str):
    """The first importable driver for ``kind`` (or ``None``)."""
    if kind == "mysql":
        return _import_first("pymysql", "mysql.connector", "MySQLdb")
    if kind == "postgres":
        return _import_first("psycopg", "psycopg2", "pg8000.dbapi", "pg8000")
    if kind == "mongo":
        return _import_first("pymongo")
    if kind == "redis":
        return _import_first("redis")
    return None


def _mysql_connect(spec: dict, timeout: float):
    mod = _driver_module("mysql")
    if mod is None:
        raise DriverMissing(driver_hint("mysql") or "no MySQL driver")
    kwargs = dict(host=spec["host"], port=spec["port"], user=spec["user"],
                  autocommit=True, connect_timeout=max(1, int(timeout)))
    name = getattr(mod, "__name__", "")
    if name == "MySQLdb":                           # mysqlclient uses db=/passwd=
        return mod.connect(db=spec["database"], passwd=spec["password"] or "", **kwargs)
    if name == "mysql.connector":
        kwargs["connection_timeout"] = kwargs.pop("connect_timeout")
        return mod.connect(password=spec["password"], database=spec["database"], **kwargs)
    return mod.connect(password=spec["password"], database=spec["database"],
                       charset="utf8mb4", **kwargs)


def _postgres_connect(spec: dict, timeout: float):
    mod = _driver_module("postgres")
    if mod is None:
        raise DriverMissing(driver_hint("postgres") or "no PostgreSQL driver")
    name = getattr(mod, "__name__", "")
    if name.startswith("pg8000"):
        conn = mod.connect(user=spec["user"], host=spec["host"], port=spec["port"],
                           database=spec["database"], password=spec["password"],
                           timeout=max(1.0, float(timeout)))
        conn.autocommit = True
        return conn
    if name == "psycopg":                           # psycopg 3
        return mod.connect(host=spec["host"], port=spec["port"], user=spec["user"],
                           password=spec["password"], dbname=spec["database"],
                           connect_timeout=max(1, int(timeout)), autocommit=True)
    conn = mod.connect(host=spec["host"], port=spec["port"], user=spec["user"],
                       password=spec["password"], dbname=spec["database"],
                       connect_timeout=max(1, int(timeout)))
    conn.autocommit = True
    return conn


def _prepare(conn, sql: str) -> None:
    """Run a statement on an open connection and keep the connection alive."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
        finally:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:                            # autocommit connections
            pass
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def _sql_rows(conn, sql: str, params: tuple = (), *, write: bool = False):
    """Run one statement; return the first row (or ``None``), closing everything."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            row = None if write else cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        if write:
            try:
                conn.commit()
            except Exception:                        # autocommit connections
                pass
        return row
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _json_load(raw) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except Exception as e:
        log.warning("State in the database is not valid JSON (%s) — ignoring it", e)
        return None
    return data if isinstance(data, dict) else None


# ── URI parsing ────────────────────────────────────────────────────────────
def parse_database_uri(kind: str, text: str) -> tuple[dict | None, str]:
    """``(spec, "")`` for a usable connection URI — or ``(None, problem)``.

    ``kind`` may be empty: the type is then read from the URI's scheme.  When a
    type is given (the admin already tapped a button) the URI has to match it,
    so a PostgreSQL URI cannot land in the MySQL adapter by accident.

    The returned ``spec`` is what :func:`build_store` consumes.  ``spec["uri"]``
    is the normalized URI that is *saved*; ``describe`` never shows the password
    that lives inside it.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "A connection URI is required."
    if len(raw) > 2048 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None, "The connection URI is too long or contains whitespace."

    wanted = normalize_kind(kind) if kind else None
    inferred = kind_from_uri(raw)
    if wanted is None and inferred is None:
        return None, UNSUPPORTED_URI_ERROR
    if wanted is not None and inferred is not None and inferred != wanted:
        return None, (f"That does not look like a {KIND_LABELS[wanted]} URI — send one like "
                      f"{EXAMPLES[wanted]}")
    kind = wanted or inferred

    if kind == "sqlite":
        return _parse_sqlite(raw)
    if kind in ("mysql", "postgres"):
        return _parse_sql(raw, kind)
    if kind == "mongo":
        return _parse_mongo(raw)
    return _parse_redis(raw)


def _parse_sqlite(raw: str) -> tuple[dict | None, str]:
    path = raw
    lowered = raw.lower()
    if lowered.startswith("sqlite://"):
        path = raw[len("sqlite://"):]
    elif lowered.startswith("sqlite:"):
        path = raw[len("sqlite:"):]
    elif lowered.startswith("file://"):
        path = raw[len("file://"):]
    if not path or path.strip() == ":memory:":
        return None, f"Send a file path for the SQLite database, for example {EXAMPLES['sqlite']}"
    if path.startswith("//"):                        # sqlite:////abs/path → /abs/path
        path = path[1:]
    path = str(Path(path).expanduser())
    # The stored URI round-trips through this parser (absolute and relative alike).
    return {"kind": "sqlite", "uri": f"sqlite://{path}", "path": path}, ""


def _parse_sql(raw: str, kind: str) -> tuple[dict | None, str]:
    label, example = KIND_LABELS[kind], EXAMPLES[kind]
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None, f"That {label} URI is not valid — send one like {example}"
    if parsed.scheme.lower() not in KIND_SCHEMES[kind]:
        return None, f"That does not look like a {label} URI — send one like {example}"
    if not parsed.hostname:
        return None, f"The {label} URI needs a host — send one like {example}"
    if parsed.query or parsed.fragment:
        return None, ("Omit any ?query or #fragment in the URI — send the plain connection "
                      f"URI, like {example}")
    database = parsed.path.lstrip("/")
    if not database or "/" in database:
        return None, f"Include the database name — send one like {example}"
    if port is None:
        port = KIND_PORTS[kind]
    return {"kind": kind, "uri": raw, "host": parsed.hostname, "port": port,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password) if parsed.password else "",
            "database": database}, ""


def _parse_mongo(raw: str) -> tuple[dict | None, str]:
    example = EXAMPLES["mongo"]
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return None, f"That MongoDB URI is not valid — send one like {example}"
    if parsed.scheme.lower() not in KIND_SCHEMES["mongo"]:
        return None, f"That does not look like a MongoDB URI — send one like {example}"
    if not parsed.hostname:
        return None, f"The MongoDB URI needs a host — send one like {example}"
    database = parsed.path.lstrip("/")
    if not database or "/" in database:
        return None, (f"Include the database name in the path — send one like {example}")
    return {"kind": "mongo", "uri": raw, "host": parsed.hostname, "database": database}, ""


def _parse_redis(raw: str) -> tuple[dict | None, str]:
    example = EXAMPLES["redis"]
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return None, f"That Redis URI is not valid — send one like {example}"
    if parsed.scheme.lower() not in KIND_SCHEMES["redis"]:
        return None, f"That does not look like a Redis URI — send one like {example}"
    if not parsed.hostname:
        return None, f"The Redis URI needs a host — send one like {example}"
    database = parsed.path.lstrip("/") or "0"
    if not database.isdigit():
        return None, (f"The Redis URI path must be a database number (or empty) — "
                      f"send one like {example}")
    return {"kind": "redis", "uri": raw, "host": parsed.hostname, "database": database}, ""


def describe_spec(spec: dict) -> str:
    """Secret-free one-liner: type + host + database — never the password."""
    kind = spec.get("kind", "?")
    if kind == "sqlite":
        return f"sqlite ({spec.get('path')})"
    if kind in ("mysql", "postgres"):
        return f"{kind} ({spec.get('host')}:{spec.get('port')}/{spec.get('database')})"
    if kind == "mongo":
        return f"mongo ({spec.get('host')}/{spec.get('database')})"
    return f"redis ({spec.get('host')}/{spec.get('database', '0')})"


# ── stores ────────────────────────────────────────────────────────────────
class SQLiteStore:
    """State kept in a SQLite database file (stdlib — always available)."""

    backend = "sqlite"

    def __init__(self, path: str | Path, key: str = DEFAULT_KEY, timeout: float = 6.0):
        self.path = Path(path)
        self.key, self.timeout = key, timeout

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(str(self.path), timeout=self.timeout)
        _prepare(conn, f"CREATE TABLE IF NOT EXISTS {TABLE} "
                       f"(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        return conn

    def probe(self, timeout: float | None = None) -> tuple[bool, str]:
        try:
            conn = self._connect()
            _sql_rows(conn, "SELECT 1")
            return True, "the database answered"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def load(self) -> dict | None:
        try:
            conn = self._connect()
            row = _sql_rows(conn, f"SELECT v FROM {TABLE} WHERE k = ?", (self.key,))
        except Exception as e:
            log.warning("SQLite load failed (%s) — starting from current defaults", e)
            return None
        return _json_load(row[0] if row else None)

    def save(self, data: dict) -> None:
        try:
            conn = self._connect()
            _sql_rows(conn, f"INSERT OR REPLACE INTO {TABLE} (k, v) VALUES (?, ?)",
                      (self.key, json.dumps(data, separators=(",", ":"))), write=True)
        except Exception as e:
            log.warning("SQLite save failed (%s) — state kept in memory only", e)

    def describe(self) -> str:
        return f"sqlite ({self.path})"


class MySQLStore:
    """State kept in a MySQL / MariaDB table."""

    backend = "mysql"

    def __init__(self, spec: dict, key: str = DEFAULT_KEY, timeout: float = 6.0):
        self.spec, self.key, self.timeout = spec, key, timeout

    def _connect(self):
        conn = _mysql_connect(self.spec, self.timeout)
        _prepare(conn, f"CREATE TABLE IF NOT EXISTS {TABLE} "
                       f"(k VARCHAR(191) PRIMARY KEY, v LONGTEXT NOT NULL)")
        return conn

    def probe(self, timeout: float | None = None) -> tuple[bool, str]:
        try:
            conn = _mysql_connect(self.spec, self.timeout if timeout is None else timeout)
            _sql_rows(conn, "SELECT 1")
            return True, "the database answered"
        except DriverMissing as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def load(self) -> dict | None:
        try:
            conn = self._connect()
            row = _sql_rows(conn, f"SELECT v FROM {TABLE} WHERE k = %s", (self.key,))
        except Exception as e:
            log.warning("MySQL load failed (%s) — starting from current defaults", e)
            return None
        return _json_load(row[0] if row else None)

    def save(self, data: dict) -> None:
        try:
            conn = self._connect()
            _sql_rows(conn, f"INSERT INTO {TABLE} (k, v) VALUES (%s, %s) "
                            f"ON DUPLICATE KEY UPDATE v = VALUES(v)",
                      (self.key, json.dumps(data, separators=(",", ":"))), write=True)
        except Exception as e:
            log.warning("MySQL save failed (%s) — state kept in memory only", e)

    def describe(self) -> str:
        return describe_spec(self.spec)


class PostgresStore:
    """State kept in a PostgreSQL table."""

    backend = "postgres"

    def __init__(self, spec: dict, key: str = DEFAULT_KEY, timeout: float = 6.0):
        self.spec, self.key, self.timeout = spec, key, timeout

    def _connect(self):
        conn = _postgres_connect(self.spec, self.timeout)
        _prepare(conn, f"CREATE TABLE IF NOT EXISTS {TABLE} "
                       f"(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        return conn

    def probe(self, timeout: float | None = None) -> tuple[bool, str]:
        try:
            conn = _postgres_connect(self.spec, self.timeout if timeout is None else timeout)
            _sql_rows(conn, "SELECT 1")
            return True, "the database answered"
        except DriverMissing as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def load(self) -> dict | None:
        try:
            conn = self._connect()
            row = _sql_rows(conn, f"SELECT v FROM {TABLE} WHERE k = %s", (self.key,))
        except Exception as e:
            log.warning("PostgreSQL load failed (%s) — starting from current defaults", e)
            return None
        return _json_load(row[0] if row else None)

    def save(self, data: dict) -> None:
        try:
            conn = self._connect()
            _sql_rows(conn, f"INSERT INTO {TABLE} (k, v) VALUES (%s, %s) "
                            f"ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                      (self.key, json.dumps(data, separators=(",", ":"))), write=True)
        except Exception as e:
            log.warning("PostgreSQL save failed (%s) — state kept in memory only", e)

    def describe(self) -> str:
        return describe_spec(self.spec)


class MongoStore:
    """State kept in a MongoDB collection."""

    backend = "mongo"

    def __init__(self, spec: dict, key: str = DEFAULT_KEY, timeout: float = 6.0):
        self.spec, self.key, self.timeout = spec, key, timeout

    def _client(self):
        try:
            import pymongo
        except ImportError:
            raise DriverMissing(driver_hint("mongo") or "no MongoDB driver")
        ms = max(500, int(float(self.timeout) * 1000))
        return pymongo.MongoClient(self.spec["uri"], serverSelectionTimeoutMS=ms,
                                   connectTimeoutMS=ms)

    def _collection(self, client):
        return client[self.spec.get("database") or "p2p"][TABLE]

    def probe(self, timeout: float | None = None) -> tuple[bool, str]:
        try:
            client = self._client()
            try:
                client.admin.command("ping")
                return True, "the database answered"
            finally:
                client.close()
        except DriverMissing as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def load(self) -> dict | None:
        try:
            client = self._client()
            try:
                doc = self._collection(client).find_one({"_id": self.key})
            finally:
                client.close()
        except Exception as e:
            log.warning("MongoDB load failed (%s) — starting from current defaults", e)
            return None
        return _json_load((doc or {}).get("value"))

    def save(self, data: dict) -> None:
        try:
            client = self._client()
            try:
                self._collection(client).update_one(
                    {"_id": self.key},
                    {"$set": {"value": json.dumps(data, separators=(",", ":"))}},
                    upsert=True)
            finally:
                client.close()
        except Exception as e:
            log.warning("MongoDB save failed (%s) — state kept in memory only", e)

    def describe(self) -> str:
        return describe_spec(self.spec)


class RedisTcpStore:
    """State kept in a Redis / Valkey server over TCP (``redis://``)."""

    backend = "redis"

    def __init__(self, spec: dict, key: str = DEFAULT_KEY, timeout: float = 6.0):
        self.spec, self.key, self.timeout = spec, key, timeout

    def _client(self):
        try:
            import redis
        except ImportError:
            raise DriverMissing(driver_hint("redis") or "no Redis driver")
        return redis.Redis.from_url(self.spec["uri"], socket_timeout=self.timeout,
                                    socket_connect_timeout=self.timeout)

    def probe(self, timeout: float | None = None) -> tuple[bool, str]:
        try:
            client = self._client()
            try:
                client.ping()
                return True, "the database answered"
            finally:
                client.close()
        except DriverMissing as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def load(self) -> dict | None:
        try:
            client = self._client()
            try:
                raw = client.get(self.key)
            finally:
                client.close()
        except Exception as e:
            log.warning("Redis load failed (%s) — starting from current defaults", e)
            return None
        return _json_load(raw)

    def save(self, data: dict) -> None:
        try:
            client = self._client()
            try:
                client.set(self.key, json.dumps(data, separators=(",", ":")))
            finally:
                client.close()
        except Exception as e:
            log.warning("Redis save failed (%s) — state kept in memory only", e)

    def describe(self) -> str:
        return describe_spec(self.spec)


def build_store(spec: dict, key: str = DEFAULT_KEY):
    """The store adapter for a parsed connection ``spec`` (see :func:`parse_database_uri`)."""
    kind = normalize_kind(spec.get("kind")) or ""
    if kind == "sqlite":
        return SQLiteStore(spec["path"], key=key)
    if kind == "mysql":
        return MySQLStore(spec, key=key)
    if kind == "postgres":
        return PostgresStore(spec, key=key)
    if kind == "mongo":
        return MongoStore(spec, key=key)
    if kind == "redis":
        return RedisTcpStore(spec, key=key)
    raise ValueError(f"unsupported database type: {spec.get('kind')!r}")
