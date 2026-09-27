"""Profile-first buttons/prices, optional ad links, and saved target settings."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_buttons_default_to_the_merchant_profile(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    buttons = _buttons(bot.report_keyboard(prices))
    assert bot.link_mode() == "profile"
    assert len(buttons) == 2
    assert [b.url for b in buttons] == [merchant.profile_url, merchant.profile_url]
    assert all(b.callback_data is None for b in buttons)


def test_buttons_can_opt_in_to_ad_templates(bot, merchant, prices):
    bot.state["settings"]["btn_link_mode"] = "ad"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    kb = bot.report_keyboard(prices)
    urls = {b.text.split()[0]: b.url for b in _buttons(kb)}
    assert urls["🟢"] == "https://www.okx.com/p2p-markets/usd/sell-usdt?adId=260912150134999"
    assert urls["🔴"] == "https://www.okx.com/p2p-markets/usd/buy-usdt?adId=260912150134452"


def test_button_text_shows_the_prices_and_merchant(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    texts = [b.text for b in _buttons(bot.report_keyboard(prices))]
    assert any("0.999" in t and "Fast_sonic" in t for t in texts)
    assert any("1.001" in t for t in texts)


def test_profile_mode_links_to_the_merchant_page(bot, merchant, prices):
    bot.state["settings"]["btn_link_mode"] = "profile"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    urls = [b.url for b in _buttons(bot.report_keyboard(prices))]
    assert all(u == merchant.url for u in urls)


def test_missing_ad_id_falls_back_to_profile(bot, merchant):
    bot.state["settings"]["btn_link_mode"] = "ad"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    r = {"sell": 1.0, "buy": 1.1, "sell_amount": None, "buy_amount": None,
         "sell_ad_id": None, "buy_ad_id": None, "error": None}
    url = bot.btn_url("sell", merchant, r)
    assert url == merchant.url


def test_custom_button_url_wins_and_gets_placeholders(bot, merchant, prices):
    bot.state["settings"]["btn_sell_url"] = "https://t.me/support?ad={AD_ID}&p={PRICE}"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    r = prices[merchant.key]
    assert bot.btn_url("sell", merchant, r) == "https://t.me/support?ad=260912150134452&p=0.999"


def test_custom_ad_template_is_used(bot, merchant, prices):
    bot.state["settings"]["btn_link_mode"] = "ad"
    bot.state["settings"]["ad_link_templates"] = {"okx": "https://my.tld/ad/{AD_ID}/{TAKER_SIDE}"}
    bot.state["merchants"][merchant.key] = merchant.__dict__
    assert bot.btn_url("sell", merchant, prices[merchant.key]) == "https://my.tld/ad/260912150134452/buy"


def test_prices_link_to_the_profile_by_default(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    text = bot.report(prices)
    assert f'href="{merchant.profile_url}">1.001</a>' in text
    assert f'href="{merchant.profile_url}">0.999</a>' in text
    assert "adId=" not in text


def test_prices_can_follow_ad_templates(bot, merchant, prices):
    bot.state["settings"]["btn_link_mode"] = "ad"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    text = bot.report(prices)
    assert 'href="https://www.okx.com/p2p-markets/usd/sell-usdt?adId=260912150134999">1.001</a>' in text
    assert 'href="https://www.okx.com/p2p-markets/usd/buy-usdt?adId=260912150134452">0.999</a>' in text


def test_price_links_can_be_switched_off(bot, merchant, prices):
    bot.state["settings"]["price_links"] = False
    bot.state["merchants"][merchant.key] = merchant.__dict__
    text = bot.report(prices)
    assert "adId=" not in text
    assert "<b>0.999</b>" in text and "<b>1.001</b>" in text


def test_body_template_placeholders(bot, merchant, prices):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    block = bot.apply_body_template(
        "{EXCHANGE}|{SELL}|{BUY}|{SELL_URL}|{BUY_AD_ID}", merchant, prices[merchant.key])
    assert block == f"Okx|0.999|1.001|{merchant.profile_url}|260912150134999"


def test_market_page_fallback_when_no_profile_url(bot, prices):
    from exchanges import Merchant
    m = Merchant("bybit", "123", "Nick", "USDT", "USD", "")
    r = {"sell": 1.0, "buy": None, "sell_amount": None, "buy_amount": None,
         "sell_ad_id": None, "buy_ad_id": None, "error": None}
    assert bot.btn_url("sell", m, r) == "https://www.bybit.com/en/p2p/buy/USDT/USD"


def test_broken_settings_do_not_break_rendering(bot, merchant, prices):
    """State written by an older/hand-edited version must not crash the bot."""
    bot.state["settings"]["ad_link_templates"] = "not-a-dict"
    bot.state["settings"]["btn_link_mode"] = "🎯"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    assert bot.ad_templates()["okx"] == bot.AD_LINK_TEMPLATES["okx"]
    assert bot.link_mode() == "profile"
    assert bot.report(prices)


@pytest.mark.parametrize("old_settings", [None, {}, {"btn_link_mode": "ad"},
                                         {"btn_link_mode": "profile"}, {"btn_link_mode": "🎯"}])
def test_old_saved_settings_migrate_to_profiles(bot, monkeypatch, merchant, old_settings):
    stored = {"settings": old_settings, "group": -100123,
              "merchants": {merchant.key: merchant.__dict__}}
    monkeypatch.setattr(bot.STORE, "load", lambda: deepcopy(stored))
    loaded = bot.load()
    assert loaded["settings"]["btn_link_mode"] == "profile"
    assert loaded["link_target_version"] == bot.LINK_TARGET_VERSION
    assert loaded["group"] == stored["group"]
    assert loaded["merchants"] == stored["merchants"]


def test_migration_preserves_custom_links_and_other_settings(bot, monkeypatch):
    settings = {"btn_link_mode": "ad", "show_liquidity": True,
                "btn_buy_url": "https://t.me/support", "btn_sell_label": "My Sell",
                "ad_link_templates": {"okx": "https://example.com/{AD_ID}"}}
    monkeypatch.setattr(bot.STORE, "load", lambda: {"settings": deepcopy(settings)})
    loaded = bot.load()["settings"]
    assert loaded["btn_link_mode"] == "profile"
    for key, value in settings.items():
        if key != "btn_link_mode":
            assert loaded[key] == value


def test_explicit_ad_choice_after_upgrade_survives_reload(bot, monkeypatch, tmp_path):
    from storage import FileStore

    store = FileStore(tmp_path / "state.json")
    store.save({"settings": {"btn_link_mode": "ad"}})  # the old persisted default
    monkeypatch.setattr(bot, "STORE", store)
    bot.refresh_state()
    assert bot.link_mode() == "profile"

    # An admin can opt back in after the one-time migration. This must work
    # across restarts and Vercel's refresh_state() before each request.
    query = SimpleNamespace(data="toggle_link_mode", message=SimpleNamespace(text="Buy / Sell buttons"),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=next(iter(bot.ADMINS))),
                             callback_query=query)
    asyncio.run(bot.on_button(update, SimpleNamespace()))
    assert store.load()["link_target_version"] == bot.LINK_TARGET_VERSION
    bot.refresh_state()
    assert bot.link_mode() == "ad"
    bot.refresh_state()
    assert bot.link_mode() == "ad"


def test_every_menu_renders(bot, merchant, prices):
    """Every panel/menu screen must build without raising."""
    from telegram import InlineKeyboardMarkup
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -1001234567890
    bot.state["group_title"] = "My P2P group"
    for text in (bot.panel_text(), bot.settings_text(), bot.buttons_menu_text(),
                 bot.adlink_menu_text(), bot.custom_menu_text(), bot.database_text(),
                 bot.button_icons_text(), bot.banner_text(), bot.icon_editor_text("🟢"),
                 bot.antiscam_text(), bot.cleanup_text()):
        assert isinstance(text, str) and text.strip()
    for kb in (bot.panel(), bot.settings_kb(), bot.buttons_menu_kb(),
               bot.adlink_menu_kb(), bot.custom_menu_kb(), bot.list_kb(),
               bot.database_kb(), bot.button_icons_kb(), bot.banner_kb(),
               bot.icon_editor_kb("🟢"), bot.report_keyboard(prices),
               bot.antiscam_kb(), bot.cleanup_kb()):
        assert isinstance(kb, InlineKeyboardMarkup) and kb.inline_keyboard
    # the ad-link screen lists one editor per supported exchange
    callbacks = [b.callback_data for row in bot.adlink_menu_kb().inline_keyboard for b in row]
    for ex in bot.EXCHANGE_NAMES:
        assert f"edit_adlink:{ex}" in callbacks


def test_each_merchant_row_uses_its_own_profile(bot, merchant, prices):
    other = replace(merchant, merchant_id="another-public-id", url="")
    bot.state["merchants"] = {m.key: m.__dict__ for m in (merchant, other)}
    prices[other.key] = dict(prices[merchant.key])
    rows = bot.report_keyboard(prices).inline_keyboard
    assert [[b.url for b in row] for row in rows] == [
        [merchant.profile_url] * 2, [other.profile_url] * 2]


def test_legacy_okx_market_url_uses_real_profile_everywhere(bot, merchant, prices):
    merchant.url = "https://www.okx.com/p2p-markets/usd/buy-usdt?publicUserId=0dec824eed"
    bot.state["merchants"][merchant.key] = merchant.__dict__
    assert [b.url for b in _buttons(bot.report_keyboard(prices))] == [merchant.profile_url] * 2
    assert merchant.url not in bot.report(prices)
    block = bot.apply_body_template("{URL}|{LINK}|{SELL_URL}|{BUY_URL}", merchant, prices[merchant.key])
    assert block.count(merchant.profile_url) == 4
    assert bot.link_values("buy", merchant, prices[merchant.key])["URL"] == merchant.profile_url


def test_missing_profile_can_still_fall_back_to_an_ad(bot):
    from exchanges import Merchant

    merchant = Merchant("binance", "123")
    assert bot.btn_url("buy", merchant, {"buy_ad_id": "456"}) == \
        "https://c2c.binance.com/en/adv?code=456"


def test_reset_buttons_restores_profile_target_and_clears_overrides(bot, monkeypatch):
    bot.state["settings"].update({"btn_link_mode": "ad", "buttons_order": "sell_buy",
                                   "btn_buy_url": "https://example.com/buy",
                                   "btn_sell_url": "https://example.com/sell"})
    saved = Mock()
    monkeypatch.setattr(bot, "save", saved)
    query = SimpleNamespace(data="reset_buttons", answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=next(iter(bot.ADMINS))),
                             callback_query=query)
    asyncio.run(bot.on_button(update, SimpleNamespace()))
    assert bot.link_mode() == "profile"
    assert bot.buttons_order() == "buy_sell"
    assert bot.state["settings"]["btn_buy_url"] == ""
    assert bot.state["settings"]["btn_sell_url"] == ""
    saved.assert_called_once()


def test_reset_adlinks_restores_profile_target(bot, monkeypatch):
    bot.state["settings"]["btn_link_mode"] = "ad"
    monkeypatch.setattr(bot, "save", Mock())
    bot.reset_adlinks()
    assert bot.link_mode() == "profile"


def test_profile_target_is_explained_in_the_menu(bot):
    text = bot.buttons_menu_text()
    assert text.count("(merchant profile URL)") == 2
    assert "Both buttons open the merchant's public P2P profile" in text
    assert "no order is placed automatically" in text
    bot.state["settings"]["btn_link_mode"] = "ad"
    assert "(merchant profile URL)" not in bot.buttons_menu_text()


def test_post_sends_profile_buttons(bot, merchant, prices, monkeypatch):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    monkeypatch.setattr(bot, "save", Mock())
    telegram = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
    assert asyncio.run(bot.post(telegram)) is True
    keyboard = telegram.send_message.call_args.kwargs["reply_markup"]
    assert [b.url for b in _buttons(keyboard)] == [merchant.profile_url] * 2


# ── the "connect database" link ────────────────────────────────────────────
def test_panel_links_to_the_database_page_when_no_shared_store(bot, monkeypatch):
    """Nothing remembers the group without a database — so say how to connect one."""
    monkeypatch.setattr(bot, "db_is_persistent", lambda: False)
    monkeypatch.setenv("P2P_DATABASE_LINK", "https://vercel.com/dashboard/stores")

    connect = [b for b in _buttons(bot.panel()) if b.text.startswith("🔌")]

    assert len(connect) == 1
    assert connect[0].url == "https://vercel.com/dashboard/stores"
    assert connect[0].callback_data is None                  # a plain link, one tap
    assert "NOT connected" in bot.panel_text()


def test_panel_has_no_connect_button_once_a_database_is_connected(bot, monkeypatch):
    """A connected store needs no call to action; the settings screen still explains it."""
    monkeypatch.setattr(bot, "db_is_persistent", lambda: True)

    buttons = _buttons(bot.panel())

    assert not any("Connect database" in b.text for b in buttons)
    assert not any(b.url and "vercel.com" in b.url for b in buttons)
    assert "connected ✅" in bot.panel_text()
    assert "database" in [b.callback_data for b in _buttons(bot.settings_kb())]


def test_database_screen_shows_the_store_and_the_link(bot, monkeypatch):
    monkeypatch.setattr(bot, "db_is_persistent", lambda: False)
    monkeypatch.setenv("P2P_DATABASE_LINK", "https://console.upstash.com/redis/1")

    text = bot.database_text()
    buttons = _buttons(bot.database_kb())

    assert "NOT connected" in text
    assert "console.upstash.com/redis/1" in text
    assert "KV_REST_API_URL" in text and "python setup_cli.py" in text   # the self-hosted steps
    assert buttons[0].url == "https://console.upstash.com/redis/1"
    assert [b.callback_data for b in buttons] == [None, "db_check", "panel"]


def test_database_screen_gives_the_vercel_steps_on_vercel(bot, monkeypatch):
    monkeypatch.setattr(bot, "db_is_persistent", lambda: False)
    monkeypatch.setenv("VERCEL", "1")

    text = bot.database_text()

    assert "Storage" in text and "Connect to this project" in text
    assert "Redeploy" in text


def test_settings_offers_the_database_screen(bot, monkeypatch):
    monkeypatch.setattr(bot, "db_is_persistent", lambda: False)

    callbacks = [b.callback_data for b in _buttons(bot.settings_kb())]

    assert "database" in callbacks
    assert "🗄 Database" in bot.settings_text()


def test_check_connection_re_reads_the_store_and_reports_it(bot, monkeypatch):
    """Credentials are usually added outside the bot, so the button re-reads them."""
    import runtime_config

    class FreshStore:
        backend = "redis"

        def load(self):
            return None

        def save(self, data):
            pass

        def describe(self):
            return "redis (kv.example, key p2p)"

    monkeypatch.setattr(bot, "STORE", FreshStore())
    monkeypatch.setattr(bot, "rebuild_store", lambda: bot.STORE.describe())
    monkeypatch.setattr(bot, "refresh_state", lambda: bot.state)
    monkeypatch.setattr(runtime_config, "apply", lambda *a, **k: {})

    query = SimpleNamespace(data="db_check", answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=next(iter(bot.ADMINS))),
                             callback_query=query)
    asyncio.run(bot.on_button(update, SimpleNamespace()))

    assert "redis (kv.example, key p2p)" in query.edit_message_text.await_args.args[0]
    assert "connected ✅" in query.edit_message_text.await_args.args[0]
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is not None


def test_database_command_is_admin_only_and_sends_the_screen(bot, monkeypatch):
    monkeypatch.setattr(bot, "db_is_persistent", lambda: False)
    message = SimpleNamespace(reply_html=AsyncMock())
    stranger = SimpleNamespace(effective_user=SimpleNamespace(id=999999), message=message)
    asyncio.run(bot.database_cmd(stranger, SimpleNamespace()))
    message.reply_html.assert_not_awaited()

    admin = SimpleNamespace(effective_user=SimpleNamespace(id=next(iter(bot.ADMINS))),
                            message=message)
    asyncio.run(bot.database_cmd(admin, SimpleNamespace()))
    message.reply_html.assert_awaited()
    assert "Connect database" in str(message.reply_html.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text)


def test_database_screen_reports_a_store_that_stopped_answering(bot, monkeypatch):
    """A configured-but-dead database must not masquerade as a healthy one."""
    monkeypatch.setattr(bot, "db_is_persistent", lambda: True)
    monkeypatch.setattr(bot, "db_health", lambda force=False: (False, "ConnectError: refused"))

    text = bot.database_text()

    assert "NOT answering" in text
    assert "ConnectError: refused" in text
    assert "forgets the group" in text
    assert "connected ✅" not in text


def test_database_screen_stays_quiet_when_the_store_answers(bot, monkeypatch):
    monkeypatch.setattr(bot, "db_is_persistent", lambda: True)
    monkeypatch.setattr(bot, "db_health", lambda force=False: (True, "the database answered"))

    text = bot.database_text()

    assert "connected ✅" in text
    assert "NOT answering" not in text
