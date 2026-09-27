"""Detecting the database that is already there — and skipping the step for it.

The setup page used to ask for a KV/Redis pair whenever the *environment* had
none, which is the wrong question on Vercel: the project can have a store
connected long before its credentials reach a deployment (they only land on a
redeploy).  So the page asks twice — first the environment, then Vercel itself
(``api.vercel.com``) — and skips the database step the moment either one
answers, saying *where* the database was found.

These tests pin both answers, the skip, the explanation for the "connected but
not redeployed" case, and the fact that a look-up that cannot be made (no token,
a refused token, an offline host) is reported instead of silently breaking the
page.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api.app as entrypoint                       # noqa: E402
import serverless                                  # noqa: E402


@pytest.fixture(autouse=True)
def no_cached_probe():
    """Every test sees the store as it is, not as the last one left it."""
    serverless.reset_store_probe()
    yield
    serverless.reset_store_probe()


# ── helpers ────────────────────────────────────────────────────────────────
def get_setup_page():
    """``GET /api/setup`` in a browser: ``(status, html)``."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "https",
        "method": "GET", "path": "/api/setup", "raw_path": b"/api/setup", "query_string": b"",
        "root_path": "", "client": ("test", 1234), "server": ("test", 443),
        "headers": [(b"accept", b"text/html")],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(entrypoint.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body.decode()


def setup_page_json():
    """``GET /api/setup`` as a script would: the parsed status object."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "https",
        "method": "GET", "path": "/api/setup", "raw_path": b"/api/setup", "query_string": b"",
        "root_path": "", "client": ("test", 1234), "server": ("test", 443),
        "headers": [(b"accept", b"application/json")],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    # The form itself answers JSON only for POST; the status is read directly
    # from the same function the page and the diagnostics share.
    asyncio.run(entrypoint.app(scope, receive, send))
    return serverless.setup_status()


def fake_vercel(monkeypatch, *, stores=None, envs=None, status=200, body=None,
                store_status=None):
    """Answer the Vercel API calls the detection makes; returns the calls made."""
    import httpx

    calls: list[tuple[str, dict]] = []

    class Reply:
        def __init__(self, payload, code):
            self._payload, self.status_code = payload, code

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if "/storage/stores" in url:
            if store_status is not None:
                return Reply(body if body is not None else {}, store_status)
            return Reply({"stores": [] if stores is None else stores}, 200)
        if body is not None and store_status is None:
            return Reply(body, status)
        if url.endswith("/env") or "/env?" in url:
            return Reply({"envs": [] if envs is None else envs}, 200)
        return Reply({}, 404)

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


def vercel_offline(monkeypatch):
    """No network at all — the look-up must report that, not raise."""
    import httpx

    def dead(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "get", dead)


@pytest.fixture(autouse=True)
def no_cached_answer():
    """Every test sees the database as it is, not as the last one left it."""
    serverless.reset_database_detection()
    yield
    serverless.reset_database_detection()


@pytest.fixture(autouse=True)
def clean_environment(tmp_path, monkeypatch):
    """A private settings file and none of the variables under test."""
    monkeypatch.setenv("P2P_RUNTIME_CONFIG_FILE", str(tmp_path / "runtime_config.json"))
    for name in ("KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
                 "UPSTASH_REDIS_REST_TOKEN", "REDIS_REST_URL", "REDIS_REST_TOKEN",
                 "VERCEL", "VERCEL_PROJECT_ID", "VERCEL_PROJECT_NAME", "VERCEL_TEAM_ID",
                 "VERCEL_TOKEN", "P2P_VERCEL_TOKEN", "P2P_VERCEL_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture()
def on_vercel(monkeypatch):
    """A Vercel deployment that can be asked about its own project."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj_p2p_bot")
    monkeypatch.setenv("VERCEL_TOKEN", "vercel-access-token")


