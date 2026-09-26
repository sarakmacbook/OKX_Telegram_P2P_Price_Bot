"""Serverless (Vercel) mode: the ``api/*`` entry points and everything behind them.

Vercel imports every ``api/*.py`` and loads the top-level ``app``: it is ASGI
when the callable is ``async def app(scope, receive, send)`` (exactly three
required positional parameters — that is how ``detect_app_type`` in the Vercel
Python runtime decides).  These tests pin that contract, the secret checks, the
status page, the cron endpoint and the state handling that serverless needs —
all of it offline.
"""

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "api"


# ── helpers ────────────────────────────────────────────────────────────────
def load_api(name: str):
    """Import ``api/<name>.py`` the way Vercel's runtime does — by file path."""
    path = API / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"api_{name}", path)
    assert spec and spec.loader, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def asgi_call(app, method="GET", path="/api/webhook", headers=None, params=None,
              body: bytes = b""):
    """Drive one ASGI request; returns (status, headers, body, messages)."""
    sent = []
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "query_string": urlencode(params or {}).encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = sent[0]["status"]
    response_headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, response_headers, payload, sent


def asgi_json(app, **kwargs):
    status, headers, payload, _ = asgi_call(app, **kwargs)
    return status, json.loads(payload or b"{}")


def required_positional_params(func) -> int:
    return sum(1 for p in inspect.signature(func).parameters.values()
               if p.default is inspect.Parameter.empty
               and p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD))


class Recorder:
    """An async stand-in that remembers how it was called."""

    def __init__(self, result=None):
        self.calls, self.result = [], result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeUpdate:
    """Just enough of a PTB Update for the in-flight-edit helpers."""

    def __init__(self, user_id):
        self.effective_user = FakeUser(user_id)


# ── fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def clean_shared_state():
    """Leave the bot's state and the container cache as they were found."""
    import bot as bot_module
    import serverless

    saved = dict(bot_module.state)
    saved_edits = dict((bot_module.state.get("edits") or {}))
    saved_apps = dict(serverless._apps)
    yield
    bot_module.state.clear()
    bot_module.state.update(saved)
    bot_module.state["edits"] = saved_edits
    serverless._apps.clear()
    serverless._apps.update(saved_apps)
    serverless._locks.clear()


@pytest.fixture()
def serverless():
    sys.path.insert(0, str(ROOT))
    import serverless as module
    return module


@pytest.fixture()
def webhook():
    return load_api("webhook")


@pytest.fixture()
def tick():
    return load_api("tick")


# ── the entry points Vercel loads ──────────────────────────────────────────
@pytest.mark.parametrize("name", ["webhook", "tick"])
def test_api_exposes_an_asgi_app(name):
    """`async def app(scope, receive, send)` — detected as ASGI by the runtime."""
    module = load_api(name)
    assert inspect.iscoroutinefunction(module.app), f"api/{name}.py: app must be async"
    assert required_positional_params(module.app) == 3, \
        f"api/{name}.py: app must take (scope, receive, send) with no defaults"
    assert callable(module.handle), f"api/{name}.py: missing the request handler"


@pytest.mark.parametrize("name", ["webhook", "tick"])
def test_api_imports_the_repo_root_modules(name):
    """The functions import bot.py/exchanges.py etc. from the project root."""
    load_api(name)
    assert str(ROOT) in sys.path


def test_lifespan_startup_and_shutdown_are_answered(serverless, monkeypatch):
    """Vercel speaks ASGI lifespan — answer it (and warm the bot up)."""
    started, stopped = Recorder(), Recorder()
    monkeypatch.setattr(serverless, "get_application", started)
    monkeypatch.setattr(serverless, "shutdown", stopped)
    replies: list[dict] = []
    playing = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return playing.pop(0)

    async def send(message):
        replies.append(message)

    asyncio.run(serverless._lifespan(receive, send))

    assert started.calls, "startup must initialize the application"
    assert stopped.calls, "shutdown must close it again"
    assert [reply["type"] for reply in replies] == ["lifespan.startup.complete",
                                                    "lifespan.shutdown.complete"]


