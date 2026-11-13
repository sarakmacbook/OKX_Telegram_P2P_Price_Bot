"""📈 Price movement — the ▲/▼ badge and the "only post on a real move" rule.

Two behaviours share one idea: the prices of the last post the audience saw.

* every price can carry how far it moved since then (📈 +0.42%, 📉 -0.30%);
* a move smaller than the owner's minimum does not make a message of its own —
  it stays pending and shows up in the next post that *is* big enough, so the
  badge always measures from the last post the group actually received.

Everything that is not a price number (wording, buttons, banner, merchants) is
still worth a post, and 📊 Post prices now always posts.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ADMIN = 424242


class _FakeBot:
    """Records what would go to the group (same shape the other modules use)."""

    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=8, photo=None)

    async def send_photo(self, chat_id, **kwargs):
        self.calls.append({"chat_id": chat_id, **kwargs})
        return SimpleNamespace(message_id=9, photo=[object()], caption=kwargs.get("caption"))

    async def delete_message(self, **kwargs):
        return True


def _tap(bot, data):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(text="⚙️ Settings"))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                             effective_chat=SimpleNamespace(type="private", id=ADMIN),
                             callback_query=query,
                             message=SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock()))
    asyncio.run(bot.on_button(update, SimpleNamespace()))
    return query


def _price(sell, buy=None, ad="260912150134452"):
    return {"sell": sell, "sell_amount": 10645.56, "sell_ad_id": ad,
            "buy": buy if buy is not None else sell + 0.002, "buy_amount": 500.0,
            "buy_ad_id": ad + "9", "error": None}


def _one_merchant(bot, merchant):
    bot.state["merchants"][merchant.key] = merchant.__dict__
    bot.state["group"] = -100123


def _posted(bot, merchant, prices, monkeypatch):
    """Let the bot post ``prices`` once, so a previous post exists."""
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    monkeypatch.setattr(bot, "save", Mock())
    assert asyncio.run(bot.post(_FakeBot())) is True


# ── the numbers ────────────────────────────────────────────────────────────
def test_move_pct_is_the_relative_change(bot):
    assert bot.move_pct(101.0, 100.0) == pytest.approx(1.0)
    assert bot.move_pct(99.0, 100.0) == pytest.approx(-1.0)
    assert bot.move_pct(100.0, 100.0) == 0.0


def test_move_pct_refuses_what_cannot_be_compared(bot):
    assert bot.move_pct(None, 100.0) is None
    assert bot.move_pct(100.0, None) is None
    assert bot.move_pct(100.0, 0) is None
    assert bot.move_pct("nonsense", 100.0) is None


def test_the_badge_reads_as_an_arrow_and_a_percentage(bot):
    assert bot.format_move(0.42) == "📈 +0.42%"
    assert bot.format_move(-1.5) == "📉 -1.50%"
    assert bot.format_move(0.001) == "➖ 0.00%"      # a wobble is not a move
    assert bot.format_move(None) == ""
    assert bot.format_move(0.42, arrow=False) == "+0.42%"


# ── the badge in the post ──────────────────────────────────────────────────
def test_the_first_post_has_no_badge(bot, merchant, prices):
    _one_merchant(bot, merchant)
    text = bot.report(prices)
    assert "📈" not in text and "📉" not in text
    assert "0.999" in text


def test_the_next_post_says_how_far_each_price_moved(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)                   # sell 0.999, buy 1.001

    moved = {merchant.key: _price(1.009, 0.991)}                  # +1.00% / -1.00%
    text = bot.report(moved)
    assert "📈 +1.00%" in text
    assert "📉 -1.00%" in text
    # the badge sits on the same line as its price
    sell_line = [ln for ln in text.splitlines() if "Best SELL" in ln][0]
    assert "1.009" in sell_line and "📈 +1.00%" in sell_line


def test_an_unchanged_price_says_so_quietly(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    assert "➖ 0.00%" in bot.report(prices)


def test_the_badge_can_be_switched_off(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["show_change"] = False
    text = bot.report({merchant.key: _price(1.009)})
    assert "📈" not in text and "➖" not in text


def test_switching_the_badge_off_reposts_the_same_prices(bot, merchant, prices, monkeypatch):
    """The post looks different, so it goes out again — like a new banner."""
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    assert asyncio.run(bot.post(fake)) is False            # nothing changed

    bot.state["settings"]["show_change"] = False
    assert asyncio.run(bot.post(fake)) is True


def test_a_custom_body_asks_for_the_move_itself(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["custom_body"] = "{EXCHANGE} {SELL} {SELL_CHANGE} | {BUY_MOVE}"
    bot.state["settings"]["show_change"] = False                  # the template decides

    block = bot.apply_body_template(bot.state["settings"]["custom_body"],
                                    merchant, _price(1.009, 1.011))
    assert block == "Okx 1.009 📈 +1.00% | +1.00%"


def test_a_custom_body_without_the_placeholders_is_left_alone(bot, merchant, prices):
    block = bot.apply_body_template("{EXCHANGE} {SELL}", merchant, prices[merchant.key])
    assert block == "Okx 0.999"


# ── the minimum move ───────────────────────────────────────────────────────
def test_a_move_under_the_minimum_is_not_posted(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 1.0

    monkeypatch.setattr(bot, "get_prices",
                        AsyncMock(return_value={merchant.key: _price(1.002)}))   # +0.30%
    assert asyncio.run(bot.post(_FakeBot())) is False


def test_a_move_over_the_minimum_is_posted(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 1.0

    monkeypatch.setattr(bot, "get_prices",
                        AsyncMock(return_value={merchant.key: _price(1.010)}))   # +1.10%
    assert asyncio.run(bot.post(_FakeBot())) is True


def test_the_small_moves_add_up_until_one_post_carries_them(bot, merchant, prices, monkeypatch):
    """The badge measures from the last post the group received, not the last check."""
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)                    # group saw 0.999
    bot.state["settings"]["min_move"] = 1.0
    fake = _FakeBot()

    for small in (1.001, 1.004, 1.007):                            # +0.2%, +0.5%, +0.8%
        monkeypatch.setattr(bot, "get_prices",
                            AsyncMock(return_value={merchant.key: _price(1.002)}))
        assert asyncio.run(bot.post(fake)) is False

    monkeypatch.setattr(bot, "get_prices",
                        AsyncMock(return_value={merchant.key: _price(1.009)}))   # +1.00%
    assert asyncio.run(bot.post(fake)) is True
    assert "📈 +1.00%" in fake.calls[-1]["text"]                    # from 0.999, not 1.002


def test_wording_always_posts_whatever_the_minimum(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 5.0
    bot.state["settings"]["custom_header"] = "📊 Our best P2P rates"

    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    assert asyncio.run(bot.post(fake)) is True
    assert "Our best P2P rates" in fake.calls[-1]["text"]


def test_a_new_merchant_always_posts(bot, merchant, prices, monkeypatch):
    from exchanges import Merchant
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 5.0

    second = Merchant("binance", "Advertiser123", "Second", "USDT", "USD",
                      "https://p2p.binance.com/en/advertiserDetail?advertiserNo=Advertiser123")
    bot.state["merchants"][second.key] = second.__dict__
    monkeypatch.setattr(bot, "get_prices",
                        AsyncMock(return_value={merchant.key: _price(0.999),
                                                second.key: _price(1.002)}))
    assert asyncio.run(bot.post(_FakeBot())) is True


def test_post_now_ignores_the_minimum(bot, merchant, prices, monkeypatch):
    """📊 Post prices now is an explicit order — it always posts."""
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 5.0
    bot.state["auto"] = True

    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    assert asyncio.run(bot.post(_FakeBot(), force=True)) is True


def test_without_a_minimum_every_ad_change_still_posts(bot, merchant, prices, monkeypatch):
    """The old behaviour is the default: a new cheapest ad is a new post."""
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    same_price_new_ad = {merchant.key: _price(0.999, 1.001, ad="260912150134999")}

    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=same_price_new_ad))
    assert asyncio.run(bot.post(_FakeBot())) is True


def test_a_new_ad_alone_does_not_beat_the_minimum(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 1.0

    monkeypatch.setattr(bot, "get_prices", AsyncMock(
        return_value={merchant.key: _price(0.999, 1.001, ad="260912150134999")}))
    assert asyncio.run(bot.post(_FakeBot())) is False


def test_a_channel_added_later_gets_its_own_post_under_a_minimum(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.state["settings"]["min_move"] = 5.0

    bot.state["channel"] = -100777
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value=prices))
    fake = _FakeBot()
    assert asyncio.run(bot.post(fake)) is True
    assert [call["chat_id"] for call in fake.calls] == [-100777]     # channel only


# ── what the state remembers ───────────────────────────────────────────────
def test_a_successful_post_remembers_the_prices_it_showed(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    assert bot.state["last_prices"][merchant.key] == {"sell": 0.999, "buy": 1.001}


def test_a_post_that_never_arrived_does_not_move_the_reference(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)

    class _Broken(_FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("Bad Request: chat not found")

    bot.state["settings"]["show_change"] = False        # a change that must repost
    monkeypatch.setattr(bot, "get_prices", AsyncMock(return_value={merchant.key: _price(1.05)}))
    assert asyncio.run(bot.post(_Broken())) is False
    assert bot.state["last_prices"][merchant.key]["sell"] == 0.999


def test_the_last_prices_survive_a_restart(bot, merchant, prices, monkeypatch):
    _one_merchant(bot, merchant)
    _posted(bot, merchant, prices, monkeypatch)
    bot.STORE.save(bot.state)                           # the real store writes it
    assert bot.STORE.load()["last_prices"][merchant.key]["sell"] == 0.999


def test_an_old_state_learns_the_new_key(bot):
    assert "last_prices" in bot.empty_state()
    fresh = bot.load()
    assert isinstance(fresh.get("last_prices"), dict)


def test_a_nonsense_minimum_falls_back_to_any_change(bot):
    bot.state["settings"]["min_move"] = 7.5                # not an offered step
    assert bot.min_move() == 0.0
    assert bot.min_move_label() == "any change"


# ── the settings screen ────────────────────────────────────────────────────
def test_the_settings_screen_offers_price_movement(bot):
    callbacks = [b.callback_data for row in bot.settings_kb().inline_keyboard for b in row]
    assert "move_menu" in callbacks
    assert "📈 Price movement: <b>▲▼ badge ✅</b> · repost on <b>any change</b>" \
        in bot.settings_text()


def test_the_move_screen_explains_both_switches(bot):
    text = bot.move_menu_text()
    assert "▲▼ badge in the post: <b>ON ✅</b>" in text
    assert "Repost only on a move of: <b>any change</b>" in text
    assert "always post, whatever the minimum says" in text
    assert "{SELL_CHANGE}" in text and "{BUY_MOVE}" in text


def test_the_badge_switch_flips_and_persists(bot):
    query = _tap(bot, "toggle_change")
    assert bot.state["settings"]["show_change"] is False
    assert query.answer.await_args.args[0] == "▲▼ badge OFF"
    assert bot.move_menu_kb().inline_keyboard[0][0].text == "📈 Show change: OFF ❌"
    assert bot.STORE.load()["settings"]["show_change"] is False


def test_the_minimum_cycles_through_the_steps_and_persists(bot):
    seen = []
    for _ in range(len(bot.MOVE_CHOICES)):
        _tap(bot, "cycle_min_move")
        seen.append(bot.min_move())
    assert seen == [0.25, 0.5, 1.0, 2.0, 0.0]           # and back to "any change"
    assert bot.STORE.load()["settings"]["min_move"] == 0.0


def test_the_minimum_button_names_the_current_step(bot):
    bot.state["settings"]["min_move"] = 1.0
    assert bot.move_menu_kb().inline_keyboard[1][0].text == "⛔ Min move: ≥ 1%"
    assert "Repost only on a move of: <b>≥ 1%</b>" in bot.move_menu_text()
