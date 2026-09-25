"""The one ASGI entry point of the deployment — every request lands here.

Vercel's Python runtime builds a Python project as a **single application**: it
loads one top-level ``app`` and rewrites every request to it, instead of turning
each file in ``api/`` into a function of its own.  A project that only has
``api/tick.py`` and ``api/webhook.py`` therefore does not build any more::

    Error: No python entrypoint found in default locations, but found potential
    entrypoints:
      api/tick.py (variable: app)
      api/webhook.py (variable: app)

``pyproject.toml`` points the build at this module::

    [tool.vercel]
    entrypoint = "api.app:app"

This file does nothing but route — the endpoints themselves stay where they
were, and their ``handle()`` coroutines are called exactly as before, so the
public URLs are unchanged:

* ``/api/webhook`` → ``api/webhook.py`` (Telegram updates + the status page)
* ``/api/tick``    → ``api/tick.py``    (the cron round)
* ``/``            → 302 to the status page (``vercel.json`` redirects there as
  well; answering it here too means the page opens even when a request reaches
  the function directly)
* anything else    → 404 — with one catch-all function the platform's own 404
  is gone, so this router has to produce it

The per-file ``app`` of the two endpoint modules is kept: it is what lets them
run on their own (``uvicorn api.webhook:app``) and it is what an older runtime
loads when the project is deployed file-by-file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root (bot.py, serverless.py)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import tick as tick_endpoint                   # noqa: E402
from api import webhook as webhook_endpoint             # noqa: E402
from serverless import (                                # noqa: E402
    TICK_PATH, WEBHOOK_PATH, Request, Response, asgi_dispatch,
)


def _route(path: str) -> str:
    """Normalize the path Vercel passes through (``/api/tick/`` → ``/api/tick``)."""
    return path.rstrip("/") or "/"


async def handle(request: Request) -> Response:
    """Hand the request to the endpoint that owns its path."""
    path = _route(request.path)
    if path == WEBHOOK_PATH:
        return await webhook_endpoint.handle(request)
    if path == TICK_PATH:
        return await tick_endpoint.handle(request)
    if path == "/":
        return Response("Redirecting to the status page …", 302,
                        content_type="text/plain; charset=utf-8",
                        headers={"location": WEBHOOK_PATH})
    return Response.json(
        {"ok": False, "path": request.path,
         "error": "not found — this deployment serves "
                  f"{WEBHOOK_PATH} (Telegram updates + status page) and "
                  f"{TICK_PATH} (the cron round)"}, 404)


async def app(scope, receive, send) -> None:
    """ASGI entry point (Vercel detects ASGI by this signature)."""
    await asgi_dispatch(scope, receive, send, handle)
