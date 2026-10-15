"""🗂 Adding a channel by link, or picking one of the chats the bot has joined.

The price channel used to be reachable in exactly one way: put the bot inside the
channel and send ``/setchannel`` there (or tap the deep link that does both).
These tests cover the two routes that were added — a **link** an admin sends to
the bot (public ``t.me/name``, invite, private ``t.me/c/…`` or a bare ``-100…``
id) and the **pick-list** of every chat the bot was added to, public and private
alike — plus the registry behind that list and the ↪️ picker that shares it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore

GROUP, CHANNEL = -100123, -100987
NEWS, VIP, TALK = -100555, -100777, -100999
PRIVATE_LINK_ID = -1001234567890
ADMIN, STRANGER = 424242, 999


@pytest.fixture()
def manager(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    monkeypatch.setattr(bot, "fetch", AsyncMock(side_effect=AssertionError("Unexpected price fetch")))
    bot.state["group"], bot.state["group_title"] = GROUP, "Rates"
    bot.save()
    return bot


def _chat(chat_id, kind="channel", title="News", username=""):
    return SimpleNamespace(id=chat_id, type=kind, title=title, username=username)


def _bot(chat=None, *, status="administrator", can_post=True, can_send=True):
    """A bot stub: resolves one chat and reports the rights the bot has there."""
    return SimpleNamespace(
        id=123456,
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        delete_message=AsyncMock(),
        get_chat=AsyncMock(return_value=chat or _chat(NEWS, "channel", "News", "newsroom")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(
            status=status, can_post_messages=can_post, can_send_messages=can_send)),
    )


def _private_text(text, message_id=5, **message):
    """A message an admin sent to the bot in their private chat."""
    msg = SimpleNamespace(text=text, caption=None, message_id=message_id, chat_id=ADMIN,
                          reply_text=AsyncMock(), reply_html=AsyncMock(), **message)
    return SimpleNamespace(message=msg, effective_message=msg,
                           effective_user=SimpleNamespace(id=ADMIN, is_bot=False),
                           effective_chat=SimpleNamespace(id=ADMIN, type="private"), args=None)


def _callback(manager, data, fake=None):
    """An admin tapping a button; the update carries the in-flight edit state."""
    fake = fake or _bot()
    query = SimpleNamespace(data=data,
                            message=SimpleNamespace(text="", chat_id=ADMIN,
                                                    reply_text=AsyncMock(), reply_html=AsyncMock()),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private"),
                             callback_query=query)
    asyncio.run(manager.on_button(update, SimpleNamespace(bot=fake)))
    return update


def _buttons(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _my_chat_member(chat_id, kind="channel", old="left", new="administrator", user=ADMIN,
                    title="News", username=""):
    return SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=_chat(chat_id, kind, title, username), from_user=SimpleNamespace(id=user),
        old_chat_member=SimpleNamespace(status=old), new_chat_member=SimpleNamespace(status=new)))


# ── 🗂 the registry: every chat the bot was added to or was pointed at ───────
def test_a_chat_the_bot_is_added_to_joins_the_pick_list(manager):
    asyncio.run(manager.on_my_chat_member(_my_chat_member(NEWS, username="newsroom"),
                                          SimpleNamespace(bot=_bot())))

    record = manager.known_chat(NEWS)
    assert (record["title"], record["username"], record["type"]) == ("News", "newsroom", "channel")
    assert record["member"] is True and record["admin"] is True


def test_a_chat_somebody_else_added_the_bot_to_is_listed_too(manager):
    """Not only an admin's own invites are remembered — the bot is a member."""
    asyncio.run(manager.on_my_chat_member(_my_chat_member(VIP, user=STRANGER, new="member"),
                                          SimpleNamespace(bot=_bot())))

    record = manager.known_chat(VIP)
    assert record["member"] is True and record["admin"] is False
    assert manager.state["channel"] is None              # …but it is not registered


