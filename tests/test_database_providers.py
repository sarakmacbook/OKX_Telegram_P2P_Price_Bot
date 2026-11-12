"""One-tap provider presets in ``/database``.

A hosted database publishes the same port, database name, user and TLS setting
for every instance, so a preset carries those and asks only what is unique —
usually the endpoint host and the password.  These tests cover the presets
themselves (every one assembles a URI its parser accepts) and the Telegram flow
that skips the questions a preset already answers.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import database_adapters

ADMIN = 424242
TRICKY_PASSWORD = "p@ss/w:rd#1%& x"


def _private(message, query=None):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private", id=ADMIN),
                             message=message)
    if query is not None:
        update.callback_query = query
    return update


def _message(text=""):
    return SimpleNamespace(text=text, reply_text=AsyncMock(), reply_html=AsyncMock())


def _query(data, message=None):
    message = message or _message()
    return SimpleNamespace(data=data, message=message, answer=AsyncMock(),
                           edit_message_text=AsyncMock())


def _tap(bot, data):
    query = _query(data)
    asyncio.run(bot.on_button(_private(query.message, query), SimpleNamespace()))
    return query


def _say(bot, text):
    message = _message(text)
    asyncio.run(bot.on_text(_private(message), SimpleNamespace()))
    return message


@pytest.fixture(autouse=True)
def isolate_custom_database_environment(tmp_path, monkeypatch):
    import runtime_config

    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime_config.json"))
    runtime_config.invalidate()
    yield
    runtime_config.invalidate()


# ── the presets ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("slug", database_adapters.PROVIDER_ORDER)
def test_every_preset_is_coherent_and_assembles_a_usable_uri(slug):
    preset = database_adapters.provider(slug)

    assert preset is not None and preset["id"] == slug
    assert preset["kind"] in database_adapters.KIND_PORTS
    assert preset["label"] and preset["emoji"] and preset["where"]
    asked = database_adapters.provider_fields(slug)
    assert asked and set(asked) <= set(database_adapters.FIELD_STEPS)
    for field in database_adapters.FIELD_STEPS:
        value = database_adapters.field_default(preset["kind"], field, slug)
        assert database_adapters.validate_field(preset["kind"], field, value)[1] == ""

    values = {field: database_adapters.field_default(preset["kind"], field, slug)
              for field in database_adapters.FIELD_STEPS}
    values.update({"host": "db.example.com", "password": TRICKY_PASSWORD})
    uri, error = database_adapters.uri_from_fields(
        preset["kind"], values, scheme=preset.get("scheme"), params=preset.get("params"))
    spec, parse_error = database_adapters.parse_database_uri(preset["kind"], uri)

    assert error == "" and parse_error == "" and spec is not None
    assert spec["kind"] == preset["kind"]
    assert TRICKY_PASSWORD not in json.dumps(database_adapters.describe_spec(spec))


def test_the_preset_defaults_replace_the_generic_suggestions():
    assert database_adapters.field_default("postgres", "database", "neon") == "neondb"
    assert database_adapters.field_default("postgres", "user", "neon") == "neondb_owner"
    assert database_adapters.field_default("postgres", "port", "neon") == "5432"
    # no preset: the generic suggestion is unchanged
    assert database_adapters.field_default("postgres", "host") == "localhost"


def test_provider_uris_carry_the_tls_and_scheme_the_provider_needs():
    neon = {"host": "ep-cool-name-123456.us-east-2.aws.neon.tech", "port": "5432",
            "database": "neondb", "user": "neondb_owner", "password": "pw"}
    assert database_adapters.uri_from_fields(
        "postgres", neon, params={"sslmode": "require"})[0] == (
        "postgres://neondb_owner:pw@ep-cool-name-123456.us-east-2.aws.neon.tech:5432"
        "/neondb?sslmode=require")

    atlas = {"host": "cluster0.abcde.mongodb.net",
             "port": database_adapters.field_default("mongo", "port"),
             "database": "p2p", "user": "me", "password": "pw"}
    uri = database_adapters.uri_from_fields("mongo", atlas, scheme="mongodb+srv")[0]
    assert uri == "mongodb+srv://me:pw@cluster0.abcde.mongodb.net/p2p"

    upstash = {"host": "superb-otter-12345.upstash.io", "port": "6379",
               "database": "0", "user": "default", "password": "pw"}
    assert database_adapters.uri_from_fields("redis", upstash, scheme="rediss")[0] == (
        "rediss://default:pw@superb-otter-12345.upstash.io:6379/0")


def test_the_menus_only_offer_a_type_s_presets():
    postgres = database_adapters.providers_for("postgres")
    assert {preset["kind"] for preset in postgres} == {"postgres"}
    assert [preset["id"] for preset in postgres][:2] == ["neon", "supabase"]
    assert database_adapters.provider_fields("no-such-provider") == ()
    assert database_adapters.provider("no-such-provider") is None


# ── Telegram: the questions a preset skips ─────────────────────────────────
def test_quick_connect_offers_every_preset(bot):
    query = _tap(bot, "db_quick")

    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    for slug in database_adapters.PROVIDER_ORDER:
        assert f"db_preset:{slug}" in callbacks
    assert "one-tap" in query.edit_message_text.await_args.args[0].lower()


def test_neon_asks_only_for_the_host_and_the_password_and_connects(bot, monkeypatch):
    connected = Mock(return_value=(True, "Custom database connected and selected."))
    monkeypatch.setattr(bot, "set_custom_database_uri", connected)
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    query = _tap(bot, "db_preset:neon")
    prompt = query.edit_message_text.await_args.args[0]
    assert "Neon" in prompt and "question 1 of 2" in prompt
    assert "neondb" in prompt and "5432" in prompt        # already set, not asked
    edit = bot.state["edits"][str(ADMIN)]
    assert edit["db_preset"] == "neon" and edit["db_field_step"] == 0

    _say(bot, "ep-cool-name-123456.us-east-2.aws.neon.tech")   # host
    _say(bot, TRICKY_PASSWORD)                                 # password — connects

    kind, uri = connected.call_args.args[:2]
    assert kind == "postgres"
    assert uri.startswith("postgres://neondb_owner:")
    assert uri.endswith("@ep-cool-name-123456.us-east-2.aws.neon.tech:5432/neondb"
                        "?sslmode=require")
    assert "awaiting_custom" not in bot.state["edits"][str(ADMIN)]
    assert TRICKY_PASSWORD not in json.dumps(bot.state)


def test_a_hosted_preset_never_suggests_localhost_or_a_standard_port(bot):
    """A shortcut that could only fail is worse than no shortcut."""
    query = _tap(bot, "db_preset:neon")
    text = query.edit_message_text.await_args.args[0]
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "localhost" not in text
    assert not any("Use suggested" in label for label in labels)

    # Railway assigns the public port, so the standard 5432 must not be offered.
    _tap(bot, "db_preset:railway-postgres")
    message = _say(bot, "containers-us-west-12.railway.app")
    text = message.reply_html.await_args.args[0]
    markup = message.reply_html.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "question 2 of 3" in text and "Port" in text
    assert not any("Use suggested" in label for label in labels)


def test_a_preset_with_one_question_connects_after_a_single_answer(bot, monkeypatch):
    connected = Mock(return_value=(True, "ok"))
    monkeypatch.setattr(bot, "set_custom_database_uri", connected)
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_preset:local-mongo")
    _say(bot, "-")                     # keep the suggested localhost

    assert connected.call_args.args[0] == "mongo"
    assert connected.call_args.args[1] == "mongodb://localhost:27017/p2p"


def test_a_failed_preset_connection_restarts_at_its_first_question(bot, monkeypatch):
    monkeypatch.setattr(bot, "set_custom_database_uri",
                        Mock(return_value=(False, "did not answer — Connection refused")))
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_preset:railway-postgres")
    _say(bot, "containers-us-west-12.railway.app")     # host
    _say(bot, "6543")                                  # port Railway assigned
    message = _say(bot, TRICKY_PASSWORD)               # password — fails

    reply = message.reply_html.await_args.args[0]
    assert "did not answer" in reply and "Nothing was changed" in reply
    edit = bot.state["edits"][str(ADMIN)]
    assert edit["db_field_step"] == 0 and edit["db_preset"] == "railway-postgres"
    assert TRICKY_PASSWORD not in json.dumps(bot.state)


def test_picking_another_provider_or_type_drops_the_old_preset(bot, monkeypatch):
    monkeypatch.setattr(bot, "set_custom_database_uri", Mock(return_value=(True, "ok")))
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_preset:neon")
    _tap(bot, "db_fields:mysql")
    assert "db_preset" not in bot.state["edits"][str(ADMIN)]

    _tap(bot, "db_preset:supabase")
    _tap(bot, "db_type:postgres")
    assert "db_preset" not in bot.state["edits"][str(ADMIN)]


def test_an_unknown_provider_is_refused_without_touching_the_draft(bot):
    query = _tap(bot, "db_preset:not-a-provider")

    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_awaited()
