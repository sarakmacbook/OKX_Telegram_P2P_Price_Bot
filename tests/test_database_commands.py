"""Custom Redis REST database management from the private Telegram panel."""

import asyncio
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ADMIN = 424242


@pytest.fixture(autouse=True)
def isolate_custom_database_environment():
    """A Telegram-selected pair from one test must not alter later store tests."""
    import runtime_config

    names = runtime_config.CUSTOM_REDIS_URL, runtime_config.CUSTOM_REDIS_TOKEN
    previous = {name: os.environ.pop(name, None) for name in names}
    runtime_config.invalidate()
    yield
    for name in names:
        os.environ.pop(name, None)
    for name, value in previous.items():
        if value is not None:
            os.environ[name] = value
    runtime_config.invalidate()


def _private_update(bot, message):
    user_id = next(iter(bot.ADMINS))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type="private"),
        message=message,
    )


def test_database_menu_offers_custom_database_and_reset(bot, monkeypatch):
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_URL", "https://custom.example")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_TOKEN", "hidden-token")

    callbacks = [button.callback_data for row in bot.database_kb().inline_keyboard for button in row]

    assert callbacks == ["db_custom", "db_reset", "db_check", "panel"]
    text = bot.database_text()
    assert "custom Redis-compatible REST database" in text
    assert "hidden-token" not in text


def test_database_command_and_custom_setup_are_admin_private_only(bot):
    message = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())
    stranger = SimpleNamespace(effective_user=SimpleNamespace(id=999999),
                               effective_chat=SimpleNamespace(type="private"), message=message)
    asyncio.run(bot.database_cmd(stranger, SimpleNamespace()))
    message.reply_html.assert_not_awaited()

    admin_in_group = SimpleNamespace(effective_user=SimpleNamespace(id=next(iter(bot.ADMINS))),
                                     effective_chat=SimpleNamespace(type="group"), message=message)
    asyncio.run(bot.database_cmd(admin_in_group, SimpleNamespace()))
    message.reply_text.assert_awaited_with(
        "Open a private chat with the bot to manage database settings.")


def test_custom_database_conversation_never_repeats_the_token(bot, monkeypatch):
    admin_id = next(iter(bot.ADMINS))
    bot.state.setdefault("edits", {})[str(admin_id)] = {"awaiting_custom": "database_url"}
    url_message = SimpleNamespace(text="https://redis.example", reply_text=AsyncMock())
    url_update = _private_update(bot, url_message)

    asyncio.run(bot.on_text(url_update, SimpleNamespace()))

    edit = bot.state["edits"][str(admin_id)]
    assert edit["awaiting_custom"] == "database_token"
    assert edit["db_custom_url"] == "https://redis.example"

    selected = Mock(return_value=(True, "Custom database connected and selected."))
    monkeypatch.setattr(bot, "set_custom_database", selected)
    token_message = SimpleNamespace(text="super-secret-token", reply_text=AsyncMock(),
                                    reply_html=AsyncMock())
    token_update = _private_update(bot, token_message)

    asyncio.run(bot.on_text(token_update, SimpleNamespace()))

    selected.assert_called_once_with("https://redis.example", "super-secret-token", update=token_update)
    answer = token_message.reply_html.await_args.args[0]
    assert "super-secret-token" not in answer
    assert "Custom database connected" in answer


