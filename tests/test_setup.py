"""Both halves of the first-start setup: the browser form and the terminal wizard.

The deployment needs ``BOT_TOKEN`` / ``ADMIN_IDS`` (and a KV store on Vercel).
They can be added

* in the browser — ``POST /api/setup`` stores them in ``runtime_config`` and the
  bot picks them up on the next request, no redeploy, or
* in a terminal — ``python setup_cli.py``, which writes to the same store and can
  be skipped (``--skip``) with the web UI printed instead.

These tests pin both paths, the validation they share, the lock-down of the
public form, and the fact that neither one ever echoes a secret back.
"""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api.app as entrypoint                       # noqa: E402
import runtime_config                              # noqa: E402
import setup_cli                                   # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
def call(path="/api/setup", method="GET", params=None, headers=None, body=b""):
    """Drive the ASGI app once; returns ``(status, headers, body)``."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "https", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": urlencode(params or {}).encode(), "root_path": "",
        "client": ("test", 1234), "server": ("test", 443),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(entrypoint.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, payload


def post_form(values: dict, **kwargs):
    body = urlencode(values).encode()
    return call(method="POST", body=body,
                headers={"content-type": "application/x-www-form-urlencoded",
                         "accept": "application/json", **(kwargs.pop("headers", None) or {})},
                **kwargs)


def post_json(values: dict, **kwargs):
    return call(method="POST", body=json.dumps(values).encode(),
                headers={"content-type": "application/json", **(kwargs.pop("headers", None) or {})},
                **kwargs)


# ── fixtures ───────────────────────────────────────────────────────────────
# A save writes straight into os.environ (that is what makes it apply at once),
# so every key the setup can touch is put back exactly as it was found.
TOUCHED = tuple(runtime_config.SUPPORTED) + ("KV_REST_API_URL", "KV_REST_API_TOKEN",
                                             "UPSTASH_REDIS_REST_URL",
                                             "UPSTASH_REDIS_REST_TOKEN", "REDIS_REST_URL",
                                             "REDIS_REST_TOKEN", "SETUP_SECRET")


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """A private settings file, an empty cache, and the environment back as it was."""
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime_config.json"))
    monkeypatch.delenv("P2P_CONFIG_KEY", raising=False)
    for name in ("KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
                 "UPSTASH_REDIS_REST_TOKEN", "REDIS_REST_URL", "REDIS_REST_TOKEN",
                 "VERCEL", "VERCEL_ENV", "VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL",
                 "SETUP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    before = {name: os.environ.get(name) for name in TOUCHED}
    runtime_config.invalidate()
    runtime_config._injected.clear()
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    runtime_config.invalidate()
    runtime_config._injected.clear()
    # A save reloads bot.py so the new settings take effect at once (that is the
    # point of "reconfigure").  Reload it once more with the environment back to
    # normal, so the rest of the suite sees the module it expects.
    module = sys.modules.get("bot")
    if module is not None:
        importlib.reload(module)


@pytest.fixture(autouse=True)
def token_accepted(monkeypatch):
    """Telegram answers ``getMe`` — the token check must not need the network."""
    class Reply:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"username": "p2p_test_bot"}}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Reply())


@pytest.fixture()
def unconfigured(monkeypatch):
    """A deployment that has nothing yet — the state the setup page is for."""
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN", "ADMIN_IDS", "ADMINS"):
        monkeypatch.delenv(name, raising=False)
    # A config.json left behind by an earlier test would let the webhook endpoint
    # skip the setup page; this deployment has nothing, so it must not.
    import api.webhook as webhook_endpoint
    monkeypatch.setattr(webhook_endpoint, "_local_config_has_credentials", lambda: False)
    runtime_config.invalidate()
    runtime_config._injected.clear()
    import serverless
    return serverless


# ── runtime_config: the store both paths share ─────────────────────────────
def test_values_survive_a_round_trip_and_can_be_cleared(tmp_path):
    assert runtime_config.load() == {}

    runtime_config.save({"BOT_TOKEN": "123456:ABC", "ADMIN_IDS": "424242", "nope": "x"})
    assert runtime_config.load() == {"BOT_TOKEN": "123456:ABC", "ADMIN_IDS": "424242"}, \
        "only supported settings may be stored"

    runtime_config.save({"ASSET": "USDC"})                       # merges, does not replace
    assert runtime_config.load()["BOT_TOKEN"] == "123456:ABC"
    assert runtime_config.load()["ASSET"] == "USDC"

    assert runtime_config.clear() is True
    assert runtime_config.load() == {}


def test_the_settings_file_is_private_and_named(tmp_path):
    runtime_config.save({"BOT_TOKEN": "123456:ABC"})
    path = tmp_path / "runtime_config.json"
    assert path.is_file() and oct(path.stat().st_mode)[-3:] == "600"


def test_apply_fills_gaps_but_never_overrides_the_environment(monkeypatch):
    runtime_config.save({"BOT_TOKEN": "111111:STORED", "ADMIN_IDS": "123"})
    monkeypatch.setenv("ADMIN_IDS", "999")            # the deployment environment wins
    monkeypatch.delenv("BOT_TOKEN", raising=False)

    applied = runtime_config.apply()

    assert applied == {"BOT_TOKEN": "111111:STORED"}
    assert "BOT_TOKEN" in runtime_config.injected() and "ADMIN_IDS" not in runtime_config.injected()
    assert runtime_config.mask("111111:STORED").startswith("111")
    assert "STORED" not in runtime_config.mask("111111:STORED")


def test_validate_rejects_what_would_break_the_bot():
    cleaned, errors = runtime_config.validate({"BOT_TOKEN": "not-a-token"})
    assert not cleaned and "Telegram token" in errors[0]

    cleaned, errors = runtime_config.validate({"ADMIN_IDS": "me,you"})
    assert not cleaned and "numeric" in errors[0]

    cleaned, errors = runtime_config.validate({"INTERVAL": "3"})
    assert not cleaned and "INTERVAL" in errors[0]

    cleaned, errors = runtime_config.validate(
        {"BOT_TOKEN": "123456:ABC", "ADMIN_IDS": " 424242, 777 ", "asset": "usdt",
         "INTERVAL": "30"})
    assert errors == []
    assert cleaned == {"BOT_TOKEN": "123456:ABC", "ADMIN_IDS": "424242,777",
                       "ASSET": "usdt".upper(), "INTERVAL": "30"}


def test_an_empty_form_is_ignored_not_rejected():
    cleaned, errors = runtime_config.validate({"BOT_TOKEN": "", "ADMIN_IDS": "  "})
    assert cleaned == {} and errors == []


def test_a_rejected_token_is_reported(monkeypatch):
    class Reply:
        status_code = 401

        def json(self):
            return {"ok": False, "description": "Unauthorized"}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Reply())
    ok, detail = runtime_config.verify_bot_token("123456:ABC")
    assert ok is False and "Unauthorized" in detail


def test_an_unreachable_telegram_does_not_block_the_setup(monkeypatch):
    import httpx

    def explode(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(httpx, "get", explode)
    ok, detail = runtime_config.verify_bot_token("123456:ABC")
    assert ok is None and "without being verified" in detail


def test_the_summary_is_secret_free():
    runtime_config.save({"BOT_TOKEN": "123456:SUPERSECRETVALUE", "ADMIN_IDS": "424242"})
    summary = runtime_config.summary()
    assert summary["saved"] == ["ADMIN_IDS", "BOT_TOKEN"]
    assert "SUPERSECRETVALUE" not in json.dumps(summary)
    assert summary["masked"]["BOT_TOKEN"].startswith("123")


# ── GET /api/setup — the page and its form ─────────────────────────────────
def test_the_setup_page_offers_the_form(unconfigured):
    status, headers, payload = call()
    text = payload.decode()
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert 'method="post" action="/api/setup"' in text
    assert 'name="BOT_TOKEN"' in text and 'name="ADMIN_IDS"' in text
    assert 'name="KV_REST_API_URL"' in text            # no store connected → it is asked for
    assert "python setup_cli.py" in text               # …and the terminal way is right there
    assert "SUPERSECRET" not in text


def test_setup_page_does_not_ask_for_database_credentials_when_vercel_kv_is_connected(
        unconfigured, monkeypatch):
    """A connected Vercel KV/Upstash integration supplies both credentials in env."""
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "private-test-token")

    class RedisReply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": None}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: RedisReply())

    status, _, payload = call()
    text = payload.decode()

    assert status == 200
    assert 'name="KV_REST_API_URL"' not in text
    assert 'name="KV_REST_API_TOKEN"' not in text
    assert "database is already connected" in text
    assert "no database" in text
    assert "How to insert the database" not in text
    assert "private-test-token" not in text


def test_the_first_run_page_on_the_webhook_endpoint_has_the_form_too(unconfigured):
    """``GET /api/webhook`` renders the same page while the bot cannot start."""
    status, headers, payload = call(path="/api/webhook", headers={"accept": "text/html"})
    text = payload.decode()
    assert status == 500 and 'action="/api/setup"' in text
    assert "Setup needed" in text and "Redeploy" in text and "KV_REST_API_URL" in text


def test_the_form_closes_once_the_deployment_is_ready():
    status, _, payload = call()
    text = payload.decode()
    assert status == 200
    assert 'name="BOT_TOKEN"' not in text and "Locked" in text
    assert "SETUP_SECRET" in text                      # …and says how to open it again


def test_the_setup_path_is_routed_by_the_entry_point(unconfigured):
    status, _, payload = call()
    assert status == 200 and b"setup" in payload.lower()


def test_other_methods_are_refused():
    status, _, payload = call(method="DELETE")
    assert status == 405 and b"POST the settings form" in payload


# ── POST /api/setup — adding the required things in the browser ────────────
def test_the_form_stores_what_is_missing(unconfigured, monkeypatch):
    status, _, payload = post_form({"BOT_TOKEN": "123456:SUPERSECRETVALUE",
                                    "ADMIN_IDS": "424242"})
    data = json.loads(payload)

    assert status == 200 and data["ok"] is True
    assert data["saved"] == ["BOT_TOKEN", "ADMIN_IDS"]
    assert data["store"]["saved"] == ["ADMIN_IDS", "BOT_TOKEN"]
    assert "Telegram accepted the token" in " ".join(data["notes"])
    assert "SUPERSECRETVALUE" not in payload.decode()   # nothing secret comes back
    assert data["next"] and "/api/webhook" in data["next"][0]

    # …and the very next request sees a configured deployment, without a redeploy
    assert runtime_config.load() == {"BOT_TOKEN": "123456:SUPERSECRETVALUE",
                                     "ADMIN_IDS": "424242"}
    assert unconfigured.setup_status()["ready"] is True
    assert unconfigured.setup_status()["missing"] == ""


def test_the_checklist_says_where_a_value_came_from(unconfigured):
    post_form({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242"})
    checks = {check["name"]: check for check in unconfigured.setup_status()["checks"]}
    assert checks["bot_token"]["source"] == "setup"
    assert "setup page" in checks["bot_token"]["detail"]


def test_a_stored_token_reaches_the_bot_configuration(unconfigured, monkeypatch):
    """``bot.env_or_cli`` reads the environment — which apply() has just filled."""
    post_form({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242"})

    import bot as bot_module
    assert bot_module.env_or_cli(["BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"], None) \
        == "123456:ABCdef"


def test_invalid_values_are_rejected_without_storing_anything(unconfigured):
    status, _, payload = post_form({"BOT_TOKEN": "nope", "ADMIN_IDS": "me"})
    data = json.loads(payload)
    assert status == 400 and data["ok"] is False
    assert data["saved"] == [] and len(data["errors"]) == 2
    assert runtime_config.load() == {}


def test_an_empty_submission_says_so(unconfigured):
    status, _, payload = post_form({})
    assert status == 400 and b"empty" in payload


def test_a_token_telegram_rejects_is_not_stored(unconfigured, monkeypatch):
    class Reply:
        status_code = 404

        def json(self):
            return {"ok": False, "description": "Not Found"}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Reply())
    status, _, payload = post_form({"BOT_TOKEN": "123456:WRONG"})
    assert status == 400 and b"Telegram rejected the token" in payload
    assert runtime_config.load() == {}


def test_a_json_body_works_too(unconfigured):
    status, _, payload = post_json({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242"})
    assert status == 200 and json.loads(payload)["ok"] is True


def test_the_kv_pair_is_checked_before_anything_is_written(unconfigured, monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("connection refused")

    import httpx
    monkeypatch.setattr(httpx, "post", refuse)
    status, _, payload = post_form({"BOT_TOKEN": "123456:ABCdef",
                                    "KV_REST_API_URL": "https://eu1-x.upstash.io",
                                    "KV_REST_API_TOKEN": "kv-token"})
    assert status == 400 and b"did not accept the connection" in payload
    assert runtime_config.load() == {}


def test_a_working_kv_pair_is_used_by_this_instance(unconfigured, monkeypatch):
    class Reply:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": None}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Reply())
    status, _, payload = post_form({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242",
                                    "KV_REST_API_URL": "https://eu1-x.upstash.io/",
                                    "KV_REST_API_TOKEN": "kv-token"})
    data = json.loads(payload)
    assert status == 200 and data["ok"] is True
    assert "KV_REST_API_URL" not in data["saved"], "the KV pair is not stored, only used"
    assert any("cold start" in note for note in data["notes"])


def test_the_form_is_locked_once_the_deployment_is_ready():
    status, _, payload = post_form({"ADMIN_IDS": "1"})
    assert status == 403 and b"locked" in payload
    assert runtime_config.load() == {}


def test_setup_secret_locks_the_form_from_the_start(unconfigured, monkeypatch):
    monkeypatch.setenv("SETUP_SECRET", "s3cret")

    status, _, payload = post_form({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242"})
    assert status == 403 and b"SETUP_SECRET" in payload
    assert runtime_config.load() == {}

    status, _, payload = post_form({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242",
                                    "SETUP_SECRET": "s3cret"})
    assert status == 200 and json.loads(payload)["ok"] is True

    status, _, payload = call(params={"secret": "s3cret"})     # ?secret= works as well
    assert 'name="SETUP_SECRET"' in payload.decode()


def test_the_browser_answer_renders_for_a_person(unconfigured):
    status, headers, payload = call(
        method="POST",
        body=urlencode({"BOT_TOKEN": "123456:SUPERSECRETVALUE",
                        "ADMIN_IDS": "424242"}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded", "accept": "text/html"})
    text = payload.decode()
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert "Saved BOT_TOKEN, ADMIN_IDS" in text and "SUPERSECRETVALUE" not in text
    assert 'class="banner ok"' in text


def test_a_rejected_form_keeps_what_was_typed(unconfigured):
    status, _, payload = call(
        method="POST", body=urlencode({"BOT_TOKEN": "nope", "ADMIN_IDS": "424242"}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded", "accept": "text/html"})
    text = payload.decode()
    assert status == 400 and 'class="banner error"' in text
    assert 'value="424242"' in text and 'value="nope"' not in text


# ── python setup_cli.py — the terminal half ────────────────────────────────
def test_the_wizard_can_be_skipped_and_points_at_the_web_ui(capsys, tmp_path):
    assert setup_cli.main(["--skip"]) == 0
    out = capsys.readouterr().out
    assert "/api/setup" in out and "python setup_cli.py --show" in out
    assert runtime_config.load() == {}, "--skip must not store anything"


def test_the_wizard_shows_what_is_stored(capsys, monkeypatch, tmp_path):
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN", "ADMIN_IDS", "ADMINS"):
        monkeypatch.delenv(name, raising=False)
    runtime_config.save({"BOT_TOKEN": "123456:SUPERSECRET", "ADMIN_IDS": "424242"})
    assert setup_cli.main(["--show"]) == 0
    out = capsys.readouterr().out
    assert "runtime_config.json" in out
    assert "424242" in out and "stored" in out      # the owner's own ID, in their terminal
    assert "SUPERSECRET" not in out and "123" in out  # …but the token stays redacted


def test_the_wizard_stores_the_answers(monkeypatch, capsys):
    assert setup_cli.main(["--token", "123456:ABCdef", "--admins", "424242", "--yes"]) == 0
    assert runtime_config.load() == {"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242"}
    out = capsys.readouterr().out
    assert "Saved" in out and "ABCdef" not in out


def test_the_wizard_refuses_a_bad_token(monkeypatch, capsys):
    class Reply:
        status_code = 401

        def json(self):
            return {"ok": False, "description": "Unauthorized"}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Reply())
    assert setup_cli.main(["--token", "123456:WRONG", "--admins", "1", "--yes"]) == 1
    assert runtime_config.load() == {}
    assert "Unauthorized" in capsys.readouterr().out


def test_pressing_enter_skips_every_question(monkeypatch, capsys):
    """The whole wizard is optional — Enter everywhere means \"later, in the web UI\"."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert setup_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "Skipped" in out and "/api/setup" in out
    assert runtime_config.load() == {}


