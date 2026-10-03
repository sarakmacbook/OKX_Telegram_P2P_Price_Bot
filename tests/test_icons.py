"""Button icons (custom emoji images) and the post banner.

Telegram can show a custom emoji — a premium, often animated emoji image — in
front of a button label and colour the button (Bot API 9.4).  The bot chooses
the icon by the emoji a label starts with, so one setting per emoji covers every
button that uses it: the group price post, the admin panel and the menus.

These tests pin that mapping, both ways of setting an icon (forwarding the emoji
or pasting its id), the screens, the banner photo and GIF (their caption-limit
fallback and the size they are posted at — 📐 full HD), and that malformed
saved state can never break a keyboard.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


# ── helpers ────────────────────────────────────────────────────────────────
def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _extras(keyboard):
    """The icon/colour fields Telegram would receive for each button."""
    return [button.to_dict() for button in _buttons(keyboard)]


def _admin_update(data=None, chat_type="private", query_message=None, **query_kwargs):
    """An admin callback update, like the other test modules build."""
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=query_message, **query_kwargs)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             callback_query=query,
                             effective_chat=SimpleNamespace(type=chat_type))
    return update, query


def _message(text="", entities=None, sticker=None, caption_entities=None):
    return SimpleNamespace(text=text, entities=entities, caption_entities=caption_entities,
                           sticker=sticker, reply_text=AsyncMock(), reply_html=AsyncMock())


def _emoji_entity(emoji_id):
    return SimpleNamespace(type="custom_emoji", custom_emoji_id=emoji_id)


# ── the icon key: the emoji a label starts with ────────────────────────────
@pytest.mark.parametrize("label,expected", [
    ("⚙️ Settings", "⚙️"),                       # variation selector
    ("📊 Post prices now", "📊"),
    ("🟢 BUY {PRICE} {NICK}", "🟢"),
    ("🚪 Del Join/Left msgs: ON ✅", "🚪"),
    ("⏰ Delete after 24h", "⏰"),                # U+23F0, outside the pictograph block
    ("⬅️ Back", "⬅️"),
    ("♻️ Reset buttons to default", "♻️"),
    ("Add button", ""),                          # no emoji → nothing to key on
    ("   ", ""),
    ("", ""),
])
def test_the_icon_key_is_the_leading_emoji(bot, label, expected):
    assert bot.leading_emoji(label) == expected
    assert bot.icon_key(label) == (expected if len(expected.encode()) <= 48 else "")


def test_an_over_long_emoji_sequence_is_rejected_as_a_key(bot):
    """callback_data is capped at 64 bytes, so a giant key must not be stored."""
    long_key = "👨‍👩‍👧‍👦" * 8
    assert bot.icon_key(long_key) == ""
    assert bot.normalize_button_icons({long_key: {"icon": "123"}}) == {}


# ── setting an icon restyles every button that uses it ─────────────────────
def test_an_icon_is_shown_on_the_group_post_buttons(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["button_icons"] = {
        "🟢": {"icon": "5368324170671202286", "style": "success"},
        "🔴": {"icon": "5368324170671202287", "style": "danger"},
    }

    buttons = {b["text"].split()[0]: b for b in _extras(bot.report_keyboard(prices))}

    assert buttons["🟢"]["icon_custom_emoji_id"] == "5368324170671202286"
    assert buttons["🟢"]["style"] == "success"
    assert buttons["🔴"]["icon_custom_emoji_id"] == "5368324170671202287"
    assert buttons["🔴"]["style"] == "danger"


def test_an_icon_is_shown_on_the_admin_panel_and_menus(bot):
    """The same setting covers the panel and the menus — “every button”."""
    bot.state["settings"]["button_icons"] = {"⚙️": {"icon": "5368324170671202288"},
                                             "🔘": {"icon": "5368324170671202289"},
                                             "📝": {"icon": "5368324170671202290"}}

    panel = {b["text"].split()[0]: b for b in _extras(bot.panel())}
    settings = {b["text"].split()[0]: b for b in _extras(bot.settings_kb())}
    buttons_menu = {b["text"].split()[0]: b for b in _extras(bot.buttons_menu_kb())}

    assert panel["⚙️"]["icon_custom_emoji_id"] == "5368324170671202288"
    assert panel["📝"]["icon_custom_emoji_id"] == "5368324170671202290"
    assert settings["🔘"]["icon_custom_emoji_id"] == "5368324170671202289"
    assert buttons_menu["🔘"]["icon_custom_emoji_id"] == "5368324170671202289"


def test_buttons_with_an_unknown_or_missing_emoji_stay_plain(bot):
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286"}}

    plain = [b for b in _extras(bot.panel()) if b["text"].startswith("👁")]

    assert plain and all("icon_custom_emoji_id" not in b for b in plain)
    assert bot.B("no emoji here", callback_data="x").to_dict() == {"text": "no emoji here",
                                                                  "callback_data": "x"}


def test_extra_buttons_inherit_the_icon_of_their_own_label(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["extra_buttons"] = [
        {"id": "abc123", "label": "💬 Support", "url": "https://t.me/support"},
        {"id": "def456", "label": "🌐 Website", "url": "https://example.com"},
    ]
    bot.state["settings"]["button_icons"] = {"💬": {"icon": "5368324170671202299",
                                                    "style": "primary"}}

    extras = [b for b in _extras(bot.report_keyboard(prices)) if b["text"].startswith("💬")]

    assert extras and all(b["icon_custom_emoji_id"] == "5368324170671202299" for b in extras)
    assert all(b["style"] == "primary" for b in extras)


def test_a_button_can_override_or_force_a_style(bot):
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "1", "style": "success"}}

    forced = bot.B("🟢 BUY", url="https://a.example", style="danger").to_dict()
    plain = bot.B("x", url="https://a.example", icon="").to_dict()

    assert forced["style"] == "danger"                      # explicit style wins
    assert forced["icon_custom_emoji_id"] == "1"
    assert "icon_custom_emoji_id" not in plain              # explicit emoji key: none set


# ── saved state is sanitised, never trusted ────────────────────────────────
def test_malformed_icon_state_is_cleaned_up(bot):
    bot.state["settings"]["button_icons"] = {
        "🟢": {"icon": "5368324170671202286", "style": "success"},
        "🔴": "5368324170671202287",                        # a bare id is accepted
        "⚙️": {"icon": "not-a-number", "style": "rainbow"},  # dropped: both fields invalid
        "👁": {"style": "danger"},                          # colour only is kept
        "no emoji": {"icon": "1"},                          # not a usable key
        "🚪": "also not an id",
    }

    assert bot.button_icons() == {
        "🟢": {"icon": "5368324170671202286", "style": "success"},
        "🔴": {"icon": "5368324170671202287", "style": ""},
        "👁": {"icon": "", "style": "danger"},
    }


def test_broken_icon_state_cannot_break_a_menu(bot, monkeypatch):
    """get_settings() is read while building every keyboard: it must never raise."""
    monkeypatch.setattr(bot, "get_settings", Mock(side_effect=RuntimeError("boom")))

    button = bot.B("⚙️ Settings", callback_data="settings")

    assert button.to_dict() == {"text": "⚙️ Settings", "callback_data": "settings"}


def test_load_sanitises_icons_and_the_banner(bot, monkeypatch):
    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"button_icons": {"🟢": {"icon": "77"}}, "post_photo": "  not a file id  "},
    })

    loaded = bot.load()

    assert loaded["settings"]["button_icons"] == {"🟢": {"icon": "77", "style": ""}}
    assert loaded["settings"]["post_photo"] == ""


# ── the 🖼 screens ─────────────────────────────────────────────────────────
def test_the_icons_screen_lists_the_emoji_the_bot_actually_uses(bot):
    keys = bot.emoji_in_use()
    listed = [b.text.rstrip(" ✅") for b in _buttons(bot.button_icons_kb()) if b.callback_data]

    assert "🟢" in keys and "⚙️" in keys and "🖼" in keys
    assert [k for k in listed if k in keys] == [k for k in keys if k in listed]
    assert bot.state["settings"]["button_icons"] == {}
    assert "one entry covers every button" in bot.button_icons_text()
    assert "Fragment" in bot.button_icons_text()             # the Premium requirement


def test_the_icons_screen_marks_configured_emoji(bot):
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286"}}

    marked = [b.text for b in _buttons(bot.button_icons_kb()) if b.callback_data == "icon_menu:🟢"]

    assert marked == ["🟢 ✅"]


def test_the_editor_names_the_buttons_it_affects(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286",
                                                    "style": "success"}}

    text = bot.icon_editor_text("🟢")

    assert "🟢 BUY {PRICE} {NICK}" in text                   # the group post template
    assert "5368324170671202286" in text and "green 🟢" in text
    assert "Forward" in text and "paste the numeric id" in text.lower()


def test_tapping_an_emoji_opens_its_editor(bot):
    update, query = _admin_update("icon_menu:🟢")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert "Icon for 🟢 buttons" in query.edit_message_text.await_args.args[0]
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for b in _buttons(keyboard)]
    assert "icon_set:🟢" in callbacks and "icon_style:🟢" in callbacks
    assert "button_icons" in callbacks                        # a way back to the list


def test_setting_an_icon_asks_for_the_emoji_and_can_be_cancelled(bot):
    update, query = _admin_update("icon_set:🟢")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert bot.edit_get(update, "awaiting_custom") == "icon:🟢"
    assert "Send the icon for 🟢 buttons" in query.edit_message_text.await_args.args[0]
    cancel = [b.callback_data for b in
              query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[0]]
    assert cancel == ["icon_menu:🟢"]


def test_the_colour_button_cycles_through_the_three_styles(bot):
    update, query = _admin_update("icon_style:🟢")
    seen = []
    for _ in range(4):
        asyncio.run(bot.on_button(update, SimpleNamespace()))
        seen.append((bot.button_icons().get("🟢") or {}).get("style", ""))

    assert seen == ["success", "danger", "primary", ""]      # …and back to default
    assert bot.button_icons() == {}                          # nothing left to store


def test_an_icon_is_removed_without_touching_the_others(bot):
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "1"}, "🔴": {"icon": "2"}}
    update, query = _admin_update("icon_clear:🟢")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert bot.button_icons() == {"🔴": {"icon": "2", "style": ""}}


# ── the two ways to set an icon ────────────────────────────────────────────
def test_forwarding_the_emoji_stores_its_id(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message("🟢", entities=[_emoji_entity("5368324170671202286")]))
    bot.edit_set(update, "awaiting_custom", "icon:🟢")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.button_icons()["🟢"]["icon"] == "5368324170671202286"
    assert bot.edit_get(update, "awaiting_custom") is None   # the prompt is done
    assert "Icon saved for 🟢 buttons" in update.message.reply_html.await_args.args[0]


def test_pasting_the_numeric_id_works_too(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message("5368324170671202286"))
    bot.edit_set(update, "awaiting_custom", "icon:🟢")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.button_icons()["🟢"]["icon"] == "5368324170671202286"


def test_a_plain_emoji_has_no_id_and_says_so(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message("😀"))
    bot.edit_set(update, "awaiting_custom", "icon:🟢")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.button_icons() == {}
    assert "No custom emoji found" in update.message.reply_text.await_args.args[0]
    assert bot.edit_get(update, "awaiting_custom") == "icon:🟢"    # still waiting


def test_a_premium_emoji_sticker_sets_the_icon(bot):
    sticker = SimpleNamespace(custom_emoji_id="5368324170671202286")
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message(sticker=sticker))
    bot.edit_set(update, "awaiting_custom", "icon:🔴")

    asyncio.run(bot.on_sticker(update, SimpleNamespace()))

    assert bot.button_icons()["🔴"]["icon"] == "5368324170671202286"


def test_a_normal_sticker_is_refused(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message(sticker=SimpleNamespace(custom_emoji_id=None)))
    bot.edit_set(update, "awaiting_custom", "icon:🔴")

    asyncio.run(bot.on_sticker(update, SimpleNamespace()))

    assert bot.button_icons() == {}
    assert "not a custom emoji" in update.message.reply_text.await_args.args[0]


def test_icon_input_is_ignored_outside_the_prompt(bot):
    """An emoji or an id sent while none was asked for must not be saved."""
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message("5368324170671202286",
                                              entities=[_emoji_entity("99")]))
    asyncio.run(bot.on_text(update, SimpleNamespace()))
    asyncio.run(bot.on_sticker(update, SimpleNamespace()))

    assert bot.button_icons() == {}
    assert bot.edit_get(update, "awaiting_custom") is None
    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert not any("Icon saved" in str(reply) for reply in replies)
    assert "99" not in json.dumps(bot.state["settings"]["button_icons"])


def test_a_stranger_cannot_set_icons(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999999),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message("5368324170671202286"))
    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.button_icons() == {}


# ── the post banner ────────────────────────────────────────────────────────
def test_sending_a_photo_sets_the_banner(bot):
    photos = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="AgACAgIAAxkBAAICbig")]
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message(), )
    update.message.photo = photos
    bot.edit_set(update, "awaiting_custom", "banner_photo")

    asyncio.run(bot.on_photo(update, SimpleNamespace()))

    assert bot.post_banner() == "AgACAgIAAxkBAAICbig"        # the largest size
    assert bot.edit_get(update, "awaiting_custom") is None
    assert "Banner saved" in update.message.reply_html.await_args.args[0]


def test_a_photo_sent_without_being_asked_for_is_ignored(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message())
    update.message.photo = [SimpleNamespace(file_id="AgACAgIAAxkBAAICbig")]

    asyncio.run(bot.on_photo(update, SimpleNamespace()))

    assert bot.post_banner() == ""


@pytest.mark.parametrize("value,stored", [
    ("https://cdn.example.com/banner.png", "https://cdn.example.com/banner.png"),
    ("http://cdn.example.com/banner.jpg", "http://cdn.example.com/banner.jpg"),
    ("ftp://cdn.example.com/banner.png", ""),
    ("https://user:pass@cdn.example.com/b.png", ""),         # credentials in a URL: no
    ("https://cdn.example.com/a b.png", ""),                 # spaces: no
    ("not a url", ""),
    ("", ""),
    ("AgACAgIAAxkBAAICbig", "AgACAgIAAxkBAAICbig"),          # a Telegram file id
    ("short", ""),                                           # too short to be a file id
])
def test_only_a_file_id_or_an_http_image_is_stored(bot, value, stored):
    assert bot.clean_banner(value) == stored


def test_the_banner_url_flow_validates_and_saves(bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private"),
                             message=_message("https://cdn.example.com/logo.png"))
    bot.edit_set(update, "awaiting_custom", "banner_url")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.post_banner() == "https://cdn.example.com/logo.png"
    assert bot.edit_get(update, "awaiting_custom") is None

    bad = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                          effective_chat=SimpleNamespace(type="private"),
                          message=_message("nope"))
    bot.edit_set(bad, "awaiting_custom", "banner_url")
    asyncio.run(bot.on_text(bad, SimpleNamespace()))

    assert bot.post_banner() == "https://cdn.example.com/logo.png"
    assert "https://" in bad.message.reply_text.await_args.args[0]


def test_the_banner_screen_shows_the_state_and_its_actions(bot):
    assert "none" in bot.banner_text()
    assert [b.callback_data for b in _buttons(bot.banner_kb())] == ["banner_send", "banner_url",
                                                                    "banner_gif_url", "banner_hd",
                                                                    "button_icons", "preview",
                                                                    "settings"]

    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    callbacks = [b.callback_data for b in _buttons(bot.banner_kb())]
    assert "banner_test" in callbacks and "banner_clear" in callbacks
    assert "caption" in bot.banner_text() and "1024" in bot.banner_text()
    assert "photo" in bot.banner_text()        # a still picture, not a GIF


def test_removing_the_banner_clears_it(bot):
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    update, _ = _admin_update("banner_clear")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert bot.post_banner() == ""


# ── GIF banners ───────────────────────────────────────────────────────────
def _media_update(**attrs):
    """A private admin message carrying the given media attribute."""
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private", id=424242),
                             message=_message())
    update.message.message_id = 10                     # 📤 forwarding needs it
    for key, value in attrs.items():
        setattr(update.message, key, value)
    return update


def _repost_context():
    """A context whose copy_message succeeds, so a 📤 repost can be asserted."""
    return SimpleNamespace(bot=SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
        forward_message=AsyncMock(return_value=SimpleNamespace(message_id=5))))


def test_only_a_photo_or_an_animation_is_a_banner_kind(bot):
    assert bot.clean_banner_kind("animation") == "animation"
    assert bot.clean_banner_kind("PHOTO ") == "photo"
    assert bot.clean_banner_kind("video") == "photo"          # unknown → the safe default
    assert bot.clean_banner_kind(None) == "photo"
    assert bot.banner_kind() == "photo"                       # a fresh install posts photos


def test_a_gif_sent_as_an_animation_becomes_the_banner(bot):
    update = _media_update(animation=SimpleNamespace(file_id="CgACAgIAAxkBAAIBnGif"))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert bot.post_banner() == "CgACAgIAAxkBAAIBnGif"
    assert bot.banner_kind() == "animation"
    assert bot.edit_get(update, "awaiting_custom") is None
    assert "Banner saved" in update.message.reply_html.await_args.args[0]
    assert "GIF" in bot.banner_text()                  # the screen says what is saved


def test_a_gif_sent_as_a_file_becomes_the_banner(bot):
    update = _media_update(document=SimpleNamespace(file_id="BQACAgIAAxkBAAIBnGif",
                                                    mime_type="image/gif", file_name="rates.gif"))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert bot.post_banner() == "BQACAgIAAxkBAAIBnGif"
    assert bot.banner_kind() == "animation"


def test_a_photo_is_still_saved_as_a_photo_banner(bot):
    update = _media_update(photo=[SimpleNamespace(file_id="AgACAgIAAxkBAAICbig")])
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_photo(update, SimpleNamespace()))

    assert bot.post_banner() == "AgACAgIAAxkBAAICbig"
    assert bot.banner_kind() == "photo"


def test_a_gif_nobody_asked_for_is_reposted_instead(bot):
    bot.state["group"] = -100123                       # 📤 forwarding is on by default
    update = _media_update(animation=SimpleNamespace(file_id="CgACAgIAAxkBAAIBnGif"))
    context = _repost_context()

    asyncio.run(bot.on_animation(update, context))

    assert bot.post_banner() == ""
    context.bot.copy_message.assert_awaited_once()


def test_a_gif_url_is_saved_as_an_animation(bot):
    update = _media_update(text="https://cdn.example.com/rates")
    bot.edit_set(update, "awaiting_custom", "banner_gif_url")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.post_banner() == "https://cdn.example.com/rates"
    assert bot.banner_kind() == "animation"
    assert bot.edit_get(update, "awaiting_custom") is None


def test_an_image_url_ending_in_gif_is_an_animation_too(bot):
    update = _media_update(text="https://cdn.example.com/rates.gif?raw=1")
    bot.edit_set(update, "awaiting_custom", "banner_url")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.banner_kind() == "animation"


def test_an_ordinary_image_url_stays_a_photo(bot):
    update = _media_update(text="https://cdn.example.com/logo.png")
    bot.edit_set(update, "awaiting_custom", "banner_url")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.banner_kind() == "photo"


def test_removing_a_gif_banner_clears_the_kind_too(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation")
    update, _ = _admin_update("banner_clear")

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert bot.post_banner() == "" and bot.banner_kind() == "photo"


def test_a_gif_banner_and_a_garbage_kind_are_sanitised_on_load(bot, monkeypatch):
    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "CgACAgIAAxkBAAIBnGif", "post_photo_kind": "animation"}})
    assert bot.load()["settings"]["post_photo_kind"] == "animation"

    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "CgACAgIAAxkBAAIBnGif", "post_photo_kind": "video"}})
    assert bot.load()["settings"]["post_photo_kind"] == "photo"


# ── 📐 full HD: the banner goes out at the size it was uploaded at ─────────
def test_a_fresh_install_posts_the_banner_in_full_hd(bot):
    """HD is the default — nobody should have to find the switch to look sharp."""
    assert bot.banner_hd() is True
    assert bot.DEFAULT_SETTINGS["post_photo_hd"] is True
    assert bot.banner_send_kwargs() == {}                 # no banner, so nothing to pass


# a size is only trusted when both sides are real numbers; the play length is a
# decoration, so anything odd about it is dropped and the size itself survives
@pytest.mark.parametrize("value,kept", [
    ({"width": 1080, "height": 640, "duration": 3},
     {"width": 1080, "height": 640, "duration": 3}),
    ({"width": 1080.0, "height": 640}, {"width": 1080, "height": 640, "duration": 0}),
    ({"width": "800", "height": "600", "duration": ""},
     {"width": 800, "height": 600, "duration": 0}),
    ({"width": 1080, "height": 640, "duration": True},
     {"width": 1080, "height": 640, "duration": 0}),        # only a decoration → dropped
    ({"width": 1080, "height": 640, "duration": 900},
     {"width": 1080, "height": 640, "duration": 0}),   # a wild play length never costs the size
    ({"width": 1080}, {}),                                # a half-known size is not a size
    ({"width": 1080, "height": 0}, {}),                    # would render an invisible banner
    ({"width": 99999, "height": 640}, {}),                 # past what Telegram accepts
    ({"width": -5, "height": 640}, {}),
    ({}, {}), ("", {}), (None, {}), ([], {}),
])
def test_only_a_believable_size_is_kept(bot, value, kept):
    assert bot.clean_banner_size(value) == kept


def test_an_uploaded_gif_is_remembered_at_its_own_size(bot):
    update = _media_update(animation=SimpleNamespace(file_id="CgACAgIAAxkBAAIBnGif",
                                                      width=1080, height=640, duration=4))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert bot.banner_size() == {"width": 1080, "height": 640, "duration": 4}
    assert "1080×640" in bot.banner_text()                # the screen says what is stored
    assert "Full HD: ON ✅" in bot.banner_text()


def test_an_uploaded_gif_is_posted_at_the_size_it_was_sent_at(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation",
                   {"width": 1080, "height": 640, "duration": 4})
    fake = _FakeBot()

    asyncio.run(bot.send_report(fake, -100123, "📊 P2P USDT/USD", "KB"))

    kind, _, kwargs = fake.calls[0]
    assert kind == "animation"
    assert (kwargs["width"], kwargs["height"], kwargs["duration"]) == (1080, 640, 4)


def test_turning_full_hd_off_holds_the_size_back(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation",
                   {"width": 1080, "height": 640, "duration": 4})
    bot.state["settings"]["post_photo_hd"] = False
    fake = _FakeBot()

    asyncio.run(bot.send_report(fake, -100123, "📊 P2P USDT/USD", "KB"))

    assert "width" not in fake.calls[0][2] and "height" not in fake.calls[0][2]
    assert "Full HD: OFF ❌" in bot.banner_text()


def test_a_photo_banner_needs_no_size_field_but_keeps_its_largest_copy(bot):
    """``sendPhoto`` has no dimensions — the HD photo copy is stored and posted as is."""
    photos = [SimpleNamespace(file_id="small", width=100, height=100),
              SimpleNamespace(file_id="AgACAgIAAxkBAAICbig", width=1600, height=900),
              SimpleNamespace(file_id="medium", width=400, height=400)]
    update = _media_update(photo=photos)
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_photo(update, SimpleNamespace()))

    assert bot.post_banner() == "AgACAgIAAxkBAAICbig"     # the largest, not simply the last
    assert bot.banner_size() == {"width": 1600, "height": 900, "duration": 0}

    fake = _FakeBot()
    asyncio.run(bot.send_report(fake, -100123, "📊 P2P USDT/USD", "KB"))

    kind, _, kwargs = fake.calls[0]
    assert kind == "photo"
    assert "width" not in kwargs and "duration" not in kwargs


def test_a_gif_sent_as_a_file_claims_no_size_it_does_not_have(bot):
    """A GIF that arrives as a document carries no dimensions — nothing is invented."""
    update = _media_update(document=SimpleNamespace(file_id="BQACAgIAAxkBAAIBnGif",
                                                     mime_type="image/gif",
                                                     file_name="rates.gif"))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert bot.banner_kind() == "animation"
    assert bot.banner_size() == {}
    assert bot.banner_send_kwargs() == {}                  # Telegram reads the file itself
    assert "nothing to downscale" in bot.banner_text()


def test_a_new_banner_never_inherits_the_size_of_the_old_one(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation",
                   {"width": 1080, "height": 640, "duration": 4})
    update = _media_update(text="https://cdn.example.com/rates.gif")
    bot.edit_set(update, "awaiting_custom", "banner_gif_url")

    asyncio.run(bot.on_text(update, SimpleNamespace()))

    assert bot.post_banner() == "https://cdn.example.com/rates.gif"
    assert bot.banner_size() == {}
    assert bot.banner_send_kwargs() == {}


def test_the_hd_switch_flips_saves_and_relabels_itself(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation",
                   {"width": 720, "height": 720, "duration": 2})
    update, query = _admin_update("banner_hd")

    assert bot.banner_hd_label() == "ON ✅"
    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert bot.banner_hd() is False
    assert query.answer.await_args.args[0].startswith("📐 Full HD OFF")
    assert bot.banner_kb().inline_keyboard[2][0].text == "📐 Post in full HD: OFF ❌"
    assert bot.STORE.load()["settings"]["post_photo_hd"] is False      # survives a restart


def test_turning_full_hd_off_reposts_unchanged_prices(bot, merchant, prices, monkeypatch):
    """The post looks different, so it goes out again — same rule as a new banner."""
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    fake.delete_message = AsyncMock()
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation",
                   {"width": 1080, "height": 640, "duration": 4})

    assert asyncio.run(bot.post(fake)) is True
    assert asyncio.run(bot.post(fake)) is False                        # unchanged
    assert fake.calls[-1][2]["width"] == 1080

    assert bot.toggle_banner_hd() is False
    assert asyncio.run(bot.post(fake)) is True
    assert "width" not in fake.calls[-1][2]


def test_the_test_post_says_what_size_the_gif_went_out_at(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation",
                   {"width": 1080, "height": 640, "duration": 4})
    fake = _FakeBot()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private", id=424242))

    asyncio.run(bot.banner_test(update, SimpleNamespace(bot=fake)))

    assert "1080×640" in fake.calls[-1][2]["text"]


def test_a_saved_size_and_hd_flag_are_sanitised_on_load(bot, monkeypatch):
    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "CgACAgIAAxkBAAIBnGif",
                     "post_photo_size": {"width": 1080, "height": "tall", "duration": 4}}})
    assert bot.load()["settings"]["post_photo_size"] == {}      # not a size → not trusted

    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "CgACAgIAAxkBAAIBnGif", "post_photo_kind": "animation",
                     "post_photo_size": {"width": 1080, "height": 640, "duration": 4},
                     "post_photo_hd": "yes"}})
    loaded = bot.load()

    assert loaded["settings"]["post_photo_size"] == {"width": 1080, "height": 640, "duration": 4}
    assert loaded["settings"]["post_photo_hd"] is True         # a non-bool → the default


def test_a_size_never_survives_the_banner_it_belongs_to(bot, monkeypatch):
    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "", "post_photo_size": {"width": 1080, "height": 640}}})

    assert bot.load()["settings"]["post_photo_size"] == {}


def test_the_fields_are_real_telegram_fields(bot):
    """A made-up field would cost every banner post — only what Telegram knows is sent."""
    import inspect

    from telegram import Bot
    animation = inspect.signature(Bot.send_animation).parameters
    document = inspect.signature(Bot.send_document).parameters

    assert {"width", "height", "duration"} <= set(animation)
    assert {"document", "caption", "parse_mode", "reply_markup"} <= set(document)



class _FakeBot:
    """Records what would be sent to the group."""

    def __init__(self, fail_photo=False):
        self.calls, self.fail_photo = [], fail_photo

    async def send_photo(self, chat_id, **kwargs):
        if self.fail_photo:
            raise RuntimeError("Bad Request: photo not found")
        self.calls.append(("photo", chat_id, kwargs))
        return SimpleNamespace(message_id=7, photo=[object()], caption=kwargs.get("caption"))

    async def send_animation(self, chat_id, **kwargs):
        if self.fail_photo:
            raise RuntimeError("Bad Request: animation not found")
        self.calls.append(("animation", chat_id, kwargs))
        return SimpleNamespace(message_id=7, photo=None, animation=object(),
                               caption=kwargs.get("caption"))

    async def send_document(self, chat_id, **kwargs):
        # the file is the *rescue* for an animation Telegram refuses to play, so it
        # works here unless a test says the upload itself is gone
        self.calls.append(("document", chat_id, kwargs))
        return SimpleNamespace(message_id=7, photo=None, animation=None, document=object(),
                               caption=kwargs.get("caption"))

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("text", chat_id, {"text": text, **kwargs}))
        return SimpleNamespace(message_id=8, photo=None)


def test_send_report_posts_the_banner_with_the_report_as_caption(bot):
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    fake = _FakeBot()

    asyncio.run(bot.send_report(fake, -100123, "📊 P2P USDT/USD\n🔴 0.999", "KB"))

    kind, chat_id, kwargs = fake.calls[0]
    assert (kind, chat_id) == ("photo", -100123)
    assert kwargs["caption"].startswith("📊 P2P")
    assert kwargs["parse_mode"] == "HTML" and kwargs["reply_markup"] == "KB"


def test_a_report_longer_than_a_caption_is_posted_without_the_banner(bot):
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    fake = _FakeBot()
    long_report = "x" * (bot.CAPTION_LIMIT + 1)

    asyncio.run(bot.send_report(fake, -100123, long_report, "KB"))

    kind, _, kwargs = fake.calls[0]
    assert kind == "text" and kwargs["text"] == long_report


def test_a_banner_telegram_refuses_falls_back_to_text(bot):
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    fake = _FakeBot(fail_photo=True)

    asyncio.run(bot.send_report(fake, -100123, "short report", "KB"))

    assert [call[0] for call in fake.calls] == ["text"]


def test_a_gif_banner_is_posted_as_an_animation(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation")
    fake = _FakeBot()

    asyncio.run(bot.send_report(fake, -100123, "📊 P2P USDT/USD\n🔴 0.999", "KB"))

    kind, chat_id, kwargs = fake.calls[0]
    assert (kind, chat_id) == ("animation", -100123)
    assert kwargs["animation"] == "CgACAgIAAxkBAAIBnGif"
    assert kwargs["caption"].startswith("📊 P2P")
    assert kwargs["parse_mode"] == "HTML" and kwargs["reply_markup"] == "KB"


def test_a_gif_banner_telegram_refuses_falls_back_to_text(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation")
    fake = _FakeBot(fail_photo=True)

    asyncio.run(bot.send_report(fake, -100123, "short report", "KB"))

    assert [call[0] for call in fake.calls] == ["text"]


# ── 📎 a GIF that came in as a file is never traded for a text post ────────
def test_a_file_gif_the_player_refuses_is_posted_as_the_file(bot):
    """A document id is not always playable — the file itself still carries the banner."""
    bot.set_banner("BQACAgIAAxkBAAIBnGif", "animation", {}, raw=True)
    fake = _FakeBot(fail_photo=True)

    asyncio.run(bot.send_report(fake, -100123, "📊 P2P USDT/USD\n🔴 0.999", "KB"))

    kind, chat_id, kwargs = fake.calls[0]
    assert (kind, chat_id) == ("document", -100123)
    assert kwargs["document"] == "BQACAgIAAxkBAAIBnGif"
    assert kwargs["caption"].startswith("📊 P2P")
    assert kwargs["parse_mode"] == "HTML" and kwargs["reply_markup"] == "KB"
    assert "width" not in kwargs                       # a file has no size to pass


@pytest.mark.parametrize("banner,raw", [
    ("https://cdn.example.com/rates.gif", False),      # Telegram downloads the link
    ("CgACAgIAAxkBAAIBnGif", False),                   # a GIF sent as a GIF
])
def test_only_a_file_id_is_rescued_as_a_file(bot, banner, raw):
    """The rescue is for the one id type that needs it — anything else is really gone."""
    bot.set_banner(banner, "animation", {}, raw=raw)
    fake = _FakeBot(fail_photo=True)

    asyncio.run(bot.send_report(fake, -100123, "short report", "KB"))

    assert [call[0] for call in fake.calls] == ["text"]


def test_a_gone_upload_loses_the_banner_the_way_it_always_did(bot):
    """File refused too (deleted upload): the post still goes out, as text."""

    class _FileGoneToo(_FakeBot):
        async def send_document(self, chat_id, **kwargs):
            self.calls.append(("document", chat_id, kwargs))     # tried…
            raise RuntimeError("Bad Request: file not found")    # …the upload is really gone

    bot.set_banner("BQACAgIAAxkBAAIBnGif", "animation", {}, raw=True)
    fake = _FileGoneToo(fail_photo=True)

    asyncio.run(bot.send_report(fake, -100123, "short report", "KB"))

    assert [call[0] for call in fake.calls] == ["document", "text"]


def test_an_icon_refusal_is_not_mistaken_for_a_refused_banner(bot, merchant, prices, monkeypatch):
    """The buttons are the problem there — rebuilding them beats sending a file."""
    monkeypatch.setattr(bot, "_rejected_icons", None)
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286"}}
    bot.set_banner("BQACAgIAAxkBAAIBnGif", "animation", {}, raw=True)

    class _PickyAboutIcons(_FakeBot):
        """Refuses an animation whose buttons carry an icon, then accepts it."""

        @staticmethod
        def _has_icons(kwargs):
            rows = getattr(kwargs.get("reply_markup"), "inline_keyboard", None) or []
            return any(b.to_dict().get("icon_custom_emoji_id") for row in rows for b in row)

        async def send_animation(self, chat_id, **kwargs):
            if self._has_icons(kwargs):
                raise RuntimeError("Bad Request: BUTTON_ICON_CUSTOM_EMOJI_ID_INVALID")
            return await super().send_animation(chat_id, **kwargs)

    fake = _PickyAboutIcons()

    asyncio.run(bot.send_report(fake, -100123, "report", bot.report_keyboard(prices),
                                rebuild=lambda: bot.report_keyboard(prices)))

    assert [call[0] for call in fake.calls] == ["animation"]     # played after the rebuild
    assert bot.icons_available() is False                        # remembered, as everywhere else


def test_a_gif_sent_as_media_is_not_marked_as_a_file(bot):
    update = _media_update(animation=SimpleNamespace(file_id="CgACAgIAAxkBAAIBnGif",
                                                      width=1080, height=640, duration=4))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert bot.banner_raw() is False


def test_a_gif_sent_as_a_file_is_marked_so_the_post_can_be_rescued(bot):
    update = _media_update(document=SimpleNamespace(file_id="BQACAgIAAxkBAAIBnGif",
                                                     mime_type="image/gif",
                                                     file_name="rates.gif"))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert bot.banner_raw() is True
    assert "Posted from your file" in bot.banner_text()     # the screen says how it is delivered


def test_a_playable_gif_has_nothing_to_explain(bot):
    update = _media_update(animation=SimpleNamespace(file_id="CgACAgIAAxkBAAIBnGif",
                                                      width=1080, height=640, duration=4))
    bot.edit_set(update, "awaiting_custom", "banner_media")

    asyncio.run(bot.on_animation(update, SimpleNamespace()))

    assert "Posted from your file" not in bot.banner_text()


def test_the_rescue_flag_belongs_to_the_banner_it_was_set_for(bot):
    assert bot.banner_raw() is False
    bot.set_banner("BQACAgIAAxkBAAIBnGif", "animation", {}, raw=True)
    assert bot.banner_raw() is True
    bot.set_banner("AgACAgIAAxkBAAICbig", "photo")               # a photo has its own method
    assert bot.banner_raw() is False
    bot.set_banner("BQACAgIAAxkBAAIBnGif", "animation", {}, raw=True)
    bot.set_banner("", "photo")                                  # removed with the banner
    assert bot.banner_raw() is False


@pytest.mark.parametrize("banner,kind,raw,kept", [
    ("BQACAgIAAxkBAAIBnGif", "animation", True, True),
    ("BQACAgIAAxkBAAIBnGif", "animation", "yes", True),
    ("BQACAgIAAxkBAAIBnGif", "animation", False, False),
    ("https://cdn.example.com/rates.gif", "animation", True, False),   # not an upload
    ("AgACAgIAAxkBAAICbig", "photo", True, False),                     # not a GIF
    ("", "animation", True, False),                                    # no banner at all
])
def test_the_rescue_flag_is_only_kept_where_it_means_something(bot, banner, kind, raw, kept):
    assert bot.clean_banner_raw(raw, banner, kind) is kept


def test_the_rescue_flag_is_sanitised_with_the_banner(bot, monkeypatch):
    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "BQACAgIAAxkBAAIBnGif", "post_photo_kind": "animation",
                     "post_photo_raw": True}})
    assert bot.load()["settings"]["post_photo_raw"] is True

    monkeypatch.setattr(bot.STORE, "load", lambda: {
        "settings": {"post_photo": "https://cdn.example.com/rates.gif",
                     "post_photo_kind": "animation", "post_photo_raw": True}})
    assert bot.load()["settings"]["post_photo_raw"] is False


def test_the_test_post_says_a_refused_gif_went_out_as_a_file(bot):
    bot.set_banner("BQACAgIAAxkBAAIBnGif", "animation", {}, raw=True)
    fake = _FakeBot(fail_photo=True)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private", id=424242))

    asyncio.run(bot.banner_test(update, SimpleNamespace(bot=fake)))

    assert [call[0] for call in fake.calls] == ["document", "text"]
    note = fake.calls[-1][2]["text"]
    assert "your banner" in note and "📎" in note


def test_a_gif_report_longer_than_a_caption_drops_the_gif(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation")
    fake = _FakeBot()
    long_report = "x" * (bot.CAPTION_LIMIT + 1)

    asyncio.run(bot.send_report(fake, -100123, long_report, "KB"))

    assert fake.calls[0][0] == "text"


def test_the_banner_test_recognises_a_gif(bot):
    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation")
    fake = _FakeBot()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private", id=424242))

    asyncio.run(bot.banner_test(update, SimpleNamespace(bot=fake)))

    assert [call[0] for call in fake.calls] == ["animation", "text"]
    assert "your banner" in fake.calls[-1][2]["text"]


def test_without_a_banner_nothing_changes(bot):
    fake = _FakeBot()

    asyncio.run(bot.send_report(fake, -100123, "short report", "KB"))

    assert fake.calls[0][0] == "text"


def test_the_group_post_uses_the_banner_and_reacts_to_a_new_one(bot, merchant, prices, monkeypatch):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    fake.delete_message = AsyncMock()

    assert asyncio.run(bot.post(fake)) is True
    assert fake.calls[-1][0] == "text"                       # no banner yet

    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    assert asyncio.run(bot.post(fake)) is True                # changing it reposts
    assert fake.calls[-1][0] == "photo"
    assert fake.calls[-1][2]["reply_markup"] is not None      # buttons stay under the photo


def test_the_preview_uses_the_same_delivery_as_the_group(bot, merchant, prices, monkeypatch):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private", id=424242),
                             message=SimpleNamespace(reply_html=AsyncMock()))

    asyncio.run(bot.preview_cmd(update, SimpleNamespace(bot=fake)))

    assert fake.calls[0][0] == "photo"
    assert fake.calls[0][1] == 424242


def test_the_banner_test_says_which_delivery_it_used(bot, monkeypatch):
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    fake = _FakeBot()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             effective_chat=SimpleNamespace(type="private", id=424242))

    asyncio.run(bot.banner_test(update, SimpleNamespace(bot=fake)))

    assert [call[0] for call in fake.calls] == ["photo", "text"]
    assert "your banner" in fake.calls[-1][2]["text"]


def test_the_banner_is_in_the_change_snapshot(bot, merchant, prices, monkeypatch):
    """A new banner must repost even when the prices did not move."""
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    fake.delete_message = AsyncMock()

    assert asyncio.run(bot.post(fake)) is True
    assert asyncio.run(bot.post(fake)) is False               # unchanged
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    assert asyncio.run(bot.post(fake)) is True


def test_swapping_a_photo_banner_for_a_gif_reposts(bot, merchant, prices, monkeypatch):
    """The prices did not move, but the post looks different — so it goes out again."""
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    fake.delete_message = AsyncMock()

    bot.set_banner("AgACAgIAAxkBAAICbig", "photo")
    assert asyncio.run(bot.post(fake)) is True
    assert asyncio.run(bot.post(fake)) is False               # unchanged

    bot.set_banner("CgACAgIAAxkBAAIBnGif", "animation")
    assert asyncio.run(bot.post(fake)) is True
    assert fake.calls[-1][0] == "animation"


def test_the_preview_button_shows_the_banner_too(bot, merchant, prices, monkeypatch):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["post_photo"] = "AgACAgIAAxkBAAICbig"
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    update, query = _admin_update("preview", query_message=SimpleNamespace(chat_id=424242))

    asyncio.run(bot.on_button(update, SimpleNamespace(bot=fake)))

    assert fake.calls[0][0] == "photo"
    assert "Preview" in fake.calls[0][2]["caption"]


def test_the_settings_menu_offers_both_screens(bot):
    callbacks = [b.callback_data for b in _buttons(bot.settings_kb())]
    texts = bot.settings_text()

    assert "button_icons" in callbacks and "banner_menu" in callbacks
    assert "Button icons" in texts and "Post banner" in texts
    assert "button_icons" in [b.callback_data for b in _buttons(bot.buttons_menu_kb())]
    assert "Button icons" in bot.buttons_menu_text()


def test_banner_photo_and_icon_ids_are_never_rendered_as_markup(bot, monkeypatch):
    """These values are interpolated into HTML messages — they must be escaped."""
    bot.state["settings"]["post_photo"] = "https://cdn.example.com/<script>.png"
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "77"}}

    assert "<script>" not in bot.banner_text()
    assert "<script>" not in json.dumps(bot.button_icons())
    assert bot.button_icon("🟢 BUY") == ("77", "")


# ── a Telegram that refuses the icons must not cost the price post ─────────
class _Pickier(_FakeBot):
    """Refuses any button carrying an icon — some bots are not allowed to use them."""

    def __init__(self):
        super().__init__()
        self.refused = 0

    @staticmethod
    def _check(kwargs):
        """PTB 21.6 has no attribute for the field — read what it would send."""
        keyboard = kwargs.get("reply_markup")
        rows = getattr(keyboard, "inline_keyboard", None) or []
        return any(button.to_dict().get("icon_custom_emoji_id") for row in rows for button in row)

    async def send_message(self, chat_id, text, **kwargs):
        if self._check(kwargs):
            self.refused += 1
            raise RuntimeError("Bad Request: BUTTON_ICON_CUSTOM_EMOJI_ID_INVALID")
        return await super().send_message(chat_id, text, **kwargs)

    async def send_photo(self, chat_id, **kwargs):
        if self._check(kwargs):
            self.refused += 1
            raise RuntimeError("Bad Request: BUTTON_ICON_CUSTOM_EMOJI_ID_INVALID")
        return await super().send_photo(chat_id, **kwargs)


def test_a_refused_icon_is_retried_with_plain_buttons(bot, merchant, prices, monkeypatch):
    monkeypatch.setattr(bot, "_rejected_icons", None)
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286"}}
    fake = _Pickier()

    asyncio.run(bot.send_report(fake, -100123, "report", bot.report_keyboard(prices),
                                rebuild=lambda: bot.report_keyboard(prices)))

    assert fake.refused == 1                                  # the first attempt was refused
    assert fake.calls[0][0] == "text"                         # …and the post still went out
    buttons = [b for row in fake.calls[0][2]["reply_markup"].inline_keyboard for b in row]
    assert all("icon_custom_emoji_id" not in b.to_dict() for b in buttons)
    assert bot.icons_available() is False                     # remembered for this process
    assert bot.B("🟢 BUY", url="https://a.example").to_dict().get("icon_custom_emoji_id") is None


def test_a_post_retries_with_plain_buttons_when_icons_are_refused(bot, merchant, prices, monkeypatch):
    """The whole group post path, not just send_report."""
    monkeypatch.setattr(bot, "_rejected_icons", None)
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286"}}
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _Pickier()
    fake.delete_message = AsyncMock()

    assert asyncio.run(bot.post(fake)) is True

    assert fake.refused == 1 and fake.calls[0][0] == "text"
    assert bot.icons_available() is False


def test_other_send_errors_are_not_swallowed(bot, monkeypatch):
    monkeypatch.setattr(bot, "_rejected_icons", None)

    class Broken(_FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("Forbidden: bot was kicked from the group chat")

    with pytest.raises(RuntimeError, match="kicked"):
        asyncio.run(bot.send_report(Broken(), -100123, "report", "KB",
                                    rebuild=lambda: "KB2"))
    assert bot.icons_available() is True                      # not an icon problem


def test_editing_the_icons_lets_the_bot_try_again(bot, monkeypatch):
    """The refusal must not stick: a changed (or removed) icon is retried."""
    monkeypatch.setattr(bot, "_rejected_icons", None)
    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202286"}}
    bot.disable_button_icons("Bad Request: BUTTON_ICON_CUSTOM_EMOJI_ID_INVALID")

    assert bot.icons_available() is False
    assert bot.B("🟢 BUY", url="https://a.example").to_dict() == {"text": "🟢 BUY",
                                                                 "url": "https://a.example"}

    bot.state["settings"]["button_icons"] = {"🟢": {"icon": "5368324170671202299"}}   # edited

    assert bot.icons_available() is True
    assert bot.B("🟢 BUY", url="https://a.example").to_dict()["icon_custom_emoji_id"] == \
        "5368324170671202299"

    bot.state["settings"]["button_icons"] = {}                                        # removed

    assert bot.icons_available() is True
    assert "icon_custom_emoji_id" not in bot.B("🟢 BUY", url="https://a.example").to_dict()
