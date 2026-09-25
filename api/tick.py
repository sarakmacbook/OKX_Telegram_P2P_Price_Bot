"""Cron entry point — ``GET /api/tick`` (Vercel serverless function).

The polling installs let ``bot.py`` schedule its own work in PTB's JobQueue; a
serverless deployment has no such loop, so ``vercel.json`` schedules this
endpoint instead.  Every run

* re-registers the Telegram webhook when it is missing or stale,
* posts the prices when they changed (📊 Post prices now does this on demand),
* deletes the group message once it is older than the configured age.

Protect it with a ``CRON_SECRET`` environment variable: Vercel then calls it with
``Authorization: Bearer <CRON_SECRET>`` and any other caller is rejected
(``?secret=<CRON_SECRET>`` works for manual runs and for external cron services
on the free plan, where Vercel Crons may only run once a day).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root (bot.py)

from serverless import (                                          # noqa: E402
    Request, Response, asgi_dispatch, env, matches, run_tick,
)


def _authorized(request: Request) -> bool:
    """``CRON_SECRET`` protects the endpoint; unset means "open" (documented)."""
    expected = env("CRON_SECRET")
    if not expected:
        return True
    header = request.header("authorization")
    bearer = header[7:] if header.lower().startswith("bearer ") else ""
    return matches(bearer or request.query.get("secret", ""), expected)


async def handle(request: Request) -> Response:
    if request.method not in ("GET", "POST"):
        return Response.json({"ok": False, "error": "GET only"}, 405)
    if not _authorized(request):
        return Response.json({"ok": False, "error": "unauthorized — send "
                              "'Authorization: Bearer <CRON_SECRET>' or '?secret=<CRON_SECRET>'"}, 401)
    result = await run_tick()
    if not env("CRON_SECRET"):
        result["warning"] = ("CRON_SECRET is not set — everybody who knows this URL can "
                             "trigger a post. Set CRON_SECRET in the project's environment "
                             "variables to lock the endpoint down.")
    return Response.json({"ok": True, **result})


async def app(scope, receive, send) -> None:
    """ASGI entry point (Vercel detects ASGI by this signature)."""
    await asgi_dispatch(scope, receive, send, handle)