def test_removing_the_bot_drops_the_chat_from_the_list(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")

    asyncio.run(manager.on_my_chat_member(_my_chat_member(NEWS, old="administrator", new="left"),
                                          SimpleNamespace(bot=_bot())))

    assert manager.known_chat(NEWS) is None


def test_the_destinations_and_the_sources_are_remembered(manager):
    manager._set_channel(_chat(NEWS, "channel", "News", "newsroom"))
    manager._set_group(_chat(GROUP, "supergroup", "Rates"))
    manager.add_forward_source(VIP, "VIP", "channel")

    assert manager.known_chat(NEWS)["username"] == "newsroom"
    assert manager.known_chat(GROUP)["type"] == "supergroup"
    assert manager.known_chat(VIP)["title"] == "VIP"


def test_a_source_keeps_the_username_a_link_already_knew(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")

    manager.add_forward_source(NEWS, "News", "channel")   # no username here

    assert manager.known_chat(NEWS)["username"] == "newsroom"


def test_a_link_and_a_forwarded_post_fill_the_list(manager):
    asyncio.run(manager.on_text(_private_text("https://t.me/newsroom"), SimpleNamespace(bot=_bot())))
    update = _private_text("hello", forward_origin=SimpleNamespace(chat=_chat(VIP, "channel", "VIP")))
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert manager.known_chat(NEWS)["username"] == "newsroom"
    assert manager.known_chat(VIP)["title"] == "VIP"


def test_broken_records_are_dropped_and_the_newest_kept(manager):
    manager.state["known_chats"] = {
        str(NEWS): {"chat_id": NEWS, "title": "News", "type": "channel",
                    "username": "newsroom", "seen": 20},
        str(GROUP): {"chat_id": GROUP, "title": "Rates", "type": "supergroup", "seen": 10},
        "junk": {"chat_id": 1, "title": "Sam", "type": "private", "seen": 30},
        str(VIP): {"chat_id": "not-a-number", "type": "channel"},
    }
    manager.save()

    manager.refresh_state()

    assert list(manager.state["known_chats"]) == [str(NEWS), str(GROUP)]


def test_the_list_never_grows_past_its_limit(manager):
    for index in range(manager.MAX_KNOWN_CHATS + 5):
        manager.state.setdefault("known_chats", {})[str(-300000 - index)] = {
            "chat_id": -300000 - index, "title": f"Chat {index}", "type": "channel",
            "seen": index}
    manager.save()

    manager.refresh_state()

    assert len(manager.state["known_chats"]) == manager.MAX_KNOWN_CHATS
    assert manager.known_chat(-300000) is None                     # the oldest fell off
    assert manager.known_chat(-300000 - manager.MAX_KNOWN_CHATS - 4) is not None


def test_a_chat_the_bot_is_not_in_is_not_offered(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom", member=False)

    assert manager.known_chat_records(kinds=("channel",)) == []
    assert NEWS in [r["chat_id"] for r in manager.known_chat_records(members_only=False)]


def test_an_upgrade_lists_the_chats_that_are_already_in_use(manager):
    """Registered before the pick-list existed — the menu must not be empty."""
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"
    manager.add_forward_source(VIP, "VIP signals", "channel")
    manager.state["known_chats"] = {}

    channels = [r["chat_id"] for r in manager.known_chat_records(kinds=("channel",))]

    assert CHANNEL in channels and VIP in channels
    assert GROUP in [r["chat_id"] for r in manager.known_chat_records()]   # the group too
    assert f"pick_channel:{CHANNEL}" in _buttons(manager.channel_menu_kb())


def test_a_chat_in_use_is_not_offered_as_a_new_one(manager):
    manager.state["channel"], manager.state["channel_title"] = CHANNEL, "Rates channel"

    query = _callback(manager, f"pick_channel:{CHANNEL}").callback_query

    assert manager.state["channel"] == CHANNEL
    assert "already is the channel" in query.edit_message_text.await_args.args[0]


def test_only_the_tapped_chat_is_registered(manager):
    """A getChat answer for another chat is ignored — the tap decides, not Telegram."""
    manager.remember_chat(VIP, "VIP signals", "channel")       # the stub resolves NEWS

    query = _callback(manager, f"pick_channel:{VIP}").callback_query

    assert manager.state["channel"] == VIP
    assert manager.state["channel_title"] == "VIP signals"
    assert "VIP signals" in query.edit_message_text.await_args.args[0]


def test_a_public_and_a_private_chat_are_labelled_differently(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    manager.remember_chat(VIP, "VIP signals", "channel")

    assert manager.known_chat_link(manager.known_chat(NEWS)) == "t.me/newsroom"
    assert str(VIP) in manager.known_chat_link(manager.known_chat(VIP))
    assert manager.known_chat_label(manager.known_chat(NEWS)) == "📢 News (@newsroom)"
    assert manager.known_chat_label(manager.known_chat(VIP)) == "📢 VIP signals"


def test_a_long_title_is_shortened_to_fit_a_button(manager):
    manager.remember_chat(NEWS, "N" * 200, "channel", "newsroom")

    label = manager.known_chat_label(manager.known_chat(NEWS), mark="✅ ")

    assert len(label) <= manager.MAX_KNOWN_LABEL and label.endswith("…")


# ── 📢 the channel picker ───────────────────────────────────────────────────
def test_the_panel_button_opens_the_picker(manager):
    assert manager.set_channel_button().callback_data == "channel_menu"


def test_the_picker_lists_the_channels_the_bot_has_joined(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    manager.remember_chat(VIP, "VIP signals", "channel")
    manager.remember_chat(TALK, "Rates talk", "supergroup")

    keyboard = manager.channel_menu_kb()
    text = manager.channel_menu_text()

    assert f"pick_channel:{NEWS}" in _buttons(keyboard)
    assert f"pick_channel:{VIP}" in _buttons(keyboard)
    assert f"pick_channel:{TALK}" not in _buttons(keyboard)     # a group is no price channel
    assert "channel_link_ask" in _buttons(keyboard)
    assert "t.me/newsroom" in text and "VIP signals" in text
    assert "Add by link" in text and "t.me/c/" in text


def test_the_picker_keeps_the_deep_link_that_adds_the_bot(manager, monkeypatch):
    monkeypatch.setattr(manager, "BOT_USERNAME", "p2p_test_bot")

    urls = [button.url for row in manager.channel_menu_kb().inline_keyboard
            for button in row if button.url]

    assert any("startchannel=setchannel" in url and "post_messages" in url for url in urls)


def test_the_picker_answers_when_the_bot_knows_no_channel(manager):
    text = manager.channel_menu_text()

    assert "No channel yet" in text
    assert _buttons(manager.channel_menu_kb()) == \
        ["channel_link_ask", "setchannel_help", "panel"]     # no BOT_USERNAME in the tests


def test_tapping_a_joined_channel_posts_the_prices_there(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")

    query = _callback(manager, f"pick_channel:{NEWS}").callback_query

    assert manager.state["channel"] == NEWS
    assert manager.state["channel_title"] == "News"
    assert "is the channel the price post goes to" in query.edit_message_text.await_args.args[0]
    assert "Rates" in query.edit_message_text.await_args.args[0]      # the group keeps posting


def test_a_channel_the_bot_cannot_post_in_is_set_with_a_warning(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    fake = _bot(status="member")

    query = _callback(manager, f"pick_channel:{NEWS}", fake).callback_query

    assert manager.state["channel"] == NEWS
    assert "may not <b>post</b>" in query.edit_message_text.await_args.args[0]
    assert manager.known_chat(NEWS)["member"] is True


def test_a_channel_the_bot_left_is_refused_with_the_way_in(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    fake = _bot(status="left")

    query = _callback(manager, f"pick_channel:{NEWS}", fake).callback_query

    assert manager.state["channel"] is None
    text = query.edit_message_text.await_args.args[0]
    assert "is not in" in text and "admin" in text
    assert query.answer.await_args.kwargs["show_alert"] is True


def test_a_chat_that_is_no_longer_remembered_says_so(manager):
    query = _callback(manager, f"pick_channel:{NEWS}").callback_query

    assert manager.state["channel"] is None
    assert "gone" in query.answer.await_args.args[0]
    query.edit_message_text.assert_not_awaited()


def test_the_channel_in_use_is_marked_and_can_be_unset(manager):
    manager.state["channel"], manager.state["channel_title"] = NEWS, "News"
    manager.state["channel_last_msg_id"], manager.state["channel_last_msg_time"] = 7, 1_700_000_000
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    assert any(label.startswith("✅") for label in _labels(manager.channel_menu_kb()))

    query = _callback(manager, "channel_unset").callback_query

    assert manager.state["channel"] is None and manager.state["channel_title"] == ""
    assert manager.state["channel_last_msg_id"] is None
    assert "no longer the price channel" in query.edit_message_text.await_args.args[0]


def test_the_picker_marks_a_channel_it_cannot_use_yet(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom", member=False)

    labels = _labels(manager.channel_menu_kb())          # members only → nothing to pick
    assert labels == ["🔗 Add by link", "📲 Add the bot to a channel", "⬅️ Back"]


# ── 🔗 a channel named by its link (public, invite or private) ──────────────
def test_the_link_prompt_waits_for_the_answer(manager):
    update = _callback(manager, "channel_link_ask")

    assert manager.edits_of(update)["awaiting_custom"] == "channel_link"
    assert "t.me/c/1234/5" in update.callback_query.edit_message_text.await_args.args[0]


def test_the_link_sent_after_the_prompt_sets_the_channel(manager):
    manager.state["edits"][str(ADMIN)] = {"awaiting_custom": "channel_link"}
    update = _private_text("https://t.me/newsroom")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert manager.state["channel"] == NEWS
    assert manager.edits_of(update).get("awaiting_custom") is None      # the question is over
    assert "is the channel the price post goes to" in update.message.reply_html.await_args.args[0]


def test_cancelling_the_prompt_leaves_the_channel_alone(manager):
    manager.state["edits"][str(ADMIN)] = {"awaiting_custom": "channel_link"}
    update = _private_text("/cancel")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert manager.state["channel"] is None
    assert manager.edits_of(update).get("awaiting_custom") is None
    assert "not changed" in update.message.reply_html.await_args.args[0]


def test_setchannel_takes_a_link_as_its_argument(manager):
    update = _private_text(None)

    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=_bot(), args=["t.me/newsroom"])))

    assert manager.state["channel"] == NEWS
    assert manager.state["channel_title"] == "News"


def test_a_private_channel_link_names_its_chat_id(manager):
    fake = _bot(chat=_chat(PRIVATE_LINK_ID, "channel", "VIP signals"))
    update = _private_text(None)

    asyncio.run(manager.setchannel(update, SimpleNamespace(
        bot=fake, args=["https://t.me/c/1234567890/15"])))

    fake.get_chat.assert_awaited_once_with(-1001234567890)
    assert manager.state["channel"] == PRIVATE_LINK_ID


def test_a_bare_chat_id_is_a_reference_too(manager):
    assert manager.parse_chat_reference("-1001234567890") == \
        {"kind": "chat_id", "chat_id": -1001234567890}
    assert manager.parse_chat_reference("@newsroom") == {"kind": "username", "username": "newsroom"}
    assert manager.parse_chat_reference("424242") is None       # a user id is not a chat
    assert manager.parse_chat_reference("hello there") is None


def test_something_that_is_not_a_channel_link_is_explained(manager):
    update = _private_text(None)

    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=_bot(), args=["hello"])))

    assert manager.state["channel"] is None
    assert "not a channel link" in update.message.reply_html.await_args.args[0]
    assert "t.me/c/1234/5" in update.message.reply_html.await_args.args[0]


def test_a_link_telegram_cannot_open_keeps_the_question_open(manager):
    fake = _bot()
    fake.get_chat = AsyncMock(side_effect=RuntimeError("Chat not found"))
    manager.state["edits"][str(ADMIN)] = {"awaiting_custom": "channel_link"}
    update = _private_text("https://t.me/ghost")

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    assert manager.state["channel"] is None
    assert manager.edits_of(update)["awaiting_custom"] == "channel_link"   # try another link
    assert "could not open" in update.message.reply_html.await_args.args[0]


def test_a_link_to_the_channel_in_use_says_so(manager):
    manager.state["channel"], manager.state["channel_title"] = NEWS, "News"
    update = _private_text(None)

    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=_bot(), args=["@newsroom"])))

    assert "already is the channel" in update.message.reply_html.await_args.args[0]


