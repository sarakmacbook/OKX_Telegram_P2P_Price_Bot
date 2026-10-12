"""Why a Vercel deployment goes *silent* in Telegram — and how it says so.

A registered webhook is not a working webhook: Vercel Deployment Protection
(Vercel Authentication, on by default) answers Telegram's POST with ``401`` for
preview deployments **and** for the auto-generated ``*-<hash>.vercel.app``
addresses, a domain that is not connected answers ``404``, and a missing DNS
record answers nothing at all.  In every one of those cases Telegram eventually
gives up, while the deployment's own pages still claim everything is fine.

These tests pin the answer to that silence:

* ``/api/health`` — a static marker that proves a request reached *this*
  deployment, and doubles as a cheap uptime probe;
* the reachability knock on the public URL (``serverless.webhook_reachability``)
  and the actionable hint it produces for 401/403, 404 and unreachable hosts;
* Telegram's own ``last_error_message`` translated into one fix per cause;
* the cron heartbeat (``tick``) that separates "no price changed" from "nothing
  runs this deployment", plus the isolation that keeps one failing step from
  costing the day's price post (the free plan gets one run a day);
* the Telegram side: a crashed handler now logs its traceback and answers the
  admin instead of silently ignoring them, and the panel shows the cron age.
"""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── helpers ────────────────────────────────────────────────────────────────
def load_api(name: str):
    """Import ``api/<name>.py`` the way Vercel's runtime does — by file path."""
    path = ROOT / "api" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"api_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def reach(serverless, **kwargs):
    """``webhook_reachability`` is a coroutine — drive it like the handlers do."""
    return asyncio.run(serverless.webhook_reachability(**kwargs))