def test_custom_database_is_tested_persisted_and_migrates_state(bot, monkeypatch, tmp_path):
    import runtime_config

    class FakeRedisStore:
        backend = "redis"
        stores = {}

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

    runtime_config.invalidate()
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("P2P_STATE_BACKEND", "auto")
    monkeypatch.setenv("KV_REST_API_URL", "https://deployment.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "deployment-token")
    monkeypatch.delenv("P2P_CUSTOM_KV_REST_API_URL", raising=False)
    monkeypatch.delenv("P2P_CUSTOM_KV_REST_API_TOKEN", raising=False)
    monkeypatch.setattr(bot.storage, "RedisStore", FakeRedisStore)
    original_store = bot.STORE
    monkeypatch.setattr(bot, "STORE", original_store)
    original_signature = bot._STORE_CONFIG_SIGNATURE
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", original_signature)
    bot.state["group"] = -100123456
    bot.state["group_title"] = "Prices"
    bot.save()

    okay, detail = bot.set_custom_database("https://custom.example", "custom-secret")

    assert okay and detail == "Custom database connected and selected."
    assert bot.STORE.backend == "redis"
    assert bot.STORE.url == "https://custom.example"
    assert bot.state["group"] == -100123456
    assert bot.state["group_title"] == "Prices"
    assert bot.state["edits"].get(str(ADMIN), {}).get("db_custom_url") is None
    assert bot.storage.custom_redis_config() == ("https://custom.example", "custom-secret")
    # The original deployment pair remains unchanged and stays the bootstrap source.
    assert bot.storage.environment_redis_config() == ("https://deployment.example", "deployment-token")
    saved = runtime_config.load()
    assert saved[runtime_config.CUSTOM_REDIS_URL] == "https://custom.example"
    assert saved[runtime_config.CUSTOM_REDIS_TOKEN] == "custom-secret"
    assert ("https://custom.example", runtime_config.config_key()) in FakeRedisStore.stores
    assert "custom-secret" not in str(bot.state)
    assert "custom-secret" not in str(runtime_config.summary())
    runtime_config.invalidate()


def test_returning_to_deployment_database_probes_and_migrates_state(bot, monkeypatch, tmp_path):
    import runtime_config

    class FakeRedisStore:
        backend = "redis"
        stores = {}

        def __init__(self, url, token, key="p2p-price-bot:state", timeout=6.0):
            self.url, self.token, self.key = url, token, key

        def probe(self, timeout=None):
            return self.url == "https://deployment.example", "the deployment store answered"

        def load(self):
            value = self.stores.get((self.url, self.key))
            return deepcopy(value) if value is not None else None

        def save(self, data):
            self.stores[(self.url, self.key)] = deepcopy(data)

        def describe(self):
            return f"redis ({self.url.split('//', 1)[-1]}, key {self.key})"

    runtime_config.invalidate()
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("P2P_STATE_BACKEND", "auto")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://deployment.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "deployment-token")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_URL", "https://custom.example")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_TOKEN", "custom-token")
    monkeypatch.setattr(bot.storage, "RedisStore", FakeRedisStore)
    runtime_config.save({runtime_config.CUSTOM_REDIS_URL: "https://custom.example",
                         runtime_config.CUSTOM_REDIS_TOKEN: "custom-token"})
    bot.state["group"] = -100654321
    bot.state["group_title"] = "Deployment migration"
    active = FakeRedisStore("https://custom.example", "custom-token")
    active.save(bot.state)
    monkeypatch.setattr(bot, "STORE", active)
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", bot._store_config_signature())

    okay, detail = bot.use_deployment_database()

    assert okay and "Returned to the deployment" in detail
    assert bot.STORE.url == "https://deployment.example"
    assert bot.state["group"] == -100654321
    assert bot.storage.custom_redis_config() is None
    assert runtime_config.CUSTOM_REDIS_URL not in runtime_config.load()
    assert runtime_config.CUSTOM_REDIS_TOKEN not in runtime_config.load()
    assert FakeRedisStore.stores[("https://deployment.example", bot.storage.DEFAULT_KEY)]["group"] == -100654321


def test_unhealthy_deployment_fallback_cannot_replace_custom_database(bot, monkeypatch, tmp_path):
    import runtime_config

    class OfflineFallbackStore:
        backend = "redis"
        stores = {}

        def __init__(self, url, token, key="p2p-price-bot:state", timeout=6.0):
            self.url, self.token, self.key = url, token, key

        def load(self):
            value = self.stores.get((self.url, self.key))
            return deepcopy(value) if value is not None else None

        def save(self, data):
            self.stores[(self.url, self.key)] = deepcopy(data)

        def probe(self, timeout=None):
            return False, "private connection detail"

        def describe(self):
            return f"redis ({self.url.split('//', 1)[-1]}, key {self.key})"

    runtime_config.invalidate()
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_BACKEND", "auto")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://deployment.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "deployment-token")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_URL", "https://custom.example")
    monkeypatch.setenv("P2P_CUSTOM_KV_REST_API_TOKEN", "custom-token")
    monkeypatch.setattr(bot.storage, "RedisStore", OfflineFallbackStore)
    runtime_config.save({runtime_config.CUSTOM_REDIS_URL: "https://custom.example",
                         runtime_config.CUSTOM_REDIS_TOKEN: "custom-token"})

    okay, detail = bot.use_deployment_database()

    assert okay is False
    assert "did not answer" in detail
    assert "private connection detail" not in detail
    assert bot.storage.custom_redis_config() == ("https://custom.example", "custom-token")
    assert runtime_config.CUSTOM_REDIS_TOKEN in runtime_config.load()


