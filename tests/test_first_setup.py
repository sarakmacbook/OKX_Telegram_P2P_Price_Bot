"""The first-start setup checklist, and the gates that enforce it.

The bot may start with nothing, but it cannot post prices with nothing: a
destination and at least one merchant are required. These tests cover the
checklist the first ``/start`` sends, the panel button that reopens it, and the
two actions that refuse to run until it is complete.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ADMIN = 424242


def _private(message):
    return SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                           effective_chat=SimpleNamespace(type="private", id=ADMIN),
                           message=message)


def _message():
    return SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())


def _tap(bot, data):
    """Tap a panel button; the message it edits is the one the bot sent."""
    message = _message()
    query = SimpleNamespace(data=data, message=message, answer=AsyncMock(),
                            edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private", id=ADMIN),
                             message=message, callback_query=query)
    asyncio.run(bot.on_button(update, SimpleNamespace(bot=SimpleNamespace())))
    return query, message


def _ready(bot):
    """Complete both required steps: a group and one merchant."""
    bot.state["group"] = -100555
    bot.state["group_title"] = "P2P Prices"
    bot.state["merchants"]["okx:0dec824eed"] = {"sell": 0.999}


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ── the checklist itself ───────────────────────────────────────────────────
def test_a_fresh_bot_reports_both_required_steps_missing(bot):
    assert bot.setup_missing() == ["destination", "merchants"]
    assert bot.setup_ready() is False

    text = bot.setup_text()
    assert "2 required steps left" in text
    assert "1. Where prices go" in text and "2. At least one merchant" in text
    assert text.count("⬜") == 3                   # both steps and the recommended database
    assert "Post prices and 🟢 Auto stay off" in text


def test_a_destination_or_a_merchant_completes_its_step(bot):
    bot.state["channel"] = -100777
    bot.state["channel_title"] = "Rates"
    assert bot.setup_state()["destination"] is True
    assert bot.setup_missing() == ["merchants"]

    bot.state["merchants"]["okx:1"] = {"sell": 1.0}
    assert bot.setup_ready() is True
    assert "Setup complete" in bot.setup_text()


def test_a_database_is_recommended_but_never_required(bot):
    _ready(bot)
    assert bot.setup_ready() is True               # no database connected
    assert bot.setup_state()["database"] is False
    assert "recommended" in bot.setup_text()


def test_the_database_line_is_stronger_on_a_host_that_cannot_remember(bot, monkeypatch):
    monkeypatch.setattr(bot, "on_vercel", lambda: True)

    assert "strongly recommended" in bot.setup_text()


def test_the_checklist_buttons_reach_every_step(bot):
    callbacks = _callbacks(bot.setup_kb())

    assert "setgroup_help" in callbacks and "channel_menu" in callbacks
    assert "setup_merchant" in callbacks and "database" in callbacks
    assert "setup_refresh" in callbacks and "panel" in callbacks
    assert "post" not in callbacks                 # nothing to post yet


def test_the_post_button_appears_once_the_required_steps_are_done(bot):
    _ready(bot)

    assert "post" in _callbacks(bot.setup_kb())
    assert "Finish setup" not in str(bot.panel().inline_keyboard)


def test_the_panel_offers_the_way_back_while_setup_is_incomplete(bot):
    texts = [button.text for row in bot.panel().inline_keyboard for button in row]
    callbacks = _callbacks(bot.panel())

    assert "setup" in callbacks
    assert any("Finish setup (2 left)" in text for text in texts)


# ── the gates ──────────────────────────────────────────────────────────────
def test_post_is_refused_with_the_checklist_instead_of_silently_doing_nothing(bot, monkeypatch):
    posted = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "post", posted)

    query, message = _tap(bot, "post")

    posted.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True
    assert "Finish setup first" in query.answer.await_args.args[0]
    assert "First-time setup" in message.reply_html.await_args.args[0]
    assert _callbacks(message.reply_html.await_args.kwargs["reply_markup"])


def test_post_works_once_the_required_steps_are_done(bot, monkeypatch):
    _ready(bot)
    posted = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "post", posted)

    query, message = _tap(bot, "post")

    posted.assert_awaited_once()
    assert query.answer.await_args.args[0] == "✅ Posted!"
    message.reply_html.assert_not_awaited()


def test_auto_cannot_be_switched_on_before_setup_is_done(bot):
    query, message = _tap(bot, "auto")

    assert bot.state["auto"] is False
    assert query.answer.await_args.kwargs.get("show_alert") is True
    assert "Auto needs" in query.answer.await_args.args[0]
    assert "at least one merchant" in query.answer.await_args.args[0]
    assert "First-time setup" in message.reply_html.await_args.args[0]


def test_auto_switches_on_normally_once_setup_is_done(bot):
    _ready(bot)

    query, _ = _tap(bot, "auto")

    assert bot.state["auto"] is True
    assert query.answer.await_args.args[0] == "Auto ON"


# ── reopening and re-checking the checklist ────────────────────────────────
def test_the_setup_button_edits_the_message_into_the_checklist(bot):
    query, _ = _tap(bot, "setup")

    text = query.edit_message_text.await_args.args[0]
    assert "First-time setup" in text
    assert _callbacks(query.edit_message_text.await_args.kwargs["reply_markup"])


def test_re_check_reflects_a_step_completed_elsewhere(bot):
    query, _ = _tap(bot, "setup")
    bot.state["group"] = -100555
    bot.state["group_title"] = "Prices"

    query, _ = _tap(bot, "setup_refresh")

    assert "1 step(s) still to do" in query.answer.await_args.args[0]
    text = query.edit_message_text.await_args.args[0]
    assert "1 required step left" in text
    assert "at least one merchant" in text.lower() or "At least one merchant" in text


def test_the_add_merchant_screen_says_what_to_paste(bot):
    query, _ = _tap(bot, "setup_merchant")

    text = query.edit_message_text.await_args.args[0]
    assert "public profile link" in text
    assert "okx.com" in text
    callbacks = _callbacks(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert "list" in callbacks and "setup" in callbacks and "panel" in callbacks


def test_the_setup_screen_is_private_only(bot):
    message = _message()
    query = SimpleNamespace(data="setup", message=message, answer=AsyncMock(),
                            edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="group", id=-100555),
                             message=message, callback_query=query)

    asyncio.run(bot.on_button(update, SimpleNamespace(bot=SimpleNamespace())))

    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_awaited()
