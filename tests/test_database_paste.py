"""Connecting a database by pasting it — with or without a type button.

Every provider hands out its connection as two environment lines:

    UPSTASH_REDIS_REST_URL="https://superb-xxxxx.upstash.io"
    UPSTASH_REDIS_REST_TOKEN="gQAA…"

and that snippet is what an owner has on their clipboard.  This module covers
the recognition of those lines (plus connection URIs and SQLite file names),
the reply that asks for a missing half, and the pair being connected even when
no button was tapped first — which is also what makes the choice survive hosts
that recycle the instance between two updates (Vercel), where a remembered
draft cannot be relied on.
"""

import asyncio
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database_adapters

ADMIN = 424242

UPSTASH_BLOCK = (
    'UPSTASH_REDIS_REST_URL="https://superb-redbixxxh.upstash.io"\n'
    'UPSTASH_REDIS_REST_TOKEN="gQAAxxxgcDE3OTI5MjBkM2Y2ZTM0M2E4OWZhMTQwN2U2YzRjNjU2ZA"'
)


@pytest.fixture(autouse=True)
def isolate_custom_database_environment():
    """A pair pasted in one test must not leak into the next one."""
    import runtime_config

    names = (runtime_config.CUSTOM_REDIS_URL, runtime_config.CUSTOM_REDIS_TOKEN,
             runtime_config.CUSTOM_DB_URL)
    previous = {name: os.environ.pop(name, None) for name in names}
    runtime_config.invalidate()
    yield
    for name in names:
        os.environ.pop(name, None)
    for name, value in previous.items():
        if value is not None:
            os.environ[name] = value
    runtime_config.invalidate()


@pytest.fixture(autouse=True)
def restore_state_store(bot):
    """A test that switches the store must not decide the next test's store."""
    store, signature = bot.STORE, bot._STORE_CONFIG_SIGNATURE
    yield
    bot.STORE, bot._STORE_CONFIG_SIGNATURE = store, signature


@pytest.fixture()
def fake_rest_store(monkeypatch):
    """A REST store that answers, so connecting one never touches a network."""
    import bot

    class FakeRedisStore:
        backend = "redis"
        stores: dict = {}

        def __init__(self, url, token, key="p2p-price-bot:state", timeout=6.0):
            self.url, self.token, self.key = url, token, key

        def probe(self, timeout=None):
            return True, "the database answered"

        def load(self):
            value = self.stores.get((self.url, self.key))
            return deepcopy(value) if value is not None else None

        def save(self, data):
            self.stores[(self.url, self.key)] = deepcopy(data)

        def describe(self):
            return f"redis ({self.url.split('//', 1)[-1]}, key {self.key})"

    monkeypatch.setattr(bot.storage, "RedisStore", FakeRedisStore)
    monkeypatch.setattr(bot, "STORE", bot.STORE)
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", bot._STORE_CONFIG_SIGNATURE)
    return FakeRedisStore


@pytest.fixture()
def isolated_settings(monkeypatch, tmp_path):
    """Keep the saved settings and the state file inside the test."""
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("P2P_STATE_BACKEND", "auto")
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    return tmp_path


def _private(message):
    return SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                           effective_chat=SimpleNamespace(type="private", id=ADMIN),
                           message=message)


def _message(text="", **extra):
    return SimpleNamespace(text=text, reply_text=AsyncMock(), reply_html=AsyncMock(), **extra)


# ── what a pasted message is ───────────────────────────────────────────────
def test_the_upstash_snippet_is_understood():
    paste = database_adapters.parse_database_paste(UPSTASH_BLOCK)

    assert paste["action"] == "rest_pair"
    assert paste["url"] == "https://superb-redbixxxh.upstash.io"
    assert paste["token"] == "gQAAxxxgcDE3OTI5MjBkM2Y2ZTM0M2E4OWZhMTQwN2U2YzRjNjU2ZA"
    assert paste["provider"] == "Upstash Redis"