def test_the_wizard_asks_again_after_a_wrong_answer(monkeypatch, capsys):
    answers = iter(["y", "not-a-token", "123456:ABCdef", "424242",
                    "", "", "", "", ""])           # asset, fiat, interval, KV url, KV token
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert setup_cli.main([]) == 0
    assert runtime_config.load()["BOT_TOKEN"] == "123456:ABCdef"
    assert "Telegram token" in capsys.readouterr().out


def test_the_wizard_can_clear_the_store(capsys):
    runtime_config.save({"BOT_TOKEN": "123456:ABCdef"})
    assert setup_cli.main(["--clear"]) == 0
    assert runtime_config.load() == {}
    assert "forgotten" in capsys.readouterr().out


def test_the_wizard_can_push_to_the_vercel_environment(monkeypatch, capsys):
    calls: list[tuple] = []

    class Done:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("input")))
        return Done()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert setup_cli.main(["--token", "123456:ABCdef", "--admins", "424242",
                           "--yes", "--vercel-env"]) == 0
    # ``vercel env ls`` discovers what is already there first (``env add``
    # refuses duplicates), then the two missing values are added.
    assert [call[0][:3] for call in calls] == [["vercel", "env", "ls"],
                                               ["vercel", "env", "add"],
                                               ["vercel", "env", "add"]]
    assert b"123456:ABCdef" in [call[1] for call in calls]
    assert "Vercel" in capsys.readouterr().out


