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
    SETUP_LINK_ACTION, SETUP_LINK_FIELD, SETUP_PATH, WEBHOOK_PATH, Request, Response,
    _setup_page, asgi_dispatch, consume_setup_link, issue_setup_link, save_setup_values,
    setup_link_valid, setup_status, setup_write_allowed, env, matches,
)

log = logging.getLogger("p2p-bot.serverless.setup")


async def _send_link(request: Request) -> Response:
    """The 🔧 button: hand the owner a one-time link, in Telegram.

    The answer — for the browser and for a script — deliberately never contains
    the link itself: it is only ever delivered to the admins, which is what
    makes it safe to let anybody press the button.
    """
    try:
        report = await issue_setup_link()
    except Exception as exc:
        log.warning("reconfigure link could not be issued: %s", exc)
        if request.wants_html:
            return Response.html(
                _setup_page(f"The reconfigure link could not be sent: {exc}", setup_status(),
                            banner=("error", f"⚠️ The reconfigure link could not be sent: {exc}")),
                status=500)
        return Response.json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    minutes = max(1, int(report["expires_in"] // 60))
    if report["ok"] and not report.get("resent", True):
        text = (f"📨 You already have a link — it was sent a moment ago and is still valid "
                f"for {minutes} more minutes. Open it in Telegram, or wait a minute and "
                f"press the button again for a fresh one.")
        log.info("reconfigure link re-requested inside the cooldown — not sent again")
    elif report["ok"]:
        text = (f"📬 Sent to the bot admin(s) in Telegram — the link works once and expires "
                f"in {minutes} minutes. Open it on the same device you use Telegram on.")
        log.info("reconfigure link sent to %s admin(s)", len(report["delivered"]))
    else:
        text = ("⚠️ The bot could not reach Telegram, so no link was delivered "
                f"({'; '.join(report['errors']) or 'unknown error'}). Run "
                "python setup_cli.py in a terminal instead — it writes to the same store.")
    if request.wants_html:
        banner = ("ok" if report["ok"] else "error", text)
        return Response.html(_setup_page("Everything the bot needs is present.", setup_status(),
                                         banner=banner))
    # No URL on purpose: it is the key to the form, and it belongs to the admin.
    return Response.json({"ok": report["ok"], "delivered": report["delivered"],
                          "errors": report["errors"], "expires_in": report["expires_in"],
                          "message": text}, status=200 if report["ok"] else 502)


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
        # Two ways to open a configured deployment's form: SETUP_SECRET, and the
        # one-time link the 🔧 button sends to the admins in Telegram.  Neither
        # is remembered globally — the authorization is only used to render this
        # response, and POST validates it again from scratch.
        secret = request.query.get("secret", "")
        configured_secret = env("SETUP_SECRET")
        if secret and ((configured_secret and matches(secret, configured_secret))
                       or setup_link_valid(secret)):
            status["edit_authorized"] = True
            status["edit_secret"] = secret
        message = (status["message"]
                   or ("Something needs repairing — see the checklist below."
                       if status.get("broken") else "Everything the bot needs is present."))
        return Response.html(_setup_page(message, status))

    if request.method != "POST":
        return Response.json({"ok": False,
                              "error": f"POST the settings form to {SETUP_PATH}, "
                                       "or GET it for the setup page"}, 405)

    # 🔧 Reconfigure — one button, and the link goes to the admins, not to the
    # page.  Handled before the write check: it changes nothing by itself.
    if str(request.submitted().get(SETUP_LINK_FIELD) or "").strip() == SETUP_LINK_ACTION:
        return await _send_link(request)

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
    # A one-time link is spent by the save it authorized, so the link in the
    # admin's chat history cannot be used to change the bot again later.
    if result["ok"]:
        consume_setup_link(str(values.get("SETUP_SECRET") or
                               request.query.get("secret") or "").strip())

    if request.wants_html:
        status = result["status"]
        message = (status["message"]
                   or ("Something needs repairing — see the checklist below."
                       if status.get("broken") else "Everything the bot needs is present."))
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