@pytest.mark.parametrize("text, expected_url, expected_token", [
    # one line, unquoted, with export, ; separators, reversed order
    ("export UPSTASH_REDIS_REST_URL=https://a.upstash.io UPSTASH_REDIS_REST_TOKEN=tok1",
     "https://a.upstash.io", "tok1"),
    ("UPSTASH_REDIS_REST_TOKEN='tok2'; UPSTASH_REDIS_REST_URL='https://b.upstash.io/';",
     "https://b.upstash.io/", "tok2"),
    # Vercel KV calls the same thing something else
    ('KV_REST_API_URL="https://kv.example"\nKV_REST_API_TOKEN="kv-token"',
     "https://kv.example", "kv-token"),
    # …and hands out a read-only token beside the real one, which must be ignored
    ('KV_REST_API_URL="https://kv.example"\nKV_REST_API_READ_ONLY_TOKEN="ro"\n'
     'KV_REST_API_TOKEN="real-token"',
     "https://kv.example", "real-token"),
    # surrounding prose is ignored: only the named lines count
    ('Here you go:\nUPSTASH_REDIS_REST_URL="https://c.upstash.io"\n'
     'UPSTASH_REDIS_REST_TOKEN="tok3"\nbye!',
     "https://c.upstash.io", "tok3"),
])
def test_the_pair_is_read_however_the_provider_wrote_it(text, expected_url, expected_token):
    paste = database_adapters.parse_database_paste(text)

    assert paste["action"] == "rest_pair"
    assert paste["url"] == expected_url
    assert paste["token"] == expected_token


def test_half_a_pair_is_recognized_as_half():
    assert database_adapters.parse_database_paste(
        'UPSTASH_REDIS_REST_URL="https://d.upstash.io"')["action"] == "rest_url"
    assert database_adapters.parse_database_paste(
        'UPSTASH_REDIS_REST_TOKEN="tok4"')["action"] == "rest_token"


@pytest.mark.parametrize("text, kind", [
    ("mysql://user:password@db-host:3306/p2p", "mysql"),
    ("mariadb://user:password@db-host/p2p", "mysql"),
    ("postgres://user:password@db-host:5432/p2p", "postgres"),
    ("mongodb+srv://user:password@cluster.example/p2p", "mongo"),
    ("redis://user:password@db-host:6379/0", "redis"),
    ("sqlite:///home/user/p2p-bot.db", "sqlite"),
    ("/var/lib/p2p-bot/bot.sqlite", "sqlite"),
    ("p2p-bot.sqlite3", "sqlite"),
    ("KV_URL=redis://default:password@db-host:6379/0", "redis"),
    ("P2P_CUSTOM_DB_URL=postgres://user:password@db-host:5432/p2p", "postgres"),
])
def test_connection_uris_and_sqlite_paths_are_recognized(text, kind):
    paste = database_adapters.parse_database_paste(text)

    assert paste["action"] == "uri"
    assert paste["kind"] == kind


def test_a_bare_rest_endpoint_counts_only_when_it_names_a_database():
    assert database_adapters.parse_database_paste(
        "https://superb-abc.upstash.io")["action"] == "rest_url"
    assert database_adapters.parse_database_paste("https://eu1-abcd.redislabs.com")["action"] == "rest_url"
    # a port that cannot be reached / is not a port at all stays unrecognized
    for odd in ("https://x.upstash.io:0", "https://x.upstash.io:notaport"):
        assert database_adapters.parse_database_paste(odd)["action"] == "none"


@pytest.mark.parametrize("text", [
    "", "hello", "just a sentence",
    "https://www.okx.com/p2p-markets/USD/USDT",          # a merchant page
    "https://t.me/some_channel",                          # a channel link
    "https://example.com/pricing",                        # a website with a path
    "https://example.com",                                # …and without one
    "a plain file with no extension",
    "UPSTASH_REDIS_REST_URL",                             # a name without a value
])
def test_prose_and_links_are_never_mistaken_for_a_database(text):
    assert database_adapters.parse_database_paste(text)["action"] == "none"


# ── the Telegram flow ──────────────────────────────────────────────────────
def test_pasting_the_two_lines_connects_the_database(bot, fake_rest_store, isolated_settings):
    message = _message(UPSTASH_BLOCK)

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    answer = message.reply_html.await_args.args[0]
    assert "connected and selected" in answer
    assert bot.STORE.backend == "redis"
    assert bot.storage.custom_redis_config() == ("https://superb-redbixxxh.upstash.io",
                                                 "gQAAxxxgcDE3OTI5MjBkM2Y2ZTM0M2E4OWZhMTQwN2U2YzRjNjU2ZA")
    import runtime_config
    saved = runtime_config.load()
    assert saved[runtime_config.CUSTOM_REDIS_URL] == "https://superb-redbixxxh.upstash.io"


