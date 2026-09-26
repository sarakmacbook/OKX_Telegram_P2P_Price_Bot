"""The optional channel destination and 📤 auto-forward of admin messages."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore

GROUP, CHANNEL = -100123, -100987
ADMIN = 424242


@pytest.fixture()
def manager(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    # None of these admin operations should query an exchange.
    monkeypatch.setattr(bot, "fetch", AsyncMock(side_effect=AssertionError("Unexpected price fetch")))
    bot.save()
    return bot


def _bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        delete_message=AsyncMock(),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        forward_message=AsyncMock(return_value=SimpleNamespace(message_id=98)),
    )


def _private(bot, text=None, **message):
    msg = SimpleNamespace(text=text, message_id=5, chat_id=1,
                          reply_text=AsyncMock(), reply_html=AsyncMock(), **message)
    return SimpleNamespace(message=msg, effective_message=msg,
                           effective_user=SimpleNamespace(id=ADMIN, is_bot=False),
                           effective_chat=SimpleNamespace(type="private", id=1))


def _callback(bot, data, context=None):
    query = SimpleNamespace(data=data, message=SimpleNamespace(text="", chat_id=ADMIN),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private"),
                             callback_query=query)
    asyncio.run(bot.on_button(update, context or SimpleNamespace(bot=_bot())))
    return update


def _seed(bot, merchant, prices, monkeypatch):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))


# ── 📢 the channel as a second destination ──────────────────────────────────
def test_the_price_post_goes_to_the_group_and_the_channel(manager, merchant, prices, monkeypatch):
    _seed(manager, merchant, prices, monkeypatch)
    manager.state["group"] = GROUP
    manager.state["channel"] = CHANNEL
    fake = _bot()

    assert asyncio.run(manager.post(fake)) is True

    assert [c.args[0] for c in fake.send_message.await_args_list] == [GROUP, CHANNEL]
    assert manager.state["last_msg_id"] and manager.state["channel_last_msg_id"]
    assert manager.chat_of("group") == GROUP and manager.chat_of("channel") == CHANNEL


def test_each_destination_deletes_its_own_previous_message(manager, merchant, prices, monkeypatch):
    _seed(manager, merchant, prices, monkeypatch)
    manager.state["group"], manager.state["channel"] = GROUP, CHANNEL
    fake = _bot()
    asyncio.run(manager.post(fake))
    manager.state["last_msg_id"], manager.state["channel_last_msg_id"] = 21, 22

    assert asyncio.run(manager.post(fake, force=True)) is True
    assert [c.kwargs["chat_id"] for c in fake.delete_message.await_args_list] == [GROUP, CHANNEL]
    assert fake.delete_message.await_count == 2


def test_adding_a_channel_does_not_repost_to_the_group(manager, merchant, prices, monkeypatch):
    _seed(manager, merchant, prices, monkeypatch)
    manager.state["group"] = GROUP
    fake = _bot()
    assert asyncio.run(manager.post(fake)) is True
    assert fake.send_message.await_count == 1

    manager._set_channel(SimpleNamespace(id=CHANNEL, title="Rates channel"))
    assert asyncio.run(manager.post(fake)) is True
    # only the channel is new — the group keeps its message
    assert [c.args[0] for c in fake.send_message.await_args_list] == [GROUP, CHANNEL]


def test_a_failing_channel_does_not_stop_the_group_post(manager, merchant, prices, monkeypatch):
    _seed(manager, merchant, prices, monkeypatch)
    manager.state["group"], manager.state["channel"] = GROUP, CHANNEL
    fake = _bot()

    async def only_group(chat_id, *a, **kw):
        if chat_id == CHANNEL:
            raise RuntimeError("not enough rights to post in the channel")
        return SimpleNamespace(message_id=11)

    fake.send_message = AsyncMock(side_effect=only_group)
    assert asyncio.run(manager.post(fake)) is True
    assert manager.state["last_msg_id"] == 11
    assert manager.state["channel_last_msg_id"] is None


def test_only_a_set_destination_receives_the_post(manager, merchant, prices, monkeypatch):
    _seed(manager, merchant, prices, monkeypatch)
    fake = _bot()
    assert asyncio.run(manager.post(fake)) is False          # neither group nor channel
    assert fake.send_message.await_count == 0


def test_setchannel_registers_the_channel(manager):
    chat = SimpleNamespace(id=CHANNEL, title="Rates channel", type="channel")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=chat, effective_user=None,
                             effective_message=message, message=message)
    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=_bot())))
    assert manager.state["channel"] == CHANNEL
    assert manager.state["channel_title"] == "Rates channel"


def test_setchannel_explains_itself_in_a_private_chat(manager):
    chat = SimpleNamespace(id=1, title=None, type="private")
    message = SimpleNamespace(reply_html=AsyncMock())
    update = SimpleNamespace(effective_chat=chat,
                             effective_user=SimpleNamespace(id=ADMIN, is_bot=False),
                             effective_message=message, message=message)
    asyncio.run(manager.setchannel(update, SimpleNamespace()))
    assert manager.state["channel"] is None
    assert "Set channel" in update.message.reply_html.await_args.args[0]


def test_the_channel_is_unset_when_the_bot_is_removed(manager):
    manager.state["channel"] = CHANNEL
    manager.state["channel_last_msg_id"] = 5
    chat = SimpleNamespace(id=CHANNEL, title="Rates channel", type="channel")
    update = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=chat, from_user=SimpleNamespace(id=ADMIN),
        old_chat_member=SimpleNamespace(status="administrator"),
        new_chat_member=SimpleNamespace(status="left")))
    asyncio.run(manager.on_my_chat_member(update, SimpleNamespace(bot=_bot())))
    assert manager.state["channel"] is None
    assert manager.state["channel_last_msg_id"] is None


def test_the_start_deep_link_sets_a_channel(manager):
    chat = SimpleNamespace(id=CHANNEL, title="Rates channel", type="channel")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=chat,
                             effective_user=SimpleNamespace(id=ADMIN, is_bot=False),
                             effective_message=message, message=message)
    asyncio.run(manager.start(update, SimpleNamespace(args=["setchannel"], bot=_bot())))
    assert manager.state["channel"] == CHANNEL


def test_cleanup_deletes_the_stale_channel_message_too(manager):
    manager.state["group"] = GROUP
    manager.state["channel"] = CHANNEL
    manager.state["last_msg_id"] = 31
    manager.state["channel_last_msg_id"] = 32
    manager.state["last_msg_time"] = manager.state["channel_last_msg_time"] = 1_700_000_000
    fake = _bot()

    assert asyncio.run(manager.cleanup_task(fake)) is True
    assert [c.kwargs["chat_id"] for c in fake.delete_message.await_args_list] == [GROUP, CHANNEL]
    assert manager.state["last_msg_id"] is None and manager.state["channel_last_msg_id"] is None


def test_the_panel_shows_the_channel_and_the_channel_button(manager):
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"
    assert "Rates channel" in manager.panel_text()
    buttons = [b for row in manager.panel().inline_keyboard for b in row]
    assert any("channel" in (b.text or "").lower() for b in buttons)


# ── 📤 auto-forward ────────────────────────────────────────────────────────
def test_a_message_the_menus_do_not_use_is_copied_to_the_group(manager):
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    fake = _bot()
    update = _private(manager, "hello everyone")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_awaited_once()
    assert fake.copy_message.await_args.kwargs["chat_id"] == GROUP
    assert "Sent to" in update.message.reply_html.await_args.args[0]
    assert len(manager.state["forwards"]) == 1


def test_forward_can_go_to_the_group_and_the_channel(manager):
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"
    manager.state["settings"]["forward_target"] = "both"
    fake = _bot()

    asyncio.run(manager.on_text(_private(manager, "hello"), SimpleNamespace(bot=fake)))

    assert [c.kwargs["chat_id"] for c in fake.copy_message.await_args_list] == [GROUP, CHANNEL]


def test_forward_respects_the_selected_destination(manager):
    manager.state["group"] = GROUP
    manager.state["channel"] = CHANNEL
    for target, expected in (("channel", [CHANNEL]), ("group", [GROUP]), ("off", [])):
        manager.state["settings"]["forward_target"] = target
        fake = _bot()
        asyncio.run(manager.on_text(_private(manager, "hello"), SimpleNamespace(bot=fake)))
        assert [c.kwargs["chat_id"] for c in fake.copy_message.await_args_list] == expected


def test_undo_deletes_what_the_forward_created(manager):
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    fake = _bot()
    asyncio.run(manager.on_text(_private(manager, "hello"), SimpleNamespace(bot=fake)))
    token = next(iter(manager.state["forwards"]))

    _callback(manager, f"fwd_undo:{token}", SimpleNamespace(bot=fake))

    fake.delete_message.assert_awaited_once_with(chat_id=GROUP, message_id=99)
    assert manager.state["forwards"] == {}


def test_an_unknown_undo_token_changes_nothing(manager):
    fake = _bot()
    _callback(manager, "fwd_undo:nope", SimpleNamespace(bot=fake))
    assert fake.delete_message.await_count == 0


def test_the_forward_history_stays_small(manager):
    for index in range(manager.MAX_FORWARD_HISTORY + 5):
        manager.remember_forward(f"token{index}", [{"kind": "group", "chat_id": GROUP,
                                                    "message_id": index}])
    assert len(manager.state["forwards"]) == manager.MAX_FORWARD_HISTORY
    assert "token0" not in manager.state["forwards"]


def test_a_merchant_url_is_added_instead_of_forwarded(manager, monkeypatch):
    manager.state["group"] = GROUP
    monkeypatch.setattr(manager, "fetch", AsyncMock(return_value={
        "sell": 1.0, "buy": 1.01, "sell_amount": 100.0, "buy_amount": 90.0,
        "sell_ad_id": "1", "buy_ad_id": "2", "error": None}))
    fake = _bot()

    asyncio.run(manager.on_text(
        _private(manager, "https://www.okx.com/p2p/ads-merchant?publicUserId=abc123"),
        SimpleNamespace(bot=fake)))

    assert fake.copy_message.await_count == 0
    assert manager.state["merchants"]


def test_a_photo_without_a_pending_banner_is_forwarded(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    update = _private(manager, photo=[SimpleNamespace(file_id="AgACAgIAAxkBAAICbig")])

    asyncio.run(manager.on_photo(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_awaited_once()
    assert manager.post_banner() == ""


def test_a_pending_banner_photo_is_still_saved(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    update = _private(manager, photo=[SimpleNamespace(file_id="AgACAgIAAxkBAAICbig")])
    manager.edit_set(update, "awaiting_custom", "banner_photo")

    asyncio.run(manager.on_photo(update, SimpleNamespace(bot=fake)))

    assert fake.copy_message.await_count == 0
    assert manager.post_banner() == "AgACAgIAAxkBAAICbig"


def test_forward_falls_back_to_a_real_forward(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.copy_message = AsyncMock(side_effect=RuntimeError("protected content"))
    update = _private(manager, "hello")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.forward_message.assert_awaited_once()
    assert "Sent to" in update.message.reply_html.await_args.args[0]


def test_a_failed_forward_tells_the_admin(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.copy_message = AsyncMock(side_effect=RuntimeError("blocked"))
    fake.forward_message = AsyncMock(side_effect=RuntimeError("blocked"))
    update = _private(manager, "hello")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    assert any("Nothing was sent" in str(c.args[0])
               for c in update.message.reply_text.await_args_list)


def test_off_forward_keeps_the_merchant_url_hint(manager):
    manager.state["group"] = GROUP
    manager.state["settings"]["forward_target"] = "off"
    fake = _bot()
    update = _private(manager, "just a sentence")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    assert fake.copy_message.await_count == 0
    assert "Not a supported merchant URL" in update.message.reply_text.await_args.args[0]


def test_the_forward_target_button_cycles(manager):
    seen = []
    for _ in range(len(manager.FORWARD_TARGETS) + 1):
        seen.append(manager.forward_target())
        _callback(manager, "toggle_forward_target")
    assert seen == ["group", "channel", "both", "off", "group"]


@pytest.mark.parametrize("target", ["strange", None, 5])
def test_an_unknown_forward_target_falls_back(manager, target):
    manager.state["settings"]["forward_target"] = target
    manager.refresh_state()
    assert manager.forward_target() == "group"