def test_unreachable_custom_database_leaves_current_store_untouched(bot, monkeypatch, tmp_path):
    import runtime_config

    class OfflineRedisStore:
        backend = "redis"

        def __init__(self, url, token, key="p2p-price-bot:state", timeout=6.0):
            self.url, self.token, self.key = url, token, key

        def probe(self, timeout=None):
            return False, "private error detail"

        def describe(self):
            return "redis (offline.example, key state)"

    runtime_config.invalidate()
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("P2P_CUSTOM_KV_REST_API_URL", raising=False)
    monkeypatch.delenv("P2P_CUSTOM_KV_REST_API_TOKEN", raising=False)
    monkeypatch.setattr(bot.storage, "RedisStore", OfflineRedisStore)
    original_store = bot.STORE
    monkeypatch.setattr(bot, "STORE", original_store)
    bot.state["group"] = -100123456

    okay, detail = bot.set_custom_database("https://offline.example", "secret-token")

    assert okay is False
    assert "did not answer" in detail
    assert "secret-token" not in detail and "private error detail" not in detail
    assert bot.STORE is original_store
    assert bot.state["group"] == -100123456
    assert bot.storage.custom_redis_config() is None
    runtime_config.invalidate()


# ── starting with no database at all (the pinned chat copy) ────────────────
def test_a_database_is_connected_in_telegram_without_any_deployment_store(
        bot, monkeypatch, tmp_path):
    """The flow this feature exists for.

    Vercel, no KV pair, no custom pair, no file that survives a request: the bot
    still runs, and /database connects a store.  What is written in the chat is
    the *sealed* settings copy, so the next instance finds the database again
    instead of asking the admin to type the token a second time.
    """
    import runtime_config
    import storage
    import telegram_store
    from test_telegram_store import FakeTelegram          # sibling test module

    class FakeRedisStore:
        backend = "redis"
        stores = {}

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

    api = FakeTelegram()
    import httpx
    monkeypatch.setattr(httpx, "post", api.post)
    telegram_store.reset_cache()
    runtime_config.invalidate()
    runtime_config.prefer_chat(ADMIN)

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("P2P_TELEGRAM_SETTINGS", raising=False)     # auto: Vercel, no disk
    monkeypatch.setenv("BOT_TOKEN", "123456:TOKEN")
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN))
    monkeypatch.setenv("P2P_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("P2P_CUSTOM_KV_REST_API_URL", raising=False)
    monkeypatch.delenv("P2P_CUSTOM_KV_REST_API_TOKEN", raising=False)
    monkeypatch.setattr(bot.storage, "RedisStore", FakeRedisStore)
    monkeypatch.setattr(bot, "STORE", bot.STORE)
    monkeypatch.setattr(bot, "_STORE_CONFIG_SIGNATURE", bot._STORE_CONFIG_SIGNATURE)

    # The bot is running with nothing but its token and admin id.
    assert runtime_config.describe()["backend"] == "telegram"
    assert bot.db_is_persistent() is False

    okay, detail = bot.set_custom_database("https://custom.example", "custom-secret")

    assert okay and detail == "Custom database connected and selected."
    assert bot.STORE.backend == "redis"
    pinned = api.pinned_text(ADMIN)
    assert pinned.startswith(telegram_store.MARKER)
    assert "custom-secret" not in pinned, "the chat never shows the token"
    assert telegram_store.open_sealed(telegram_store.payload_of(pinned), "123456:TOKEN")[
        runtime_config.CUSTOM_REDIS_TOKEN] == "custom-secret"
    assert bot.verify_pinned_settings()[0] is True
    assert "pinned message" in bot.pinned_copy_note()

    # A recycled instance: environment empty, only the chat left.
    for name in storage.CUSTOM_REDIS_ENV_PAIR:
        os.environ.pop(name, None)
    telegram_store.reset_cache()
    runtime_config.invalidate()

    runtime_config.apply(force=True)

    assert storage.custom_redis_config() == ("https://custom.example", "custom-secret")
    assert storage.state_backend() == "redis"
    for name in storage.CUSTOM_REDIS_ENV_PAIR:
        os.environ.pop(name, None)
    telegram_store.reset_cache()
    runtime_config.invalidate()


def test_the_database_claim_is_not_made_when_the_copy_cannot_be_read(
        bot, monkeypatch):
    """A pin nobody can read back must not be presented as durable storage."""
    import telegram_store
    from test_telegram_store import FakeTelegram

    api = FakeTelegram()
    api.fail = "getChat"
    import httpx
    monkeypatch.setattr(httpx, "post", api.post)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")
    monkeypatch.setenv("BOT_TOKEN", "123456:TOKEN")
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN))
    telegram_store.reset_cache()

    okay, detail = bot.verify_pinned_settings()

    assert okay is False
    assert "connection refused" in detail
