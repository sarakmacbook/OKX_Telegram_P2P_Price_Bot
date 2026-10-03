"""🧹 Group cleanup: remove the join notices and every useless message.

The group is a price board, so the bot removes what hides the post.  What
counts as useless is the admin's choice — one switch per rule, plus 🔇 strict,
where an ordinary member cannot leave anything standing.  Whatever is switched
on, three senders are never touched: the bot itself, the admins (its own and
the group's) and a member who is answering the 🛡 anti-scam check.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore

GROUP, OTHER_GROUP, ADMIN, BOT_ID = -100123, -100999, 424242, 123456
MEMBER, MODERATOR = 555, 777


@pytest.fixture()
def manager(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    # both caches are process-wide: a test must never read another one's answer
    bot._ADMIN_CACHE.clear()
    bot._CLEANUP_SKIP.clear()
    bot.state["group"] = GROUP
    bot.state["group_title"] = "Rates"
    bot.save()
    yield bot
    bot._ADMIN_CACHE.clear()
    bot._CLEANUP_SKIP.clear()


def _bot(admins=()):
    """A Telegram stub: it can delete, warn and read the administrator list."""
    return SimpleNamespace(
        id=BOT_ID,
        delete_message=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        get_chat_administrators=AsyncMock(
            return_value=[SimpleNamespace(user=SimpleNamespace(id=user_id, is_bot=False))
                          for user_id in (BOT_ID, *admins)]),
    )


def _user(user_id=MEMBER, name="Sam", is_bot=False):
    return SimpleNamespace(id=user_id, first_name=name, full_name=name, is_bot=is_bot,
                           username=None)


def _message(text=None, user=None, message_id=77, **attrs):
    """A group message — ``attrs`` adds whatever Telegram would have set."""
    fields = {"text": text, "caption": None, "message_id": message_id, "from_user": user,
              "entities": None, "caption_entities": None, "delete": AsyncMock()}
    fields.update(attrs)
    return SimpleNamespace(**fields)


def _update(message, user=None, chat_id=GROUP, chat_type="supergroup", title="Rates"):
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=title)
    return SimpleNamespace(message=message, effective_message=message, effective_chat=chat,
                           effective_user=user if user is not None else message.from_user)


def run(manager, fake, message, user=None, chat_id=GROUP):
    update = _update(message, user=user, chat_id=chat_id)
    asyncio.run(manager.on_group_cleanup(update, SimpleNamespace(bot=fake)))
    return fake


def switch(manager, **settings):
    manager.state["settings"].update(settings)
    manager.save()


def deleted(fake) -> bool:
    return fake.delete_message.await_count > 0


def callback(manager, data):
    query = SimpleNamespace(data=data, message=SimpleNamespace(text="", chat_id=ADMIN),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private"),
                             callback_query=query)
    asyncio.run(manager.on_button(update, SimpleNamespace(bot=_bot())))
    return query


# ── the rules, one by one ───────────────────────────────────────────────────
def test_a_plain_message_survives_the_default_settings(manager):
    """Out of the box only the join/left notices go — chat stays chat."""
    fake = run(manager, _bot(), _message("hello everyone", _user()))

    assert not deleted(fake)
    assert manager.cleanup_summary() == "Join/left notices"


def test_join_and_left_notices_are_removed(manager):
    message = _message(user=None, new_chat_members=[_user()])
    asyncio.run(manager.on_join_left(_update(message), SimpleNamespace(bot=_bot())))

    message.delete.assert_awaited_once()
    assert manager.cleanup_stats()["removed"] == 1
    assert manager.cleanup_stats()["reason"] == manager.CLEANUP_REASONS["delete_join_left"]


def test_the_join_left_rule_can_be_switched_off(manager):
    switch(manager, delete_join_left=False)
    message = _message(user=None, left_chat_member=_user())

    asyncio.run(manager.on_join_left(_update(message), SimpleNamespace(bot=_bot())))

    message.delete.assert_not_awaited()


def test_other_service_notices_need_their_own_rule(manager):
    off = _message(user=None, new_chat_title="Rates 2.0", message_id=31)
    assert not deleted(run(manager, _bot(), off))

    switch(manager, cleanup_service=True)
    assert deleted(run(manager, _bot(),
                       _message(user=None, new_chat_title="Rates 2.0", message_id=32)))
    assert deleted(run(manager, _bot(),
                       _message(user=None, message_id=33,
                                pinned_message=_message("look at this", message_id=9))))
    assert manager.cleanup_violations(_message(user=None, delete_chat_photo=True)) == \
        [("cleanup_service", manager.CLEANUP_REASONS["cleanup_service"])]


def test_links_and_usernames_are_removed(manager):
    switch(manager, cleanup_links=True)

    fake = run(manager, _bot(), _message("free money https://scam.example", _user(),
                                         message_id=41))
    assert deleted(fake)

    fake = run(manager, _bot(), _message("ask @some_scammer", _user(), message_id=42))
    assert deleted(fake)

    # an entity alone is enough, even when the text hides the URL
    entity_message = _message("tap here", _user(),
                              entities=[SimpleNamespace(type="text_link", url="https://x.y")])
    assert manager.is_link_message(entity_message) is True

    fake = run(manager, _bot(), _message("no link here at all", _user(), message_id=43))
    assert not deleted(fake)


def test_media_and_stickers_are_removed(manager):
    switch(manager, cleanup_media=True)

    for index, attrs in enumerate(({"sticker": SimpleNamespace(file_id="s1")},
                                   {"photo": [SimpleNamespace(file_id="p1")]},
                                   {"video": SimpleNamespace(file_id="v1")},
                                   {"document": SimpleNamespace(file_id="d1")},
                                   {"voice": SimpleNamespace(file_id="a1")})):
        fake = run(manager, _bot(), _message(user=_user(), message_id=50 + index, **attrs))
        assert deleted(fake), attrs

    fake = run(manager, _bot(), _message("just words", _user(), message_id=59))
    assert not deleted(fake)


def test_forwarded_messages_are_removed(manager):
    switch(manager, cleanup_forwards=True)

    fake = run(manager, _bot(), _message("spam", _user(), message_id=71,
                                         forward_origin=SimpleNamespace(type="user")))
    assert deleted(fake)

    # PTB 20 and earlier named the fields differently — both are recognised
    assert manager.is_forwarded_message(_message("x", _user(), forward_date=1700000000)) \
        is True
    assert manager.is_forwarded_message(
        _message("x", _user(), forward_from_chat=SimpleNamespace(id=-1))) is True


def test_commands_from_members_are_removed(manager):
    switch(manager, cleanup_commands=True)

    fake = run(manager, _bot(), _message("/start@p2p_test_bot", _user(), message_id=61))
    assert deleted(fake)

    entity_command = _message("start", _user(),
                              entities=[SimpleNamespace(type="bot_command")])
    assert manager.is_command_message(entity_command) is True

    fake = run(manager, _bot(), _message("not a command", _user(), message_id=62))
    assert not deleted(fake)


def test_strict_mode_removes_everything_a_member_posts(manager):
    switch(manager, cleanup_strict=True)

    fake = run(manager, _bot(), _message("hello", _user()))
    assert deleted(fake)
    assert manager.cleanup_stats()["reason"] == manager.CLEANUP_REASONS["cleanup_strict"]
    assert manager.cleanup_summary() == "STRICT — members post nothing"


def test_the_master_switch_stops_every_rule(manager):
    switch(manager, cleanup_enabled=False, cleanup_strict=True, cleanup_links=True,
           cleanup_media=True, delete_join_left=True)

    assert not deleted(run(manager, _bot(), _message("hello", _user())))
    assert manager.cleanup_violations(_message("https://x.y", _user())) == []
    # …including the join/left notices, which the master switch owns as well
    message = _message(user=None, new_chat_members=[_user()])
    asyncio.run(manager.on_join_left(_update(message), SimpleNamespace(bot=_bot())))
    message.delete.assert_not_awaited()


# ── who is never touched ────────────────────────────────────────────────────
def test_the_bots_own_posts_are_never_removed(manager):
    """The price post, the 🛡 challenge and ↪️ relayed messages stay up."""
    switch(manager, cleanup_strict=True)
    own = _message("📊 P2P USDT/USD", _user(BOT_ID, "P2P bot", is_bot=True))

    fake = run(manager, _bot(), own)
    assert not deleted(fake)
    chat = SimpleNamespace(id=GROUP, type="supergroup", title="Rates")
    assert asyncio.run(manager.cleanup_exempt(fake, own, chat, own.from_user)) == \
        "the bot posted it"


def test_a_bot_admin_may_post_anything(manager):
    switch(manager, cleanup_strict=True)

    assert not deleted(run(manager, _bot(), _message("announcement", _user(ADMIN, "Owner"))))


def test_a_group_administrator_may_post_anything(manager):
    switch(manager, cleanup_strict=True)
    fake = _bot(admins=(MODERATOR,))

    assert not deleted(run(manager, fake, _message("rules here", _user(MODERATOR, "Mod"))))
    # the list is read once and then remembered — not for every message
    assert fake.get_chat_administrators.await_count == 1
    run(manager, fake, _message("more rules", _user(MODERATOR, "Mod")))
    assert fake.get_chat_administrators.await_count == 1


def test_an_anonymous_admin_post_is_kept(manager):
    switch(manager, cleanup_strict=True)
    anonymous = _message("posted as the group", _user(GROUP, "Rates"), author_signature="Owner")

    assert not deleted(run(manager, _bot(), anonymous))


def test_a_newcomer_answering_the_check_is_left_to_the_anti_scam_flow(manager):
    """The 🛡 handler deletes that message itself — 🧹 must not race it."""
    switch(manager, cleanup_strict=True)
    manager.state["captcha"][manager.captcha_key(GROUP, MEMBER)] = {
        "word": "amber", "tries": 0, "expires": int(time.time()) + 300}
    manager.save()

    assert not deleted(run(manager, _bot(), _message("amber", _user())))


def test_a_message_somebody_already_deleted_is_not_tried_again(manager):
    switch(manager, cleanup_strict=True)
    manager.remember_cleanup_skip(GROUP, 77)

    assert not deleted(run(manager, _bot(), _message("hello", _user())))


def test_only_the_registered_group_is_cleaned(manager):
    switch(manager, cleanup_strict=True)

    assert not deleted(run(manager, _bot(), _message("hello", _user()), chat_id=OTHER_GROUP))


def test_nothing_is_cleaned_while_no_group_is_registered(manager):
    """The bot may sit in other chats — 🧹 only ever works in the price group."""
    switch(manager, cleanup_strict=True)
    manager.state["group"] = None

    assert manager.cleanup_covers(SimpleNamespace(id=GROUP, type="supergroup")) is False
    assert not deleted(run(manager, _bot(), _message("hello", _user())))


def test_a_service_notice_without_a_sender_is_still_removed(manager):
    """Telegram wrote it itself — there is no member to protect."""
    switch(manager, cleanup_service=True)

    assert deleted(run(manager, _bot(),
                       _message(user=None, message_id=81, migrate_to_chat_id=-1001)))
    # …but a message with neither a sender nor a service field is left alone
    assert not deleted(run(manager, _bot(), _message("ghost", None, message_id=82)))


def test_a_notice_from_telegram_is_named_and_warns_nobody(manager):
    """There is no author to blame — and nobody in the group to warn."""
    switch(manager, cleanup_service=True, cleanup_notify="group")
    fake = run(manager, _bot(), _message(user=None, message_id=83, new_chat_title="Rates 2"))

    assert deleted(fake)
    fake.send_message.assert_not_awaited()
    assert manager.cleanup_stats()["name"] == "Telegram"


def test_the_screen_asks_for_a_group_while_none_is_set(manager):
    manager.state["group"] = None

    assert "No group is set yet" in manager.cleanup_text()


def test_a_private_chat_or_a_channel_is_never_cleaned(manager):
    switch(manager, cleanup_strict=True)

    assert manager.cleanup_covers(SimpleNamespace(id=ADMIN, type="private")) is False
    assert manager.cleanup_covers(SimpleNamespace(id=-100555, type="channel")) is False

    private = _update(_message("hello", _user(), message_id=91), chat_id=ADMIN,
                      chat_type="private", title=None)
    fake = _bot()
    asyncio.run(manager.on_group_cleanup(private, SimpleNamespace(bot=fake)))
    assert not deleted(fake)


# ── saying why ──────────────────────────────────────────────────────────────
def test_by_default_the_removal_is_silent(manager):
    switch(manager, cleanup_links=True)
    fake = run(manager, _bot(), _message("https://scam.example", _user()))

    assert deleted(fake)
    fake.send_message.assert_not_awaited()
    assert manager.cleanup_stats()["removed"] == 1
    assert manager.cleanup_stats()["name"] == "Sam"


def test_the_group_can_be_warned_and_the_warning_goes_away(manager):
    switch(manager, cleanup_links=True, cleanup_notify="group")
    fake = run(manager, _bot(), _message("https://scam.example", _user()))

    fake.send_message.assert_awaited_once()
    warning = fake.send_message.await_args.args[1]
    assert "Sam" in warning and "link" in warning
    assert manager.cleanup_notices()[f"{GROUP}:99"] > int(time.time())

    # the warning is short-lived: the next sweep removes it
    manager.state["cleanup_notices"][f"{GROUP}:99"] = int(time.time()) - 1
    manager.save()
    assert asyncio.run(manager.sweep_cleanup_notices(fake)) == 1
    assert fake.delete_message.await_args.kwargs == {"chat_id": GROUP, "message_id": 99}
    assert manager.cleanup_notices() == {}


def test_the_admins_can_be_told_instead(manager):
    switch(manager, cleanup_media=True, cleanup_notify="admin")
    fake = run(manager, _bot(), _message(user=_user(), sticker=SimpleNamespace(file_id="s1")))

    assert deleted(fake)
    fake.send_message.assert_awaited_once()          # the DM to the admin, not the group
    text = fake.send_message.await_args.args[1]
    assert fake.send_message.await_args.args[0] == ADMIN
    assert "Sam" in text and "Rates" in text


def test_a_removal_the_bot_is_not_allowed_to_make_is_reported(manager):
    switch(manager, cleanup_strict=True)
    fake = _bot()
    fake.delete_message = AsyncMock(side_effect=Exception("not enough rights"))

    run(manager, fake, _message("hello", _user()))

    fake.delete_message.assert_awaited_once()
    assert manager.cleanup_stats()["removed"] == 0    # nothing was counted
    assert manager.cleanup_notices() == {}            # and nothing was warned about


def test_the_notice_list_never_grows_past_its_limit(manager):
    for index in range(manager.CLEANUP_NOTICE_LIMIT + 5):
        notices = manager.cleanup_notices()
        notices[f"{GROUP}:{index}"] = int(time.time()) + index
        manager.state["cleanup_notices"] = manager.normalize_cleanup_notices(notices)

    assert len(manager.cleanup_notices()) == manager.CLEANUP_NOTICE_LIMIT


# ── broken state never breaks the bot ───────────────────────────────────────
@pytest.mark.parametrize("stored, expected", [
    ({}, {"removed": 0, "reason": "", "name": "", "at": 0}),
    ("nonsense", {"removed": 0, "reason": "", "name": "", "at": 0}),
    ({"removed": "12", "reason": "x" * 200, "at": -5},
     {"removed": 12, "reason": "x" * 80, "name": "", "at": 0}),
])
def test_the_cleanup_counter_is_sanitised(manager, stored, expected):
    assert manager.normalize_cleanup_stats(stored) == expected


def test_broken_cleanup_settings_fall_back_to_the_defaults(manager, monkeypatch):
    import copy
    raw = copy.deepcopy(manager.state)
    raw["settings"].update(cleanup_enabled="yes", cleanup_links=None, cleanup_strict=1,
                           cleanup_notify="shout", delete_join_left="ON")
    raw["cleanup"] = "broken"
    raw["cleanup_notices"] = {"nope": 1, f"{GROUP}:5": "soon"}
    monkeypatch.setattr(manager.STORE, "load", lambda: raw)

    loaded = manager.load()

    assert loaded["settings"]["cleanup_enabled"] is True
    assert loaded["settings"]["cleanup_links"] is False
    assert loaded["settings"]["cleanup_strict"] is False
    assert loaded["settings"]["cleanup_notify"] == "off"
    assert loaded["settings"]["delete_join_left"] is True
    assert loaded["cleanup"] == {"removed": 0, "reason": "", "name": "", "at": 0}
    assert loaded["cleanup_notices"] == {}


# ── the admin screen ────────────────────────────────────────────────────────
def test_the_cleanup_screen_lists_every_rule(manager):
    text, keyboard = manager.cleanup_text(), manager.cleanup_kb()

    for key, label, _about in manager.CLEANUP_RULES:
        assert label in text
        assert f"cleanup_rule:{key}" in [b.callback_data
                                         for row in keyboard.inline_keyboard for b in row]
    assert "cleanup_toggle" in [b.callback_data for row in keyboard.inline_keyboard
                                for b in row]
    assert "Never removed" in text and "anti-scam" in text


def test_the_settings_menu_links_to_the_cleanup_screen(manager):
    buttons = [b.callback_data for row in manager.settings_kb().inline_keyboard for b in row]

    assert "cleanup_menu" in buttons and "toggle_joinleft" in buttons
    assert "🧹 Group cleanup" in manager.settings_text()
    assert "🧹 Group cleanup" in manager.panel_text()


def test_the_menu_switches_the_rules(manager):
    switch(manager, cleanup_links=True)

    callback(manager, "cleanup_rule:cleanup_links")
    assert manager.state["settings"]["cleanup_links"] is False

    callback(manager, "cleanup_rule:cleanup_strict")
    assert manager.state["settings"]["cleanup_strict"] is True
    assert manager.cleanup_summary() == "STRICT — members post nothing"

    callback(manager, "cleanup_toggle")
    assert manager.cleanup_enabled() is False
    assert manager.cleanup_summary() == "OFF"


def test_an_unknown_rule_is_refused(manager):
    query = callback(manager, "cleanup_rule:delete_everything")

    query.answer.assert_awaited_with("Unknown rule", show_alert=True)
    assert "cleanup_rule:delete_everything" not in manager.CLEANUP_RULE_KEYS


def test_the_menu_cycles_how_the_bot_says_it(manager):
    assert manager.cleanup_notify_mode() == "off"

    for expected in ("group", "admin", "off"):
        callback(manager, "cleanup_notify")
        assert manager.cleanup_notify_mode() == expected


def test_the_menu_shows_what_was_removed(manager):
    switch(manager, cleanup_strict=True)
    run(manager, _bot(), _message("hello", _user()))

    text = manager.cleanup_text()
    assert "Removed so far: <b>1</b>" in text and "Sam" in text


def test_the_warning_button_appears_while_a_warning_is_up(manager):
    def callbacks():
        return [b.callback_data for row in manager.cleanup_kb().inline_keyboard for b in row]

    assert "cleanup_notices" not in callbacks()
    manager.state["cleanup_notices"][f"{GROUP}:99"] = int(time.time()) + 20

    assert "cleanup_notices" in callbacks()

    fake = _bot()
    manager.state["cleanup_notices"][f"{GROUP}:99"] = int(time.time()) - 1
    assert asyncio.run(manager.sweep_cleanup_notices(fake)) == 1
    assert "cleanup_notices" not in callbacks()
