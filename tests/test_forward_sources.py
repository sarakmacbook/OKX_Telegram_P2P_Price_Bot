"""↪️ Choosing *which* channel or group the bot forwards from.

The relay used to read exactly one chat — the channel the price post goes to.
These tests cover the selection itself (commands, deep links, menu), the relay
that follows it, and the two ways a source can disappear again.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore

GROUP, CHANNEL = -100123, -100987
NEWS_CHANNEL, SIGNALS_GROUP = -100555, -100777
ADMIN, STRANGER = 424242, 999


@pytest.fixture()
def manager(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    monkeypatch.setattr(bot, "fetch", AsyncMock(side_effect=AssertionError("Unexpected price fetch")))
    bot.state["group"], bot.state["group_title"] = GROUP, "Rates"
    bot.save()
    return bot


def _bot():
    return SimpleNamespace(
        id=123456,
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        forward_message=AsyncMock(return_value=SimpleNamespace(message_id=98)),
        delete_message=AsyncMock(),
        # ➕ adding a source by link: the bot looks the chat up itself
        get_chat=AsyncMock(return_value=_chat(NEWS_CHANNEL, "channel", "News")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
    )


def _private_text(text, message_id=5, **message):
    """A message an admin sent to the bot in their private chat."""
    msg = SimpleNamespace(text=text, caption=None, message_id=message_id, chat_id=ADMIN,
                          reply_text=AsyncMock(), reply_html=AsyncMock(), **message)
    return SimpleNamespace(message=msg, effective_message=msg,
                           effective_user=SimpleNamespace(id=ADMIN, is_bot=False),
                           effective_chat=SimpleNamespace(id=ADMIN, type="private"), args=None)


def _chat(chat_id, chat_type="channel", title="Source"):
    return SimpleNamespace(id=chat_id, type=chat_type, title=title)


def _update(chat_id, chat_type="channel", message_id=34, title="Source", user=None,
            text=None, caption=None):
    """A message (or channel post) sent inside ``chat_id``."""
    message = SimpleNamespace(message_id=message_id, text=text, caption=caption,
                              reply_html=AsyncMock(return_value=SimpleNamespace(message_id=77)),
                              reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message, message=message,
                           effective_chat=_chat(chat_id, chat_type, title),
                           effective_user=user, args=None)


def _callback(manager, data, fake=None):
    fake = fake or _bot()
    query = SimpleNamespace(data=data,
                            message=SimpleNamespace(text="", chat_id=ADMIN,
                                                    reply_text=AsyncMock(), reply_html=AsyncMock()),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private"),
                             callback_query=query)
    asyncio.run(manager.on_button(update, SimpleNamespace(bot=fake)))
    return query


# ── the selection ──────────────────────────────────────────────────────────
def test_without_a_selection_the_configured_channel_is_relayed(manager):
    manager.state["channel"] = CHANNEL

    assert manager.forward_sources() == {}
    assert manager.is_forward_source(CHANNEL) is True
    assert manager.is_forward_source(NEWS_CHANNEL) is False
    assert manager.forward_source_summary() == "the channel"


def test_a_selected_group_is_relayed_into_the_group(manager):
    assert manager.add_forward_source(SIGNALS_GROUP, "Signals", "supergroup") is True
    fake = _bot()

    assert asyncio.run(manager.on_source_message(
        _update(SIGNALS_GROUP, "supergroup", title="Signals"), SimpleNamespace(bot=fake))) is True
    fake.copy_message.assert_awaited_once()
    assert fake.copy_message.await_args.kwargs["chat_id"] == GROUP
    assert fake.copy_message.await_args.kwargs["caption"] == "👥 <b>Signals</b>"


def test_a_selected_channel_is_relayed_too(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()

    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News"), SimpleNamespace(bot=fake))) is True
    fake.copy_message.assert_awaited_once()
    assert fake.copy_message.await_args.kwargs["caption"] == "📢 <b>News</b>"


def test_selecting_a_source_replaces_the_channel_default(manager):
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"
    manager.add_forward_source(SIGNALS_GROUP, "Signals", "group")
    fake = _bot()

    assert manager.is_forward_source(CHANNEL) is False
    assert asyncio.run(manager.on_channel_post(
        _update(CHANNEL, title="Rates channel"), SimpleNamespace(bot=fake))) is False
    fake.copy_message.assert_not_awaited()
    fake.forward_message.assert_not_awaited()


# ── every relayed message carries the source chat's name ───────────────────
def test_relayed_text_carries_the_source_name_above_it(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()

    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News", text="Bitcoin pumps"),
        SimpleNamespace(bot=fake))) is True

    assert fake.send_message.await_args.kwargs["chat_id"] == GROUP
    assert fake.send_message.await_args.kwargs["text"] == "📢 <b>News</b>\n\nBitcoin pumps"
    fake.forward_message.assert_not_awaited()


def test_relayed_media_keeps_its_caption_below_the_name(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()

    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News", caption="chart attached"),
        SimpleNamespace(bot=fake))) is True

    assert fake.copy_message.await_args.kwargs["caption"] == "📢 <b>News</b>\n\nchart attached"


def test_the_stored_title_names_the_relay_when_the_update_has_none(manager):
    """The classic default (nothing selected) still shows the channel name."""
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"
    fake = _bot()

    asyncio.run(manager.on_channel_post(
        _update(CHANNEL, title=""), SimpleNamespace(bot=fake)))

    assert fake.copy_message.await_args.kwargs["caption"] == "📢 <b>Rates channel</b>"


def test_the_source_name_is_html_escaped(manager):
    manager.add_forward_source(NEWS_CHANNEL, "R&D <news>", "channel")
    fake = _bot()

    asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="R&D <news>"), SimpleNamespace(bot=fake)))

    assert fake.copy_message.await_args.kwargs["caption"] == "📢 <b>R&amp;D &lt;news&gt;</b>"


def test_a_real_forward_is_the_fallback_when_the_copy_fails(manager):
    """"Forwarded from" then shows the name — stickers, polls, protected posts."""
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()
    fake.copy_message = AsyncMock(side_effect=RuntimeError("can't copy"))

    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News"), SimpleNamespace(bot=fake))) is True
    fake.forward_message.assert_awaited_once_with(
        chat_id=GROUP, from_chat_id=NEWS_CHANNEL, message_id=34)


def test_several_chats_are_relayed_at_the_same_time(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    manager.add_forward_source(SIGNALS_GROUP, "Signals", "group")
    manager.state["channel"] = CHANNEL
    manager.add_forward_source(CHANNEL, "Rates channel", "channel")
    fake = _bot()

    for chat_id in (NEWS_CHANNEL, SIGNALS_GROUP, CHANNEL):
        assert manager.is_forward_source(chat_id) is True
    assert manager.forward_source_summary() == "3 chats"
    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News"), SimpleNamespace(bot=fake))) is True


def test_the_destination_group_can_never_be_a_source(manager):
    manager.add_forward_source(GROUP, "Rates", "supergroup")

    # relaying a group into itself is pointless — and it would steal the group's
    # messages from the 🛡 anti-scam handler
    assert manager.is_forward_source(GROUP) is False


def test_the_limit_keeps_the_menu_usable(manager):
    for index in range(manager.MAX_FORWARD_SOURCES):
        assert manager.add_forward_source(-200000 - index, f"Chat {index}", "channel") is True
    assert manager.add_forward_source(-300000, "One too many", "channel") is False
    assert len(manager.forward_sources()) == manager.MAX_FORWARD_SOURCES


def test_a_broken_source_record_is_dropped(manager):
    manager.state["forward_sources"] = {
        str(NEWS_CHANNEL): {"chat_id": NEWS_CHANNEL, "title": "News", "type": "channel"},
        "nonsense": 5,
        str(SIGNALS_GROUP): {"chat_id": "abc", "title": "Signals", "type": "group"},
        "-1": {"chat_id": -1, "title": "No type", "type": "dm"},
    }
    manager.save()

    manager.refresh_state()

    assert list(manager.forward_sources()) == [str(NEWS_CHANNEL)]


# ── /forwardfrom and /stopforward ──────────────────────────────────────────
def test_forwardfrom_selects_the_chat_it_is_sent_in(manager):
    fake = _bot()
    update = _update(SIGNALS_GROUP, "supergroup", title="Signals",
                     user=SimpleNamespace(id=ADMIN, is_bot=False))

    asyncio.run(manager.forwardfrom(update, SimpleNamespace(bot=fake)))

    assert manager.source_selected(SIGNALS_GROUP) is True
    assert manager.forward_sources()[str(SIGNALS_GROUP)]["title"] == "Signals"
    assert "forwarded to" in update.message.reply_html.await_args.args[0]
    # the admin is told in the private chat as well
    assert any("Signals" in str(call.args[1]) for call in fake.send_message.await_args_list)


def test_forwardfrom_works_as_a_channel_post_without_a_sender(manager):
    fake = _bot()
    asyncio.run(manager.forwardfrom(_update(NEWS_CHANNEL, title="News"), SimpleNamespace(bot=fake)))
    assert manager.source_selected(NEWS_CHANNEL) is True


def test_forwardfrom_ignores_a_stranger(manager):
    fake = _bot()
    update = _update(SIGNALS_GROUP, "supergroup", title="Signals",
                     user=SimpleNamespace(id=STRANGER, is_bot=False))

    asyncio.run(manager.forwardfrom(update, SimpleNamespace(bot=fake)))

    assert manager.forward_sources() == {}
    update.message.reply_html.assert_not_awaited()
    fake.send_message.assert_not_awaited()


def test_forwardfrom_needs_a_destination_group(manager):
    manager.state["group"] = None
    update = _update(NEWS_CHANNEL, title="News")

    asyncio.run(manager.forwardfrom(update, SimpleNamespace(bot=_bot())))

    assert manager.forward_sources() == {}
    assert "Set the destination group" in update.message.reply_html.await_args.args[0]


def test_forwardfrom_refuses_the_destination_group(manager):
    update = _update(GROUP, "supergroup", title="Rates")

    asyncio.run(manager.forwardfrom(update, SimpleNamespace(bot=_bot())))

    assert manager.forward_sources() == {}
    assert "forwarded" in update.message.reply_html.await_args.args[0]


def test_forwardfrom_twice_keeps_a_single_entry(manager):
    asyncio.run(manager.forwardfrom(_update(NEWS_CHANNEL, title="News"),
                                    SimpleNamespace(bot=_bot())))
    second = _update(NEWS_CHANNEL, title="News")

    asyncio.run(manager.forwardfrom(second, SimpleNamespace(bot=_bot())))

    assert len(manager.forward_sources()) == 1
    assert "Already forwarding" in second.message.reply_html.await_args.args[0]


def test_forwardfrom_switches_the_relay_on(manager):
    manager.state["settings"]["channel_to_group"] = False

    asyncio.run(manager.forwardfrom(_update(NEWS_CHANNEL, title="News"),
                                    SimpleNamespace(bot=_bot())))

    assert manager.channel_to_group_enabled() is True


def test_stopforward_deselects_the_chat(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    update = _update(NEWS_CHANNEL, title="News")

    asyncio.run(manager.stopforward(update, SimpleNamespace(bot=_bot())))

    assert manager.source_selected(NEWS_CHANNEL) is False
    assert "Stopped forwarding" in update.message.reply_html.await_args.args[0]


def test_stopforward_in_an_unselected_chat_changes_nothing(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    update = _update(SIGNALS_GROUP, "group", title="Signals")

    asyncio.run(manager.stopforward(update, SimpleNamespace(bot=_bot())))

    assert manager.source_selected(NEWS_CHANNEL) is True
    assert "not selected" in update.message.reply_html.await_args.args[0]


def test_the_deep_link_payload_selects_the_chat(manager):
    """``t.me/bot?startchannel=forwardfrom`` arrives as ``/start forwardfrom``."""
    update = _update(NEWS_CHANNEL, title="News")

    asyncio.run(manager.start(update, SimpleNamespace(bot=_bot(), args=["forwardfrom"])))

    assert manager.source_selected(NEWS_CHANNEL) is True
    assert manager.state["channel"] is None      # it is a source, not a destination


def test_the_group_deep_link_payload_selects_the_group(manager):
    update = _update(SIGNALS_GROUP, "supergroup", title="Signals",
                     user=SimpleNamespace(id=ADMIN, is_bot=False))

    asyncio.run(manager.start(update, SimpleNamespace(bot=_bot(), args=["forwardfrom"])))

    assert manager.source_selected(SIGNALS_GROUP) is True
    assert manager.state["group"] == GROUP       # the destination stayed as it was


def test_the_old_deep_links_still_set_a_destination(manager):
    channel = _update(CHANNEL, title="Rates channel")
    asyncio.run(manager.start(channel, SimpleNamespace(bot=_bot(), args=["setchannel"])))
    assert manager.state["channel"] == CHANNEL

    group = _update(-100444, "supergroup", title="Other", user=SimpleNamespace(id=ADMIN))
    asyncio.run(manager.start(group, SimpleNamespace(bot=_bot(), args=["setgroup"])))
    assert manager.state["group"] == -100444


def test_an_unknown_deep_link_payload_is_ignored(manager):
    update = _update(NEWS_CHANNEL, title="News")

    # a channel cannot be the group, so this payload means nothing here
    asyncio.run(manager.start(update, SimpleNamespace(bot=_bot(), args=["setgroup"])))

    assert manager.forward_sources() == {} and manager.state["channel"] is None


def test_the_bot_does_not_relay_its_own_answer(manager):
    asyncio.run(manager.forwardfrom(_update(NEWS_CHANNEL, title="News"),
                                    SimpleNamespace(bot=_bot())))
    fake = _bot()

    # the ✅ confirmation the bot just posted into that very channel
    echoed = _update(NEWS_CHANNEL, title="News", message_id=77)
    assert asyncio.run(manager.on_channel_post(echoed, SimpleNamespace(bot=fake))) is False
    fake.forward_message.assert_not_awaited()

    # anything else in there is relayed as usual
    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News", message_id=78), SimpleNamespace(bot=fake))) is True


def test_a_message_of_the_bot_itself_is_not_relayed(manager):
    manager.add_forward_source(SIGNALS_GROUP, "Signals", "group")
    fake = _bot()
    update = _update(SIGNALS_GROUP, "group", title="Signals",
                     user=SimpleNamespace(id=fake.id, is_bot=True))
    update.effective_message.from_user = update.effective_user

    assert asyncio.run(manager.on_source_message(update, SimpleNamespace(bot=fake))) is False
    fake.forward_message.assert_not_awaited()


def test_forwarding_stops_when_the_switch_is_off(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    manager.state["settings"]["channel_to_group"] = False
    fake = _bot()

    assert asyncio.run(manager.on_channel_post(
        _update(NEWS_CHANNEL, title="News"), SimpleNamespace(bot=fake))) is False
    fake.forward_message.assert_not_awaited()


def test_a_source_the_bot_cannot_read_is_dropped_when_it_leaves(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()
    update = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=_chat(NEWS_CHANNEL, "channel", "News"), from_user=SimpleNamespace(id=ADMIN),
        old_chat_member=SimpleNamespace(status="administrator"),
        new_chat_member=SimpleNamespace(status="left")))

    asyncio.run(manager.on_my_chat_member(update, SimpleNamespace(bot=fake)))

    assert manager.source_selected(NEWS_CHANNEL) is False
    assert any("no longer forwarded" in str(call.args[1])
               for call in fake.send_message.await_args_list)


def test_leaving_a_destination_still_unsets_it(manager):
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"
    manager.add_forward_source(CHANNEL, "Rates channel", "channel")
    update = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=_chat(CHANNEL, "channel", "Rates channel"), from_user=SimpleNamespace(id=ADMIN),
        old_chat_member=SimpleNamespace(status="administrator"),
        new_chat_member=SimpleNamespace(status="kicked")))

    asyncio.run(manager.on_my_chat_member(update, SimpleNamespace(bot=_bot())))

    assert manager.state["channel"] is None
    assert manager.source_selected(CHANNEL) is False


# ── the menu ───────────────────────────────────────────────────────────────
def test_the_settings_menu_links_to_the_source_picker(manager):
    buttons = [button for row in manager.settings_kb().inline_keyboard for button in row]
    picker = next(button for button in buttons if button.callback_data == "forward_sources")
    assert "Forward from" in picker.text
    assert manager.forward_source_summary() in picker.text


def test_the_picker_lists_every_source_and_the_ways_to_add_one(manager, monkeypatch):
    monkeypatch.setattr(manager, "BOT_USERNAME", "p2p_test_bot")
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    manager.add_forward_source(SIGNALS_GROUP, "Signals", "group")
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"

    text, keyboard = manager.forward_sources_text(), manager.forward_sources_kb()
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "News" in text and "Signals" in text and "Rates" in text
    assert f"fwd_src_del:{NEWS_CHANNEL}" in data and f"fwd_src_del:{SIGNALS_GROUP}" in data
    assert "fwd_src_use_channel" in data          # the price channel is one tap away
    assert any(button.url and "startchannel=forwardfrom" in button.url
               for row in keyboard.inline_keyboard for button in row)
    assert any(button.url and "startgroup=forwardfrom" in button.url
               for row in keyboard.inline_keyboard for button in row)


def test_tapping_a_source_removes_it(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")

    query = _callback(manager, f"fwd_src_del:{NEWS_CHANNEL}")

    assert manager.source_selected(NEWS_CHANNEL) is False
    query.edit_message_text.assert_awaited_once()


def test_the_price_channel_can_be_selected_with_one_tap(manager):
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"

    _callback(manager, "fwd_src_use_channel")

    assert manager.source_selected(CHANNEL) is True
    assert manager.forward_sources()[str(CHANNEL)]["title"] == "Rates channel"


def test_the_picker_can_switch_forwarding_off_and_on(manager):
    assert manager.channel_to_group_enabled() is True
    _callback(manager, "fwd_src_toggle")
    assert manager.channel_to_group_enabled() is False
    _callback(manager, "fwd_src_toggle")
    assert manager.channel_to_group_enabled() is True


def test_an_unknown_source_id_is_refused(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")

    query = _callback(manager, "fwd_src_del:not-a-chat")

    assert manager.source_selected(NEWS_CHANNEL) is True
    query.edit_message_text.assert_not_awaited()


def test_the_picker_explains_itself_when_the_bot_has_no_username(manager):
    _callback(manager, "fwd_src_help")
    assert manager.source_selected(NEWS_CHANNEL) is False


def test_the_panel_and_the_settings_show_where_it_forwards_from(manager):
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")

    assert "News" in manager.panel_text()
    assert "News" in manager.settings_text()


# ── ➕ a chat named by link (public) or by a forwarded post (private) ───────
@pytest.mark.parametrize("value,expected", [
    ("https://t.me/Newsroom", {"kind": "username", "username": "Newsroom"}),
    ("t.me/newsroom", {"kind": "username", "username": "newsroom"}),
    ("@newsroom", {"kind": "username", "username": "newsroom"}),
    ("https://t.me/newsroom/12", {"kind": "username", "username": "newsroom"}),
    ("https://t.me/s/newsroom", {"kind": "username", "username": "newsroom"}),
    ("https://telegram.me/newsroom", {"kind": "username", "username": "newsroom"}),
    ("https://t.me/+AbCdEf12", {"kind": "invite", "hash": "AbCdEf12"}),
    ("https://t.me/joinchat/AbCdEf12", {"kind": "invite", "hash": "AbCdEf12"}),
    ("https://t.me/c/1234567890/15", {"kind": "chat_id", "chat_id": -1001234567890}),
])
def test_a_chat_link_is_read(manager, value, expected):
    assert manager.parse_source_link(value) == expected


@pytest.mark.parametrize("value", [
    "hello everyone", "0dec824eed", "@ab", "t.me/", "https://t.me/addstickers/Cats",
    "https://t.me/bot?startgroup=forwardfrom", "https://t.me/bot?startchannel=forwardfrom",
    "https://t.me/c/12/1", "https://www.okx.com/p2p/ads-merchant?publicUserId=abc123",
    "https://example.com/t.me/news", "https://t.me/news extra", "",
])
def test_anything_that_is_not_a_chat_link_is_left_alone(manager, value):
    assert manager.parse_source_link(value) is None


def test_a_link_asks_before_anything_is_relayed(manager):
    manager.state["channel"] = CHANNEL
    update = _private_text("https://t.me/newsroom")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert manager.forward_sources() == {} and manager.is_forward_source(NEWS_CHANNEL) is False
    assert manager.source_offer(ADMIN)["chat_id"] == NEWS_CHANNEL
    text = update.message.reply_html.await_args.args[0]
    assert "Forward from this channel?" in text and "News" in text
    buttons = update.message.reply_html.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in buttons.inline_keyboard for b in row] == \
        ["fwd_src_offer_add", "fwd_src_offer_post", "fwd_src_offer_no"]


def test_the_offer_is_sent_for_a_normalized_handle(manager):
    fake = _bot()
    manager.state["group"] = GROUP

    asyncio.run(manager.on_text(_private_text("@newsroom"), SimpleNamespace(bot=fake)))

    fake.get_chat.assert_awaited_once_with("@newsroom")
    assert manager.source_offer(ADMIN)["title"] == "News"


def test_tapping_add_starts_the_relay(manager):
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    manager.state["settings"]["channel_to_group"] = False
    manager.state["forward_sources"] = {}

    asyncio.run(manager.on_text(_private_text("https://t.me/newsroom"), SimpleNamespace(bot=_bot())))
    query = _callback(manager, "fwd_src_offer_add")

    record = manager.forward_sources()[str(NEWS_CHANNEL)]
    assert (record["title"], record["type"]) == ("News", "channel")
    assert manager.channel_to_group_enabled() is True      # selecting a source means "use it"
    assert manager.source_offer(ADMIN) is None             # the offer is spent
    text = query.edit_message_text.await_args.args[0]
    assert "are forwarded to" in text and "Forwarding was OFF" in text
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is not None


def test_tapping_not_now_adds_nothing(manager):
    manager.state["group"] = GROUP
    asyncio.run(manager.on_text(_private_text("https://t.me/newsroom"), SimpleNamespace(bot=_bot())))

    query = _callback(manager, "fwd_src_offer_no")

    assert manager.forward_sources() == {}
    assert manager.source_offer(ADMIN) is None
    assert "Nothing was added" in query.edit_message_text.await_args.args[0]


def test_an_expired_offer_only_says_so(manager):
    manager.state["group"] = GROUP
    manager.state["source_offers"] = {str(ADMIN): {"chat_id": NEWS_CHANNEL, "title": "News",
                                                   "type": "channel", "ts": 1}}

    query = _callback(manager, "fwd_src_offer_add")

    assert manager.forward_sources() == {}
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert "expired" in query.answer.await_args.args[0]
    query.edit_message_text.assert_not_awaited()


def test_offers_are_sanitized_and_expire_on_load(manager):
    manager.state["source_offers"] = {
        str(ADMIN): {"chat_id": NEWS_CHANNEL, "title": "News", "type": "channel",
                     "ts": int(time.time())},
        "999": {"chat_id": SIGNALS_GROUP, "title": "Old", "type": "channel", "ts": 1},
        "junk": {"chat_id": 1, "title": "Junk", "type": "channel", "ts": int(time.time())},
    }
    manager.save()

    manager.refresh_state()

    assert list(manager.state["source_offers"]) == [str(ADMIN)]


def test_a_link_to_a_user_chat_is_refused(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat = AsyncMock(return_value=_chat(ADMIN, "private", "Sam"))

    update = _private_text("https://t.me/sam_smith")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    assert "user chat" in update.message.reply_html.await_args.args[0]
    assert manager.source_offer(ADMIN) is None


def test_a_link_that_telegram_cannot_open_points_at_a_forward(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat = AsyncMock(side_effect=RuntimeError("Chat not found"))

    update = _private_text("https://t.me/ghost_channel")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    text = update.message.reply_html.await_args.args[0]
    assert "could not open" in text and "private" in text
    assert manager.forward_sources() == {}


def test_an_invite_link_asks_the_bot_to_be_added_first(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat = AsyncMock(side_effect=RuntimeError("chat not found"))

    update = _private_text("https://t.me/+AbCdEf12")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.get_chat.assert_awaited_once_with("https://t.me/+AbCdEf12")
    assert "not in that chat" in update.message.reply_html.await_args.args[0]


def test_a_private_channel_link_is_looked_up_by_its_id(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat = AsyncMock(return_value=_chat(NEWS_CHANNEL, "channel", "News"))

    asyncio.run(manager.on_text(_private_text("https://t.me/c/1234567890/15"),
                                SimpleNamespace(bot=fake)))

    fake.get_chat.assert_awaited_once_with(-1001234567890)
    assert manager.source_offer(ADMIN)["chat_id"] == NEWS_CHANNEL


def test_the_destination_group_cannot_be_added(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat = AsyncMock(return_value=_chat(GROUP, "supergroup", "Rates"))

    update = _private_text("https://t.me/rates")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    assert "forwarded <b>into</b>" in update.message.reply_html.await_args.args[0]
    assert manager.source_offer(ADMIN) is None


def test_a_chat_that_is_already_relayed_says_so(manager):
    manager.state["group"] = GROUP
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()
    fake.get_chat = AsyncMock(return_value=_chat(NEWS_CHANNEL, "channel", "News"))

    update = _private_text("https://t.me/newsroom")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    text = update.message.reply_html.await_args.args[0]
    assert "Already forwarding from" in text
    assert update.message.reply_html.await_args.kwargs["reply_markup"] is not None


def test_without_a_group_first_a_link_says_what_is_missing(manager):
    manager.state["group"] = None
    update = _private_text("https://t.me/newsroom")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert "Set the destination group first" in update.message.reply_html.await_args.args[0]
    assert manager.source_offer(ADMIN) is None


def test_a_full_picker_says_the_limit_is_reached(manager):
    manager.state["group"] = GROUP
    for index in range(manager.MAX_FORWARD_SOURCES):
        manager.add_forward_source(-200000 - index, f"Chat {index}", "channel")

    update = _private_text("https://t.me/newsroom")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert "limit of" in update.message.reply_html.await_args.args[0]
    assert manager.source_offer(ADMIN) is None


def test_the_offer_warns_when_the_bot_is_not_an_admin(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="left"))

    update = _private_text("https://t.me/newsroom")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    text = update.message.reply_html.await_args.args[0]
    assert "add the bot to that channel as an <b>admin</b>" in text
    assert "channel posts" in text


def test_the_offer_is_quiet_when_the_bot_is_already_an_admin(manager):
    manager.state["group"] = GROUP
    update = _private_text("https://t.me/newsroom")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    text = update.message.reply_html.await_args.args[0]
    assert "already an <b>admin</b> there" in text
    assert "add the bot" not in text


def test_a_group_offer_explains_privacy_mode(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.get_chat = AsyncMock(return_value=_chat(SIGNALS_GROUP, "supergroup", "Signals"))
    fake.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))

    update = _private_text("https://t.me/signals_group")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    text = update.message.reply_html.await_args.args[0]
    assert "Forward from this group?" in text and "privacy mode" in text


def test_a_forwarded_post_names_a_private_channel(manager):
    """A private channel has no link — the post the admin forwarded is the link."""
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    fake = _bot()
    update = _private_text("a post from a private channel",
                           forward_origin=SimpleNamespace(chat=_chat(NEWS_CHANNEL, "channel", "News")))

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_not_awaited()                 # asked first, not reposted
    assert manager.source_offer(ADMIN)["chat_id"] == NEWS_CHANNEL
    assert manager.forward_sources() == {}
    assert "Forward from this channel?" in update.message.reply_html.await_args.args[0]
    fake.get_chat_member.assert_awaited_once_with(NEWS_CHANNEL, fake.id)


def test_a_forwarded_post_from_a_relayed_chat_is_still_reposted(manager):
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    manager.add_forward_source(NEWS_CHANNEL, "News", "channel")
    fake = _bot()
    update = _private_text("hello", forward_from_chat=_chat(NEWS_CHANNEL, "channel", "News"))

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_awaited_once()
    assert manager.source_offer(ADMIN) is None


def test_a_forward_from_a_user_keeps_its_ordinary_meaning(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    update = _private_text("hello", forward_origin=SimpleNamespace(type="user"))

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_awaited_once()
    assert manager.source_offer(ADMIN) is None


def test_a_post_from_the_destination_group_is_never_offered(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    update = _private_text("group news",
                           forward_origin=SimpleNamespace(chat=_chat(GROUP, "supergroup", "Rates")))

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_awaited_once()
    assert manager.source_offer(ADMIN) is None


def test_a_forwarded_photo_can_name_the_channel_too(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    update = _private_text(None, photo=[SimpleNamespace(file_id="AgACAg")],
                           forward_origin=SimpleNamespace(sender_chat=_chat(NEWS_CHANNEL, "channel", "News")))

    asyncio.run(manager.on_photo(update, SimpleNamespace(bot=fake)))

    fake.copy_message.assert_not_awaited()
    assert manager.source_offer(ADMIN)["chat_id"] == NEWS_CHANNEL


def test_the_menu_and_the_help_explain_the_link_route(manager):
    keyboard = manager.forward_sources_kb()
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "fwd_src_link_help" in data
    assert "t.me/name" in manager.forward_sources_text()

    query = _callback(manager, "fwd_src_link_help")

    assert query.answer.await_args.kwargs["show_alert"] is True
    assert "t.me/name" in query.answer.await_args.args[0]


def test_forwardfrom_in_the_private_chat_mentions_the_link_route(manager):
    update = _private_text(None)

    asyncio.run(manager.forwardfrom(update, SimpleNamespace(bot=_bot())))

    assert "t.me/name" in update.message.reply_html.await_args.args[0]


def test_the_offer_can_post_the_message_instead(manager):
    """Asking about a forwarded post must not swallow it — 📤 posts it anyway."""
    manager.state["group"], manager.state["group_title"] = GROUP, "Rates"
    fake = _bot()
    update = _private_text("hello", message_id=61,
                           forward_origin=SimpleNamespace(chat=_chat(NEWS_CHANNEL, "channel", "News")))
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    query = _callback(manager, "fwd_src_offer_post", fake)

    fake.copy_message.assert_awaited_once_with(chat_id=GROUP, from_chat_id=ADMIN, message_id=61)
    assert manager.forward_sources() == {}                 # nothing was selected
    assert manager.source_offer(ADMIN) is None
    assert "without adding" in query.edit_message_text.await_args.args[0]
    # the ordinary 📤 answer, Undo button included, is written under the offer
    assert "Sent to" in query.message.reply_html.await_args.args[0]


def test_a_link_offer_can_be_posted_instead_too(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    asyncio.run(manager.on_text(_private_text("https://t.me/newsroom", message_id=62),
                                SimpleNamespace(bot=fake)))

    _callback(manager, "fwd_src_offer_post", fake)

    fake.copy_message.assert_awaited_once_with(chat_id=GROUP, from_chat_id=ADMIN, message_id=62)


def test_posting_instead_needs_a_live_offer(manager):
    manager.state["group"] = GROUP

    query = _callback(manager, "fwd_src_offer_post")

    assert query.answer.await_args.kwargs["show_alert"] is True
    assert "expired" in query.answer.await_args.args[0]
    query.edit_message_text.assert_not_awaited()


def test_posting_instead_says_so_when_nothing_can_be_posted(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    asyncio.run(manager.on_text(_private_text("hello", message_id=63,
                                forward_origin=SimpleNamespace(chat=_chat(NEWS_CHANNEL, "channel", "News"))),
                                SimpleNamespace(bot=fake)))
    manager.state["settings"]["forward_target"] = "off"      # turned off in between

    query = _callback(manager, "fwd_src_offer_post", fake)

    fake.copy_message.assert_not_awaited()
    assert "Nothing was posted" in query.edit_message_text.await_args.args[0]


def test_the_offer_has_no_post_button_when_auto_forward_is_off(manager):
    manager.state["group"] = GROUP
    manager.state["settings"]["forward_target"] = "off"

    update = _private_text("https://t.me/newsroom")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    buttons = update.message.reply_html.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in buttons.inline_keyboard for b in row] == \
        ["fwd_src_offer_add", "fwd_src_offer_no"]


def test_nothing_means_neither_added_nor_posted(manager):
    manager.state["group"] = GROUP
    update = _private_text("https://t.me/newsroom")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    query = _callback(manager, "fwd_src_offer_no")

    assert "Nothing was added and the message was not posted" in \
        query.edit_message_text.await_args.args[0]
