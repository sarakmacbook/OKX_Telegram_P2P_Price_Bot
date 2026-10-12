"""The settings copy that lives in a pinned Telegram message.

This is what lets a deployment start with **no database at all** and still keep
the database an admin connects later with ``/database``: Vercel throws its
filesystem away with the instance, so the only durable place the bot already has
is the chat itself.

The tests fake the Bot API (``httpx.post``) the same way the rest of the suite
fakes Redis, and they check both halves of the promise: the values come back
after a "restart", and a copy that cannot be read back is *reported* instead of
being trusted.
"""

import os

import pytest

import runtime_config
import storage
import telegram_store
from telegram_store import TelegramConfigStore, open_sealed, seal

#: Every name ``runtime_config.apply`` may write into ``os.environ`` — it does
#: that on purpose (that is how a cold start picks the pinned credentials up),
#: so a test that exercises it must not leave them behind for the next module.
_LEAKED = (*storage.CUSTOM_REDIS_ENV_PAIR, "KV_REST_API_URL", "KV_REST_API_TOKEN",
           "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
           "REDIS_REST_URL", "REDIS_REST_TOKEN")


@pytest.fixture(autouse=True)
def no_leaked_store_config():
    yield
    for name in _LEAKED:
        os.environ.pop(name, None)
    runtime_config.invalidate()


class FakeReply:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeTelegram:
    """Enough of the Bot API to keep a pinned message per chat."""

    def __init__(self):
        self.messages: dict[int, list[dict]] = {}      # chat id → [{message_id, text}]
        self.pinned: dict[int, int] = {}               # chat id → pinned message id
        self.unknown: set[int] = set()                 # chats the bot cannot look at
        self.fail: str | None = None                   # a method that raises
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 100

    # -- helper for tests ---------------------------------------------------
    def pin_text(self, chat_id: int, text: str) -> int:
        """Put a message in a chat and pin it, as the bot itself would."""
        self.next_id += 1
        self.messages.setdefault(chat_id, []).append({"message_id": self.next_id, "text": text})
        self.pinned[chat_id] = self.next_id
        return self.next_id

    def pinned_text(self, chat_id: int) -> str:
        message_id = self.pinned.get(chat_id)
        for message in self.messages.get(chat_id, []):
            if message["message_id"] == message_id:
                return message["text"]
        return ""

    # -- the API ------------------------------------------------------------
    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        payload = dict(json or {})
        self.calls.append((method, payload))
        if self.fail == method:
            raise RuntimeError("connection refused")
        if method == "getChat":
            chat_id = int(payload["chat_id"])
            if chat_id in self.unknown:
                return FakeReply({"ok": False, "description": "Bad Request: chat not found"})
            result = {"id": chat_id, "type": "private"}
            message_id = self.pinned.get(chat_id)
            if message_id is not None:
                for message in self.messages.get(chat_id, []):
                    if message["message_id"] == message_id:
                        result["pinned_message"] = {"message_id": message_id,
                                                    "chat": {"id": chat_id},
                                                    "text": message["text"]}
            return FakeReply({"ok": True, "result": result})
        if method in ("sendMessage", "editMessageText", "pinChatMessage") \
                and int(payload["chat_id"]) in self.unknown:
            return FakeReply({"ok": False, "description": "Bad Request: chat not found"})
        if method == "sendMessage":
            chat_id = int(payload["chat_id"])
            self.next_id += 1
            self.messages.setdefault(chat_id, []).append({"message_id": self.next_id,
                                                          "text": payload.get("text", "")})
            return FakeReply({"ok": True, "result": {"message_id": self.next_id, "chat":
                                                     {"id": chat_id}}})
        if method == "editMessageText":
            chat_id, message_id = int(payload["chat_id"]), int(payload["message_id"])
            for message in self.messages.get(chat_id, []):
                if message["message_id"] == message_id:
                    message["text"] = payload.get("text", "")
                    return FakeReply({"ok": True, "result": message})
            return FakeReply({"ok": False, "description": "message to edit not found"})
        if method == "pinChatMessage":
            self.pinned[int(payload["chat_id"])] = int(payload["message_id"])
            return FakeReply({"ok": True, "result": True})
        return FakeReply({"ok": True, "result": True})