def test_a_link_to_a_group_is_refused(manager):
    fake = _bot(chat=_chat(TALK, "supergroup", "Rates talk"))
    update = _private_text(None)

    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=fake, args=["t.me/ratestalk"])))

    assert manager.state["channel"] is None
    assert "not a channel" in update.message.reply_html.await_args.args[0]


def test_a_stranger_cannot_set_the_channel(manager):
    update = _private_text(None)
    update.effective_user = SimpleNamespace(id=STRANGER, is_bot=False)

    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=_bot(), args=["t.me/newsroom"])))

    assert manager.state["channel"] is None
    update.message.reply_html.assert_not_awaited()


def test_setchannel_without_an_argument_shows_the_picker(manager):
    update = _private_text(None)

    asyncio.run(manager.setchannel(update, SimpleNamespace(bot=_bot())))

    assert manager.state["channel"] is None
    assert "Set channel" in update.message.reply_html.await_args.args[0]
    assert "channel_link_ask" in _buttons(update.message.reply_html.await_args.kwargs["reply_markup"])


# ── 📢 the same link an admin sends for ↪️ can name the price channel ───────
def test_the_offer_can_make_a_channel_the_price_destination(manager):
    fake = _bot()
    asyncio.run(manager.on_text(_private_text("https://t.me/newsroom"), SimpleNamespace(bot=fake)))

    query = _callback(manager, "fwd_src_offer_channel", fake).callback_query

    assert manager.state["channel"] == NEWS
    assert manager.forward_sources() == {}                  # ➕ was never tapped
    assert manager.source_offer(ADMIN) is None              # the offer is spent
    assert "is the channel the price post goes to" in query.edit_message_text.await_args.args[0]