# ── POST /api/webhook — Telegram updates ───────────────────────────────────
def test_webhook_accepts_a_telegram_update(serverless, webhook, monkeypatch):
    processed = Recorder(result=True)
    monkeypatch.setattr(webhook, "process_update", processed)
    payload = {"update_id": 1, "message": {"message_id": 2}}

    status, data = asgi_json(
        webhook.app, method="POST", body=json.dumps(payload).encode(),
        headers={serverless.TELEGRAM_SECRET_HEADER: serverless.webhook_secret()})

    assert status == 200 and data["ok"] is True
    assert processed.calls and processed.calls[0][0][0] == payload


def test_webhook_rejects_a_missing_or_wrong_secret(serverless, webhook, monkeypatch):
    processed = Recorder()
    monkeypatch.setattr(webhook, "process_update", processed)
    body = json.dumps({"update_id": 1}).encode()

    for headers in ({}, {serverless.TELEGRAM_SECRET_HEADER: "not-the-secret"}):
        status, data = asgi_json(webhook.app, method="POST", body=body, headers=headers)
        assert status == 403 and data["ok"] is False

    assert not processed.calls, "an unauthenticated body must never reach the handlers"


def test_webhook_rejects_a_body_that_is_not_an_update(serverless, webhook, monkeypatch):
    processed = Recorder()
    monkeypatch.setattr(webhook, "process_update", processed)
    status, data = asgi_json(
        webhook.app, method="POST", body=b'{"hello": "world"}',
        headers={serverless.TELEGRAM_SECRET_HEADER: serverless.webhook_secret()})
    assert status == 400 and not processed.calls


def test_webhook_dispatches_through_the_real_handlers(serverless, webhook, monkeypatch):
    """End to end: a real PTB application handles the update (no network involved).

    The message comes from a user who is not an admin, so the bot's own
    ``on_text`` returns immediately — which is exactly the point: the update went
    through ``Application.process_update`` and every registered handler.
    """
    import bot as bot_module
    from telegram import Bot, Update, User

    async def fake_get_me(self, **kwargs):                # no network in the test
        return User(id=123456, first_name="Test Bot", is_bot=True, username="p2p_test_bot")

    monkeypatch.setattr(Bot, "get_me", fake_get_me)

    async def scenario():
        app = bot_module.build_application(polling=False)
        await app.initialize()                            # verifies the token via getMe
        serverless._apps[id(asyncio.get_running_loop())] = app
        try:
            update = Update.de_json({"update_id": 7, "message": {
                "message_id": 1, "date": 0, "text": "hello",
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 999999, "is_bot": False, "first_name": "Nobody"}}}, app.bot)
            sent = []
            scope = {"type": "http", "method": "POST", "path": "/api/webhook",
                     "query_string": b"", "headers": [
                         (serverless.TELEGRAM_SECRET_HEADER.encode(),
                          serverless.webhook_secret().encode())]}
            body = json.dumps(update.to_dict()).encode()
            cursor = {"done": False}

            async def receive():
                if cursor["done"]:
                    return {"type": "http.request", "body": b"", "more_body": False}
                cursor["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                sent.append(message)

            await webhook.app(scope, receive, send)
            return sent
        finally:
            await serverless.shutdown()

    sent = asyncio.run(scenario())
    assert sent[0]["status"] == 200
    assert json.loads(sent[-1]["body"])["ok"] is True


# ── GET /api/webhook — status page & webhook registration ──────────────────
def test_status_page_registers_the_webhook(serverless, webhook, monkeypatch):
    registered = Recorder(result={"url": "https://bot.vercel.app/api/webhook",
                                  "pending_updates": 0, "last_error": None})
    monkeypatch.setattr(webhook, "ensure_webhook", registered)

    status, data = asgi_json(webhook.app, method="GET",
                             headers={"Accept": "application/json"})

    assert status == 200 and data["ok"] is True
    assert registered.calls, "opening the status page must (re)register the webhook"
    assert data["webhook"]["url"].endswith("/api/webhook")
    assert data["mode"].startswith("webhook")


def test_status_page_renders_html_for_browsers(serverless, webhook, monkeypatch):
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "https://bot.vercel.app/api/webhook",
                                         "pending_updates": 0}))
    status, headers, payload, _ = asgi_call(webhook.app, method="GET",
                                            headers={"Accept": "text/html,text/*"})
    text = payload.decode()
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert "P2P Price Bot" in text and "State store" in text


