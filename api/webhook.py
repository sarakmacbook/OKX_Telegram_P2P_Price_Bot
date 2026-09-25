"""Telegram webhook — ``POST /api/webhook`` (Vercel serverless function).

Telegram delivers every update here, and the payload is handed to the same
handlers the polling bot uses (``bot.register_handlers``).  ``GET`` is the status
page you open right after deploying: it registers the webhook when it is
missing and shows what the bot knows — state store, group, merchants, cron note.

The top-level ``app`` is an ASGI application: Vercel detects that by signature
(``async def app(scope, receive, send)``) and drives it for every request.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root (bot.py)

from serverless import (                                          # noqa: E402
    TELEGRAM_SECRET_HEADER, Request, Response, asgi_dispatch, ensure_webhook,
    get_bot, matches, page, process_update, state_status, webhook_secret,
    webhook_secret_note,
)

log = logging.getLogger("p2p-bot.serverless.webhook")


def _verified(request: Request) -> bool:
    """Only Telegram knows the secret token (set when the webhook is registered).

    Telegram sends it in ``X-Telegram-Bot-Api-Secret-Token``; a ``?secret=``
    query value is accepted as well so the endpoint is easy to test by hand.
    """
    return matches(request.header(TELEGRAM_SECRET_HEADER) or request.query.get("secret", ""),
                   webhook_secret())


async def handle(request: Request) -> Response:
    if request.method == "GET":
        return await _status(request)
    if request.method != "POST":
        return Response.json({"ok": False,
                              "error": "POST a Telegram update here, or GET for the status page"},
                             405)
    if not _verified(request):
        return Response.json({"ok": False, "error": "missing or invalid secret token — this "
                              "endpoint only accepts Telegram updates"}, 403)
    payload = request.json()
    if not payload.get("update_id"):
        return Response.json({"ok": False, "error": "body is not a Telegram update"}, 400)
    await process_update(payload)
    return Response.json({"ok": True})


# ── GET /api/webhook — status page (and one-click webhook registration) ────
async def _status(request: Request) -> Response:
    bot_module = get_bot()
    bot_module.refresh_state()

    check_webhook = request.query.get("register") != "0"
    data: dict = {
        "ok": True,
        "mode": "webhook (Vercel serverless)",
        "pair": f"{bot_module.ASSET}/{bot_module.FIAT}",
        "group": bot_module.state.get("group"),
        "group_title": bot_module.state.get("group_title") or None,
        "merchants": len(bot_module.state.get("merchants") or {}),
        "auto_posting": bool(bot_module.state.get("auto")),
        "state": state_status(),
        "webhook_secret": webhook_secret_note(),
    }
    if check_webhook:
        try:
            data["webhook"] = await ensure_webhook(force=request.query.get("register") == "1")
        except Exception as exc:                  # bad config, network, revoked token …
            log.warning("webhook check failed: %s", exc)
            data["webhook"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        data["webhook"] = "not checked (?register=0)"

    if request.wants_html:
        return Response.html(_status_page(data))
    return Response.json(data)


def _status_page(data: dict) -> str:
    webhook = data.get("webhook") if isinstance(data.get("webhook"), dict) else {}
    state = data.get("state") or {}
    warnings = []
    if state.get("warning"):
        warnings.append(str(state["warning"]))
    if webhook.get("error"):
        warnings.append(f"webhook not registered: {webhook['error']}")
    elif webhook.get("last_error"):
        warnings.append(f"Telegram's last webhook error: {webhook['last_error']}")
    if not data.get("group"):
        warnings.append("no group yet — add the bot to your group and tap 👥 Set group.")
    return page(
        "🤖 P2P Price Bot · Vercel",
        [
            ("Mode", data.get("mode")),
            ("Pair / interval", f"{data.get('pair')} · cron-driven"),
            ("Webhook", webhook.get("url") or (webhook.get("error") or "unknown")),
            ("Pending updates", webhook.get("pending_updates")),
            ("Webhook secret", data.get("webhook_secret")),
            ("State store", f"{state.get('backend')} — "
                            f"{'persistent ✅' if state.get('persistent') else 'NOT persistent ❌'}"),
            ("Group", data.get("group_title") or data.get("group") or "not set"),
            ("Merchants", data.get("merchants")),
            ("Auto posting", data.get("auto_posting")),
        ],
        notes=[
            "Open the bot in Telegram, /start, tap 👥 Set group, then paste a merchant URL."
            if not data.get("group") else
            "Add or remove merchants by sending their profile URL to the bot in Telegram.",
            "Prices are posted when /api/tick runs (see \"crons\" in vercel.json): every minute "
            "on Vercel Pro, once a day on the free Hobby plan.",
            "Register the webhook again at any time: /api/webhook?register=1",
        ],
        warnings=warnings,
    )


async def app(scope, receive, send) -> None:
    """ASGI entry point (Vercel detects ASGI by this signature)."""
    await asgi_dispatch(scope, receive, send, handle)