def test_the_offer_refuses_a_channel_the_bot_is_not_in(manager):
    fake = _bot()
    asyncio.run(manager.on_text(_private_text("https://t.me/newsroom"), SimpleNamespace(bot=fake)))
    fake.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="left"))

    query = _callback(manager, "fwd_src_offer_channel", fake).callback_query

    assert manager.state["channel"] is None
    assert "is not in" in query.edit_message_text.await_args.args[0]


def test_no_price_channel_is_offered_for_a_group(manager):
    fake = _bot(chat=_chat(TALK, "supergroup", "Rates talk"))

    update = _private_text("https://t.me/ratestalk")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=fake)))

    assert "fwd_src_offer_channel" not in _buttons(update.message.reply_html.await_args
                                                   .kwargs["reply_markup"])


def test_no_price_channel_is_offered_for_the_one_in_use(manager):
    manager.state["channel"], manager.state["channel_title"] = NEWS, "News"

    update = _private_text("https://t.me/newsroom")
    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert "fwd_src_offer_channel" not in _buttons(update.message.reply_html.await_args
                                                   .kwargs["reply_markup"])
    assert "fwd_src_offer_add" in _buttons(update.message.reply_html.await_args
                                           .kwargs["reply_markup"])


def test_a_forwarded_post_keeps_meaning_post_it(manager):
    """Forwarding a post publishes it — it is not how a destination is picked."""
    update = _private_text("hello",
                           forward_origin=SimpleNamespace(chat=_chat(VIP, "channel", "VIP")))

    asyncio.run(manager.on_text(update, SimpleNamespace(bot=_bot())))

    assert "fwd_src_offer_channel" not in _buttons(update.message.reply_html.await_args
                                                   .kwargs["reply_markup"])
    assert "fwd_src_offer_add" in _buttons(update.message.reply_html.await_args
                                           .kwargs["reply_markup"])


