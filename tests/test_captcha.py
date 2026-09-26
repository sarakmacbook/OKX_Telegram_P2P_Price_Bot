"""🛡 Anti-scam verification: mute new members until they type a random word."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore

GROUP = -100123
ADMIN = 424242
NEWCOMER = 555


@pytest.fixture()
def manager(bot, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    bot.save()
    return bot


def _bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=71)),
        delete_message=AsyncMock(),
        edit_message_text=AsyncMock(),
        restrict_chat_member=AsyncMock(),
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
    )


def _member(user_id=NEWCOMER, is_bot=False, name="Sam"):
    return SimpleNamespace(id=user_id, is_bot=is_bot, full_name=name, first_name=name)


def _join(members, chat_id=GROUP, chat_type="supergroup", title="Rates"):
    message = SimpleNamespace(new_chat_members=list(members), left_chat_member=None,
                              message_id=9, delete=AsyncMock())
    return SimpleNamespace(message=message, effective_message=message,
                           effective_chat=SimpleNamespace(type=chat_type, id=chat_id, title=title),
                           effective_user=None)


def _leave(member, chat_id=GROUP):
    message = SimpleNamespace(new_chat_members=None, left_chat_member=member,
                              message_id=10, delete=AsyncMock())
    return SimpleNamespace(message=message, effective_message=message,
                           effective_chat=SimpleNamespace(type="supergroup", id=chat_id,
                                                          title="Rates"),
                           effective_user=None)


def _say(text, user_id=NEWCOMER, chat_id=GROUP, chat_type="supergroup"):
    message = SimpleNamespace(text=text, caption=None, message_id=77, delete=AsyncMock())
    return SimpleNamespace(message=message, effective_message=message,
                           effective_chat=SimpleNamespace(type=chat_type, id=chat_id, title="Rates"),
                           effective_user=_member(user_id))


def run(coroutine):
    return asyncio.run(coroutine)


# ── the challenge ───────────────────────────────────────────────────────────
def test_a_new_member_is_muted_and_gets_a_word(manager):
    manager.state["group"] = GROUP
    fake = _bot()

    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    record = manager.pending_captcha(GROUP, NEWCOMER)
    assert record and record["word"]
    # muted, but still able to type the word: can_send_messages must stay True
    fake.restrict_chat_member.assert_awaited_once_with(GROUP, NEWCOMER,
                                                       manager.PENDING_PERMISSIONS)
    assert manager.PENDING_PERMISSIONS.can_send_messages is True
    assert manager.PENDING_PERMISSIONS.can_send_other_messages is not True
    challenge = fake.send_message.await_args.args[1]
    assert record["word"] in challenge
    assert "tg://user?id=555" in challenge            # {MENTION}


def test_the_challenge_uses_the_custom_message(manager):
    manager.state["group"] = GROUP
    manager.state["settings"]["captcha_message"] = (
        "Hey {NAME}, type {WORD} to join {GROUP} — {MINUTES} min, {LEFT} left")
    fake = _bot()

    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    text = fake.send_message.await_args.args[1]
    record = manager.pending_captcha(GROUP, NEWCOMER)
    assert record["word"] in text and "Sam" in text and "Rates" in text
    assert "5 min" in text and "3 left" in text


def test_admins_and_bots_are_never_challenged(manager):
    manager.state["group"] = GROUP
    fake = _bot()

    run(manager.on_join_left(_join([_member(ADMIN), _member(777, is_bot=True)]),
                             SimpleNamespace(bot=fake)))

    assert manager.pending_captchas() == {}
    assert fake.restrict_chat_member.await_count == 0


def test_a_disabled_check_challenges_nobody(manager):
    manager.state["group"] = GROUP
    manager.state["settings"]["captcha_enabled"] = False
    fake = _bot()

    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    assert manager.pending_captchas() == {}
    assert fake.restrict_chat_member.await_count == 0


def test_a_bot_without_rights_skips_the_check(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    fake.restrict_chat_member = AsyncMock(side_effect=RuntimeError("not enough rights"))

    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    assert manager.pending_captchas() == {}
    assert fake.send_message.await_count == 0


def test_only_the_registered_group_is_checked(manager):
    manager.state["group"] = GROUP
    fake = _bot()

    run(manager.on_join_left(_join([_member()], chat_id=-999), SimpleNamespace(bot=fake)))

    assert manager.pending_captchas() == {}


# ── passing and failing ─────────────────────────────────────────────────────
def test_the_right_word_unmutes_the_member(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    word = manager.pending_captcha(GROUP, NEWCOMER)["word"]

    run(manager.on_group_text(_say(word.upper()), SimpleNamespace(bot=fake)))

    assert manager.pending_captcha(GROUP, NEWCOMER) is None
    assert fake.restrict_chat_member.await_args.args[2] == manager.MEMBER_PERMISSIONS
    assert fake.delete_message.await_count == 1          # the challenge is cleaned up
    assert "verified" in fake.send_message.await_args.args[1]


def test_a_wrong_word_is_deleted_and_counted(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    update = _say("let me in please")
    run(manager.on_group_text(update, SimpleNamespace(bot=fake)))

    assert manager.pending_captcha(GROUP, NEWCOMER)["tries"] == 1
    update.message.delete.assert_awaited_once()          # spam never stays up
    assert "2" in fake.edit_message_text.await_args.args[0]   # attempts left
    assert manager.pending_captcha(GROUP, NEWCOMER) is not None


def test_running_out_of_attempts_mutes_and_asks_the_admin(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    for _ in range(manager.captcha_attempts()):
        run(manager.on_group_text(_say("wrong"), SimpleNamespace(bot=fake)))

    record = manager.pending_captcha(GROUP, NEWCOMER)
    assert record["locked"] is True and record["reason"] == "attempts"
    assert fake.restrict_chat_member.await_args.args[2] == manager.MUTED_PERMISSIONS
    report = [c for c in fake.send_message.await_args_list if c.args[0] == ADMIN]
    assert report and "Anti-scam" in report[-1].args[1]
    keyboard = report[-1].kwargs["reply_markup"].inline_keyboard
    assert [b.callback_data for b in keyboard[0]] == [f"cap_approve:{GROUP}:{NEWCOMER}",
                                                      f"cap_kick:{GROUP}:{NEWCOMER}"]


def test_a_timeout_mutes_the_member_too(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    manager.pending_captcha(GROUP, NEWCOMER)["expires"] = int(time.time()) - 1

    assert run(manager.sweep_captcha(fake)) == 1

    record = manager.pending_captcha(GROUP, NEWCOMER)
    assert record["locked"] is True and record["reason"] == "timeout"
    assert fake.restrict_chat_member.await_args.args[2] == manager.MUTED_PERMISSIONS


def test_an_unanswered_challenge_is_not_expired_twice(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    manager.pending_captcha(GROUP, NEWCOMER)["expires"] = int(time.time()) - 1

    assert run(manager.sweep_captcha(fake)) == 1
    assert run(manager.sweep_captcha(fake)) == 0


def test_old_failed_records_are_forgotten(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    record = manager.pending_captcha(GROUP, NEWCOMER)
    record["expires"] = int(time.time()) - 1
    run(manager.sweep_captcha(fake))
    record["locked_at"] = int(time.time()) - manager.CAPTCHA_LOCK_TTL - 1

    run(manager.sweep_captcha(fake))

    assert manager.pending_captchas() == {}


def test_leaving_before_answering_drops_the_record(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    message_id = manager.pending_captcha(GROUP, NEWCOMER)["msg_id"]

    run(manager.on_join_left(_leave(_member()), SimpleNamespace(bot=fake)))

    assert manager.pending_captchas() == {}
    fake.delete_message.assert_awaited_with(GROUP, message_id)


def test_other_members_are_not_affected(manager):
    manager.state["group"] = GROUP
    fake = _bot()

    update = _say("hello", user_id=999)
    run(manager.on_group_text(update, SimpleNamespace(bot=fake)))

    update.message.delete.assert_not_awaited()
    assert fake.restrict_chat_member.await_count == 0


def test_the_check_is_case_and_punctuation_insensitive(manager):
    assert manager.captcha_word_matches("  Tiger4821! ", "tiger4821")
    assert not manager.captcha_word_matches("tiger 482", "tiger4821")
    assert not manager.captcha_word_matches("", "tiger4821")


# ── the admin decides ───────────────────────────────────────────────────────
def _callback(manager, data, context):
    query = SimpleNamespace(data=data, message=SimpleNamespace(text="", chat_id=ADMIN),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private"),
                             callback_query=query)
    run(manager.on_button(update, context))
    return update


def test_the_admin_can_approve_a_failed_member(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    run(manager.sweep_captcha(fake))                     # pretend the time ran out
    manager.pending_captcha(GROUP, NEWCOMER)["expires"] = int(time.time()) - 1

    _callback(manager, f"cap_approve:{GROUP}:{NEWCOMER}", SimpleNamespace(bot=fake))

    assert manager.pending_captchas() == {}
    assert fake.restrict_chat_member.await_args.args[2] == manager.MEMBER_PERMISSIONS


def test_the_admin_can_kick_a_failed_member(manager):
    manager.state["group"] = GROUP
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))

    _callback(manager, f"cap_kick:{GROUP}:{NEWCOMER}", SimpleNamespace(bot=fake))

    assert manager.pending_captchas() == {}
    fake.ban_chat_member.assert_awaited_once_with(GROUP, NEWCOMER)
    fake.unban_chat_member.assert_awaited_once_with(GROUP, NEWCOMER)


def test_the_configured_action_decides_what_happens(manager):
    manager.state["group"] = GROUP
    manager.state["settings"]["captcha_action"] = "ban"
    fake = _bot()
    run(manager.on_join_left(_join([_member()]), SimpleNamespace(bot=fake)))
    manager.pending_captcha(GROUP, NEWCOMER)["expires"] = int(time.time()) - 1

    run(manager.sweep_captcha(fake))

    fake.ban_chat_member.assert_awaited_once_with(GROUP, NEWCOMER)
    assert fake.unban_chat_member.await_count == 0


# ── the settings screen ─────────────────────────────────────────────────────
def _text(manager, text):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_html=AsyncMock(),
                              message_id=3, chat_id=1)
    update = SimpleNamespace(message=message, effective_message=message,
                             effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private"))
    run(manager.on_text(update, SimpleNamespace(bot=_bot())))
    return update


def test_a_challenge_without_the_word_is_refused(manager):
    tapped = _callback(manager, "captcha_edit", SimpleNamespace(bot=_bot()))
    assert manager.edit_get(tapped, "awaiting_custom") == "captcha_message"

    update = _text(manager, "welcome, behave yourself")

    assert "{WORD}" in update.message.reply_html.await_args.args[0]
    assert manager.captcha_message() == manager.DEFAULT_CAPTCHA_MESSAGE


def test_a_custom_challenge_is_saved(manager):
    _callback(manager, "captcha_edit", SimpleNamespace(bot=_bot()))

    update = _text(manager, "Type {WORD} now, {NAME}!")

    assert manager.captcha_message() == "Type {WORD} now, {NAME}!"
    assert "saved" in update.message.reply_html.await_args.args[0]
    assert manager.edit_get(update, "awaiting_custom") is None


def test_the_built_in_text_can_be_restored(manager):
    _callback(manager, "captcha_edit", SimpleNamespace(bot=_bot()))
    _text(manager, "Type {WORD} now!")

    _callback(manager, "captcha_edit", SimpleNamespace(bot=_bot()))
    _text(manager, "default")

    assert manager.captcha_message() == manager.DEFAULT_CAPTCHA_MESSAGE


def test_the_settings_screen_cycles_attempts_timeout_and_action(manager):
    start_attempts, start_timeout, start_action = (manager.captcha_attempts(),
                                                   manager.captcha_timeout(),
                                                   manager.captcha_action())
    for _ in range(len(manager.CAPTCHA_ATTEMPT_CHOICES)):
        _callback(manager, "captcha_attempts", SimpleNamespace(bot=_bot()))
    assert manager.captcha_attempts() == start_attempts     # wrapped around

    for _ in range(len(manager.CAPTCHA_TIMEOUT_CHOICES)):
        _callback(manager, "captcha_timeout", SimpleNamespace(bot=_bot()))
    assert manager.captcha_timeout() == start_timeout

    for _ in range(len(manager.CAPTCHA_ACTIONS)):
        _callback(manager, "captcha_action", SimpleNamespace(bot=_bot()))
    assert manager.captcha_action() == start_action


def test_the_check_can_be_switched_off_from_the_menu(manager):
    _callback(manager, "captcha_toggle", SimpleNamespace(bot=_bot()))
    assert manager.captcha_enabled() is False
    _callback(manager, "captcha_toggle", SimpleNamespace(bot=_bot()))
    assert manager.captcha_enabled() is True


def test_the_menu_shows_the_current_state(manager):
    manager.state["settings"]["captcha_attempts"] = 5
    text = manager.antiscam_text()
    assert "5" in text and "{WORD}" in text
    buttons = [b.callback_data for row in manager.antiscam_kb().inline_keyboard for b in row]
    assert {"captcha_edit", "captcha_timeout", "captcha_action", "captcha_preview"} <= set(buttons)


# ── robustness ──────────────────────────────────────────────────────────────
def test_malformed_records_never_lock_somebody_out(manager):
    manager.STORE.save({"captcha": {"junk": {"word": "x"}, f"{GROUP}:{NEWCOMER}": {"nope": 1},
                                    f"{GROUP}:777": {"word": "tiger1234"}}})
    manager.refresh_state()

    keys = set(manager.pending_captchas())
    assert keys == {f"{GROUP}:777"}


@pytest.mark.parametrize("value,expected", [("99", 3), (3, 3), (1, 1), (None, 3), ("", 3)])
def test_attempts_are_sanitised(manager, value, expected):
    manager.STORE.save({"settings": {"captcha_attempts": value}})
    manager.refresh_state()
    assert manager.captcha_attempts() == expected


def test_an_unknown_action_falls_back_to_muting(manager):
    manager.STORE.save({"settings": {"captcha_action": "nuke"}})
    manager.refresh_state()
    assert manager.captcha_action() == "restrict"


def test_a_new_challenge_is_a_word_with_digits(manager):
    word = manager.new_captcha_word()
    assert any(word.startswith(w) for w in manager.CAPTCHA_WORDS)
    assert word[-4:].isdigit()