def test_the_pasted_token_is_never_echoed_back(bot, fake_rest_store, isolated_settings):
    message = _message(UPSTASH_BLOCK)

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    for call in list(message.reply_html.await_args_list) + list(message.reply_text.await_args_list):
        for part in call.args:
            assert "gQAAxxxgcDE3OTI5MjBkM2Y2ZTM0M2E4OWZhMTQwN2U2YzRjNjU2ZA" not in str(part)
    assert "gQAAxxx" not in str(bot.state)


def test_a_url_alone_still_only_asks_for_the_token(bot, isolated_settings):
    message = _message('UPSTASH_REDIS_REST_URL="https://e.upstash.io"')
    update = _private(message)
    bot.state.setdefault("edits", {})[str(ADMIN)] = {"awaiting_custom": "database_token",
                                                     "db_custom_url": "https://old.example"}

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    edits = bot.state["edits"][str(ADMIN)]
    assert edits["db_custom_url"] == "https://e.upstash.io"      # the newer URL wins
    assert edits["awaiting_custom"] == "database_token"
    asked = message.reply_html.await_args.args[0]
    assert "REST token" in asked
    assert "UPSTASH_REDIS_REST_TOKEN" in asked


def test_a_lonely_token_asks_for_its_url(bot, isolated_settings):
    message = _message('UPSTASH_REDIS_REST_TOKEN="lonely-token"')

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    asked = message.reply_html.await_args.args[0]
    assert "UPSTASH_REDIS_REST_URL" in asked
    assert "lonely-token" not in asked
    assert bot.db_is_persistent() is False                # nothing was connected


def test_a_uri_connects_even_when_the_draft_was_lost(bot, isolated_settings):
    """The Vercel case: the type button ran in one instance, the URI hit another."""
    tmp_path = isolated_settings
    bot.state["edits"] = {}                               # no draft, no chosen type
    message = _message(f"sqlite://{tmp_path / 'lost.db'}")

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    assert "connected and selected" in message.reply_html.await_args.args[0]
    assert bot.STORE.backend == "sqlite"
    assert bot.db_is_persistent() is True


def test_a_sqlite_file_name_connects_without_a_type_button(bot, isolated_settings):
    target = isolated_settings / "pasted.db"
    message = _message(str(target))

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    assert "connected and selected" in message.reply_html.await_args.args[0]
    assert bot.STORE.backend == "sqlite"
    assert target.exists()


def test_a_chosen_type_still_guards_against_a_mismatched_uri(bot, isolated_settings):
    bot.state.setdefault("edits", {})[str(ADMIN)] = {"awaiting_custom": "database_uri",
                                                     "db_kind": "mysql"}
    message = _message("postgres://user:password@db-host:5432/p2p")

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    error = message.reply_text.await_args.args[0]
    assert "MySQL" in error
    assert bot.db_is_persistent() is False


def test_the_database_command_accepts_a_connection_inline(bot, isolated_settings):
    message = _message()
    update = _private(message)
    context = SimpleNamespace(args=[f"sqlite://{isolated_settings / 'cmd.db'}"])

    asyncio.run(bot.database_cmd(update, context))

    assert "connected and selected" in message.reply_html.await_args.args[0]
    assert bot.STORE.backend == "sqlite"


def test_the_database_command_still_shows_the_panel_without_arguments(bot):
    message = _message()

    asyncio.run(bot.database_cmd(_private(message), SimpleNamespace(args=[])))

    assert "Database — state storage" in message.reply_html.await_args.args[0]
    assert message.reply_html.await_args.kwargs["reply_markup"] is not None


def test_an_unrecognized_command_argument_explains_and_shows_the_panel(bot):
    message = _message()

    asyncio.run(bot.database_cmd(_private(message), SimpleNamespace(args=["nonsense"])))

    answer = message.reply_html.await_args.args[0]
    assert answer.startswith("❌")
    assert "Database — state storage" in answer


def test_the_paste_path_leaves_other_editors_alone(bot, isolated_settings):
    """A URL being typed for the banner is not a database connection."""
    bot.state.setdefault("edits", {})[str(ADMIN)] = {"awaiting_custom": "banner_url"}
    message = _message(f"sqlite://{isolated_settings / 'not-this.db'}")

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    assert "❌" in message.reply_text.await_args.args[0]      # the banner editor answered
    assert bot.db_is_persistent() is False


def test_an_ordinary_sentence_keeps_the_merchant_hint(bot):
    message = _message("hello there")

    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))

    text = message.reply_text.await_args.args[0]
    assert "Not a supported merchant URL" in text
    assert "UPSTASH_REDIS_REST_URL" in text                  # …plus how to connect a database
