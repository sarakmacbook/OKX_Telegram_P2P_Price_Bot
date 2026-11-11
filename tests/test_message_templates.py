"""🎨 Message templates: ready-made header/body/footer looks for the group post.

The presets are the one place where bot-written text is fed through the same
placeholder renderer an admin's own text goes through, so these tests pin the
contract both ways: a preset never uses a placeholder the renderer cannot fill,
and applying one replaces exactly the three custom-message fields.
"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]+)\}")


def _placeholders(text):
    return set(PLACEHOLDER_RE.findall(text or ""))


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _admin_update(data):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(chat_id=424242))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=424242),
                             callback_query=query,
                             effective_chat=SimpleNamespace(type="private"))
    return update, query


def _tap(bot, data):
    update, query = _admin_update(data)
    asyncio.run(bot.on_button(update, SimpleNamespace()))
    return query


# ── the presets ────────────────────────────────────────────────────────────
def test_the_templates_are_complete_and_uniquely_named(bot):
    templates = bot.MESSAGE_TEMPLATES
    assert templates and "classic" in templates
    titles = [tmpl["title"] for tmpl in templates.values()]
    assert len(titles) == len(set(titles))
    for template_id, tmpl in templates.items():
        assert re.fullmatch(r"[a-z0-9_]{1,16}", template_id)
        assert tmpl["title"] and tmpl["about"]
        assert tmpl["header"].strip()
        assert tmpl["body"].strip()
        assert isinstance(tmpl["footer"], str)
        # callback_data stays far below Telegram's 64-byte limit
        assert len(f"tmpl_view:{template_id}".encode()) < 64


def test_a_header_or_footer_only_uses_placeholders_apply_template_fills(bot):
    for tmpl in bot.MESSAGE_TEMPLATES.values():
        assert _placeholders(tmpl["header"]) <= bot.HEADER_PLACEHOLDERS, tmpl["title"]
        assert _placeholders(tmpl["footer"]) <= bot.HEADER_PLACEHOLDERS, tmpl["title"]


def test_a_body_only_uses_placeholders_apply_body_template_fills(bot):
    for tmpl in bot.MESSAGE_TEMPLATES.values():
        assert _placeholders(tmpl["body"]) <= bot.BODY_PLACEHOLDERS, tmpl["title"]


def test_every_body_placeholder_really_renders(bot, merchant, prices):
    """Every name in BODY_PLACEHOLDERS is filled — the constant cannot drift."""
    template = " ".join("{%s}" % name for name in sorted(bot.BODY_PLACEHOLDERS))
    rendered = bot.apply_body_template(template, merchant, prices[merchant.key])
    assert not PLACEHOLDER_RE.findall(rendered)
    assert "Fast_sonic" in rendered and "0.999" in rendered


def test_the_header_and_footer_render_with_the_configured_pair(bot):
    for tmpl in bot.MESSAGE_TEMPLATES.values():
        assert "{" not in bot.apply_template(tmpl["header"])
        assert "{" not in bot.apply_template(tmpl["footer"])


# ── the screens ────────────────────────────────────────────────────────────
def test_the_custom_menu_and_settings_offer_the_templates_button(bot):
    for keyboard in (bot.custom_menu_kb(), bot.settings_kb()):
        assert "tmpl_menu" in [button.callback_data for button in _buttons(keyboard)]
    assert "Message templates" in bot.custom_menu_text()


def test_the_templates_screen_lists_every_preset(bot):
    listed = [button.callback_data for button in _buttons(bot.templates_kb())]

    for template_id in bot.MESSAGE_TEMPLATES:
        assert f"tmpl_view:{template_id}" in listed
    assert "tmpl_reset" in listed and "custom_menu" in listed
    assert "Built-in default" in bot.templates_text()


def test_the_active_template_is_ticked(bot):
    tmpl = bot.MESSAGE_TEMPLATES["compact"]
    bot.state["settings"].update({f"custom_{part}": tmpl[part]
                                  for part in ("header", "body", "footer")})

    ticked = [button.text for button in _buttons(bot.templates_kb())
              if button.text.endswith("✅")]

    assert ticked == ["🎨 Compact ✅"]
    assert "Compact" in bot.templates_text()


def test_custom_text_is_not_reported_as_a_template(bot):
    bot.state["settings"]["custom_header"] = "my own header"

    assert bot.active_message_template() is None
    assert "Your own text" in bot.templates_text()


def test_a_template_preview_shows_sample_values_and_the_raw_placeholders(bot):
    text = bot.template_text(bot.MESSAGE_TEMPLATES["classic"])

    assert "Fast_sonic" in text and "0.999" in text and "1.001" in text
    assert "&lt;b&gt;" in text                    # the raw template, escaped
    assert "{LINK}" in text and "{SELL}" in text and "{ASSET}" in text
    assert bot.TEMPLATE_SAMPLE_PROFILE in text    # the sample link is clickable


def test_a_template_without_a_footer_says_so(bot):
    assert "(none)" in bot.template_text(bot.MESSAGE_TEMPLATES["minimal"])


def test_the_unknown_template_is_refused(bot):
    query = _tap(bot, "tmpl_view:does_not_exist")

    query.answer.assert_awaited_once_with("Unknown template", show_alert=True)
    query.edit_message_text.assert_not_awaited()


# ── applying one ───────────────────────────────────────────────────────────
def test_applying_a_template_sets_and_saves_the_three_fields(bot):
    tmpl = bot.MESSAGE_TEMPLATES["detailed"]

    query = _tap(bot, "tmpl_apply:detailed")

    settings = bot.state["settings"]
    assert (settings["custom_header"], settings["custom_body"],
            settings["custom_footer"]) == (tmpl["header"], tmpl["body"], tmpl["footer"])
    assert bot.active_message_template() is tmpl
    query.answer.assert_awaited_once_with("✅ Detailed applied")
    # the admin lands back on 📝 Custom Msg with the new text
    assert query.edit_message_text.await_args.kwargs["reply_markup"] == bot.custom_menu_kb()


def test_an_applied_template_survives_a_reload(bot, tmp_path, monkeypatch):
    from storage import FileStore
    monkeypatch.setattr(bot, "STORE", FileStore(tmp_path / "state.json"))
    bot.save()

    _tap(bot, "tmpl_apply:khmer")

    saved = FileStore(tmp_path / "state.json").load()["settings"]
    assert saved["custom_header"] == bot.MESSAGE_TEMPLATES["khmer"]["header"]
    assert saved["custom_body"] == bot.MESSAGE_TEMPLATES["khmer"]["body"]


def test_applying_a_template_abandons_a_half_finished_edit(bot):
    bot.edit_set(SimpleNamespace(effective_user=SimpleNamespace(id=424242)),
                 "awaiting_custom", "body")

    _tap(bot, "tmpl_apply:classic")

    assert bot.state["edits"][str(424242)].get("awaiting_custom") is None


def test_the_built_in_default_clears_every_field(bot):
    for key in ("custom_header", "custom_body", "custom_footer"):
        bot.state["settings"][key] = "something"

    query = _tap(bot, "tmpl_reset")

    assert not any(bot.state["settings"][key] for key in
                   ("custom_header", "custom_body", "custom_footer"))
    assert bot.custom_message_is_empty()
    query.answer.assert_awaited_once_with("↩️ Built-in default restored")
    assert "Built-in default" in bot.templates_text()


def test_a_non_admin_cannot_apply_a_template(bot):
    query = SimpleNamespace(data="tmpl_apply:promo", answer=AsyncMock(),
                            edit_message_text=AsyncMock(), message=SimpleNamespace(chat_id=1))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999),
                             callback_query=query,
                             effective_chat=SimpleNamespace(type="private"))

    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert bot.custom_message_is_empty()
    query.edit_message_text.assert_not_awaited()