def test_the_wizard_leaves_variables_alone_that_vercel_already_has(monkeypatch, capsys):
    """A KV pair created by an integration must not be turned into an error."""
    calls: list[tuple] = []

    class Done:
        returncode = 0
        stderr = b""

        def __init__(self, stdout=b""):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("input")))
        # the listing prints every variable the project already has
        return Done(b"BOT_TOKEN    123456:OLD    Production\n")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert setup_cli.main(["--token", "123456:ABCdef", "--admins", "424242",
                           "--yes", "--vercel-env"]) == 0

    added = [call[0][3] for call in calls if call[0][:3] == ["vercel", "env", "add"]]
    assert added == ["ADMIN_IDS"]                   # BOT_TOKEN exists → left alone
    assert b"424242" in [call[1] for call in calls]
    out = capsys.readouterr().out
    assert "already exists" in out and "BOT_TOKEN" in out


def test_a_missing_vercel_cli_is_reported_not_fatal(monkeypatch, capsys):
    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError("vercel")

    monkeypatch.setattr(subprocess, "run", missing)
    assert setup_cli.main(["--token", "123456:ABCdef", "--admins", "424242",
                           "--yes", "--vercel-env"]) == 0
    assert "not installed" in capsys.readouterr().out
    assert runtime_config.load()["BOT_TOKEN"] == "123456:ABCdef"