def asgi_call(app, method="GET", path="/api/webhook", headers=None, params=None, body: bytes = b""):
    sent = []
    scope = {
        "type": "http", "http_version": "1.1", "method": method, "path": path,
        "query_string": urlencode(params or {}).encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    status: dict = {"done": False}

    async def receive():
        if status["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        status["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    first = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return first["status"], {k.decode(): v.decode() for k, v in first["headers"]}, payload


class Recorder:
    """An async stand-in that remembers how it was called."""

    def __init__(self, result=None):
        self.calls, self.result = [], result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class FakeReply:
    """Just enough of an httpx.Response for the probe."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def fake_probe(serverless, monkeypatch, reply=None, error=None, calls=None):
    """Answer the probe's knock without touching the network."""

    async def knock(url):
        if calls is not None:
            calls.append(url)
        if error is not None:
            raise error
        return reply

    monkeypatch.setattr(serverless, "_probe_request", knock)


@pytest.fixture()
def serverless():
    sys.path.insert(0, str(ROOT))
    import serverless as module
    return module


@pytest.fixture(autouse=True)
def clean(serverless):
    """Leave the caches and the shared state as they were found."""
    import bot as bot_module

    saved = dict(bot_module.state)
    saved_apps = dict(serverless._apps)
    serverless.reset_reachability_probe()
    for name in ("P2P_WEBHOOK_PROBE", "VERCEL"):
        saved_env = None
        yield_target = None  # noqa: F841  (readability)
    yield
    bot_module.state.clear()
    bot_module.state.update(saved)
    serverless._apps.clear()
    serverless._apps.update(saved_apps)
    serverless._locks.clear()
    serverless.reset_reachability_probe()


@pytest.fixture()
def stored_state():
    """Let a test write into the real state store (``run_tick`` re-reads it) and undo it."""
    import bot as bot_module

    previous = dict(bot_module.state)
    yield bot_module
    bot_module.state.clear()
    bot_module.state.update(previous)
    bot_module.save()


@pytest.fixture()
def on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_URL", "p2p-price-bot-abc123-me.vercel.app")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "p2p-price-bot.vercel.app")


# ── /api/health: the marker that proves the request got in ─────────────────
def test_health_route_answers_with_the_marker():
    entry = load_api("app")
    status, headers, payload = asgi_call(entry.app, path="/api/health")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert json.loads(payload) == {"ok": True, "app": "p2p-price-bot",
                                   "mode": "webhook (Vercel serverless)"}


def test_health_route_never_touches_the_bot_or_the_store(monkeypatch, serverless):
    """It is the one route that must work even while everything else is broken."""
    entry = load_api("app")

    def explode(*args, **kwargs):                       # any state access would blow up
        raise AssertionError("the health route must not read the bot or its state")

    monkeypatch.setattr(serverless, "get_bot", explode)
    monkeypatch.setattr(serverless, "setup_status", explode)
    status, _, payload = asgi_call(entry.app, path="/api/health")
    assert status == 200 and json.loads(payload)["app"] == "p2p-price-bot"


def test_health_route_is_known_to_the_router(serverless):
    assert serverless.HEALTH_PATH == "/api/health"
    assert serverless.HEALTH_MARKER == "p2p-price-bot"


# ── the reachability knock on the public URL ───────────────────────────────
def test_a_public_deployment_is_reported_as_reachable(serverless, monkeypatch):
    monkeypatch.setenv("P2P_WEBHOOK_PROBE", "1")
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    calls: list = []
    fake_probe(serverless, monkeypatch, FakeReply(200, {"ok": True, "app": "p2p-price-bot"}), calls=calls)

    info = reach(serverless)

    assert calls == ["https://bot.example.com/api/health"]
    assert info["checked"] is True and info["reachable"] is True
    assert info["hint"] == "" and info["protected"] is False


def test_deployment_protection_is_named_with_the_fix(serverless, monkeypatch, on_vercel):
    monkeypatch.setenv("PUBLIC_URL", "https://p2p-price-bot-abc123-me.vercel.app")
    fake_probe(serverless, monkeypatch, FakeReply(401, None, "<html>login</html>"))

    info = reach(serverless)

    assert info["checked"] is True and info["reachable"] is False
    assert info["protected"] is True and info["status"] == 401
    assert "Deployment Protection" in info["detail"]
    assert "PUBLIC_URL" in info["hint"] and "production domain" in info["hint"]


def test_a_disconnected_domain_is_reported_as_404(serverless, monkeypatch, on_vercel):
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    fake_probe(serverless, monkeypatch, FakeReply(404, {"error": "not found"}))

    info = reach(serverless)

    assert info["reachable"] is False and info["status"] == 404
    assert "404" in info["detail"] and "Domains" in info["hint"]


def test_an_unreachable_host_says_so_instead_of_raising(serverless, monkeypatch, on_vercel):
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")

    fake_probe(serverless, monkeypatch, error=ConnectionError("Name or service not known"))

    info = reach(serverless)

    assert info["checked"] is True and info["reachable"] is False and info["status"] == 0
    assert "could not be reached" in info["detail"]
    assert "Telegram cannot deliver" in info["hint"]


def test_another_service_answering_is_not_mistaken_for_the_bot(serverless, monkeypatch, on_vercel):
    """A 200 from something else (a proxy, an old deployment) is not our bot."""
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    fake_probe(serverless, monkeypatch, FakeReply(200, {"hello": "world"}))

    info = reach(serverless)

    assert info["reachable"] is False
    assert "instead of the bot's health page" in info["detail"]


def test_the_answer_is_cached_and_can_be_forced(serverless, monkeypatch, on_vercel):
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    calls: list = []
    fake_probe(serverless, monkeypatch, FakeReply(200, {"app": "p2p-price-bot"}), calls=calls)

    assert reach(serverless)["cached"] is False
    assert reach(serverless)["cached"] is True
    assert len(calls) == 1, "a page view must not hammer the deployment's own URL"

    reach(serverless, force=True)
    assert len(calls) == 2

    serverless.reset_reachability_probe()
    reach(serverless)
    assert len(calls) == 3


def test_registering_the_webhook_forgets_the_old_answer(serverless, monkeypatch, on_vercel):
    """``?register=1`` is what an owner does right after fixing the domain."""
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    calls: list = []
    fake_probe(serverless, monkeypatch, FakeReply(401), calls=calls)

    assert reach(serverless)["reachable"] is False
    serverless.reset_reachability_probe()             # what set_webhook does
    assert reach(serverless)["reachable"] is False
    assert len(calls) == 2, "a re-registration must re-check, not reprint the cache"


def test_registering_the_webhook_resets_the_probe(serverless, monkeypatch, stored_state):
    """set_webhook itself must drop the cache — the address may just have changed."""
    from types import SimpleNamespace

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    monkeypatch.setattr(serverless, "get_bot", lambda: stored_state)
    calls: list = []
    fake_probe(serverless, monkeypatch, FakeReply(401), calls=calls)

    class Bot:
        async def set_webhook(self, **kwargs):
            return True

        async def get_webhook_info(self):
            return SimpleNamespace(url="https://bot.example.com/api/webhook",
                                   pending_update_count=0, last_error_message=None)

    assert reach(serverless)["reachable"] is False          # e.g. before the fix
    asyncio.run(serverless.set_webhook(app=SimpleNamespace(bot=Bot())))
    assert serverless._probe["key"] == "", "a re-registration must forget the old answer"
    assert reach(serverless)["cached"] is False and len(calls) == 2


def test_off_vercel_nothing_is_asked_over_the_network(serverless, monkeypatch):
    """A local ``uvicorn`` (or a Docker install) never calls out to the internet."""
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("VERCEL_URL", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    monkeypatch.delenv("P2P_WEBHOOK_PROBE", raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")

    def explode(*args, **kwargs):
        raise AssertionError("the probe must be off outside Vercel")

    monkeypatch.setattr(serverless, "_probe_request", explode)

    info = reach(serverless)

    assert info["checked"] is False and info["reachable"] is None
    assert "P2P_WEBHOOK_PROBE" in info["detail"]


def test_the_probe_can_be_switched_off_on_vercel_too(serverless, monkeypatch, on_vercel):
    monkeypatch.setenv("P2P_WEBHOOK_PROBE", "0")
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    monkeypatch.setattr(serverless, "_probe_request",
                        lambda *a, **kw: pytest.fail("probe must not run"))

    info = reach(serverless)

    assert info["checked"] is False and info["reachable"] is None


def test_without_a_public_url_the_check_is_skipped(serverless, monkeypatch, on_vercel):
    for name in ("PUBLIC_URL", "P2P_PUBLIC_URL", "WEBHOOK_URL",
                 "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(serverless, "_probe_request",
                        lambda *a, **kw: pytest.fail("nothing to probe"))

    info = reach(serverless)

    assert info["checked"] is False and info["reachable"] is None
    assert "webhook cannot be" in info["detail"]


# ── Telegram's own error message, translated ───────────────────────────────
@pytest.mark.parametrize("message,fragment", [
    ("Wrong response from the webhook: 401 Unauthorized", "Deployment Protection"),
    ("Wrong response from the webhook: 403 Forbidden", "Deployment Protection"),
    ("Wrong response from the webhook: 404 Not Found", "404"),
    ("Read timeout expired", "cold start"),
    ("SSLCertVerificationError", "certificate"),
    ("Too Many Requests: retry after 5", "rate-limiting"),
    ("Wrong response from the webhook: 500 Internal Server Error", "Logs"),
])
def test_telegram_errors_come_with_one_fix_each(serverless, message, fragment):
    hint = serverless.webhook_error_hint(message)
    assert fragment in hint, f"{message!r} → {hint!r}"


def test_an_unknown_error_has_no_invented_hint(serverless):
    assert serverless.webhook_error_hint("something nobody has seen yet") == ""
    assert serverless.webhook_error_hint("") == ""


def test_the_diagnostics_prefer_the_outside_knock_over_telegram(serverless, monkeypatch, on_vercel):
    """When Telegram cannot get in, the cause is the URL — not the handler."""
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    fake_probe(serverless, monkeypatch, FakeReply(401))

    report = serverless.webhook_diagnostics({"url": "https://bot.example.com/api/webhook",
                                             "last_error": "Wrong response from the webhook: 401"},
                                            reach(serverless))

    assert "Telegram cannot reach this deployment" in report["warning"]
    assert "Deployment Protection" in report["warning"]
    assert report["hint"]


def test_pending_updates_alone_are_a_warning_too(serverless):
    report = serverless.webhook_diagnostics(
        {"url": "https://bot.example.com/api/webhook", "pending_updates": 42, "last_error": None},
        {"checked": True, "reachable": True})

    assert "42 undelivered updates" in report["warning"]
    assert report["last_error"] is None


def test_a_healthy_deployment_has_no_diagnostic_warning(serverless):
    report = serverless.webhook_diagnostics(
        {"url": "https://bot.example.com/api/webhook", "pending_updates": 0, "last_error": None},
        {"checked": True, "reachable": True})
    assert report["warning"] == ""


# ── the cron heartbeat ─────────────────────────────────────────────────────
def test_the_cron_age_is_human_readable(serverless):
    assert serverless.short_age(0) == "0s"
    assert serverless.short_age(59) == "59s"
    assert serverless.short_age(60) == "1m"
    assert serverless.short_age(3600) == "1h"
    assert serverless.short_age(3700) == "1h 1m"
    assert serverless.short_age(90 * 3600) == "90h"


def test_a_cron_that_never_ran_is_reported_not_hidden(serverless, monkeypatch, on_vercel):
    import bot as bot_module

    bot_module.state.pop("tick", None)
    status = serverless.tick_status(bot_module)

    assert status["at"] == 0 and status["age"] is None and status["count"] == 0
    assert "never" in serverless.tick_note(status)
    warning = serverless.tick_warning(status)
    assert "never posted a price" in warning and "/api/tick" in warning


def test_a_fresh_cron_run_is_not_a_warning(serverless, monkeypatch, on_vercel):
    import bot as bot_module

    bot_module.state["tick"] = {"at": int(time.time()) - 300, "count": 7}
    status = serverless.tick_status(bot_module)

    assert status["age"] is not None and status["age"] < 600
    assert "5m" in serverless.tick_note(status)
    assert serverless.tick_warning(status) == ""


def test_a_cron_that_stopped_is_explained_with_the_free_plan_recipe(serverless, monkeypatch,
                                                                   on_vercel):
    import bot as bot_module

    bot_module.state["tick"] = {"at": int(time.time()) - 30 * 3600, "count": 12}
    warning = serverless.tick_warning(serverless.tick_status(bot_module))

    assert "30h" in warning
    assert "once a day" in warning and "VERCEL.md §5" in warning


def test_off_vercel_the_cron_warning_stays_quiet(serverless, monkeypatch):
    """A polling install posts from its own JobQueue — no cron, no warning."""
    import bot as bot_module

    monkeypatch.delenv("VERCEL", raising=False)
    bot_module.state.pop("tick", None)

    assert serverless.tick_warning(serverless.tick_status(bot_module)) == ""


# ── run_tick: one failing step must not cost the whole day ─────────────────
def _tick_env(monkeypatch, bot_module):
    """Stub the Telegram calls so run_tick can be driven offline."""
    from types import SimpleNamespace

    from telegram import Bot, User

    calls: dict = {"posted": 0, "cleanup": 0}

    async def fake_get_me(self, **kwargs):
        return User(id=123456, first_name="Test Bot", is_bot=True, username="p2p_test_bot")

    async def fake_set_webhook(self, url, **kwargs):
        calls["webhook"] = url

    async def fake_get_webhook_info(self, **kwargs):
        return SimpleNamespace(url=calls.get("webhook", ""), pending_update_count=0,
                               last_error_message=None)

    async def fake_auto_post(bot, *args, **kwargs):
        calls["posted"] += 1
        return True

    async def fake_cleanup(bot, *args, **kwargs):
        calls["cleanup"] += 1
        return False

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    monkeypatch.setattr(Bot, "set_webhook", fake_set_webhook)
    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)
    monkeypatch.setattr(bot_module, "auto_post_task", fake_auto_post)
    monkeypatch.setattr(bot_module, "cleanup_task", fake_cleanup)
    monkeypatch.setenv("PUBLIC_URL", "https://p2p-test.vercel.app")
    return calls


def test_run_tick_records_when_it_ran(serverless, monkeypatch, stored_state):
    bot_module = stored_state

    calls = _tick_env(monkeypatch, bot_module)
    bot_module.state.pop("tick", None)
    bot_module.save()                     # run_tick re-reads the store, not this dict
    before = int(time.time())

    result = asyncio.run(serverless.run_tick())

    assert result["posted"] is True
    assert result["tick"]["count"] == 1
    assert before <= result["tick"]["at"] <= int(time.time()) + 1
    assert bot_module.state["tick"] == result["tick"]
    assert "cron" not in result.get("errors", [])            # it ran; nothing to report


def test_run_tick_counts_every_run(serverless, monkeypatch, stored_state):
    bot_module = stored_state

    _tick_env(monkeypatch, bot_module)
    bot_module.state["tick"] = {"at": int(time.time()) - 86400, "count": 5}
    bot_module.save()

    result = asyncio.run(serverless.run_tick())

    assert result["tick"]["count"] == 6


def test_a_failing_webhook_check_does_not_cost_the_price_post(serverless, monkeypatch):
    """On the free plan the cron may be the only run in 24 hours."""
    import bot as bot_module

    calls = _tick_env(monkeypatch, bot_module)

    async def boom(*args, **kwargs):
        raise RuntimeError("Telegram is unreachable")

    monkeypatch.setattr(serverless, "ensure_webhook", boom)

    result = asyncio.run(serverless.run_tick())

    assert calls["posted"] == 1, "the price post must still happen"
    assert calls["cleanup"] == 1, "and so must the cleanup"
    assert result["posted"] is True
    assert result["webhook"]["error"].startswith("RuntimeError")
    assert any("webhook" in item for item in result["errors"])
    assert result["tick"]["count"] >= 1


def test_a_failing_post_still_sweeps_the_group(serverless, monkeypatch):
    import bot as bot_module

    calls = _tick_env(monkeypatch, bot_module)

    async def boom(*args, **kwargs):
        raise RuntimeError("group is not reachable")

    monkeypatch.setattr(bot_module, "auto_post_task", boom)

    result = asyncio.run(serverless.run_tick())

    assert result["posted"] is False
    assert calls["cleanup"] == 1
    assert any("post prices" in item for item in result["errors"])


def test_the_tick_answer_keeps_its_old_keys(serverless, monkeypatch):
    """External schedulers and scripts read these — they must not disappear."""
    import bot as bot_module

    _tick_env(monkeypatch, bot_module)

    result = asyncio.run(serverless.run_tick())

    for key in ("webhook", "posted", "deleted_stale_message", "captcha_expired", "cleanup",
                "cleanup_warnings_removed", "auto", "group", "channel", "forward_from",
                "merchants", "state"):
        assert key in result, f"{key} vanished from the cron answer"
    assert "public" in result and "diagnostics" in result


def test_the_tick_knocks_on_the_public_url_too(serverless, monkeypatch, stored_state):
    """The cron may be the only chance in 24h to notice the bot is unreachable."""
    bot_module = stored_state

    _tick_env(monkeypatch, bot_module)
    monkeypatch.setenv("P2P_WEBHOOK_PROBE", "1")
    fake_probe(serverless, monkeypatch, FakeReply(401))

    result = asyncio.run(serverless.run_tick())

    assert result["public"]["reachable"] is False
    assert "Deployment Protection" in result["diagnostics"]["warning"]
    assert any("Deployment Protection" in item for item in result["errors"])


# ── the status page and its JSON twin say all of it ────────────────────────
def test_the_status_page_reports_a_protected_deployment(serverless, monkeypatch):
    import bot as bot_module

    webhook = load_api("webhook")
    monkeypatch.setenv("P2P_WEBHOOK_PROBE", "1")
    monkeypatch.setenv("PUBLIC_URL", "https://p2p-price-bot-abc123-me.vercel.app")
    fake_probe(serverless, monkeypatch, FakeReply(401))
    monkeypatch.setattr(webhook, "ensure_webhook",
                        Recorder(result={"url": "https://p2p-price-bot-abc123-me.vercel.app/api/webhook",
                                         "pending_updates": 0, "last_error": None}))
    bot_module.state.pop("tick", None)

    status, headers, payload = asgi_call(webhook.app, path="/api/webhook",
                                         headers={"Accept": "text/html"})

    body = payload.decode()
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert "Reachable" in body and "Deployment Protection" in body
    assert "Last cron run" in body


def test_the_status_json_carries_the_reachability_and_the_cron(serverless, monkeypatch,
                                                              stored_state):
    bot_module = stored_state

    webhook = load_api("webhook")
    monkeypatch.setenv("P2P_WEBHOOK_PROBE", "1")
    monkeypatch.setenv("PUBLIC_URL", "https://bot.example.com")
    fake_probe(serverless, monkeypatch, FakeReply(200, {"app": "p2p-price-bot"}))
    monkeypatch.setattr(webhook, "ensure_webhook", Recorder(result={"url": "", "pending_updates": 0}))
    bot_module.state["tick"] = {"at": int(time.time()) - 600, "count": 3}
    bot_module.save()

    status, headers, payload = asgi_call(webhook.app, path="/api/webhook",
                                         headers={"Accept": "application/json"})
    data = json.loads(payload)

    assert status == 200
    assert data["public"]["reachable"] is True
    assert data["diagnostics"]["warning"] == ""
    assert data["cron"]["count"] == 3 and data["cron"]["age"] >= 600


def test_the_setup_page_needs_no_reachability_probe(serverless, monkeypatch):
    """The config-error page must render fast, and it has nothing to knock on yet."""
    webhook = load_api("webhook")
    monkeypatch.setattr(serverless, "_probe_request",
                        lambda *a, **kw: pytest.fail("no probe before setup"))
    monkeypatch.setattr(webhook, "setup_status", lambda: {
        "ready": False, "serverless": True, "message": "bot.py refused to start — BOT_TOKEN "
        "and ADMIN_IDS are missing.", "checks": [], "database": {}, "missing": "BOT_TOKEN"})
    monkeypatch.setattr(webhook, "_local_config_has_credentials", lambda: False)

    status, _, payload = asgi_call(webhook.app, path="/api/webhook",
                                   headers={"Accept": "text/html"})

    assert status == 500 and b"BOT_TOKEN" in payload


# ── the Telegram side: never silent about a crash ──────────────────────────
def test_a_crashed_handler_logs_its_traceback(monkeypatch, caplog):
    import logging

    import bot as bot_module
    from telegram.ext import ContextTypes

    class Chat:
        id = 424242
        type = "private"

    class Update:
        effective_chat = Chat()
        effective_user = type("U", (), {"id": 424242})()

    context = type("C", (), {"error": RuntimeError("boom"), "bot": None})()
    with caplog.at_level(logging.ERROR, logger="p2p-bot"):
        asyncio.run(bot_module.error_handler(Update(), context))

    record = next(r for r in caplog.records if "caused error" in r.getMessage())
    assert "boom" in record.getMessage()
    assert record.exc_info and record.exc_info[0] is RuntimeError, \
        "the traceback must reach the deployment's logs"


def test_a_crashed_handler_answers_the_admin_in_private(monkeypatch):
    """On Vercel the webhook answers 200, so a crash must not be silence."""
    import bot as bot_module

    sent: list = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    class Chat:
        id = 424242
        type = "private"

    class Update:
        effective_chat = Chat()
        effective_user = type("U", (), {"id": 424242})()

    context = type("C", (), {"error": RuntimeError("boom"), "bot": Bot()})()
    asyncio.run(bot_module.error_handler(Update(), context))

    assert sent and sent[0][0] == 424242
    assert "went wrong" in sent[0][1] and "Logs" in sent[0][1]


def test_a_crashed_group_handler_stays_quiet(monkeypatch):
    """The group must not be spammed with the bot's own errors."""
    import bot as bot_module

    sent: list = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    class Chat:
        id = -1001234567890
        type = "supergroup"

    class Update:
        effective_chat = Chat()
        effective_user = type("U", (), {"id": 424242})()

    context = type("C", (), {"error": RuntimeError("boom"), "bot": Bot()})()
    asyncio.run(bot_module.error_handler(Update(), context))

    assert sent == []


def test_a_crashed_handler_spares_strangers(monkeypatch):
    import bot as bot_module

    sent: list = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    class Chat:
        id = 999999
        type = "private"

    class Update:
        effective_chat = Chat()
        effective_user = type("U", (), {"id": 999999})()

    context = type("C", (), {"error": RuntimeError("boom"), "bot": Bot()})()
    asyncio.run(bot_module.error_handler(Update(), context))

    assert sent == []


# ── the panel shows the cron heartbeat ─────────────────────────────────────
def test_the_panel_shows_the_last_cron_run_on_vercel(bot, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    bot.state["tick"] = {"at": int(time.time()) - 3 * 3600, "count": 4}

    lines = [line for line in bot.panel_text().splitlines() if "Cron" in line]

    assert lines and "3h" in lines[0] and "4 runs" in lines[0]


def test_the_panel_explains_a_cron_that_never_ran(bot, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    bot.state.pop("tick", None)

    lines = [line for line in bot.panel_text().splitlines() if "Cron" in line]

    assert lines and "not run yet" in lines[0] and "/api/tick" not in lines[0]


def test_the_panel_stays_unchanged_off_vercel(bot, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("VERCEL_URL", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    bot.state["tick"] = {"at": int(time.time()) - 3600, "count": 2}

    assert "Cron" not in bot.panel_text()