def test_status_page_can_skip_registration(serverless, webhook, monkeypatch):
    registered = Recorder(result={})
    monkeypatch.setattr(webhook, "ensure_webhook", registered)
    status, data = asgi_json(webhook.app, method="GET", params={"register": "0"})
    assert status == 200 and not registered.calls
    assert data["webhook"] == "not checked (?register=0)"


def test_status_page_explains_a_broken_configuration(serverless, webhook, monkeypatch):
    def broken():
        raise serverless.ConfigError("BOT_TOKEN is missing")

    monkeypatch.setattr(webhook, "get_bot", broken)
    status, data = asgi_json(webhook.app, method="GET")
    assert status == 500 and "BOT_TOKEN" in json.dumps(data)
    assert "hint" in data


# ── GET /api/tick — the cron endpoint ─────────────────────────────────────
def test_tick_runs_the_periodic_work(serverless, tick, monkeypatch):
    ran = Recorder(result={"posted": True, "deleted_stale_message": False, "webhook": {}})
    monkeypatch.setattr(tick, "run_tick", ran)
    monkeypatch.setenv("CRON_SECRET", "s3cret")

    status, data = asgi_json(tick.app, method="GET",
                             headers={"Authorization": "Bearer s3cret"})

    assert status == 200 and data["ok"] is True and data["posted"] is True
    assert ran.calls


def test_tick_is_locked_down_by_cron_secret(serverless, tick, monkeypatch):
    ran = Recorder(result={})
    monkeypatch.setattr(tick, "run_tick", ran)
    monkeypatch.setenv("CRON_SECRET", "s3cret")

    for params, headers in ((None, None),
                            (None, {"Authorization": "Bearer wrong"}),
                            ({"secret": "wrong"}, None)):
        status, data = asgi_json(tick.app, method="GET", params=params, headers=headers)
        assert status == 401 and data["ok"] is False
    assert not ran.calls

    # …while `?secret=` and the Vercel cron header both work
    for params, headers in (({"secret": "s3cret"}, None),
                            (None, {"Authorization": "Bearer s3cret"})):
        status, data = asgi_json(tick.app, method="GET", params=params, headers=headers)
        assert status == 200 and data["ok"] is True