@pytest.fixture()
def telegram(monkeypatch):
    api = FakeTelegram()
    import httpx
    monkeypatch.setattr(httpx, "post", api.post)
    telegram_store.reset_cache()
    runtime_config.invalidate()
    runtime_config.prefer_chat(None)
    yield api
    telegram_store.reset_cache()
    runtime_config.invalidate()
    runtime_config.prefer_chat(None)


def _sealed_in_chat(api: FakeTelegram, chat_id: int, values: dict, secret: str) -> None:
    api.pin_text(chat_id, telegram_store.message_text(seal(values, secret)))


# ── sealing ───────────────────────────────────────────────────────────────
def test_sealed_settings_come_back_unchanged():
    values = {"BOT_TOKEN": "123456:SECRET", "ASSET": "USDT", "INTERVAL": "60"}

    blob = seal(values, "123456:SECRET")

    assert "SECRET" not in blob, "the chat must never hold a readable token"
    assert open_sealed(blob, "123456:SECRET") == values


def test_the_same_values_seal_differently_every_time():
    values = {"ASSET": "USDT"}

    assert seal(values, "token") != seal(values, "token")


def test_a_payload_from_another_token_is_refused():
    blob = seal({"ASSET": "USDT"}, "123456:OTHER")

    assert open_sealed(blob, "123456:SECRET") is None


def test_an_edited_payload_is_refused():
    blob = seal({"ASSET": "USDT"}, "token")
    head, *rest = blob.split(".")
    tampered = ".".join([head] + rest[:-1] + ["AAAA"])

    assert open_sealed(tampered, "token") is None


@pytest.mark.parametrize("junk", ["", "not a payload", "P2P-CONFIG1.a.b.c", "P2P-CONFIG2.a.b.c.d"])
def test_junk_is_refused_without_raising(junk):
    assert open_sealed(junk, "token") is None


# ── the store ─────────────────────────────────────────────────────────────
def test_settings_are_pinned_once_and_read_back(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])

    store.save({"ASSET": "USDT", "P2P_CUSTOM_KV_REST_API_TOKEN": "kv-token"})

    assert telegram.pinned_text(42).startswith(telegram_store.MARKER)
    assert "kv-token" not in telegram.pinned_text(42)
    methods = [method for method, _ in telegram.calls]
    assert "sendMessage" in methods and "pinChatMessage" in methods
    assert len(telegram.messages[42]) == 1, "one message is enough"

    # A cold start (this is the point) reads the same values back.
    telegram_store.reset_cache()
    fresh = TelegramConfigStore("123456:TOKEN", [42])

    assert fresh.load() == {"ASSET": "USDT", "P2P_CUSTOM_KV_REST_API_TOKEN": "kv-token"}


def test_a_second_save_edits_the_same_pinned_message(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])
    store.save({"ASSET": "USDT"})
    first = telegram.pinned[42]

    store.save({"ASSET": "BTC"})

    assert telegram.pinned[42] == first, "no second pinned message in the chat"
    assert "editMessageText" in [method for method, _ in telegram.calls]
    telegram_store.reset_cache()
    assert TelegramConfigStore("123456:TOKEN", [42]).load() == {"ASSET": "BTC"}


def test_a_deleted_pin_is_replaced(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])
    store.save({"ASSET": "USDT"})
    telegram.messages[42] = []                     # the admin deleted it
    store.forget()

    store.save({"ASSET": "ETH"})

    assert telegram.pinned_text(42).startswith(telegram_store.MARKER)
    telegram_store.reset_cache()
    assert TelegramConfigStore("123456:TOKEN", [42]).load() == {"ASSET": "ETH"}


