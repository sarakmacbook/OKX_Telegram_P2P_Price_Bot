"""Telegram webhook — ``POST /api/webhook`` (Vercel serverless function).

Telegram delivers every update here, and the payload is handed to the same
handlers the polling bot uses (``bot.register_handlers``).  ``GET`` is the
first-start setup UI when the deployment is missing its credentials or KV store
(the same page ``/api/setup`` serves, form included); once configured it becomes
the status page you open after deploying. It registers the webhook when it is
missing and shows what the bot knows — state store, group, merchants, cron note.
``GET ?check=1`` additionally fetches every merchant once, so empty or broken
prices can be diagnosed (geo-blocked regions, stale merchant links).

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
    _local_config_has_credentials, config_error_response, env, get_bot, matches,
    page, process_update, setup_status, state_status, webhook_secret,
    webhook_secret_note,
)
from storage import database_link                                 # noqa: E402

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
    # Do this preflight before importing bot.py.  A polling install can exit on
    # missing configuration, but the first browser request on Vercel should get
    # an actionable setup UI rather than an opaque platform error page.
    setup = setup_status()
    if not setup["ready"] and not (not setup["serverless"] and
                                     _local_config_has_credentials()):
        return config_error_response(request, setup["message"], setup)

    bot_module = get_bot()
    bot_module.refresh_state()

    check_webhook = request.query.get("register") != "0"
    data: dict = {
        "ok": True,
        "mode": "webhook (Vercel serverless)",
        "pair": f"{bot_module.ASSET}/{bot_module.FIAT}",
        "region": env("VERCEL_REGION") or None,
        "environment": env("VERCEL_ENV") or None,
        "group": bot_module.state.get("group"),
        "group_title": bot_module.state.get("group_title") or None,
        "merchants": len(bot_module.state.get("merchants") or {}),
        "auto_posting": bool(bot_module.state.get("auto")),
        "state": state_status(),
        "webhook_secret": webhook_secret_note(),
        # Scripts get the same "connect a database" link the page renders.
        "connect_database": database_link(),
    }
    if check_webhook:
        try:
            data["webhook"] = await ensure_webhook(force=request.query.get("register") == "1")
        except Exception as exc:                  # bad config, network, revoked token …
            log.warning("webhook check failed: %s", exc)
            data["webhook"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        data["webhook"] = "not checked (?register=0)"

    if request.query.get("check") == "1":
        data["prices"] = await _price_check(bot_module)

    if request.wants_html:
        return Response.html(_status_page(data))
    return Response.json(data)


async def _price_check(bot_module) -> dict:
    """Fetch every merchant once, so empty or broken prices can be diagnosed.

    ``GET /api/webhook?check=1`` (combine with ``&register=0`` to skip the
    Telegram round-trip): per merchant the sell/buy price or the fetch error.
    """
    prices = await bot_module.get_prices()
    out = {}
    for m in bot_module.merchants():
        r = prices.get(m.key) or {}
        out[m.key] = {
            "exchange": m.exchange,
            "nick": m.nickname or m.merchant_id,
            "sell": r.get("sell"),
            "buy": r.get("buy"),
            "error": r.get("error"),
        }
    return out


# Substrings of exchange errors that mean "this region is blocked, not the merchant".
_GEO_HINTS = ("451", "restricted", "blocked", "forbidden", "not available", "geo")


def _looks_geo_blocked(error: str) -> bool:
    text = (error or "").lower()
    return any(hint in text for hint in _GEO_HINTS)


def _price_check_presentation(prices: dict, region: str | None) -> tuple[list, list]:
    """Rows + warnings for a ``?check=1`` result (used by the HTML page only)."""
    if not prices:
        return ([("Live prices (?check=1)",
                  "no merchants yet — paste a merchant URL to the bot first")], [])
    rows, warnings = [], []
    geo_blocked = empty = 0
    for result in prices.values():
        label = f"💱 {result.get('nick')} ({result.get('exchange')})"
        if result.get("error"):
            rows.append((label, f"⚠️ {result['error']}"))
            if _looks_geo_blocked(str(result["error"])):
                geo_blocked += 1
        elif result.get("sell") is None and result.get("buy") is None:
            rows.append((label, "no ads returned (— / —)"))
            empty += 1
        else:
            rows.append((label, f"🔴 sell {result.get('sell')} · 🟢 buy {result.get('buy')}"))
    where = region or "unknown"
    if geo_blocked:
        warnings.append(
            f"{geo_blocked} exchange(s) refuse requests from this region ({where}): the exchanges "
            "geo-block some countries (Binance answers HTTP 451 from the US). Run the functions in "
            "the EU — \"regions\": [\"fra1\"] in vercel.json — and redeploy.")
    elif empty == len(prices):
        warnings.append(
            "The exchanges returned no ads for any merchant: either the merchant links are stale, "
            "or the APIs are unreachable from this region "
            f"({where} — \"regions\": [\"fra1\"] in vercel.json runs them in the EU).")
    return rows, warnings


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
    rows: list = [
        ("Mode", data.get("mode")),
        ("Pair / interval", f"{data.get('pair')} · cron-driven"),
        ("Region", data.get("region") or "unknown (not on Vercel?)"),
        ("Webhook", webhook.get("url") or (webhook.get("error") or "unknown")),
        ("Pending updates", webhook.get("pending_updates")),
        ("Webhook secret", data.get("webhook_secret")),
        ("State store", f"{state.get('backend')} — "
                        f"{'persistent ✅' if state.get('persistent') else 'NOT persistent ❌'}"),
        ("Group", data.get("group_title") or data.get("group") or "not set"),
        ("Merchants", data.get("merchants")),
        ("Auto posting", data.get("auto_posting")),
    ]
    notes = [
        "Open the bot in Telegram, /start, tap 👥 Set group, then paste a merchant URL."
        if not data.get("group") else
        "Add or remove merchants by sending their profile URL to the bot in Telegram.",
        "Prices are posted when /api/tick runs (see \"crons\" in vercel.json): every minute "
        "on Vercel Pro, once a day on the free Hobby plan.",
        "Register the webhook again at any time: /api/webhook?register=1",
        "Diagnose empty prices: /api/webhook?check=1 fetches every merchant once and shows errors.",
    ]
    links: list[tuple[str, str]] = []
    if not state.get("persistent"):
        notes.append("Make the bot remember anything: Vercel dashboard → Storage → add Upstash for "
                     "Redis → Connect to this project → Redeploy.")
        # The one-click way there, right under the warning that says it is needed.
        links.append(("🔌 Connect database ↗", database_link()))
    # Change the token, the admins, the pair or the database without a redeploy.
    links.append(("⚙️ Reconfigure setup", "/api/setup"))
    links.append(("↻ Check the webhook again", "/api/webhook?register=1"))
    if "prices" in data:
        price_rows, price_warnings = _price_check_presentation(data["prices"] or {},
                                                               data.get("region"))
        rows.extend(price_rows)
        warnings.extend(price_warnings)
    return page("🤖 P2P Price Bot · Vercel", rows, notes=notes, warnings=warnings, links=links)


async def app(scope, receive, send) -> None:
    """ASGI entry point (Vercel detects ASGI by this signature)."""
    await asgi_dispatch(scope, receive, send, handle)