def test_tick_warns_when_it_is_open(serverless, tick, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(tick, "run_tick", Recorder(result={}))
    status, data = asgi_json(tick.app, method="GET")
    assert status == 200 and "CRON_SECRET" in data["warning"]


def test_tick_rejects_other_methods(serverless, tick, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    status, data = asgi_json(tick.app, method="DELETE")
    assert status == 405 and data["ok"] is False


def test_tick_endpoint_runs_the_real_cron_path(serverless, tick, monkeypatch):
    """ASGI → run_tick → webhook registration → the two jobs the JobQueue ran.

    Only the Telegram calls and the exchange fetches are stubbed out: the
    registration, the state marker and the response are the real code.
    """
    from types import SimpleNamespace

    import bot as bot_module
    from telegram import Bot, User

    calls: dict = {}

    async def fake_get_me(self, **kwargs):
        return User(id=123456, first_name="Test Bot", is_bot=True, username="p2p_test_bot")

    async def fake_set_webhook(self, url, **kwargs):
        calls["set_webhook"] = (url, kwargs)

    async def fake_get_webhook_info(self, **kwargs):
        return SimpleNamespace(url=calls.get("set_webhook", ("",))[0],
                               pending_update_count=0, last_error_message=None)

    async def fake_auto_post(bot, *args, **kwargs):
        calls["posted"] = True
        return True

    async def fake_cleanup(bot, *args, **kwargs):
        calls["cleaned"] = True
        return False

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    monkeypatch.setattr(Bot, "set_webhook", fake_set_webhook)
    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)
    monkeypatch.setattr(bot_module, "auto_post_task", fake_auto_post)
    monkeypatch.setattr(bot_module, "cleanup_task", fake_cleanup)
    monkeypatch.setenv("PUBLIC_URL", "https://p2p-test.vercel.app")
    monkeypatch.delenv("CRON_SECRET", raising=False)

    status, data = asgi_json(tick.app, method="GET")

    url, kwargs = calls["set_webhook"]
    assert status == 200 and data["ok"] is True
    assert url == "https://p2p-test.vercel.app/api/webhook"
    assert kwargs["secret_token"] == serverless.webhook_secret()
    assert kwargs["drop_pending_updates"] is True
    assert "getUpdates" not in kwargs.get("allowed_updates", [])   # webhook mode, no polling
    assert data["posted"] is True and data["deleted_stale_message"] is False
    assert data["webhook"]["url"] == url
    assert bot_module.state["webhook"]["url"] == url


# ── public URL & secrets ──────────────────────────────────────────────────
def test_public_url_uses_only_platform_values(serverless, monkeypatch):
    """A forged Host header must never be able to move the webhook elsewhere."""
    for var in ("PUBLIC_URL", "P2P_PUBLIC_URL", "WEBHOOK_URL",
                "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("VERCEL_URL", "my-bot-abc123.vercel.app")
    assert serverless.public_base_url() == "https://my-bot-abc123.vercel.app"

    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "my-bot.vercel.app")
    assert serverless.public_base_url() == "https://my-bot.vercel.app"

    monkeypatch.setenv("PUBLIC_URL", "https://prices.example.com/")
    assert serverless.public_base_url() == "https://prices.example.com"

    for var in ("PUBLIC_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(serverless.ConfigError):
        serverless.public_base_url()


def test_webhook_secret_defaults_to_a_token_hash(serverless, monkeypatch):
    import bot as bot_module

    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    secret = serverless.webhook_secret()
    assert secret == serverless.webhook_secret()          # stable across instances
    assert bot_module.TOKEN not in secret and len(secret) >= 16

    monkeypatch.setenv("WEBHOOK_SECRET", "my-own-secret")
    assert serverless.webhook_secret() == "my-own-secret"
    assert "WEBHOOK_SECRET" in serverless.webhook_secret_note()


def test_matches_is_exact(serverless):
    assert serverless.matches("abc", "abc")
    assert not serverless.matches("abc", "abcd")
    assert not serverless.matches("", "")
    assert not serverless.matches("abc", "")


# ── state handling for serverless ─────────────────────────────────────────
def test_serverless_application_has_no_updater_and_no_job_queue():
    import warnings

    import bot as bot_module

    app = bot_module.build_application(polling=False)
    assert app.updater is None, "serverless receives updates over HTTP, not by polling"
    with warnings.catch_warnings():           # PTB warns when job_queue is read while unset
        warnings.simplefilter("ignore")
        assert app.job_queue is None, "the cron endpoint replaces the in-process JobQueue"


def test_one_application_per_warm_container(serverless, monkeypatch):
    """Requests reuse the initialized Application — a new loop rebuilds it.

    Vercel keeps a warm container (and its event loop) between invocations, so
    the token check / ``getMe`` must happen once per container, not per request —
    and a request that arrives on a fresh loop must not fail with
    "attached to a different loop".
    """
    from telegram import Bot, User

    get_me_calls = {"n": 0}

    async def fake_get_me(self, **kwargs):
        get_me_calls["n"] += 1
        return User(id=123456, first_name="Test Bot", is_bot=True, username="p2p_test_bot")

    monkeypatch.setattr(Bot, "get_me", fake_get_me)

    loops: list = []

    async def scenario():
        await serverless.get_application()
        await serverless.get_application()
        loops.append(asyncio.get_running_loop())

    asyncio.run(scenario())            # first container
    asyncio.run(scenario())            # …recycled: a brand new event loop
    assert len(loops) == 2 and loops[0] is not loops[1]
    assert get_me_calls["n"] == 2, "one initialization per container, one request"
    assert list(serverless._apps) == [id(loops[1])], "state of the dead loop must be dropped"


def test_refresh_state_reloads_what_other_instances_wrote():
    import bot as bot_module

    bot_module.state["group"] = -100123
    bot_module.save()
    bot_module.state["group"] = None                      # stale in-memory copy
    bot_module.refresh_state()
    assert bot_module.state["group"] == -100123


def test_in_flight_edits_are_shared_not_per_process():
    """The "tap Edit, send the text" pair may land in different instances."""
    import bot as bot_module

    update = FakeUpdate(424242)
    bot_module.edit_set(update, "awaiting_custom", "header")

    assert bot_module.STORE.load()["edits"]["424242"]["awaiting_custom"] == "header"
    bot_module.state["edits"] = {}                        # a cold instance
    bot_module.refresh_state()
    assert bot_module.edit_get(update, "awaiting_custom") == "header"

    bot_module.edit_pop(update, "awaiting_custom")
    assert bot_module.edit_get(update, "awaiting_custom") is None
    assert "awaiting_custom" not in bot_module.STORE.load()["edits"]["424242"]


def test_state_status_flags_a_non_persistent_store(serverless):
    status = serverless.state_status()
    assert status["backend"] in ("file", "redis", "none")
    if status["backend"] != "redis":
        assert "warning" in status and "KV_REST_API_URL" in status["warning"]


# ── the deployment must explain itself (browser errors & diagnostics) ──────
def test_broken_configuration_renders_a_setup_guide_for_browsers(serverless, webhook, monkeypatch):
    def broken():
        raise serverless.ConfigError("BOT_TOKEN is missing")

    monkeypatch.setattr(webhook, "get_bot", broken)
    status, headers, payload, _ = asgi_call(webhook.app, method="GET",
                                            headers={"Accept": "text/html"})
    text = payload.decode()
    assert status == 500 and headers["content-type"].startswith("text/html")
    assert "Setup needed" in text and "BOT_TOKEN" in text
    assert "Redeploy" in text and "KV_REST_API_URL" in text


def test_broken_configuration_stays_json_for_scripts(serverless, webhook, monkeypatch):
    def broken():
        raise serverless.ConfigError("BOT_TOKEN is missing")

    monkeypatch.setattr(webhook, "get_bot", broken)
    status, headers, payload, _ = asgi_call(webhook.app, method="GET")
    assert status == 500 and headers["content-type"].startswith("application/json")
    assert "hint" in json.loads(payload)


def test_tick_crash_is_readable_in_browsers_too(serverless, tick, monkeypatch):
    def broken():
        raise serverless.ConfigError("Cannot tell where this deployment is reachable")

    monkeypatch.setattr(tick, "run_tick", broken)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    status, headers, payload, _ = asgi_call(tick.app, method="GET",
                                            headers={"Accept": "text/html"})
    assert status == 500 and headers["content-type"].startswith("text/html")
    assert "Setup needed" in payload.decode()


def test_warnings_come_before_the_table(serverless, webhook, monkeypatch):
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "https://bot.vercel.app/api/webhook",
                                         "pending_updates": 0}))
    status, headers, payload, _ = asgi_call(webhook.app, method="GET",
                                            headers={"Accept": "text/html"})
    text = payload.decode()
    assert "NOT persistent" in text                      # the test store is a file, not KV
    assert text.index('class="warn"') < text.index("<table>")