def test_the_settings_live_in_the_first_chat_that_has_them(telegram):
    _sealed_in_chat(telegram, 77, {"ASSET": "BTC"}, "123456:TOKEN")
    store = TelegramConfigStore("123456:TOKEN", [42, 77])

    assert store.load() == {"ASSET": "BTC"}
    assert store.chat_ids == [42, 77]


def test_an_unrelated_pinned_message_is_ignored(telegram):
    telegram.pin_text(42, "Welcome to the support chat!")
    store = TelegramConfigStore("123456:TOKEN", [42])

    assert store.load() is None

    values, _where, notes = store.fetch()
    assert values is None
    assert any("not the bot's settings copy" in note for note in notes)


def test_a_pin_from_another_bot_token_is_reported_not_trusted(telegram):
    _sealed_in_chat(telegram, 42, {"ASSET": "USDT"}, "999:OTHER-TOKEN")
    store = TelegramConfigStore("123456:TOKEN", [42])

    assert store.load() is None
    ok, detail = store.probe()

    assert ok is False and "cannot be opened" in detail


def test_a_readable_pin_verifies(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])
    store.save({"ASSET": "USDT"})

    ok, detail = store.probe()

    assert ok is True and "readable" in detail


def test_probe_reports_a_chat_that_cannot_be_read(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])
    telegram.fail = "getChat"

    ok, detail = store.probe()

    assert ok is False and "connection refused" in detail


def test_a_failing_save_never_raises_and_keeps_the_settings_in_memory(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])
    telegram.fail = "sendMessage"

    store.save({"ASSET": "USDT"})                  # must not raise

    assert telegram.pinned.get(42) is None
    assert store.load() is None


def test_a_chat_the_bot_cannot_read_falls_through_to_the_next_one(telegram):
    telegram.unknown = {42}                      # e.g. an admin who never started the bot
    store = TelegramConfigStore("123456:TOKEN", [42, 77])

    store.save({"ASSET": "USDT"})

    assert telegram.pinned.get(77) and not telegram.pinned.get(42)
    telegram_store.reset_cache()
    assert TelegramConfigStore("123456:TOKEN", [42, 77]).load() == {"ASSET": "USDT"}


def test_an_oversized_payload_is_refused_instead_of_rejected_by_telegram(telegram):
    store = TelegramConfigStore("123456:TOKEN", [42])

    store.save({"ASSET": "X" * 6000})

    assert telegram.pinned.get(42) is None, "no oversized message was sent"


def test_no_chat_means_no_work():
    store = TelegramConfigStore("", [])

    assert store.load() is None
    store.save({"ASSET": "USDT"})                  # must not raise
    assert store.probe()[0] is False


# ── how the bot uses it ───────────────────────────────────────────────────
def _as_vercel(monkeypatch, on: bool = True):
    if on:
        monkeypatch.setenv("VERCEL", "1")
    else:
        monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123456:TOKEN")
    monkeypatch.setenv("ADMIN_IDS", "424242")


def test_auto_mode_uses_the_chat_only_where_a_file_cannot_survive(telegram, monkeypatch):
    _as_vercel(monkeypatch)
    monkeypatch.delenv("P2P_TELEGRAM_SETTINGS", raising=False)

    store = runtime_config.config_store()

    assert store.backend == "telegram" and store.chat_ids == [424242]


def test_off_mode_keeps_the_local_file_even_on_vercel(telegram, monkeypatch):
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "off")

    store = runtime_config.config_store()

    assert store.backend == "file"


def test_off_the_serverless_host_the_local_file_is_used(telegram, monkeypatch):
    _as_vercel(monkeypatch, on=False)
    monkeypatch.delenv("P2P_TELEGRAM_SETTINGS", raising=False)

    store = runtime_config.config_store()

    assert store.backend == "file", "a VPS/Docker install keeps its settings on disk"


