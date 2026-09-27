"""Every new update type reaches the handler it belongs to.

The three features are wired through PTB handler groups, so a wrong filter would
silently disable them: a private-chat filter that swallows group messages would
break the 🛡 answers, a media filter that eats the photos the banner editor needs
would break 🖼.  These tests push real ``Update`` objects through a real
``Application`` and only stub the Telegram calls.
"""

import asyncio
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from storage import FileStore

GROUP, CHANNEL, ADMIN, NEWCOMER = -100123, -100987, 424242, 555

BOT_METHODS = ("send_message", "send_photo", "copy_message", "forward_message",
               "delete_message", "restrict_chat_member", "ban_chat_member",
               "unban_chat_member", "edit_message_text")


class Runner:
    """One real PTB application per scenario, with the Telegram calls stubbed."""

    def __init__(self, bot):
        self.bot = bot
        self.calls = {}

    def send(self, *payloads):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return asyncio.run(self._send(*payloads))

    async def _send(self, *payloads):
        app = self.bot.build_application(polling=False)
        await app.initialize()                        # verifies the token via getMe
        try:
            for payload in payloads:
                await app.process_update(self.bot.Update.de_json(payload, app.bot))
        finally:
            await app.shutdown()
        return self.calls


@pytest.fixture()
def router(bot, monkeypatch, tmp_path):
    from telegram import User
    from telegram.ext import ExtBot

    async def fake_get_me(self, **kwargs):                 # no network in the tests
        user = User(id=123456, first_name="Test Bot", is_bot=True, username="p2p_test_bot")
        # Bot.initialize() only calls get_me() for its side effect — the cached
        # user has to be set here, otherwise `bot.username` (used to match
        # "/command@bot") raises "ExtBot is not properly initialized".
        object.__setattr__(self, "_bot_user", user)
        return user

    # ExtBot (what Application builds) declares every API method itself, so the
    # stubs have to go on ExtBot, not on the Bot base class.
    monkeypatch.setattr(ExtBot, "get_me", fake_get_me)
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    bot.state["group"] = GROUP
    bot.state["group_title"] = "Rates"
    bot.save()

    runner = Runner(bot)
    # a Bot instance is frozen once built, so the calls are stubbed on the class
    for name in BOT_METHODS:
        mock = AsyncMock(return_value=SimpleNamespace(message_id=42), name=name)
        monkeypatch.setattr(ExtBot, name, mock)
        runner.calls[name] = mock
    return runner


def _private(text=None, **extra):
    return {"update_id": 1, "message": dict(
        {"message_id": 10, "date": 0, "text": text,
         "chat": {"id": ADMIN, "type": "private"},
         "from": {"id": ADMIN, "is_bot": False, "first_name": "Admin"}}, **extra)}


