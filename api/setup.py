"""Setup page and settings form — ``GET|POST /api/setup`` (Vercel serverless).

The browser half of the first-start setup, and the twin of the terminal wizard
(``python setup_cli.py`` — which you can skip with ``--skip`` and finish here
later).  Both write to the same place, ``runtime_config``:

``GET  /api/setup``   the readiness checklist plus a form for what is missing.
                      Rendered whether or not the deployment is ready, so it is
                      also where you *change* a setting later.
``POST /api/setup``   validates the submission, checks the token with Telegram,
                      stores it and applies it to this instance — no redeploy
                      needed.  Locked down by ``serverless.setup_write_allowed``:
                      ``SETUP_SECRET`` when it is set, and closed entirely once
                      the deployment is configured.

``GET /api/webhook`` keeps showing the same page while the deployment cannot
start; this endpoint is the one the form posts to.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root (bot.py)

from serverless import (                                          # noqa: E402
    SETUP_PATH, WEBHOOK_PATH, Request, Response, _setup_page, asgi_dispatch,
    save_setup_values, setup_status, setup_write_allowed,
)

log = logging.getLogger("p2p-bot.serverless.setup")


def _headline(result: dict) -> str:
    """One line summarising a save, for the banner and for scripts."""
    if not result["ok"]:
        return " · ".join(result["errors"])
    saved = ", ".join(result["saved"]) or "the KV/Redis connection"
    missing = result["status"].get("missing") or ""
    if not result["status"]["ready"]:
        return (f"Saved {saved}, but setup is not complete. "
                f"Still missing: {missing or 'required configuration'}. "
                "Complete the checklist below before registering the Telegram webhook.")
    return (f"Saved {saved} — the bot can start now. Open {WEBHOOK_PATH} to register the "
            "Telegram webhook.")


async def handle(request: Request) -> Response:
    if request.method == "GET":
        status = setup_status()
        message = status["message"] or "Everything the bot needs is present."
        return Response.html(_setup_page(message, status))

    if request.method != "POST":
        return Response.json({"ok": False,
                              "error": f"POST the settings form to {SETUP_PATH}, "
                                       "or GET it for the setup page"}, 405)

    allowed, why = setup_write_allowed(request)
    if not allowed:
        log.warning("setup form rejected: %s", why)
        if request.wants_html:
            return Response.html(
                _setup_page(why, setup_status(), banner=("error", why)), status=403)
        return Response.json({"ok": False, "error": why}, 403)

    values = request.submitted()
    result = save_setup_values(values)
    headline = _headline(result)
    log.info("setup save %s: %s", "ok" if result["ok"] else "rejected",
             ", ".join(result["saved"]) or "; ".join(result["errors"]))

    if request.wants_html:
        status = result["status"]
        message = status["message"] or "Everything the bot needs is present."
        banner = (("ok" if status["ready"] and not result["warnings"] else "info")
                  if result["ok"] else "error",
                  headline + "".join(f" ⚠️ {item}" for item in result["warnings"]))
        # Non-secret fields are echoed back so a rejected form is not a dead end;
        # the token and the KV token are never sent to the browser again.
        echo = {name: value for name, value in values.items()
                if name in ("ADMIN_IDS", "ASSET", "FIAT", "INTERVAL", "P2P_STATE_BACKEND",
                            "KV_REST_API_URL")}
        return Response.html(_setup_page(message, status, banner=banner,
                                         form_values=None if result["ok"] else echo),
                             status=200 if result["ok"] else 400)
    return Response.json(result, status=200 if result["ok"] else 400)


async def app(scope, receive, send) -> None:
    """ASGI entry point (Vercel detects ASGI by this signature)."""
    await asgi_dispatch(scope, receive, send, handle)