def test_the_web_ui_address_is_taken_from_the_platform(monkeypatch):
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "p2p-bot.vercel.app")
    assert setup_cli.web_ui_url() == "https://p2p-bot.vercel.app/api/setup"
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL")
    assert "<your-app>" in setup_cli.web_ui_url()


def test_state_database_choice_is_validated_and_stored():
    cleaned, errors = runtime_config.validate({"P2P_STATE_BACKEND": "KV"})
    assert errors == []
    assert cleaned == {"P2P_STATE_BACKEND": "redis"}

    cleaned, errors = runtime_config.validate({"P2P_STATE_BACKEND": "postgres"})
    assert cleaned == {}
    assert errors == ["P2P_STATE_BACKEND must be auto, file, or redis."]


def test_setup_page_exposes_state_database_selector(unconfigured):
    status, _, payload = call()
    text = payload.decode()
    assert 'name="P2P_STATE_BACKEND"' in text
    assert 'value="auto" selected' in text
    assert 'value="file"' in text and 'value="redis"' in text


def test_setup_page_explains_how_to_insert_the_database(unconfigured):
    """The page walks through getting the KV pair, not just naming the fields."""
    _, _, payload = call()
    text = payload.decode()
    assert "How to insert the database" in text
    assert "console.upstash.com" in text                    # where the database is created
    assert "REST API" in text                               # where the pair is shown
    assert "KV_REST_API_URL" in text and "KV_REST_API_TOKEN" in text
    assert "Storage" in text and "Connect to this project" in text   # the Vercel path too
    assert "Save settings" in text                          # and it ends at the form's button


