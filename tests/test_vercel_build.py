"""Vercel build regression guard: the Python entry point must stay discoverable.

Since ``@vercel/python`` 17 a Python project is built as **one** application: the
runtime loads a single top-level ``app`` and rewrites every request to it, and
``vercel build`` stops with ``No python entrypoint found in default locations``
when it cannot find one.  These tests keep that from breaking again:

* ``pyproject.toml`` declares ``[tool.vercel] entrypoint`` and the module it names
  really exists and exports a callable ``app``,
* the entry point file also sits in one of Vercel's *default* locations, so the
  build survives even if the declaration is ever ignored,
* the dependencies in ``pyproject.toml`` mirror ``requirements.txt`` — a
  ``pyproject.toml`` takes precedence, so a drift would silently drop packages,
* ``vercel.json``'s ``functions`` block still matches the entry point file (it is
  matched against the entry point, not against the built function), so the EU
  region and ``maxDuration`` keep applying,
* the router in ``api/app.py`` really hands ``/api/webhook`` and ``/api/tick`` to
  their endpoints, redirects ``/`` and answers 404 for everything else.
"""

import asyncio
import importlib
from pathlib import Path

import pytest

try:
    import tomllib                                    # Python 3.11+ (CI and Docker use 3.11)
except ModuleNotFoundError:                           # pragma: no cover - Python 3.10
    tomllib = None

import api.app as entrypoint
from serverless import TICK_PATH, WEBHOOK_PATH

ROOT = Path(__file__).resolve().parent.parent

# Vercel's own defaults (see @vercel/python: PYTHON_ENTRYPOINT_FILENAMES/DIRS).
DEFAULT_FILENAMES = ("app", "index", "server", "main", "wsgi", "asgi")
DEFAULT_DIRS = ("", "src", "app", "api")


def pyproject() -> dict:
    if tomllib is None:                               # pragma: no cover - Python 3.10
        pytest.skip("reading pyproject.toml needs Python 3.11+ (tomllib)")
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def declared_entrypoint() -> tuple[str, str]:
    """``"api.app:app"`` → ``("api.app", "app")``."""
    module, _, variable = pyproject()["tool"]["vercel"]["entrypoint"].partition(":")
    return module, variable


def entrypoint_file() -> Path:
    module, _ = declared_entrypoint()
    return ROOT / f"{module.replace('.', '/')}.py"


# ── the build finds the entry point ─────────────────────────────────────────
def test_pyproject_declares_the_vercel_entrypoint():
    module, variable = declared_entrypoint()
    assert module == "api.app" and variable == "app"


def test_the_declared_module_exists_and_exports_a_callable():
    """The same resolution Vercel applies to ``module:object``."""
    module, variable = declared_entrypoint()
    rel = module.replace(".", "/")
    assert (ROOT / f"{rel}.py").is_file() or (ROOT / rel / "__init__.py").is_file(), (
        f'tool.vercel.entrypoint names "{module}" but no module file was found — '
        "the build fails with PYTHON_ENTRYPOINT_NOT_FOUND")
    assert callable(getattr(importlib.import_module(module), variable))


def test_entrypoint_also_lives_in_a_default_location():
    """Auto-detection must work too, without the pyproject declaration."""
    relative = entrypoint_file().relative_to(ROOT).as_posix()
    defaults = {f"{d}/{name}.py" if d else f"{name}.py"
                for d in DEFAULT_DIRS for name in DEFAULT_FILENAMES}
    assert relative in defaults, (
        f"{relative} is not one of Vercel's default entry points ({sorted(defaults)})")


def test_endpoint_modules_still_export_their_own_app():
    """They stay runnable on their own (uvicorn api.webhook:app)."""
    for name in ("api.tick", "api.webhook"):
        assert callable(getattr(importlib.import_module(name), "app"))


# ── dependencies must not drift ─────────────────────────────────────────────
def requirements_txt() -> list[str]:
    lines = []
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return sorted(lines)


def test_pyproject_dependencies_mirror_requirements_txt():
    """A pyproject.toml takes precedence — a drift would drop packages silently."""
    assert sorted(pyproject()["project"]["dependencies"]) == requirements_txt()


# ── vercel.json still configures the built function ─────────────────────────
def test_functions_block_matches_the_entrypoint_file():
    import json
    config = json.loads((ROOT / "vercel.json").read_text())
    pattern = "api/**/*.py"
    assert pattern in config["functions"], "the functions block must keep its api/ glob"
    relative = entrypoint_file().relative_to(ROOT).as_posix()
    assert relative.startswith("api/") and relative.endswith(".py"), (
        f"{relative} would not match '{pattern}' — the functions block is matched "
        "against the entry point path, so regions/maxDuration would be lost")
    assert config["functions"][pattern]["regions"] == ["fra1"], (
        "the exchanges geo-block US IPs; the function must stay in the EU")


# ── routing ─────────────────────────────────────────────────────────────────
def call(path: str, method: str = "GET", query: str = "",
         headers: dict | None = None, body: bytes = b"") -> tuple[int, dict, bytes]:
    """Drive the ASGI app once and return ``(status, headers, body)``."""
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "https", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "client": ("test", 1234), "server": ("test", 443),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(entrypoint.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    decoded = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], decoded, payload


def test_root_redirects_to_the_status_page():
    status, headers, _ = call("/")
    assert status == 302
    assert headers["location"] == WEBHOOK_PATH


def test_webhook_path_reaches_the_webhook_endpoint():
    """The 403 comes from api/webhook.py — proof the request was routed there."""
    status, _, payload = call(WEBHOOK_PATH, "POST", body=b'{"update_id": 1}')
    assert status == 403
    assert b"secret token" in payload


def test_status_page_is_served():
    status, headers, payload = call(WEBHOOK_PATH, query="register=0")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert b'"mode"' in payload


def test_tick_path_reaches_the_tick_endpoint(monkeypatch):
    """Locked down with CRON_SECRET, so the test needs no Telegram round-trip."""
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret")
    status, _, payload = call(TICK_PATH)
    assert status == 401
    assert b"unauthorized" in payload


@pytest.mark.parametrize("path", ["/nope", "/api", "/api/webhook/extra", "/favicon.ico"])
def test_unknown_paths_answer_404(path):
    status, _, payload = call(path)
    assert status == 404
    assert b"not found" in payload


def test_trailing_slash_still_routes(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret")
    status, _, _ = call(f"{TICK_PATH}/")
    assert status == 401