# ── 🗂 the same list feeds the ↪️ forward-from picker ───────────────────────
def test_the_forward_from_menu_offers_the_picker(manager):
    assert "fwd_src_pick" in _buttons(manager.forward_sources_kb())
    assert "Pick a chat the bot has joined" in manager.forward_sources_text()


def test_the_source_picker_lists_channels_and_groups(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    manager.remember_chat(TALK, "Rates talk", "supergroup")

    keyboard = manager.source_picker_kb()

    assert f"fwd_src_pick_add:{NEWS}" in _buttons(keyboard)
    assert f"fwd_src_pick_add:{TALK}" in _buttons(keyboard)
    assert "fwd_src_link_help" in _buttons(keyboard)
    assert "public and private" in manager.source_picker_text()


def test_a_selected_source_is_marked_in_the_picker(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    manager.add_forward_source(NEWS, "News", "channel")

    assert any(label.startswith("✅") for label in _labels(manager.source_picker_kb()))


def test_picking_a_joined_chat_starts_the_relay(manager):
    manager.state["settings"]["channel_to_group"] = False
    manager.remember_chat(NEWS, "News", "channel", "newsroom")

    query = _callback(manager, f"fwd_src_pick_add:{NEWS}").callback_query

    assert manager.source_selected(NEWS) is True
    assert manager.channel_to_group_enabled() is True       # selecting a source means "use it"
    assert "are forwarded to" in query.edit_message_text.await_args.args[0]
    assert manager.known_chat(NEWS)["admin"] is True


def test_picking_a_chat_that_is_already_relayed_says_so(manager):
    manager.add_forward_source(NEWS, "News", "channel")

    query = _callback(manager, f"fwd_src_pick_add:{NEWS}").callback_query

    assert query.answer.await_args.args[0] == "Already selected"
    assert len(manager.forward_sources()) == 1


def test_picking_without_a_destination_group_says_what_is_missing(manager):
    manager.state["group"] = None
    manager.remember_chat(NEWS, "News", "channel", "newsroom")

    query = _callback(manager, f"fwd_src_pick_add:{NEWS}").callback_query

    assert manager.forward_sources() == {}
    assert "Set the destination group first" in query.answer.await_args.args[0]


def test_picking_a_chat_the_bot_left_warns_about_the_rights(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    fake = _bot(status="left")

    query = _callback(manager, f"fwd_src_pick_add:{NEWS}", fake).callback_query

    assert manager.source_selected(NEWS) is True            # the admin asked for it
    assert "add the bot to that channel as an <b>admin</b>" in \
        query.edit_message_text.await_args.args[0]
    assert manager.known_chat(NEWS)["member"] is False


def test_a_full_picker_refuses_one_more_source(manager):
    manager.remember_chat(NEWS, "News", "channel", "newsroom")
    for index in range(manager.MAX_FORWARD_SOURCES):
        manager.add_forward_source(-200000 - index, f"Chat {index}", "channel")

    query = _callback(manager, f"fwd_src_pick_add:{NEWS}").callback_query

    assert manager.source_selected(NEWS) is False
    assert "limit of" in query.answer.await_args.args[0]