def test_status_reports_the_vercel_region(serverless, webhook, monkeypatch):
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "", "pending_updates": 0}))
    monkeypatch.setenv("VERCEL_REGION", "fra1")
    status, data = asgi_json(webhook.app, method="GET", params={"register": "0"})
    assert status == 200 and data["region"] == "fra1"


def _seed_merchant(bot_module):
    """One merchant in memory AND in the store (the status page refreshes first)."""
    from dataclasses import asdict

    from exchanges import Merchant
    m = Merchant("okx", "0dec824eed", "Fast_sonic", "USDT", "USD",
                 "https://www.okx.com/p2p-markets/usd/buy-usdt?publicUserId=0dec824eed")
    old = dict(bot_module.state.get("merchants") or {})
    bot_module.state["merchants"] = {m.key: asdict(m)}
    bot_module.save()
    return m, old


def _unseed_merchant(bot_module, old):
    bot_module.state["merchants"] = old
    bot_module.save()


def test_price_check_fetches_every_merchant_once(serverless, webhook, monkeypatch):
    import bot as bot_module

    m, old = _seed_merchant(bot_module)
    prices = {m.key: {"sell": 0.999, "sell_amount": 10.0, "sell_ad_id": "1",
                      "buy": 1.001, "buy_amount": 20.0, "buy_ad_id": "2", "error": None}}
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "", "pending_updates": 0}))

    async def fake_get_prices():
        return prices

    monkeypatch.setattr(bot_module, "get_prices", fake_get_prices)
    try:
        status, data = asgi_json(webhook.app, method="GET",
                                 params={"register": "0", "check": "1"})
        assert status == 200
        assert data["prices"][m.key]["sell"] == 0.999
        assert data["prices"][m.key]["nick"] == "Fast_sonic"

        _, headers, payload, _ = asgi_call(webhook.app, method="GET",
                                           headers={"Accept": "text/html"},
                                           params={"register": "0", "check": "1"})
        text = payload.decode()
        assert "Fast_sonic" in text and "0.999" in text
    finally:
        _unseed_merchant(bot_module, old)