def test_a_custom_database_survives_a_restart_through_the_pinned_copy(telegram, monkeypatch):
    """The whole point: connect a database in Telegram, and find it again later."""
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    for name in storage.CUSTOM_REDIS_ENV_PAIR:
        monkeypatch.delenv(name, raising=False)

    runtime_config.prefer_chat(424242)
    saved = runtime_config.save({runtime_config.CUSTOM_REDIS_URL: "https://db.upstash.io",
                                 runtime_config.CUSTOM_REDIS_TOKEN: "db-token"})
    assert saved[runtime_config.CUSTOM_REDIS_TOKEN] == "db-token"
    assert telegram.pinned.get(424242), "the credentials were pinned in the chat"

    # A new instance: no environment variables, only the chat.
    telegram_store.reset_cache()
    runtime_config.invalidate()
    for name in (runtime_config.CUSTOM_REDIS_URL, runtime_config.CUSTOM_REDIS_TOKEN):
        monkeypatch.delenv(name, raising=False)

    applied = runtime_config.apply(force=True)

    assert applied.get(runtime_config.CUSTOM_REDIS_URL) == "https://db.upstash.io"
    assert storage.custom_redis_config() == ("https://db.upstash.io", "db-token")
    assert storage.state_backend() == "redis", "the bot is back on its database"


def test_the_environment_pair_still_wins_over_the_chat_copy(telegram, monkeypatch):
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")
    monkeypatch.setenv("KV_REST_API_URL", "https://deployment.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "deployment-token")
    _sealed_in_chat(telegram, 424242,
                    {runtime_config.CUSTOM_REDIS_URL: "https://old.upstash.io",
                     runtime_config.CUSTOM_REDIS_TOKEN: "old-token"},
                    "123456:TOKEN")

    store = runtime_config.config_store()

    assert store.backend == "redis", "a deployment-managed pair is the better store"
    assert "deployment.upstash.io" in store.describe()


def test_the_chat_copy_is_persistent_but_not_a_database(telegram, monkeypatch):
    """Settings survive the instance — the group, merchants and prices do not.

    ``persistent`` is about the saved *settings* (that is what the setup page and
    the terminal wizard report); the state database is a separate question, and
    ``storage.database_connected()`` keeps answering "no" until one is connected.
    """
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")

    described = runtime_config.describe()

    assert described["backend"] == "telegram"
    assert described["persistent"] is True
    assert storage.database_connected() is False


def test_the_chat_copy_needs_a_token_and_an_admin_id(telegram, monkeypatch):
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")
    monkeypatch.delenv("ADMIN_IDS", raising=False)

    assert runtime_config.telegram_config_store() is None
    assert runtime_config.config_store().backend == "file"


def test_a_broken_chat_api_falls_back_to_the_local_file(telegram, monkeypatch):
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")
    telegram.fail = "getChat"

    store = runtime_config.config_store()
    assert store.backend == "telegram"                 # selected…

    assert store.load() is None                        # …but it reports nothing found
    ok, detail = store.probe()
    assert ok is False and detail


def test_saved_settings_never_land_in_the_chat_unsealed(telegram, monkeypatch):
    _as_vercel(monkeypatch)
    monkeypatch.setenv("P2P_TELEGRAM_SETTINGS", "on")
    runtime_config.prefer_chat(424242)

    runtime_config.save({"BOT_TOKEN": "123456:SUPER-SECRET", "ADMIN_IDS": "424242"})

    pinned = telegram.pinned_text(424242)
    blob = telegram_store.payload_of(pinned)

    assert "SUPER-SECRET" not in pinned and "424242" not in pinned
    assert pinned.startswith(telegram_store.MARKER)
    assert blob and telegram_store.open_sealed(blob, "123456:TOKEN") == {
        "BOT_TOKEN": "123456:SUPER-SECRET", "ADMIN_IDS": "424242"}