# ── the environment answers first ──────────────────────────────────────────
def test_a_complete_kv_pair_is_detected_in_the_environment(monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")

    found = serverless.detect_database()

    assert found["found"] is True and found["wired"] is True
    assert found["source"] == "environment"
    assert found["provider"] == "Vercel KV / Upstash"
    assert "Vercel KV / Upstash" in found["where"] and "environment" in found["where"]
    assert found["skipped"] is True


def test_the_environment_answer_costs_no_vercel_call(monkeypatch):
    """A deployment that already has credentials needs no look-up at all."""
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    calls = fake_vercel(monkeypatch)

    found = serverless.detect_database()

    assert calls == []
    assert found["checked"] is False and found["error"] == ""


def test_an_incomplete_pair_is_not_a_database(monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")   # no token

    assert serverless.detect_database()["found"] is False


def test_a_second_alias_counts_as_well(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "token")

    found = serverless.detect_database()

    assert found["found"] is True and found["provider"] == "Upstash Redis"


# ── Vercel answers second ──────────────────────────────────────────────────
def test_a_store_vercel_reports_is_detected_and_skipped(on_vercel, monkeypatch):
    calls = fake_vercel(monkeypatch, stores=[{"id": "store_1", "name": "p2p-kv",
                                              "type": "redis"}])

    found = serverless.detect_database()

    assert found["found"] is True and found["skipped"] is True
    assert found["source"] == "vercel-storage"
    assert found["stores"] == ["p2p-kv"]
    assert "Vercel → Storage" in found["where"] and "p2p-kv" in found["where"]
    assert calls and calls[0][1]["params"]["projectId"] == "prj_p2p_bot"
    assert "Bearer vercel-access-token" in calls[0][1]["headers"]["Authorization"]


def test_a_store_that_is_connected_but_not_wired_says_so(on_vercel, monkeypatch):
    """Connected on Vercel, invisible here: a redeploy is the fix, not a new store."""
    fake_vercel(monkeypatch, stores=[{"name": "p2p-kv", "type": "redis"}])

    found = serverless.detect_database()

    assert found["found"] is True and found["wired"] is False
    assert "Connect to this project" in found["detail"]
    assert "No new database is needed" in found["detail"]
    assert "KV_REST_API_URL" in found["detail"]


def test_a_project_environment_variable_counts_when_no_store_is_listed(on_vercel, monkeypatch):
    fake_vercel(monkeypatch, stores=[],
                envs=[{"key": "KV_REST_API_URL", "contentHint": {"type": "redis-url",
                                                                 "storeId": "store_1"}}])

    found = serverless.detect_database()

    assert found["found"] is True and found["source"] == "vercel-env"
    assert "KV_REST_API_URL" in found["where"]


def test_a_blob_or_postgres_store_is_not_this_bots_database(on_vercel, monkeypatch):
    fake_vercel(monkeypatch, stores=[{"name": "images", "type": "blob"},
                                     {"name": "db", "type": "postgres"}])

    assert serverless.detect_database()["found"] is False


def test_a_store_without_a_type_is_judged_by_its_name(on_vercel, monkeypatch):
    fake_vercel(monkeypatch, stores=[{"name": "my-upstash-redis"}])

    assert serverless.detect_database()["found"] is True


# ── a look-up that cannot be made is reported, never fatal ─────────────────
def test_off_vercel_nothing_is_queried(monkeypatch):
    vercel_offline(monkeypatch)

    found = serverless.detect_database()

    assert found["found"] is False and found["error"] == ""
    assert "data.json" in found["detail"]


def test_without_a_token_vercel_is_not_asked(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj_p2p_bot")
    calls = fake_vercel(monkeypatch, stores=[{"name": "p2p-kv", "type": "redis"}])

    found = serverless.detect_database()

    assert calls == []
    assert found["found"] is False and found["checked"] is False
    assert "VERCEL_TOKEN" in found["error"]


def test_without_a_project_vercel_is_not_asked(on_vercel, monkeypatch):
    monkeypatch.delenv("VERCEL_PROJECT_ID")
    calls = fake_vercel(monkeypatch, stores=[{"name": "p2p-kv", "type": "redis"}])

    found = serverless.detect_database()

    assert calls == []
    assert "VERCEL_PROJECT_ID" in found["error"]


def test_a_refused_token_does_not_try_the_second_look_up(on_vercel, monkeypatch):
    """A token the API refuses will refuse the next call too — ask once."""
    calls = fake_vercel(monkeypatch, store_status=403, body={"error": {"code": "forbidden"}})

    found = serverless.detect_database()

    assert found["found"] is False
    assert len(calls) == 1, "one refused call is not a reason to make a second one"


def test_a_store_list_that_cannot_be_read_falls_back_to_the_variables(on_vercel, monkeypatch):
    """Not every token may read the store list; the project's variables still answer."""
    fake_vercel(monkeypatch, store_status=404,
                envs=[{"key": "KV_REST_API_URL", "contentHint": {"type": "redis-url"}}])

    found = serverless.detect_database()

    assert found["found"] is True and found["source"] == "vercel-env"
    assert found["error"] == "", "an unreadable store list is not an error once one answers"


def test_a_refused_token_is_reported_not_raised(on_vercel, monkeypatch):
    fake_vercel(monkeypatch, store_status=403, body={"error": {"code": "forbidden"}})

    found = serverless.detect_database()

    assert found["found"] is False
    assert "refused the token" in found["error"]


def test_an_unreachable_vercel_is_reported_not_raised(on_vercel, monkeypatch):
    vercel_offline(monkeypatch)

    found = serverless.detect_database()

    assert found["found"] is False
    assert "could not be reached" in found["error"]


def test_the_answer_is_cached_and_can_be_forgotten(on_vercel, monkeypatch):
    calls = fake_vercel(monkeypatch, stores=[{"name": "p2p-kv", "type": "redis"}])
    serverless.detect_database()
    serverless.detect_database()
    serverless.detect_database()

    assert len(calls) == 1, "one page view is not a reason to ask Vercel three times"

    serverless.reset_database_detection()
    serverless.detect_database()

    assert len(calls) == 2


# ── the setup page skips the step, and says where it found the database ────
def test_the_page_skips_the_database_step_when_vercel_has_one(on_vercel, monkeypatch):
    fake_vercel(monkeypatch, stores=[{"name": "p2p-kv", "type": "redis"}])

    status, text = get_setup_page()

    assert status == 200
    assert "Database detected — step skipped" in text
    assert "Detected from" in text and "p2p-kv" in text
    assert "How to insert the database" not in text, "no guide for a step that is skipped"
    assert "Connect database" not in text
    assert "database was detected" in text                   # the form says so too
    assert "Database detected — redeploy to apply it" in text
    assert "detected: Vercel → Storage" in text              # the checklist names the source


def test_the_page_says_the_environment_is_where_a_wired_database_came_from(monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    fake_vercel(monkeypatch)                                # must not be needed

    status, text = get_setup_page()

    assert status == 200
    assert "Database detected — step skipped" in text
    assert "database is already connected" in text
    assert "Vercel KV / Upstash" in text
    assert "Database detected — skipped" in text             # the walkthrough step


def test_a_detected_database_that_stopped_answering_is_not_skipped(on_vercel, monkeypatch):
    """Skipping a step that needs repairing would hide the outage."""
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    import httpx

    def dead(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "post", dead)              # the store does not answer
    serverless.reset_store_probe()

    status, text = get_setup_page()

    assert "Database detected — step skipped" not in text
    assert "How to insert the database" in text, "the page that reports it must repair it"
    assert "Not responding" in text


def test_without_any_database_the_guide_is_still_there(on_vercel, monkeypatch):
    fake_vercel(monkeypatch, stores=[])

    status, text = get_setup_page()

    assert status == 200
    assert "How to insert the database" in text
    assert "Database detected" not in text
    assert "Connect database" in text


def test_a_connected_store_still_blocks_the_deployment_with_an_explanation(
        on_vercel, monkeypatch):
    """Not ready — but the reason is a missing redeploy, not a missing database."""
    fake_vercel(monkeypatch, stores=[{"name": "p2p-kv", "type": "redis"}])
    status = serverless.setup_status()

    assert status["ready"] is False
    assert status["database"]["found"] is True and status["database"]["wired"] is False
    assert "already has a database" in status["message"]
    assert "No new database is needed" in status["message"]
    check = {item["name"]: item for item in status["checks"]}["state_store"]
    assert check["detected"].startswith("Vercel → Storage")
    assert "Connect to this project" in check["detail"]

    steps = serverless.setup_steps(status)

    assert "Database detected — skipped" in steps[1]
    assert len(steps) == 3


def test_the_status_json_carries_the_detection():
    status = serverless.setup_status()

    assert set(status["database"]) >= {"found", "wired", "source", "where", "skipped"}
    assert "https://example.upstash.io" not in json.dumps(status), "no secrets in the status"
