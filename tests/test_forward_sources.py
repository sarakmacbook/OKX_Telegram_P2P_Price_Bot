"""↪️ Choosing *which* channel or group the bot forwards from.

The relay used to read exactly one chat — the channel the price post goes to.
These tests cover the selection itself (commands, deep links, menu), the relay
that follows it, and the two ways a source can disappear again.
"""

import asyncio
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
    )


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
    query = SimpleNamespace(data=data, message=SimpleNamespace(text="", chat_id=ADMIN),
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
