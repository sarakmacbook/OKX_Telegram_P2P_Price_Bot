"""Admin-managed buttons: rendering, add/edit/delete flows and persistent drafts."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore


@pytest.fixture()
def manager(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    # None of these admin operations should query an exchange.
    monkeypatch.setattr(bot, "fetch", AsyncMock(side_effect=AssertionError("Unexpected price fetch")))
    bot.save()
    return bot


def _callback(bot, data, user_id=None):
    user_id = user_id if user_id is not None else next(iter(bot.ADMINS))
    query = SimpleNamespace(data=data, message=SimpleNamespace(text="Buy / Sell buttons", chat_id=user_id),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                             effective_chat=SimpleNamespace(type="private"), callback_query=query)
    asyncio.run(bot.on_button(update, SimpleNamespace()))
    return update


def _text(bot, text, user_id=None, chat_type="private"):
    user_id = user_id if user_id is not None else next(iter(bot.ADMINS))
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_html=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                             effective_chat=SimpleNamespace(type=chat_type), message=message)
    asyncio.run(bot.on_text(update, SimpleNamespace()))
    return update


def _extra(ident="support", label="💬 Support", url="https://t.me/support"):
    return {"id": ident, "label": label, "url": url}


def _seed(bot, merchant):
    bot.state["merchants"][merchant.key] = merchant.__dict__


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row] if keyboard else []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_remove_and_restore_builtin_preserves_its_settings(manager, merchant, prices, side):
    _seed(manager, merchant)
    manager.state["settings"][f"btn_{side}_label"] = "My button {PRICE}"
    manager.state["settings"][f"btn_{side}_url"] = "https://example.com/my-profile"
    _callback(manager, f"remove_{side}_button")
    manager.refresh_state()
    assert not manager.builtin_button_enabled(side)
    assert len(_buttons(manager.report_keyboard(prices))) == 1
    # Reusing an old Remove button is idempotent, not a toggle that adds it back.
    _callback(manager, f"remove_{side}_button")
    assert not manager.builtin_button_enabled(side)
    callbacks = [b.callback_data for b in _buttons(manager.buttons_menu_kb())]
    assert f"restore_{side}_button" in callbacks

    _callback(manager, f"restore_{side}_button")
    manager.refresh_state()
    buttons = _buttons(manager.report_keyboard(prices))
    assert len(buttons) == 2
    assert any(b.text.startswith("My button ") and b.url == "https://example.com/my-profile" for b in buttons)


def test_remove_both_can_leave_no_buttons_or_only_custom_buttons(manager, merchant, prices):
    _seed(manager, merchant)
    _callback(manager, "remove_buy_button")
    _callback(manager, "remove_sell_button")
    assert manager.report_keyboard(prices) is None
    assert "No buttons" in manager.buttons_menu_text()
    assert "0.999" in manager.report(prices)  # removing a button does not remove price text

    manager.state["settings"]["extra_buttons"] = [_extra()]
    assert [b.text for b in _buttons(manager.report_keyboard(prices))] == ["💬 Support"]
    manager.state["settings"]["show_buttons"] = False
    assert manager.report_keyboard(prices) is None


def test_extra_buttons_follow_each_merchant_with_two_per_row(manager, merchant, prices):
    other = replace(merchant, merchant_id="other-public-id", url="")
    for item in (merchant, other):
        _seed(manager, item)
    prices[other.key] = dict(prices[merchant.key])
    manager.state["settings"]["extra_buttons"] = [
        _extra("profile", "👤 Profile", "{URL}"), _extra(),
        _extra("website", "Website", "https://example.com"),
    ]
    rows = manager.report_keyboard(prices).inline_keyboard
    assert [len(row) for row in rows] == [2, 2, 1, 2, 2, 1]
    assert rows[1][0].url == merchant.profile_url
    assert rows[4][0].url == other.profile_url
    assert rows[1][1].url == rows[4][1].url == "https://t.me/support"
    manager.state["settings"]["buttons_order"] = "sell_buy"
    assert _buttons(manager.report_keyboard(prices))[0].text.startswith("🔴")


def test_custom_profile_button_does_not_need_buy_or_sell_prices(manager, merchant):
    _seed(manager, merchant)
    manager.state["settings"]["extra_buttons"] = [_extra("profile", "Profile", "{PROFILE_URL}")]
    prices = {merchant.key: {"buy": None, "sell": None, "error": "unavailable"}}
    buttons = _buttons(manager.report_keyboard(prices))
    assert [(b.text, b.url) for b in buttons] == [("Profile", merchant.profile_url)]


def test_missing_profile_skips_only_the_affected_extra_button(manager):
    from exchanges import Merchant

    merchant = Merchant("bybit", "123", url="")
    _seed(manager, merchant)
    manager.state["settings"]["extra_buttons"] = [
        _extra("profile", "Profile", "{URL}"), _extra()]
    buttons = _buttons(manager.report_keyboard({merchant.key: {"error": "unavailable"}}))
    assert [b.text for b in buttons] == ["💬 Support"]


def test_keyboard_is_capped_and_has_no_empty_rows(manager, merchant, prices):
    manager.state["settings"]["extra_buttons"] = [_extra(str(i)) for i in range(manager.MAX_EXTRA_BUTTONS)]
    for i in range(15):
        item = replace(merchant, merchant_id=f"merchant-{i}")
        _seed(manager, item)
        prices[item.key] = dict(prices[merchant.key])
    keyboard = manager.report_keyboard(prices)
    assert len(_buttons(keyboard)) == manager.MAX_REPORT_BUTTONS
    assert all(1 <= len(row) <= 2 for row in keyboard.inline_keyboard)


def test_add_button_persists_each_step_and_only_publishes_when_complete(manager, merchant, prices):
    _seed(manager, merchant)
    start = _callback(manager, "extra_add")
    manager.refresh_state()
    assert manager.edit_get(start, "awaiting_custom") == "extra_add_label"
    label = _text(manager, "👤 My profile")
    assert manager.extra_buttons() == []
    # Simulate a new serverless invocation between label and URL messages.
    manager.state.clear()
    manager.refresh_state()
    assert manager.edit_get(label, "extra_button_label") == "👤 My profile"
    assert manager.edit_get(label, "awaiting_custom") == "extra_add_url"
    done = _text(manager, "{URL}")
    manager.refresh_state()
    button, = manager.extra_buttons()
    assert button["label"] == "👤 My profile" and button["url"] == "{URL}"
    assert len(button["id"]) == 12
    assert manager.edit_get(done, "awaiting_custom") is None
    assert manager.edit_get(done, "extra_button_label") is None
    assert _buttons(manager.report_keyboard(prices))[-1].url == merchant.profile_url
    assert done.message.reply_html.await_count == 1


@pytest.mark.parametrize("bad_label", ["", "   ", "a" * 61, "bad\x00label", "bad\x7flabel"])
def test_invalid_label_stays_in_label_step(manager, bad_label):
    _callback(manager, "extra_add")
    update = _text(manager, bad_label)
    assert manager.edit_get(update, "awaiting_custom") == "extra_add_label"
    assert manager.extra_buttons() == []
    assert update.message.reply_text.await_count == 1


@pytest.mark.parametrize("bad_url", [
    "", "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "https://",
    "https://bad host/path", "https://example.com/\nnext", "https://example.com/\x7f", "https://example.com:bad",
    "https://[invalid", "https://name:password@example.com", "https://example.com/{UNKNOWN}",
    "https://example.com/" + "a" * 2048,
])
def test_invalid_url_does_not_save_partial_button(manager, bad_url):
    _callback(manager, "extra_add")
    _text(manager, "Support")
    update = _text(manager, bad_url)
    assert manager.edit_get(update, "awaiting_custom") == "extra_add_url"
    assert manager.extra_buttons() == []
    assert update.message.reply_text.await_count == 1
    _text(manager, "https://t.me/support")
    assert len(manager.extra_buttons()) == 1


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/path?a=1&b=2",
                                 "tg://resolve?domain=support", "{URL}", "{PROFILE_URL}"])
def test_supported_urls(manager, url):
    assert manager.valid_extra_button_url(url)


@pytest.mark.parametrize("cancel_action", ["command", "button", "menu"])
def test_cancel_clears_the_unfinished_draft(manager, cancel_action):
    _callback(manager, "extra_add")
    update = _text(manager, "Unfinished")
    if cancel_action == "command":
        asyncio.run(manager.cancel_cmd(update, SimpleNamespace()))
    else:
        _callback(manager, "cancel_edit" if cancel_action == "button" else "buttons_menu")
    manager.refresh_state()
    assert manager.extra_buttons() == []
    assert manager.edit_get(update, "awaiting_custom") is None
    assert manager.edit_get(update, "extra_button_label") is None


def test_switching_to_another_editor_discards_the_add_draft(manager):
    _callback(manager, "extra_add")
    update = _text(manager, "Unfinished")
    _callback(manager, "edit_buy_label")
    assert manager.edit_get(update, "extra_button_label") is None
    assert manager.edit_get(update, "awaiting_custom") == "buy_label"


def test_missing_draft_does_not_create_an_empty_button(manager):
    update = _callback(manager, "extra_add")
    manager.edit_set(update, "awaiting_custom", "extra_add_url")
    _text(manager, "https://example.com")
    assert manager.extra_buttons() == []
    assert manager.edit_get(update, "awaiting_custom") is None


def test_edit_and_delete_extra_button(manager):
    manager.state["settings"]["extra_buttons"] = [_extra(), _extra("keep", "Keep me")]
    _callback(manager, "extra_label:support")
    _text(manager, "  Help  desk  ")
    _callback(manager, "extra_url:support")
    _text(manager, "https://example.com/help")
    manager.refresh_state()
    assert manager.extra_button("support") == _extra("support", "Help desk", "https://example.com/help")

    confirm = _callback(manager, "extra_remove:support")
    assert manager.extra_button("support") is not None  # asking is not deleting
    assert "extra_delete:support" in [b.callback_data for b in
        _buttons(confirm.callback_query.edit_message_text.call_args.kwargs["reply_markup"])]
    _callback(manager, "extra_button:support")  # Keep button
    assert manager.extra_button("support") is not None
    _callback(manager, "extra_delete:support")
    manager.refresh_state()
    assert [b["id"] for b in manager.extra_buttons()] == ["keep"]
    stale = _callback(manager, "extra_delete:support")
    assert stale.callback_query.answer.call_args.kwargs["show_alert"] is True
    assert [b["id"] for b in manager.extra_buttons()] == ["keep"]


def test_deleted_button_cannot_be_resurrected_by_an_old_edit(manager):
    manager.state["settings"]["extra_buttons"] = [_extra()]
    _callback(manager, "extra_label:support")
    manager.state["settings"]["extra_buttons"] = []  # another admin removed it
    update = _text(manager, "Old edit")
    assert manager.extra_buttons() == []
    assert manager.edit_get(update, "awaiting_custom") is None


def test_add_limit_is_checked_at_start_and_at_save(manager):
    full = [_extra(str(i)) for i in range(manager.MAX_EXTRA_BUTTONS)]
    manager.state["settings"]["extra_buttons"] = full
    denied = _callback(manager, "extra_add")
    assert denied.callback_query.answer.call_args.kwargs["show_alert"] is True
    assert manager.edit_get(denied, "awaiting_custom") is None
    manager.state["settings"]["extra_buttons"] = full[:-1]
    _callback(manager, "extra_add")
    _text(manager, "One more")
    manager.state["settings"]["extra_buttons"] = full  # another admin took the last slot
    update = _text(manager, "https://example.com")
    assert manager.extra_buttons() == full
    assert manager.edit_get(update, "awaiting_custom") is None


def test_reset_requires_confirmation_when_it_would_delete_extras(manager):
    manager.state["settings"].update({"extra_buttons": [_extra()], "btn_buy_enabled": False,
                                       "btn_sell_enabled": False, "show_buttons": False,
                                       "btn_link_mode": "ad"})
    _callback(manager, "reset_buttons")
    assert len(manager.extra_buttons()) == 1
    assert not manager.builtin_button_enabled("buy")
    _callback(manager, "buttons_menu")  # cancel reset
    assert len(manager.extra_buttons()) == 1
    _callback(manager, "reset_buttons_confirm")
    manager.refresh_state()
    assert manager.extra_buttons() == []
    assert manager.builtin_button_enabled("buy") and manager.builtin_button_enabled("sell")
    assert manager.get_settings()["show_buttons"] is True
    assert manager.link_mode() == "profile"


@pytest.mark.parametrize("action", ["extra_add", "remove_buy_button", "extra_delete:support",
                                    "reset_buttons_confirm"])
def test_non_admin_cannot_manage_buttons(manager, action):
    manager.state["settings"]["extra_buttons"] = [_extra()]
    before = deepcopy(manager.state)
    update = _callback(manager, action, user_id=-999)
    assert manager.state == before
    assert update.callback_query.edit_message_text.await_count == 0


def test_label_or_link_input_is_private_and_admin_only(manager):
    start = _callback(manager, "extra_add")
    before = deepcopy(manager.state)
    _text(manager, "Not an admin", user_id=-999)
    _text(manager, "Wrong chat", chat_type="group")
    assert manager.state == before
    assert manager.edit_get(start, "awaiting_custom") == "extra_add_label"


def test_two_admins_have_independent_persistent_drafts(manager, monkeypatch):
    monkeypatch.setattr(manager, "ADMINS", {424242, 434343})
    for user, label in ((424242, "Profile"), (434343, "Support")):
        _callback(manager, "extra_add", user)
        _text(manager, label, user)
    manager.refresh_state()
    _text(manager, "https://t.me/support", 434343)
    manager.refresh_state()
    _text(manager, "{URL}", 424242)
    assert [(b["label"], b["url"]) for b in manager.extra_buttons()] == [
        ("Support", "https://t.me/support"), ("Profile", "{URL}")]


def test_saved_state_is_migrated_and_sanitized_without_shared_defaults(manager):
    manager.STORE.save({"settings": {"btn_buy_enabled": "bad", "extra_buttons": "bad"}})
    manager.refresh_state()
    assert manager.builtin_button_enabled("buy") and manager.builtin_button_enabled("sell")
    assert manager.extra_buttons() == []
    first, second = manager.empty_state(), manager.empty_state()
    first["settings"]["extra_buttons"].append(_extra())
    assert second["settings"]["extra_buttons"] == manager.DEFAULT_SETTINGS["extra_buttons"] == []


def test_malformed_extra_records_do_not_break_rendering_or_menus(manager, merchant, prices):
    _seed(manager, merchant)
    manager.state["settings"]["extra_buttons"] = [
        None, "bad", {}, _extra("invalid:id"), _extra("empty", ""),
        _extra("long", "x" * 61), _extra("badurl", url="javascript:alert(1)"),
        _extra(), _extra(label="Duplicate ID"),
    ]
    assert manager.extra_buttons() == [_extra()]
    assert len(_buttons(manager.report_keyboard(prices))) == 3
    assert manager.extra_buttons_text()
    assert manager.buttons_menu_text()


def test_admin_menus_escape_user_text_and_keep_callback_data_short(manager):
    button = _extra("a" * 24, "<Support> & help", "https://example.com/?a=1&b=2")
    manager.state["settings"]["extra_buttons"] = [button]
    assert "&lt;Support&gt; &amp; help" in manager.extra_button_text(button)
    assert "?a=1&amp;b=2" in manager.extra_button_text(button)
    for kb in (manager.buttons_menu_kb(), manager.extra_buttons_kb(), manager.extra_button_kb(button["id"])):
        assert all(0 < len(b.callback_data.encode()) <= 64 for b in _buttons(kb))


def test_only_button_changes_trigger_a_new_price_post(manager, merchant, prices, monkeypatch):
    _seed(manager, merchant)
    manager.state["group"] = -100123
    manager.state["settings"]["auto_delete"] = False
    monkeypatch.setattr(manager, "get_prices", AsyncMock(return_value=prices))
    telegram = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
    assert asyncio.run(manager.post(telegram)) is True
    assert asyncio.run(manager.post(telegram)) is False
    manager.state["settings"]["extra_buttons"] = [_extra()]
    assert asyncio.run(manager.post(telegram)) is True
    manager.state["settings"]["extra_buttons"][0]["label"] = "New label"
    assert manager.state["last"]["_extra_buttons"][0]["label"] == "💬 Support"
    assert asyncio.run(manager.post(telegram)) is True
    manager.state["settings"]["extra_buttons"][0]["url"] = "https://example.com"
    assert asyncio.run(manager.post(telegram)) is True
    manager.state["settings"]["btn_buy_enabled"] = False
    assert asyncio.run(manager.post(telegram)) is True
    manager.state["settings"]["extra_buttons"] = []
    assert asyncio.run(manager.post(telegram)) is True
    assert asyncio.run(manager.post(telegram)) is False
    assert telegram.send_message.await_count == 6
    assert len(_buttons(telegram.send_message.call_args.kwargs["reply_markup"])) == 1


def test_adding_while_hidden_does_not_enable_buttons_silently(manager):
    manager.state["settings"]["show_buttons"] = False
    _callback(manager, "extra_add")
    _text(manager, "Support")
    done = _text(manager, "https://t.me/support")
    assert manager.get_settings()["show_buttons"] is False
    assert "All buttons are OFF" in done.message.reply_html.call_args.args[0]
