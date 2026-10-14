"""Connecting a database step by step — no connection URI has to be written.

The Telegram ``/database`` menu and the terminal wizard both ask for the
server, port, database name, user and password one at a time and assemble the
URI themselves. These tests cover the assembly (including passwords with
characters that must be escaped), the questions in Telegram, the SQLite
one-tap option, and the terminal prompts.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import database_adapters

ADMIN = 424242
TRICKY_PASSWORD = "p@ss/w:rd#1%& x"


#: Every name the wizard can write straight into os.environ (see test_setup.py).
TOUCHED = (
    "BOT_TOKEN", "ADMIN_IDS", "ASSET", "FIAT", "INTERVAL", "P2P_STATE_BACKEND",
    "P2P_CUSTOM_DB_URL", "P2P_CUSTOM_KV_REST_API_URL", "P2P_CUSTOM_KV_REST_API_TOKEN",
    "KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "P2P_DATA_DIR",
)


@pytest.fixture(autouse=True)
def isolate_custom_database_environment(tmp_path, monkeypatch):
    """A database picked in one test must not leak into the next one."""
    import runtime_config

    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime_config.json"))
    before = {name: os.environ.get(name) for name in TOUCHED}
    runtime_config.invalidate()
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    runtime_config.invalidate()


# ── the helpers ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["mysql", "postgres", "mongo", "redis"])
def test_assembled_uri_round_trips_through_the_parser(kind):
    values = {"host": "db.example.com", "port": database_adapters.field_default(kind, "port"),
              "database": "2" if kind == "redis" else "p2p",
              "user": "p2p_user", "password": TRICKY_PASSWORD}

    uri, error = database_adapters.uri_from_fields(kind, values)
    spec, parse_error = database_adapters.parse_database_uri(kind, uri)

    assert error == "" and parse_error == ""
    assert spec["kind"] == kind
    assert spec["host"] == "db.example.com"
    if kind in ("mysql", "postgres"):
        # the SQL parsers unescape the user and password — they come back as typed
        assert spec["user"] == "p2p_user"
        assert spec["password"] == TRICKY_PASSWORD
        assert spec["database"] == "p2p"


def test_the_password_is_escaped_so_it_cannot_change_the_uri():
    uri = database_adapters.build_uri("mysql", host="h", port="3306", database="p2p",
                                      user="u", password="a/b@c:d#e")

    assert uri == "mysql://u:a%2Fb%40c%3Ad%23e@h:3306/p2p"


def test_no_user_or_password_gives_a_plain_uri():
    uri, error = database_adapters.uri_from_fields("postgres", {"host": "localhost"})

    assert error == ""
    assert uri == "postgres://localhost:5432/p2p"


def test_ipv6_hosts_are_bracketed():
    uri, error = database_adapters.uri_from_fields("mysql", {"host": "::1"})

    assert error == ""
    assert uri == "mysql://[::1]:3306/p2p"


@pytest.mark.parametrize("kind, field, value", [
    ("mysql", "port", "0"),
    ("mysql", "port", "70000"),
    ("mysql", "port", "abc"),
    ("mysql", "host", "db@example.com"),
    ("mysql", "host", "db.example.com/p2p"),
    ("mysql", "database", "my db"),
    ("mysql", "database", "a/b"),
    ("redis", "database", "16"),
    ("redis", "database", "main"),
])
def test_bad_answers_are_refused_with_a_reason(kind, field, value):
    clean, error = database_adapters.validate_field(kind, field, value)

    assert clean == ""
    assert error and "<" not in error                    # plain text, safe for terminal and chat


def test_empty_user_and_password_are_fine_and_the_defaults_are_sensible():
    assert database_adapters.validate_field("mysql", "user", "")[1] == ""
    assert database_adapters.validate_field("mysql", "password", "")[1] == ""
    assert database_adapters.field_default("mysql", "port") == "3306"
    assert database_adapters.field_default("redis", "database") == "0"
    assert database_adapters.field_default("postgres", "host") == "localhost"


def test_sqlite_has_no_step_by_step_questions():
    assert database_adapters.field_steps("sqlite") == ()
    assert database_adapters.field_steps("mysql") == database_adapters.FIELD_STEPS


def test_the_one_tap_sqlite_file_sits_next_to_the_data_or_in_p2p_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("P2P_DATA_DIR", str(tmp_path))
    assert database_adapters.default_sqlite_path() == str(tmp_path / "p2p-bot.db")

    monkeypatch.delenv("P2P_DATA_DIR")
    assert database_adapters.default_sqlite_path().endswith("p2p-bot.db")


# ── Telegram: one question at a time ───────────────────────────────────────
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


def _asked(message):
    return (message.reply_html.await_args or message.reply_text.await_args).args[0]


def test_the_type_prompt_offers_step_by_step_for_remote_databases(bot):
    markup = bot.database_uri_kb("mysql")

    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["db_fields:mysql", "db_cancel"]
    assert "step by step" in bot.database_uri_prompt("mysql")


def test_step_by_step_asks_each_question_and_connects_with_the_built_uri(bot, monkeypatch):
    connected = Mock(return_value=(True, "Custom database connected and selected."))
    monkeypatch.setattr(bot, "set_custom_database_uri", connected)
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_fields:mysql")
    edit = bot.state["edits"][str(ADMIN)]
    assert edit["awaiting_custom"] == "database_field" and edit["db_field_step"] == 0

    _say(bot, "db.example.com")          # host
    _say(bot, "-")                       # port: "-" keeps the suggested 3306
    _say(bot, "p2p")                     # database
    _say(bot, "p2p_user")                # user
    _say(bot, TRICKY_PASSWORD)           # password — the last question connects

    connected.assert_called_once()
    kind, uri = connected.call_args.args[:2]
    assert kind == "mysql"
    assert uri == "mysql://p2p_user:p%40ss%2Fw%3Ard%231%25%26%20x@db.example.com:3306/p2p"
    assert "awaiting_custom" not in bot.state["edits"][str(ADMIN)]


def test_the_password_is_never_kept_in_bot_state(bot, monkeypatch):
    monkeypatch.setattr(bot, "set_custom_database_uri",
                        Mock(return_value=(False, "did not answer — Connection refused")))
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_fields:postgres")
    for answer in ["db.example.com", "-", "-", "-", TRICKY_PASSWORD]:
        _say(bot, answer)
        assert TRICKY_PASSWORD not in json.dumps(bot.state)
    # the password was the last question and the connection failed: the draft
    # keeps the non-secret answers and starts again at the first question
    assert bot.state["edits"][str(ADMIN)]["db_field_step"] == 0
    assert TRICKY_PASSWORD not in json.dumps(bot.state)


def test_a_failed_connection_keeps_the_answers_as_suggestions(bot, monkeypatch):
    connected = Mock(return_value=(False, "did not answer — Connection refused"))
    monkeypatch.setattr(bot, "set_custom_database_uri", connected)

    _tap(bot, "db_fields:postgres")
    for answer in ["db.example.com", "5433", "shop", "-", "-"]:
        message = _say(bot, answer)

    reply = _asked(message)
    assert "did not answer" in reply and "Nothing was changed" in reply
    edit = bot.state["edits"][str(ADMIN)]
    assert edit["db_field_step"] == 0
    assert edit["db_fields"] == {"host": "db.example.com", "port": "5433", "database": "shop",
                                 "user": ""}
    assert "5433" in reply or "db.example.com" in reply   # the suggestion is shown again


def test_a_bad_answer_is_asked_again_without_moving_on(bot):
    _tap(bot, "db_fields:mysql")
    message = _say(bot, "not a host@")

    assert "❌" in _asked(message)
    assert bot.state["edits"][str(ADMIN)]["db_field_step"] == 0


def test_the_use_suggested_button_takes_the_suggested_value(bot, monkeypatch):
    monkeypatch.setattr(bot, "set_custom_database_uri", Mock(return_value=(True, "ok")))
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_fields:mysql")
    query = _tap(bot, "db_field_default")     # host: localhost
    assert bot.state["edits"][str(ADMIN)]["db_fields"]["host"] == "localhost"
    assert bot.state["edits"][str(ADMIN)]["db_field_step"] == 1
    assert query.answer.await_count >= 1


def test_cancel_drops_the_whole_draft(bot):
    _tap(bot, "db_fields:mysql")
    _say(bot, "db.example.com")
    asyncio.run(bot.cancel_cmd(_private(_message("/cancel")), SimpleNamespace()))

    edit = bot.state["edits"][str(ADMIN)]
    assert "db_fields" not in edit and "db_field_step" not in edit
    assert "awaiting_custom" not in edit


def test_a_pasted_uri_mid_way_is_connected_as_it_is(bot, monkeypatch):
    connected = Mock(return_value=(True, "ok"))
    monkeypatch.setattr(bot, "set_custom_database_uri", connected)
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_fields:mysql")
    _say(bot, "mysql://u:pw@db.example.com:3306/p2p")

    assert connected.call_args.args[1] == "mysql://u:pw@db.example.com:3306/p2p"


def test_one_tap_sqlite_uses_the_default_file(bot, monkeypatch, tmp_path):
    monkeypatch.setenv("P2P_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot.storage, "serverless_host", lambda: False)
    connected = Mock(return_value=(True, "ok"))
    monkeypatch.setattr(bot, "set_custom_database_uri", connected)
    monkeypatch.setattr(bot, "reply_database_selected", AsyncMock())

    _tap(bot, "db_sqlite_default")

    assert connected.call_args.args == ("sqlite", str(tmp_path / "p2p-bot.db"))


def test_one_tap_sqlite_is_not_offered_on_a_serverless_host(bot, monkeypatch):
    monkeypatch.setattr(bot.storage, "serverless_host", lambda: True)

    callbacks = [b.callback_data for row in bot.database_uri_kb("sqlite").inline_keyboard for b in row]

    assert "db_sqlite_default" not in callbacks


# ── terminal wizard ────────────────────────────────────────────────────────
def _scripted(answers):
    values = iter(answers)
    return lambda prompt="": next(values)


def test_the_terminal_asks_each_question_and_saves_the_tested_uri(monkeypatch, capsys):
    import setup_cli
    import storage

    tested = []

    def fake_probe(store, timeout=None):
        tested.append(store.spec["uri"])
        return True, "the database answered"

    monkeypatch.setattr(storage, "probe_store", fake_probe)
    prompts = setup_cli.Prompts(reader=_scripted(["3", "db.example.com", "", "p2p", "p2p_user",
                                                  TRICKY_PASSWORD]), echo=False)

    uri = setup_cli.ask_database(prompts)

    assert uri == "postgres://p2p_user:p%40ss%2Fw%3Ard%231%25%26%20x@db.example.com:5432/p2p"
    assert tested == [uri]
    assert TRICKY_PASSWORD not in capsys.readouterr().out


def test_the_terminal_says_why_an_unreachable_database_is_not_saved(monkeypatch, capsys):
    import setup_cli
    import storage

    monkeypatch.setattr(storage, "probe_store",
                        lambda store, timeout=None: (False, "OperationalError: refused"))
    prompts = setup_cli.Prompts(reader=_scripted(["2", "localhost", "", "p2p", "", ""]), echo=False)

    assert setup_cli.ask_database(prompts) == ""
    out = capsys.readouterr().out
    assert "did not answer" in out and "was not saved" in out


def test_skipping_the_database_asks_nothing_more(capsys):
    import setup_cli

    prompts = setup_cli.Prompts(reader=_scripted(["0"]), echo=False)

    assert setup_cli.ask_database(prompts) == ""
    assert "Server address" not in capsys.readouterr().out


def test_the_wizard_saves_the_chosen_database(monkeypatch, tmp_path):
    import runtime_config
    import setup_cli
    import storage

    monkeypatch.setenv("P2P_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "probe_store", lambda store, timeout=None: (True, "ok"))
    monkeypatch.setattr(setup_cli.runtime_config, "verify_bot_token", lambda token, **kw: (None, ""))
    # bot token, admin id, asset, fiat, interval, then the database menu: 1 = SQLite,
    # and Enter keeps the suggested file path
    answers = iter(["y", "123456:ABCdef", "424242", "", "", "", "1", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert setup_cli.main([]) == 0
    stored = runtime_config.load()
    assert stored.get(runtime_config.CUSTOM_DB_URL) == f"sqlite://{tmp_path / 'p2p-bot.db'}"