def test_price_check_explains_a_geo_blocked_region(serverless, webhook, monkeypatch):
    import bot as bot_module

    m, old = _seed_merchant(bot_module)
    error = ("Client error '451 Unavailable For Legal Reasons' "
             "for url 'https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search'")
    prices = {m.key: {"sell": None, "sell_amount": None, "sell_ad_id": None,
                      "buy": None, "buy_amount": None, "buy_ad_id": None, "error": error}}
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "", "pending_updates": 0}))
    monkeypatch.setenv("VERCEL_REGION", "iad1")

    async def fake_get_prices():
        return prices

    monkeypatch.setattr(bot_module, "get_prices", fake_get_prices)
    try:
        status, data = asgi_json(webhook.app, method="GET",
                                 params={"register": "0", "check": "1"})
        assert "451" in data["prices"][m.key]["error"]

        _, _, payload, _ = asgi_call(webhook.app, method="GET",
                                     headers={"Accept": "text/html"},
                                     params={"register": "0", "check": "1"})
        text = payload.decode()
        assert "fra1" in text and "geo-block" in text
    finally:
        _unseed_merchant(bot_module, old)


# ── the "connect database" link on the web pages ───────────────────────────
def test_status_page_links_to_the_database_page_when_state_is_not_persistent(
        serverless, webhook, monkeypatch):
    """The warning that says state will be lost carries the way to fix it."""
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "https://bot.vercel.app/api/webhook",
                                         "pending_updates": 0}))
    monkeypatch.delenv("P2P_DATABASE_LINK", raising=False)

    _, _, payload, _ = asgi_call(webhook.app, method="GET", headers={"Accept": "text/html"})
    text = payload.decode()

    assert "NOT persistent" in text                      # the test store is a file, not KV
    assert "Connect database" in text
    assert 'href="https://vercel.com/dashboard/stores"' in text


def test_status_page_hides_the_connect_button_when_a_database_is_connected(serverless, webhook):
    text = webhook._status_page({"state": {"backend": "redis", "persistent": True,
                                           "detail": "redis (kv.example, key p2p)"}})

    assert "Connect database" not in text
    assert "Check the webhook again" in text             # the other action stays


def test_broken_configuration_offers_the_database_link_to_scripts(
        serverless, webhook, monkeypatch):
    def broken():
        raise serverless.ConfigError("BOT_TOKEN is missing")

    monkeypatch.setattr(webhook, "get_bot", broken)
    monkeypatch.delenv("P2P_DATABASE_LINK", raising=False)

    status, data = asgi_json(webhook.app, method="GET")

    assert status == 500
    assert data["connect_database"] == "https://vercel.com/dashboard/stores"


def test_page_escapes_link_urls(serverless):
    """Links are rendered as HTML, so they are escaped like every other value."""
    text = serverless.page("t", [], links=[("x", 'https://a.example/?q="><script>alert(1)</script>')])

    assert "<script>" not in text
    assert "&quot;&gt;" in text


def test_status_json_carries_the_database_link_too(serverless, webhook, monkeypatch):
    """Scripts polling the status endpoint get the link, not only browsers."""
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "", "pending_updates": 0}))
    monkeypatch.delenv("P2P_DATABASE_LINK", raising=False)

    status, data = asgi_json(webhook.app, method="GET", params={"register": "0"})

    assert status == 200
    assert data["connect_database"] == "https://vercel.com/dashboard/stores"
