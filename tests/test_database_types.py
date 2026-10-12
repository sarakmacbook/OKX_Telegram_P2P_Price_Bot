"""Database support for every type — and the first-start ask to add one.

The bot can keep its state in SQLite, MySQL/MariaDB, PostgreSQL, MongoDB, Redis
over TCP, or the classic Redis REST endpoint.  On the very first ``/start`` the
admin is asked which database type to add; the connection is one URI, tested
before anything switches, and the password never appears in a reply.

The SQL/Mongo/Redis adapters are exercised against fakes (no live server is
needed); SQLite is exercised for real because it is stdlib.
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database_adapters  # noqa: E402
import storage  # noqa: E402

ADMIN = 424242


@pytest.fixture(autouse=True)
def isolate_custom_database_environment():
    """A custom database selected in one test must not alter later tests."""
    import runtime_config

    names = (runtime_config.CUSTOM_DB_URL, runtime_config.CUSTOM_REDIS_URL,
             runtime_config.CUSTOM_REDIS_TOKEN)
    previous = {name: os.environ.pop(name, None) for name in names}
    runtime_config.invalidate()
    storage._custom_db_warned.clear()
    yield
    for name in names:
        os.environ.pop(name, None)
    for name, value in previous.items():
        if value is not None:
            os.environ[name] = value
    runtime_config.invalidate()
    storage._custom_db_warned.clear()


# ── URI parsing ────────────────────────────────────────────────────────────
def test_uri_parsing_accepts_every_database_type():
    cases = [
        ("", "mysql://user:secret@db-host:3306/p2p", "mysql"),
        ("", "mariadb://user:secret@db-host/p2p", "mysql"),
        ("mysql", "mysql://user:secret@db-host:3306/p2p", "mysql"),
        ("", "postgres://user:secret@db-host:5432/p2p", "postgres"),
        ("", "postgresql://user:secret@db-host/p2p", "postgres"),
        ("", "mongodb://user:secret@db-host:27017/p2p", "mongo"),
        ("", "mongodb+srv://user:secret@cluster.example/p2p", "mongo"),
        ("", "redis://user:secret@db-host:6379/0", "redis"),
        ("", "rediss://user:secret@db-host/2", "redis"),
    ]
    for kind, uri, expected in cases:
        spec, error = database_adapters.parse_database_uri(kind, uri)
        assert error == "", (kind, uri, error)
        assert spec["kind"] == expected
        assert spec["uri"] == uri
        if expected in ("mysql", "postgres"):
            assert spec["password"] == "secret"     # extracted (and percent-decoded)


def test_uri_parsing_understands_sqlite_paths_and_aliases():
    spec, error = database_adapters.parse_database_uri("sqlite", "/tmp/one.db")
    assert error == "" and spec["path"] == "/tmp/one.db"
    spec, _ = database_adapters.parse_database_uri("", "sqlite:///tmp/two.db")
    assert spec["path"] == "/tmp/two.db"
    spec, _ = database_adapters.parse_database_uri("", "sqlite:////tmp/two.db")
    assert spec["path"] == "/tmp/two.db"
    spec, _ = database_adapters.parse_database_uri("", "sqlite://rel.db")
    assert spec["path"] == "rel.db"
    # the stored URI round-trips through the parser
    again, _ = database_adapters.parse_database_uri("", spec["uri"])
    assert again == spec
    assert database_adapters.normalize_kind("MariaDB") == "mysql"
    assert database_adapters.normalize_kind("PostgreSQL") == "postgres"
    assert database_adapters.normalize_kind("nonsense") is None


def test_uri_parsing_rejects_mistakes():
    problems = [
        ("", "ftp://db-host/p2p"),                 # unknown scheme
        ("mysql", "postgres://user:secret@h:5432/db"),  # wrong type for the chosen menu
        ("", "mysql://user:secret@db-host"),        # no database name
        ("", "mysql://user:secret@db-host/p2p?ssl=1"),  # query strings are dropped by drivers
        ("", "mongodb://user:secret@db-host"),      # no database name
        ("", "redis://user:secret@db-host/notanumber"),
        ("sqlite", ":memory:"),                    # a real file is required
        ("", ""),                                  # nothing at all
    ]
    for kind, uri in problems:
        spec, error = database_adapters.parse_database_uri(kind, uri)
        assert spec is None and error, (kind, uri)


def test_describe_never_shows_the_password():
    spec, _ = database_adapters.parse_database_uri("", "mysql://user:top-secret@db-host:3306/p2p")
    assert "top-secret" not in database_adapters.describe_spec(spec)
    assert "db-host" in database_adapters.describe_spec(spec)


# ── adapters against fakes (no live servers) ───────────────────────────────
class FakeCursor:
    def __init__(self, db):
        self.db, self.row = db, None

    def execute(self, sql, params=()):
        sql_l = " ".join(str(sql).lower().split())
        if sql_l.startswith("create"):
            self.db.created = True
        elif sql_l.startswith("insert"):
            key, value = params[0], params[1]
            self.db.rows[key] = value
        elif sql_l.startswith("select 1"):
            self.row = (1,)
        elif sql_l.startswith("select"):
            key = params[0]
            self.row = (self.db.rows[key],) if key in self.db.rows else None
        else:                                       # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self.row

    def close(self):
        pass


class FakeSQLConn:
    """One fake MySQL/PostgreSQL connection over an in-memory row dict."""

    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass

    def close(self):
        pass


class FakeSQLDb:
    def __init__(self):
        self.rows, self.created = {}, False


class FakeRedisClient:
    def __init__(self):
        self.kv = {}

    def ping(self):
        return True

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value

    def close(self):
        pass


class FakeMongoCollection:
    def __init__(self):
        self.docs = {}

    def find_one(self, filt):
        return self.docs.get(filt["_id"])

    def update_one(self, filt, update, upsert=False):
        self.docs[filt["_id"]] = {"_id": filt["_id"], **update["$set"]}


class FakeMongoDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        return self.collection


class FakeMongoClient:
    def __init__(self):
        self.admin = SimpleNamespace(command=lambda *a, **k: {"ok": 1})
        self.collection = FakeMongoCollection()

    def __getitem__(self, name):
        return FakeMongoDatabase(self.collection)

    def close(self):
        pass


MYSQL_SPEC = {"kind": "mysql", "uri": "mysql://user:pass@db-host:3306/p2p",
              "host": "db-host", "port": 3306, "user": "user", "password": "pass",
              "database": "p2p"}
POSTGRES_SPEC = {"kind": "postgres", "uri": "postgres://user:pass@db-host:5432/p2p",
                 "host": "db-host", "port": 5432, "user": "user", "password": "pass",
                 "database": "p2p"}
MONGO_SPEC = {"kind": "mongo", "uri": "mongodb://user:pass@db-host:27017/p2p",
              "host": "db-host", "database": "p2p"}
REDIS_SPEC = {"kind": "redis", "uri": "redis://user:pass@db-host:6379/0",
              "host": "db-host", "database": "0"}


def test_sqlite_store_roundtrip(tmp_path):
    store = database_adapters.build_store(
        {"kind": "sqlite", "uri": "sqlite://x", "path": str(tmp_path / "state.db")})
    assert store.backend == "sqlite"
    assert store.probe()[0] is True
    assert store.load() is None
    store.save({"group": -100123, "merchants": {"a": {}}})
    assert store.load() == {"group": -100123, "merchants": {"a": {}}}
    again = database_adapters.build_store(
        {"kind": "sqlite", "uri": "sqlite://x", "path": str(tmp_path / "state.db")})
    assert again.load()["group"] == -100123


def test_mysql_store_roundtrip_and_probe_never_raise(monkeypatch):
    db = FakeSQLDb()
    monkeypatch.setattr(database_adapters, "_mysql_connect",
                        lambda spec, timeout: FakeSQLConn(db))
    store = database_adapters.MySQLStore(MYSQL_SPEC)

    assert store.probe() == (True, "the database answered")
    assert store.load() is None
    store.save({"a": 1})
    assert store.load() == {"a": 1}
    assert "db-host:3306/p2p" in store.describe()
    assert "pass" not in store.describe()

    def dead(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(database_adapters, "_mysql_connect", dead)
    assert store.probe()[0] is False
    assert store.load() is None                      # a dead DB reads as empty, never raises
    store.save({"a": 2})                             # …and never raises either


def test_postgres_store_roundtrip(monkeypatch):
    db = FakeSQLDb()
    monkeypatch.setattr(database_adapters, "_postgres_connect",
                        lambda spec, timeout: FakeSQLConn(db))
    store = database_adapters.PostgresStore(POSTGRES_SPEC)

    assert store.probe()[0] is True
    store.save({"hello": "world"})
    assert store.load() == {"hello": "world"}
    assert store.backend == "postgres"


def test_mongo_store_roundtrip(monkeypatch):
    client = FakeMongoClient()
    monkeypatch.setattr(database_adapters.MongoStore, "_client", lambda self: client)
    store = database_adapters.MongoStore(MONGO_SPEC)

    assert store.probe()[0] is True
    assert store.load() is None
    store.save({"x": 42})
    assert store.load() == {"x": 42}


def test_redis_tcp_store_roundtrip(monkeypatch):
    client = FakeRedisClient()
    monkeypatch.setattr(database_adapters.RedisTcpStore, "_client", lambda self: client)
    store = database_adapters.RedisTcpStore(REDIS_SPEC)

    assert store.probe()[0] is True
    assert store.load() is None
    store.save({"y": 7})
    assert store.load() == {"y": 7}


def test_missing_driver_is_one_clear_install_line(monkeypatch):
    monkeypatch.setattr(database_adapters, "_driver_module", lambda kind: None)
    assert "pip install PyMySQL" in database_adapters.driver_hint("mysql")
    assert database_adapters.driver_hint("sqlite") is None
    store = database_adapters.MySQLStore(MYSQL_SPEC)
    ok, detail = store.probe()
    assert ok is False and "pip install" in detail


# ── storage selects any custom database ───────────────────────────────────
def test_build_store_selects_the_custom_database(tmp_path, monkeypatch):
    monkeypatch.setenv("P2P_CUSTOM_DB_URL", str(tmp_path / "picked.db"))
    monkeypatch.delenv("P2P_STATE_BACKEND", raising=False)

    store = storage.build_store(tmp_path)

    assert store.backend == "sqlite"
    assert str(tmp_path / "picked.db") in store.describe()
    assert storage.state_backend() == "sqlite"
    assert storage.custom_database_kind() == "sqlite"
    assert storage.database_connected(store) is True
    assert storage.database_connected() is True
    # and an unusable URI is ignored with a warning instead of crashing
    monkeypatch.setenv("P2P_CUSTOM_DB_URL", "ftp://nonsense")
    assert storage.custom_database_spec() is None
    assert storage.database_connected() is False


def test_custom_database_outranks_the_rest_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("P2P_CUSTOM_DB_URL", "mysql://user:pass@db-host:3306/p2p")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_URL", "https://custom.example")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_TOKEN", "token")

    assert storage.custom_database_kind() == "mysql"
    assert storage.state_backend() == "mysql"
    monkeypatch.delenv("P2P_CUSTOM_DB_URL")
    assert storage.state_backend() == "redis"       # the REST pair is the fallback


def test_database_connected_counts_every_database_type():
    class Store:
        def __init__(self, backend):
            self.backend = backend

    for backend in ("sqlite", "mysql", "postgres", "mongo", "redis"):
        assert storage.database_connected(Store(backend)) is True
    assert storage.database_connected(Store("file")) is False
    assert storage.database_connected(Store("none")) is False


# ── runtime settings accept the custom URI ────────────────────────────────
def test_validate_accepts_and_cleans_the_custom_database_uri():
    import runtime_config

    cleaned, errors = runtime_config.validate(
        {runtime_config.CUSTOM_DB_URL: "  mysql://user:pass@db-host:3306/p2p  "})
    assert errors == []
    assert cleaned[runtime_config.CUSTOM_DB_URL] == "mysql://user:pass@db-host:3306/p2p"

    cleaned, errors = runtime_config.validate(
        {runtime_config.CUSTOM_DB_URL: "not a database"})
    assert cleaned == {} and errors


def test_validate_rejects_two_custom_databases_at_once():
    import runtime_config

    _cleaned, errors = runtime_config.validate({
        runtime_config.CUSTOM_DB_URL: "mysql://user:pass@db-host:3306/p2p",
        runtime_config.CUSTOM_REDIS_URL: "https://custom.example",
        runtime_config.CUSTOM_REDIS_TOKEN: "token",
    })
    assert any("not both" in error for error in errors)


# ── the Telegram flow ──────────────────────────────────────────────────────
def _private_update(message):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=ADMIN),
        effective_chat=SimpleNamespace(type="private", id=ADMIN),
        message=message,
    )


def _button_update(data):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                           effective_chat=SimpleNamespace(type="private", id=ADMIN),
                           callback_query=query), query


def test_first_start_offers_database_support_with_all_types(bot):
    message = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())
    update = _private_update(message)

    asyncio.run(bot.start(update, SimpleNamespace()))

    assert message.reply_html.await_count == 2      # the offer, then the panel
    offer = message.reply_html.await_args_list[0]
    assert "Add database support" in offer.args[0]
    callbacks = [button.callback_data for row in offer.kwargs["reply_markup"].inline_keyboard
                 for button in row]
    assert callbacks == ["db_type:mysql", "db_type:postgres", "db_type:mongo",
                         "db_type:redis", "db_type:sqlite", "db_custom", "db_skip"]
    assert bot.state["db_setup_offered"] is True


def test_first_start_is_asked_only_once(bot):
    bot.state["db_setup_offered"] = True
    message = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())

    asyncio.run(bot.start(_private_update(message), SimpleNamespace()))

    assert message.reply_html.await_count == 1      # the panel only


def test_the_ask_is_skipped_when_a_database_is_already_connected(bot, monkeypatch):
    monkeypatch.setattr(bot, "db_is_persistent", lambda: True)
    message = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())

    asyncio.run(bot.start(_private_update(message), SimpleNamespace()))

    assert message.reply_html.await_count == 1      # the panel only
    assert bot.state.get("db_setup_offered") is not True


def test_database_skip_keeps_the_bot_running(bot):
    update, query = _button_update("db_skip")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    text = query.edit_message_text.await_args.args[0]
    assert "/database" in text                       # the way back is named
    assert bot.state["db_setup_offered"] is True


def test_database_type_button_prompts_for_the_connection_uri(bot):
    update, query = _button_update("db_type:postgres")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    prompt = query.edit_message_text.await_args.args[0]
    assert "PostgreSQL" in prompt
    assert "postgres://user:password@db-host:5432/p2p" in prompt
    assert bot.state["edits"][str(ADMIN)]["awaiting_custom"] == "database_uri"
    assert bot.state["edits"][str(ADMIN)]["db_kind"] == "postgres"


def test_database_type_menu_lists_every_type(bot):
    update, query = _button_update("db_types")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    for expected in ("db_type:mysql", "db_type:postgres", "db_type:mongo",
                     "db_type:redis", "db_type:sqlite", "db_custom"):
        assert expected in callbacks


def test_connecting_a_database_carries_state_over_and_hides_credentials(
        bot, monkeypatch, tmp_path):
    """The whole URI flow against a real SQLite file: test, persist, migrate."""
    import runtime_config

    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("P2P_STATE_BACKEND", "auto")
    monkeypatch.setattr(bot, "STORE", bot.STORE)
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", bot._STORE_CONFIG_SIGNATURE)
    bot.state["group"] = -100555
    bot.state["group_title"] = "Carried over"
    bot.save()
    bot.state.setdefault("edits", {})[str(ADMIN)] = {
        "awaiting_custom": "database_uri", "db_kind": "sqlite"}
    message = SimpleNamespace(text=str(tmp_path / "bot.db"), reply_text=AsyncMock(),
                              reply_html=AsyncMock())
    update = _private_update(message)

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    answer = message.reply_html.await_args.args[0]
    assert "connected and selected" in answer
    assert str(tmp_path / "bot.db") in answer          # the path is fine to show…
    assert bot.STORE.backend == "sqlite"
    assert bot.state["group"] == -100555 and bot.state["group_title"] == "Carried over"
    assert bot.db_is_persistent() is True
    saved = runtime_config.load()
    assert saved[runtime_config.CUSTOM_DB_URL] == f"sqlite://{tmp_path / 'bot.db'}"
    # the conversation draft is gone
    assert bot.state["edits"][str(ADMIN)].get("awaiting_custom") is None
    assert bot.state["edits"][str(ADMIN)].get("db_kind") is None


def test_an_invalid_uri_keeps_the_conversation_open(bot):
    bot.state.setdefault("edits", {})[str(ADMIN)] = {
        "awaiting_custom": "database_uri", "db_kind": "mysql"}
    message = SimpleNamespace(text="definitely-not-a-uri", reply_text=AsyncMock())
    update = _private_update(message)

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    error = message.reply_text.await_args.args[0]
    assert error.startswith("❌")
    assert "definitely-not-a-uri" not in error          # never echo what they sent
    assert bot.state["edits"][str(ADMIN)]["awaiting_custom"] == "database_uri"


def test_connecting_never_repeats_the_password(bot, monkeypatch, tmp_path):
    """A failed connection still never shows the secret that was sent."""
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setattr(bot, "STORE", bot.STORE)
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", bot._STORE_CONFIG_SIGNATURE)
    bot.state.setdefault("edits", {})[str(ADMIN)] = {
        "awaiting_custom": "database_uri", "db_kind": "mysql"}
    message = SimpleNamespace(text="mysql://user:super-secret@127.0.0.1:1/p2p",
                              reply_text=AsyncMock(), reply_html=AsyncMock())
    update = _private_update(message)

    asyncio.run(bot.on_text(update, SimpleNamespace()))      # nothing answers on 127.0.0.1:1

    for call in list(message.reply_text.await_args_list) + list(message.reply_html.await_args_list):
        for part in call.args:
            assert "super-secret" not in part


def test_switching_database_types_replaces_the_previous_selection(bot, monkeypatch, tmp_path):
    """One custom database at a time: a REST pair replaces a URI selection and back."""
    import runtime_config

    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(bot, "STORE", bot.STORE)
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", bot._STORE_CONFIG_SIGNATURE)

    okay, _ = bot.set_custom_database_uri("sqlite", str(tmp_path / "one.db"))
    assert okay and storage.custom_database_kind() == "sqlite"

    class FakeRedisStore:
        backend = "redis"

        def __init__(self, url, token, key="p2p-price-bot:state", timeout=6.0):
            self.url, self.token, self.key = url, token, key

        def probe(self, timeout=None):
            return True, "the database answered"

        def load(self):
            return None

        def save(self, data):
            pass

        def describe(self):
            return "redis (custom.example, key state)"

    monkeypatch.setattr(bot.storage, "RedisStore", FakeRedisStore)
    okay, _ = bot.set_custom_database("https://custom.example", "rest-token")
    assert okay
    assert storage.custom_redis_config() == ("https://custom.example", "rest-token")
    assert storage.custom_database_kind() is None
    assert runtime_config.CUSTOM_DB_URL not in runtime_config.load()

    okay, _ = bot.set_custom_database_uri("sqlite", str(tmp_path / "two.db"))
    assert okay and storage.custom_database_kind() == "sqlite"
    assert storage.custom_redis_config() is None
    saved = runtime_config.load()
    assert runtime_config.CUSTOM_REDIS_TOKEN not in saved
    assert saved[runtime_config.CUSTOM_DB_URL] == f"sqlite://{tmp_path / 'two.db'}"