def _group(text, user_id=NEWCOMER, message_id=11):
    return {"update_id": 2, "message": {
        "message_id": message_id, "date": 0, "text": text,
        "chat": {"id": GROUP, "type": "supergroup", "title": "Rates"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Sam"}}}


def _join(user_id=NEWCOMER):
    return {"update_id": 3, "message": {
        "message_id": 12, "date": 0,
        "chat": {"id": GROUP, "type": "supergroup", "title": "Rates"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Sam"},
        "new_chat_members": [{"id": user_id, "is_bot": False, "first_name": "Sam"}]}}


def _channel_post(text, command=False):
    post = {"message_id": 14, "date": 0, "text": text,
            "chat": {"id": CHANNEL, "type": "channel", "title": "Rates channel"}}
    if command:
        post["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": 4, "channel_post": post}


# ── 🛡 the answer to a challenge arrives in the group ───────────────────────
def test_a_new_member_is_challenged_and_unmuted(router):
    calls = router.send(_join())
    record = router.bot.pending_captcha(GROUP, NEWCOMER)
    assert record and record["word"]
    calls["restrict_chat_member"].assert_awaited_once()

    calls = router.send(_group(record["word"]))

    assert router.bot.pending_captcha(GROUP, NEWCOMER) is None
    # the last restriction restores the normal member rights
    assert calls["restrict_chat_member"].await_args.args[2] == router.bot.MEMBER_PERMISSIONS


def test_a_wrong_answer_is_deleted(router):
    router.send(_join())
    calls = router.send(_group("let me in"))

    assert router.bot.pending_captcha(GROUP, NEWCOMER)["tries"] == 1
    calls["delete_message"].assert_awaited()


def test_an_ordinary_group_message_is_left_alone(router):
    calls = router.send(_group("hello everyone"))

    assert calls["restrict_chat_member"].await_count == 0
    assert calls["delete_message"].await_count == 0


# ── 📤 a private message is reposted ───────────────────────────────────────
def test_a_private_message_is_reposted_to_the_group(router):
    calls = router.send(_private("hello group"))

    calls["copy_message"].assert_awaited_once()
    assert calls["copy_message"].await_args.kwargs["chat_id"] == GROUP
    assert router.bot.state["forwards"]


def test_a_video_is_reposted_too(router):
    calls = router.send(_private(video={"file_id": "v1", "file_unique_id": "u1",
                                        "width": 1, "height": 1, "duration": 1}))

    calls["copy_message"].assert_awaited_once()
    assert calls["copy_message"].await_args.kwargs["chat_id"] == GROUP


def test_a_command_is_not_reposted(router):
    calls = router.send(_private("/start", entities=[
        {"type": "bot_command", "offset": 0, "length": 6}]))

    assert calls["copy_message"].await_count == 0


def test_a_photo_that_the_banner_editor_wanted_is_not_reposted(router):
    router.bot.edit_set(SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN)),
                        "awaiting_custom", "banner_media")
    calls = router.send(_private(photo=[{"file_id": "AgACAgIAAxkBAAICbig",
                                         "file_unique_id": "u2", "width": 1, "height": 1}]))

    assert calls["copy_message"].await_count == 0
    assert router.bot.post_banner() == "AgACAgIAAxkBAAICbig"
    assert router.bot.banner_kind() == "photo"


def test_a_gif_the_banner_editor_wanted_is_saved_as_an_animation(router):
    router.bot.edit_set(SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN)),
                        "awaiting_custom", "banner_media")
    calls = router.send(_private(animation={"file_id": "CgACAgIAAxkBAAIBnGif",
                                            "file_unique_id": "u3",
                                            "width": 1, "height": 1, "duration": 1}))

    assert calls["copy_message"].await_count == 0
    assert router.bot.post_banner() == "CgACAgIAAxkBAAIBnGif"
    assert router.bot.banner_kind() == "animation"


def test_a_gif_sent_as_a_file_is_saved_as_an_animation_too(router):
    router.bot.edit_set(SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN)),
                        "awaiting_custom", "banner_media")
    calls = router.send(_private(document={"file_id": "BQACAgIAAxkBAAIBnGif",
                                           "file_unique_id": "u4",
                                           "mime_type": "image/gif", "file_name": "rates.gif"}))

    assert calls["copy_message"].await_count == 0
    assert router.bot.post_banner() == "BQACAgIAAxkBAAIBnGif"
    assert router.bot.banner_kind() == "animation"


def test_a_gif_nobody_asked_for_is_reposted(router):
    calls = router.send(_private(animation={"file_id": "CgACAgIAAxkBAAIBnGif",
                                            "file_unique_id": "u3",
                                            "width": 1, "height": 1, "duration": 1}))

    calls["copy_message"].assert_awaited_once()
    assert router.bot.post_banner() == ""


# ── 📢 the channel ─────────────────────────────────────────────────────────
def test_setchannel_works_from_inside_the_channel(router):
    router.send(_channel_post("/setchannel", command=True))

    assert router.bot.state["channel"] == CHANNEL
    assert router.bot.state["channel_title"] == "Rates channel"


def test_an_unrelated_channel_message_changes_nothing(router):
    router.send(_channel_post("hello channel"))

    assert router.bot.state["channel"] is None