def test_setup_page_links_to_the_database_connection_page(unconfigured, monkeypatch):
    """The panel that explains the KV pair starts with the way to get it."""
    monkeypatch.delenv("P2P_DATABASE_LINK", raising=False)
    _, _, payload = call()
    text = payload.decode()

    assert "Connect database" in text
    assert 'href="https://vercel.com/dashboard/stores"' in text
    assert "Connect to this project" in text               # what to press when it opens


def test_setup_page_honours_a_configured_database_link(unconfigured, monkeypatch):
    monkeypatch.setenv("P2P_DATABASE_LINK", "https://console.upstash.com/redis/1")
    _, _, payload = call()

    assert 'href="https://console.upstash.com/redis/1"' in payload.decode()
    assert "https://vercel.com/dashboard/stores" not in payload.decode()


def test_setup_page_hides_the_connect_button_once_a_database_is_connected(
        unconfigured, monkeypatch):
    """A connected store needs no call to action — the checklist says it is ready."""
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "private-test-token")
    _, _, payload = call()
    text = payload.decode()

    assert "Connect database" not in text
    assert "database is already connected" in text


def test_vercel_save_does_not_claim_ready_without_redis(unconfigured, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    status, _, payload = call(
        method="POST",
        body=urlencode({"BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242",
                        "P2P_STATE_BACKEND": "auto"}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded", "accept": "text/html"})
    text = payload.decode()
    assert status == 200
    assert "setup is not complete" in text
    assert "the bot can start now" not in text
    assert 'class="banner info"' in text
    assert "save the settings again" in text


def test_vercel_new_redis_connection_warns_about_environment(unconfigured, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": None}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Reply())
    status, _, payload = post_form({
        "BOT_TOKEN": "123456:ABCdef", "ADMIN_IDS": "424242",
        "KV_REST_API_URL": "https://example.upstash.io", "KV_REST_API_TOKEN": "kv-token"})
    data = json.loads(payload)
    assert status == 200
    assert any("applies only to this instance" in warning for warning in data["warnings"])
    assert not any("No KV/Redis is connected" in warning for warning in data["warnings"])


# ── a database that stopped answering ──────────────────────────────────────
# ``RedisStore.load()`` reports "nothing saved" both for a fresh install and for
# a database that went away.  The second one used to look like a healthy
# deployment with a locked form — nothing worked and nothing could be fixed.
@pytest.fixture(autouse=True)
def fresh_store_probe():
    """Every test sees the database as it is, not as the last one left it."""
    import serverless
    serverless.reset_store_probe()
    yield
    serverless.reset_store_probe()


def _kv_pair(monkeypatch, url="https://example.upstash.io", token="kv-token"):
    monkeypatch.setenv("KV_REST_API_URL", url)
    monkeypatch.setenv("KV_REST_API_TOKEN", token)


def _redis_answers(monkeypatch):
    class Reply:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": "PONG"}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Reply())


def _redis_dead(monkeypatch):
    import httpx

    def dead(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "post", dead)


def test_a_database_that_stopped_answering_is_reported_not_hidden(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    _kv_pair(monkeypatch)
    _redis_dead(monkeypatch)

    import serverless
    status = serverless.setup_status()

    assert status["ready"] is False, "the bot cannot work without its state"
    assert status["broken"] == ["state_store"]
    check = {item["name"]: item for item in status["checks"]}["state_store"]
    assert check["status"] == "broken" and "does not answer" in check["detail"]
    assert "database" in status["message"]


def test_a_dead_database_reopens_the_form_and_asks_for_credentials(monkeypatch):
    """The page that reports the outage is the page that can repair it."""
    monkeypatch.setenv("VERCEL", "1")
    _kv_pair(monkeypatch)
    _redis_dead(monkeypatch)

    status, _, payload = call()
    text = payload.decode()

    assert status == 200
    assert "Not responding" in text                     # its own badge, not "missing"
    assert 'name="KV_REST_API_URL"' in text             # credentials can be entered again
    assert 'name="KV_REST_API_TOKEN"' in text
    assert "How to insert the database" in text         # …and the guide is back
    assert "Locked" not in text


def test_a_dead_database_lets_the_form_save_a_repair(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    _kv_pair(monkeypatch)
    _redis_dead(monkeypatch)

    status, _, payload = post_form({"ADMIN_IDS": "424242"})
    assert status == 200 and json.loads(payload)["ok"] is True


def test_a_database_that_answers_keeps_the_deployment_ready(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    _kv_pair(monkeypatch)
    _redis_answers(monkeypatch)

    import serverless
    status = serverless.setup_status()

    assert status["ready"] is True and status["broken"] == []
    assert status["store_health"]["ok"] is True


# ── the 🔧 one-button reconfigure ─────────────────────────────────────────
class FakeApp:
    """The smallest stand-in for an initialized PTB application."""

    def __init__(self):
        self.bot = self
        self.messages: list[tuple] = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.messages.append((chat_id, text, kwargs.get("reply_markup")))


@pytest.fixture()
def telegram(monkeypatch):
    """A configured deployment whose bot can talk to Telegram — offline."""
    import serverless
    app = FakeApp()
    monkeypatch.setattr(serverless, "get_application", lambda *a, **k: _await(app))
    monkeypatch.setattr(serverless, "public_base_url", lambda: "https://p2p-bot.vercel.app")
    serverless._pending_setup_link.update(token="", expires=0.0, issued_at=0.0)
    yield app
    serverless._pending_setup_link.update(token="", expires=0.0, issued_at=0.0)


class _await:
    """``async`` return value for a patched ``get_application``."""

    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _get():
            return self.value
        return _get().__await__()


def test_the_locked_setup_page_offers_one_button_that_reconfigures():
    status, _, payload = call()
    text = payload.decode()

    assert status == 200
    assert "Reconfigure" in text
    assert 'name="action" value="send-link"' in text
    assert "locked" in text.lower()


def test_the_button_sends_a_one_time_link_to_the_admins(telegram):
    status, _, payload = post_form({"action": "send-link"})
    text = payload.decode()

    assert status == 200
    assert telegram.messages, "the admins were messaged"
    chat_id, body, markup = telegram.messages[0]
    assert chat_id == 424242
    assert "/api/setup?secret=" in (markup["inline_keyboard"][0][0]["url"])
    # The link is the key to the form: it is delivered, never shown to the clicker.
    token = markup["inline_keyboard"][0][0]["url"].split("secret=")[-1]
    assert token and token not in text
    assert "Sent to the bot admin" in text


def test_the_one_time_link_reopens_the_form(telegram):
    post_form({"action": "send-link"})
    token = telegram.messages[0][2]["inline_keyboard"][0][0]["url"].split("secret=")[-1]

    status, _, payload = call(params={"secret": token})
    text = payload.decode()

    assert status == 200
    assert 'name="BOT_TOKEN"' in text, "a configured deployment's form is open again"
    assert "Locked" not in text


def test_the_one_time_link_authorizes_a_save_and_is_then_spent(telegram):
    post_form({"action": "send-link"})
    token = telegram.messages[0][2]["inline_keyboard"][0][0]["url"].split("secret=")[-1]

    status, _, payload = post_form({"ADMIN_IDS": "424242,777", "SETUP_SECRET": token})
    assert status == 200 and json.loads(payload)["ok"] is True
    assert runtime_config.load()["ADMIN_IDS"] == "424242,777"

    # …and the link in the admin's chat history cannot change the bot again
    status, _, payload = post_form({"ADMIN_IDS": "1", "SETUP_SECRET": token})
    assert status == 403 and b"locked" in payload
    assert runtime_config.load()["ADMIN_IDS"] == "424242,777"


def test_an_unknown_secret_still_does_not_open_a_running_bot(telegram):
    status, _, payload = post_form({"ADMIN_IDS": "1", "SETUP_SECRET": "not-the-link"})
    assert status == 403 and b"locked" in payload
    assert runtime_config.load() == {}


def test_a_link_is_refused_once_it_has_expired(telegram):
    post_form({"action": "send-link"})
    token = telegram.messages[0][2]["inline_keyboard"][0][0]["url"].split("secret=")[-1]

    import serverless
    serverless._pending_setup_link["expires"] = 1.0
    bot_module = sys.modules.get("bot")
    if bot_module is not None and "setup_link" in bot_module.state:
        bot_module.state["setup_link"]["expires"] = 1.0

    status, _, payload = post_form({"ADMIN_IDS": "1", "SETUP_SECRET": token})
    assert status == 403 and b"locked" in payload
    assert runtime_config.load() == {}


def test_the_button_reports_when_telegram_cannot_be_reached(monkeypatch):
    """A revoked token is the one case the link cannot cover — say so plainly."""
    import serverless

    class Silent(FakeApp):
        async def send_message(self, chat_id=None, text=None, **kwargs):
            raise RuntimeError("Forbidden: bot was blocked by the user")

    monkeypatch.setattr(serverless, "get_application", lambda *a, **k: _await(Silent()))
    monkeypatch.setattr(serverless, "public_base_url", lambda: "https://p2p-bot.vercel.app")

    status, _, payload = post_form({"action": "send-link"})
    text = payload.decode()

    assert status == 502
    assert "could not reach Telegram" in text
    assert "python setup_cli.py" in text


def test_the_status_page_links_to_the_setup_page():
    status, _, payload = call(path="/api/webhook", params={"register": "0"},
                              headers={"accept": "text/html"})
    assert status == 200
    assert 'href="/api/setup"' in payload.decode()


def test_a_saved_setting_is_applied_to_the_running_instance(unconfigured, monkeypatch):
    """Reconfiguring must not wait for the next cold start.

    ``bot.TOKEN`` / ``bot.ADMINS`` are read once, at import time, so a warm
    container would keep serving the old values forever without the reload.
    """
    import bot as bot_module
    original = bot_module.TOKEN

    status, _, payload = post_form({"BOT_TOKEN": "123456:BRANDNEW", "ADMIN_IDS": "424242"})
    data = json.loads(payload)

    assert status == 200 and data["ok"] is True
    assert bot_module.TOKEN == "123456:BRANDNEW"      # the module itself moved on
    assert any("no redeploy" in note for note in data["notes"])
    assert original != "123456:BRANDNEW"


def test_pressing_the_button_twice_does_not_spam_the_admins(telegram):
    """The button is public — it must not become a way to message the admin in a loop."""
    status, _, payload = post_form({"action": "send-link"})
    assert status == 200 and len(telegram.messages) == 1

    status, _, payload = post_form({"action": "send-link"})
    text = payload.decode()

    assert status == 200 and len(telegram.messages) == 1, "no second message was sent"
    assert "already have a link" in text


@pytest.mark.parametrize("ready,serverless,fail", [
    (True, True, False), (True, True, True),
    (False, True, False), (True, False, False),
])
def test_setup_activates_delivery_without_another_visit(monkeypatch, ready, serverless, fail):
    from unittest.mock import AsyncMock
    from api import setup

    result = {"ok": True, "saved": ["BOT_TOKEN"], "warnings": [],
              "status": {"ready": ready, "serverless": serverless, "missing": ""}}
    register = AsyncMock(return_value={"url": "https://example.com/api/webhook"})
    if fail:
        register.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(setup, "setup_write_allowed", lambda request: (True, ""))
    monkeypatch.setattr(setup, "save_setup_values", lambda values: result)
    monkeypatch.setattr(setup, "consume_setup_link", lambda secret: None)
    monkeypatch.setattr(setup, "ensure_webhook", register)
    status, _, payload = post_json({"BOT_TOKEN": "123:TEST"})
    data = json.loads(payload)
    assert status == 200 and data["ok"]  # a delivery failure must not lose settings
    assert register.await_count == int(ready and serverless)
    if fail:
        assert "could not be activated" in data["warnings"][0]
        assert "webhook" not in data
    elif ready and serverless:
        assert data["webhook"]["url"].endswith("/api/webhook")
        assert "Telegram connected" in setup._headline(data)
