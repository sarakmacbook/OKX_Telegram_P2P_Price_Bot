"""Test bootstrap: configure the bot through env vars before it is imported."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# bot.py reads its configuration at import time — give it a throwaway one so no
# real token and no repository files are touched.
_STATE_DIR = Path(tempfile.mkdtemp(prefix="p2p-bot-tests-"))
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("ADMIN_IDS", "424242")
os.environ.setdefault("ASSET", "USDT")
os.environ.setdefault("FIAT", "USD")
os.environ.setdefault("INTERVAL", "60")
os.environ["P2P_STATE_FILE"] = str(_STATE_DIR / "data.json")
os.environ["P2P_CONFIG_FILE"] = str(_STATE_DIR / "config.json")
for _var in ("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL", "REDIS_REST_URL"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402


@pytest.fixture()
def bot():
    import bot as bot_module
    bot_module.state.clear()
    bot_module.state.update(bot_module.empty_state())
    yield bot_module
    bot_module.state.clear()
    bot_module.state.update(bot_module.empty_state())


@pytest.fixture()
def merchant():
    from exchanges import Merchant
    return Merchant("okx", "0dec824eed", "Fast_sonic", "USDT", "USD",
                    "https://www.okx.com/p2p/ads-merchant?publicUserId=0dec824eed")


@pytest.fixture()
def prices(merchant):
    return {merchant.key: {"sell": 0.999, "sell_amount": 10645.56, "sell_ad_id": "260912150134452",
                           "buy": 1.001, "buy_amount": 500.0, "buy_ad_id": "260912150134999",
                           "error": None}}