def test_a_post_in_the_configured_channel_is_relayed_with_the_channel_name(router):
    router.bot.state["channel"] = CHANNEL

    calls = router.send(_channel_post("channel announcement"))

    calls["send_message"].assert_awaited_once()
    assert calls["send_message"].await_args.kwargs["chat_id"] == GROUP
    assert calls["send_message"].await_args.kwargs["text"] == \
        "📢 <b>Rates channel</b>\n\nchannel announcement"
    calls["forward_message"].assert_not_awaited()


def test_channel_setup_commands_are_not_forwarded_to_the_group(router):
    router.bot.state["channel"] = CHANNEL

    calls = router.send(_channel_post("/setchannel", command=True))

    assert router.bot.state["channel"] == CHANNEL
    calls["forward_message"].assert_not_awaited()
    calls["copy_message"].assert_not_awaited()


def test_the_startchannel_deep_link_registers_the_channel(router):
    """``t.me/bot?startchannel=setchannel`` arrives as a /start channel post."""
    router.send({"update_id": 5, "channel_post": {
        "message_id": 15, "date": 0, "text": "/start setchannel",
        "chat": {"id": CHANNEL, "type": "channel", "title": "Rates channel"},
        "from": {"id": ADMIN, "is_bot": False, "first_name": "Admin"},
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}})

    assert router.bot.state["channel"] == CHANNEL


# ── ↪️ forwarding from a chat the admin picked ──────────────────────────────
NEWS, SIGNALS = -100555, -100777


def _chat_message(chat_id, chat_type, text=None, message_id=11, user_id=NEWCOMER,
                  command=False, **media):
    """An ordinary message in any group/channel — the ↪️ relay input."""
    payload = {"message_id": message_id, "date": 0, "text": text,
               "chat": {"id": chat_id, "type": chat_type, "title": "Source"},
               "from": {"id": user_id, "is_bot": False, "first_name": "Sam"}, **media}
    if command:
        payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    key = "channel_post" if chat_type == "channel" else "message"
    if chat_type == "channel":
        payload.pop("from")               # a channel post has no sender
    return {"update_id": 6, key: payload}


def test_a_message_in_a_selected_group_is_relayed_into_the_group(router):
    router.bot.add_forward_source(SIGNALS, "Signals", "supergroup")

    calls = router.send(_chat_message(SIGNALS, "supergroup", "buy now"))

    calls["send_message"].assert_awaited_once()
    assert calls["send_message"].await_args.kwargs["chat_id"] == GROUP
    assert calls["send_message"].await_args.kwargs["text"] == "👥 <b>Source</b>\n\nbuy now"


def test_a_photo_in_a_selected_group_is_relayed_and_not_read_as_a_banner(router):
    router.bot.add_forward_source(SIGNALS, "Signals", "supergroup")

    calls = router.send(_chat_message(SIGNALS, "supergroup", message_id=12, photo=[
        {"file_id": "AgACAgIAAxkBAAICbig", "file_unique_id": "u3", "width": 1, "height": 1}]))

    calls["copy_message"].assert_awaited_once()
    assert calls["copy_message"].await_args.kwargs["caption"] == "👥 <b>Source</b>"
    assert router.bot.post_banner() == ""


def test_only_the_selected_chats_are_relayed(router):
    router.bot.add_forward_source(SIGNALS, "Signals", "supergroup")

    calls = router.send(_chat_message(NEWS, "supergroup", "unselected chat"))

    calls["forward_message"].assert_not_awaited()
    calls["copy_message"].assert_not_awaited()
    calls["send_message"].assert_not_awaited()


def test_commands_in_a_selected_group_stay_there(router):
    router.bot.add_forward_source(SIGNALS, "Signals", "supergroup")

    calls = router.send(_chat_message(SIGNALS, "supergroup", "/forwardfrom", command=True))

    assert router.bot.source_selected(SIGNALS) is True      # it registered the chat…
    calls["forward_message"].assert_not_awaited()           # …and was not relayed
    calls["copy_message"].assert_not_awaited()


def test_the_registered_group_keeps_its_anti_scam_handler(router):
    """Selecting the destination group must not steal its messages."""
    router.bot.add_forward_source(GROUP, "Rates", "supergroup")
    router.send(_join())
    record = router.bot.pending_captcha(GROUP, NEWCOMER)

    calls = router.send(_group(record["word"]))

    assert router.bot.pending_captcha(GROUP, NEWCOMER) is None   # the 🛡 handler answered
    calls["forward_message"].assert_not_awaited()
    calls["copy_message"].assert_not_awaited()


def test_the_bot_does_not_relay_its_own_confirmation(router):
    """The ✅ answer inside a source chat is not echoed into the group."""
    calls = router.send(_chat_message(NEWS, "channel", "/forwardfrom", command=True))

    assert router.bot.source_selected(NEWS) is True
    # the stubbed Telegram call answered "message_id=42" — that is the ✅ the bot
    # posted into the channel it now relays from
    assert router.bot.is_own_message(NEWS, 42) is True
    calls = router.send(_chat_message(NEWS, "channel", "✅ New messages …", message_id=42))

    calls["forward_message"].assert_not_awaited()
    calls["copy_message"].assert_not_awaited()

    calls = router.send(_chat_message(NEWS, "channel", "a real post", message_id=43))
    assert calls["send_message"].await_args.kwargs["chat_id"] == GROUP
    assert calls["send_message"].await_args.kwargs["text"] == "📢 <b>Source</b>\n\na real post"
    calls["forward_message"].assert_not_awaited()


def test_a_message_in_a_selected_channel_is_relayed(router):
    router.bot.add_forward_source(NEWS, "News", "channel")

    calls = router.send(_chat_message(NEWS, "channel", "channel announcement", message_id=13))

    calls["send_message"].assert_awaited_once()
    assert calls["send_message"].await_args.kwargs["chat_id"] == GROUP
    assert calls["send_message"].await_args.kwargs["text"] == \
        "📢 <b>Source</b>\n\nchannel announcement"

# ── ↪️ bidirectional group / channel relay ──────────────────────────────────
def test_group_to_channel_opt_in_and_no_echo(router):
    router.bot.state["channel"] = CHANNEL
    calls = router.send(_group("group news"))
    calls["send_message"].assert_not_awaited()

    router.bot.state["settings"]["group_to_channel"] = True
    calls = router.send(_group("group news"))
    assert calls["send_message"].await_args.kwargs["chat_id"] == CHANNEL
    assert calls["send_message"].await_args.kwargs["text"] == "👥 <b>Rates</b>\n\ngroup news"
    assert router.bot.is_own_message(CHANNEL, 42)
    calls["send_message"].reset_mock()
    calls = router.send(_chat_message(CHANNEL, "channel", "echo", message_id=42))
    calls["send_message"].assert_not_awaited()


def test_group_to_channel_skips_commands_and_captcha(router):
    router.bot.state["channel"] = CHANNEL
    router.bot.state["settings"]["group_to_channel"] = True
    calls = router.send(_chat_message(GROUP, "supergroup", "/preview", command=True))
    assert not any(call.kwargs.get("chat_id") == CHANNEL
                   for call in calls["send_message"].await_args_list)
    router.send(_join())
    word = router.bot.pending_captcha(GROUP, NEWCOMER)["word"]
    calls = router.send(_group(word))
    assert router.bot.pending_captcha(GROUP, NEWCOMER) is None
    assert not any(call.kwargs.get("chat_id") == CHANNEL
                   for call in calls["send_message"].await_args_list)


def test_group_to_channel_media(router):
    router.bot.state["channel"] = CHANNEL
    router.bot.state["settings"]["group_to_channel"] = True
    calls = router.send(_chat_message(GROUP, "supergroup", message_id=50, photo=[
        {"file_id": "AgACAgIAAxkBAAICbig", "file_unique_id": "u3", "width": 1, "height": 1}]))
    assert calls["copy_message"].await_args.kwargs["chat_id"] == CHANNEL
