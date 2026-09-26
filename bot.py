"""Telegram bot: P2P merchant price feed."""

from __future__ import annotations

import os, sys, json, asyncio, logging, argparse, signal, time, re, secrets
from copy import deepcopy
from dataclasses import asdict
from urllib.parse import urlsplit
from pathlib import Path
import httpx
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as KB
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ChatMemberHandler, ContextTypes, filters)
from exchanges import Merchant, parse_url, fetch, HEADERS
from adlinks import (EXCHANGE_NAMES, AD_LINK_TEMPLATES, ad_link, market_link,
                     resolve_templates, render_template, template_is_exact, taker_side)
from storage import build_store

# ── paths: always relative to this file (works with systemd WorkingDirectory) ──
# P2P_CONFIG_FILE / P2P_STATE_FILE override them (tests, or installs that keep
# their data outside the code directory).
BASE_DIR = Path(__file__).resolve().parent
CONFIG = Path(os.getenv("P2P_CONFIG_FILE") or (BASE_DIR / "config.json"))
DB = BASE_DIR / "data.json"

# ── state store ──
# Apply settings saved by the setup page before selecting a backend.  In
# particular, this lets a stored P2P_STATE_BACKEND choice take effect in polling
# installs as well as in the serverless entry points (which already apply the
# runtime settings before importing this module).
try:
    import runtime_config
    runtime_config.apply(BASE_DIR)
except Exception as e:                            # setup storage must never stop the bot
    logging.getLogger("p2p-bot").warning("Stored settings unavailable before state setup: %s", e)

# data.json is the default; P2P_STATE_BACKEND can explicitly select file,
# Redis, or auto detection (see storage.py).
STORE = build_store(BASE_DIR)

ICON = {"binance": "🟡", "bybit": "🟣", "okx": "⚫", "bitget": "🔵"}
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("p2p-bot")


def _fatal(msg: str):
    """Log the fatal problem and exit."""
    log.error(msg)
    sys.exit(1)


def _write_config(cfg: dict) -> bool:
    """Persist config.json when possible — never fatal (read-only hosts)."""
    try:
        json.dump(cfg, open(CONFIG, "w"), indent=1)
        try: os.chmod(CONFIG, 0o600)
        except Exception: pass
        return True
    except Exception as e:
        log.info("config.json not writable (%s) — using environment configuration", e)
        return False

# ── CLI / ENV ──
def parse_cli():
    p = argparse.ArgumentParser(description="P2P Merchant Price Bot", add_help=True)
    p.add_argument("--setup", action="store_true", help="Re-run interactive setup wizard")
    p.add_argument("--reconfigure", action="store_true", help="Alias for --setup")
    p.add_argument("--token", help="Bot token from @BotFather")
    p.add_argument("--admins", help="Telegram user ID(s), comma-separated")
    p.add_argument("--asset", help="Asset, e.g. USDT")
    p.add_argument("--fiat", help="Fiat, e.g. USD")
    p.add_argument("--interval", type=int, help="Check interval seconds")
    # parse_known_args: never die on foreign argv (pytest, process managers, runtimes)
    args, _unknown = p.parse_known_args()
    return args

CLI = parse_cli()
if CLI.reconfigure:
    CLI.setup = True

# ── config helpers ──
def ask(q, default=None, check=None):
    while True:
        v = input(f"{q}{f' [{default}]' if default else ''}: ").strip() or (default or "")
        if v and (check is None or check(v)): return v
        print("  ❌ Invalid, try again.")

def env_or_cli(name_envs, cli_val, default=None):
    for n in name_envs:
        v = os.getenv(n)
        if v: return v.strip()
    if cli_val: return str(cli_val).strip()
    return default

def setup_interactive(existing=None):
    print("\n🤖 P2P Price Bot — first-time setup\n" + "-" * 40)
    print("  Get token from @BotFather → /newbot")
    print("  Get your ID from @userinfobot\n")
    def prefill(key, envs, cli_v, fallback):
        if existing and existing.get(key): return str(existing[key])
        v = env_or_cli(envs, cli_v, None)
        return v if v else fallback
    cfg = {
        "token":    ask("Bot token from @BotFather",
                        prefill("token", ["BOT_TOKEN","TELEGRAM_BOT_TOKEN","TOKEN"], CLI.token, None),
                        check=lambda v: ":" in v),
        "admins":   ask("Your Telegram user ID(s), comma-separated (from @userinfobot)",
                        prefill("admins", ["ADMIN_IDS","ADMINS"], CLI.admins, None),
                        check=lambda v: all(x.strip().isdigit() for x in v.split(","))),
        "asset":    ask("Asset", prefill("asset", ["ASSET"], CLI.asset, "USDT")).upper(),
        "fiat":     ask("Fiat currency", prefill("fiat", ["FIAT"], CLI.fiat, "USD")).upper(),
        "interval": int(ask("Check prices every N seconds",
                            str(prefill("interval", ["INTERVAL"], CLI.interval, "60")), check=str.isdigit)),
    }
    _write_config(cfg)
    print(f"✅ Saved to {CONFIG}. Re-run with  python bot.py --setup  to change.\n")
    return cfg

def load_config():
    # Settings saved on the setup page (/api/setup) or by `python setup_cli.py`
    # live in the state store; copy whatever the environment does not provide
    # into it before anything below reads the values.
    try:
        import runtime_config
        runtime_config.apply(BASE_DIR)
    except Exception as e:
        log.warning("Stored settings unavailable: %s", e)

    file_cfg = {}
    if CONFIG.exists():
        try: file_cfg = json.loads(CONFIG.read_text())
        except Exception as e:
            log.warning("config.json unreadable: %s — will recreate", e)
            file_cfg = {}

    env_token = env_or_cli(["BOT_TOKEN","TELEGRAM_BOT_TOKEN","TOKEN"], CLI.token)
    env_admins = env_or_cli(["ADMIN_IDS","ADMINS"], CLI.admins)
    env_asset = env_or_cli(["ASSET"], CLI.asset)
    env_fiat = env_or_cli(["FIAT"], CLI.fiat)
    env_interval = env_or_cli(["INTERVAL"], CLI.interval)

    if CLI.setup:
        merged = dict(file_cfg)
        if env_token: merged["token"] = env_token
        if env_admins: merged["admins"] = env_admins
        if env_asset: merged["asset"] = env_asset.upper()
        if env_fiat: merged["fiat"] = env_fiat.upper()
        if env_interval: merged["interval"] = int(str(env_interval).strip())
        return setup_interactive(merged)

    if not file_cfg and env_token and env_admins:
        log.info("Creating config.json from environment variables")
        cfg = {
            "token": env_token,
            "admins": env_admins.replace(" ", ""),
            "asset": (env_asset or "USDT").upper(),
            "fiat": (env_fiat or "USD").upper(),
            "interval": int(str(env_interval or "60").strip()),
        }
        if ":" not in cfg["token"]:
            _fatal("BOT_TOKEN invalid (missing ':')")
        _write_config(cfg)
        return cfg

    if file_cfg:
        dirty = False
        if env_token and env_token != file_cfg.get("token"):
            file_cfg["token"] = env_token; dirty = True; log.info("Overriding token from env")
        if env_admins and env_admins.replace(" ","") != file_cfg.get("admins"):
            file_cfg["admins"] = env_admins.replace(" ",""); dirty = True; log.info("Overriding admins from env")
        if env_asset and env_asset.upper() != file_cfg.get("asset"):
            file_cfg["asset"] = env_asset.upper(); dirty = True
        if env_fiat and env_fiat.upper() != file_cfg.get("fiat"):
            file_cfg["fiat"] = env_fiat.upper(); dirty = True
        if env_interval and str(env_interval) != str(file_cfg.get("interval")):
            try: file_cfg["interval"] = int(str(env_interval).strip()); dirty=True
            except: pass
        if dirty:
            _write_config(file_cfg)
        return file_cfg

    if sys.stdin.isatty():
        return setup_interactive(file_cfg)
    _fatal("No configuration found. Set the BOT_TOKEN and ADMIN_IDS environment "
           "variables (and ASSET/FIAT/INTERVAL), or create config.json with "
           "`python bot.py --setup`.")

cfg = load_config()

TOKEN, ASSET, FIAT, INTERVAL = cfg["token"], cfg["asset"], cfg["fiat"], int(cfg["interval"])
try:
    ADMINS = {int(x.strip()) for x in cfg["admins"].split(",") if x.strip()}
except Exception:
    _fatal(f"admins field invalid: {cfg.get('admins')!r} — should be comma-separated IDs")
    raise

if ":" not in TOKEN:
    _fatal("token invalid — should contain ':'")

# ── state ──
# default look of the inline Buy / Sell buttons under the group post
DEFAULT_BUY_LABEL = "🟢 BUY {PRICE} {NICK}"
DEFAULT_SELL_LABEL = "🔴 SELL {PRICE} {NICK}"

# Persisted separately so the old ad-link default is migrated only once.
LINK_TARGET_VERSION = 1
MAX_EXTRA_BUTTONS = 8
MAX_REPORT_BUTTONS = 100
PROFILE_BUTTON_URLS = ("{URL}", "{PROFILE_URL}")

DEFAULT_SETTINGS = {
    "show_liquidity": False,
    "show_buttons": True,
    "custom_header": "",
    "custom_body": "",     # per-merchant line template for the group post
    "custom_footer": "",
    "auto_delete": True,  # delete previous group message on new post
    "delete_after_hours": 24,  # auto delete after 24h
    "delete_join_left": True,  # delete Telegram "X joined/left the group" service messages
    # ── Buy / Sell buttons (editable from the private bot chat) ──
    "buttons_order": "buy_sell",   # "buy_sell" = Buy left / Sell right, "sell_buy" = the opposite
    "btn_buy_enabled": True,      # remove/restore either built-in button independently
    "btn_sell_enabled": True,
    "extra_buttons": [],          # {id, label, url}; appended per merchant, two per row
    "btn_buy_label": "",           # empty = DEFAULT_BUY_LABEL
    "btn_sell_label": "",          # empty = DEFAULT_SELL_LABEL
    "btn_buy_url": "",             # empty = selected target (profile by default)
    "btn_sell_url": "",            # empty = selected target (profile by default)
    # ── where the buttons/prices point ──
    "btn_link_mode": "profile",    # "profile" = merchant profile, "ad" = optional ad-link templates
    "ad_link_templates": {},       # per-exchange deep-link overrides (see adlinks.py)
    "price_links": True,           # also make the prices in the post clickable
}

def clean_extra_button_label(value) -> str:
    """Custom captions are plain text, not HTML or price templates."""
    if not isinstance(value, str):
        return ""
    label = " ".join(value.split())
    return label if 0 < len(label) <= 60 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in label) else ""


def valid_extra_button_url(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if value in PROFILE_BUTTON_URLS:
        return True
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in '<>"{}\\' for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        # Read .port too: urllib rejects malformed ports here, not at urlsplit().
        port = parsed.port
        return bool(parsed.scheme in ("https", "http", "tg") and parsed.hostname
                    and parsed.username is None and parsed.password is None
                    and (port is None or port > 0))
    except ValueError:
        return False


def normalize_extra_buttons(value) -> list[dict]:
    """Copy valid saved records; malformed state must not break a group post."""
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        label = clean_extra_button_label(item.get("label"))
        url = item.get("url")
        url = url.strip() if isinstance(url, str) else ""
        if (not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", ident)
                or ident in seen or not label or not valid_extra_button_url(url)):
            continue
        result.append({"id": ident, "label": label, "url": url})
        seen.add(ident)
        if len(result) == MAX_EXTRA_BUTTONS:
            break
    return result


def empty_state():
    return {"group": None, "auto": False, "merchants": {}, "last": {}, "edits": {},
            "settings": deepcopy(DEFAULT_SETTINGS), "link_target_version": LINK_TARGET_VERSION,
            "last_msg_id": None, "last_msg_time": None}


def load():
    try:
        data = STORE.load()
    except Exception as e:                                   # pragma: no cover - defensive
        log.warning("state backend unavailable: %s — resetting", e)
        data = None
    if not isinstance(data, dict):
        data = empty_state()
    # migration: ensure keys exist
    if "settings" not in data or not isinstance(data["settings"], dict):
        data["settings"] = deepcopy(DEFAULT_SETTINGS)
    # Older installs persisted "ad" even when the owner never chose a target.
    # Apply the profile default on upgrade, but preserve any later opt-in to ad
    # links. Custom URLs, labels, merchants and other settings are left intact.
    if data.get("link_target_version") != LINK_TARGET_VERSION:
        data["settings"]["btn_link_mode"] = DEFAULT_SETTINGS["btn_link_mode"]
        data["link_target_version"] = LINK_TARGET_VERSION
    for k, v in DEFAULT_SETTINGS.items():
        if k not in data["settings"]:
            data["settings"][k] = deepcopy(v)
    # sanitise the settings that are read as structured values
    data["settings"]["extra_buttons"] = normalize_extra_buttons(data["settings"].get("extra_buttons"))
    for key in ("btn_buy_enabled", "btn_sell_enabled"):
        if not isinstance(data["settings"].get(key), bool):
            data["settings"][key] = DEFAULT_SETTINGS[key]
    if not isinstance(data["settings"].get("ad_link_templates"), dict):
        data["settings"]["ad_link_templates"] = {}
    if data["settings"].get("btn_link_mode") not in ("ad", "profile"):
        data["settings"]["btn_link_mode"] = DEFAULT_SETTINGS["btn_link_mode"]
    if "last" not in data:
        data["last"] = {}
    if "merchants" not in data:
        data["merchants"] = {}
    if "auto" not in data:
        data["auto"] = False
    if "group" not in data:
        data["group"] = None
    if "last_msg_id" not in data:
        data["last_msg_id"] = None
    if "last_msg_time" not in data:
        data["last_msg_time"] = None
    if not isinstance(data.get("edits"), dict):
        data["edits"] = {}
    return data

def save():
    state["updated_at"] = int(time.time())
    STORE.save(state)


def refresh_state():
    """Re-read the shared state from the store (serverless: instances run in parallel)."""
    fresh = load()
    state.clear()
    state.update(fresh)
    return state

# ── in-flight edits: "tap Edit, then send the text" is two updates ──
# Serverless hosts (Vercel) may deliver those two updates to different processes,
# so the flag lives in the shared state instead of PTB's in-memory user_data.
def edits_of(u: Update) -> dict:
    user = u.effective_user
    return state.setdefault("edits", {}).setdefault(str(user.id if user else "0"), {})

def edit_get(u: Update, key: str, default=None):
    return edits_of(u).get(key, default)

def edit_set(u: Update, key: str, value):
    edits = edits_of(u)
    if key == "awaiting_custom":
        edits.pop("extra_button_label", None)  # discard an abandoned add-button draft
    edits[key] = value
    save()

def edit_pop(u: Update, key: str):
    edits = edits_of(u)
    edits.pop(key, None)
    if key == "awaiting_custom":
        edits.pop("extra_button_label", None)
    save()


state = load()


merchants = lambda: [Merchant(**m) for m in state["merchants"].values()]
is_admin = lambda u: u.effective_user and u.effective_user.id in ADMINS
fmt = lambda p: f"{p:.4f}".rstrip("0").rstrip(".") if p is not None else "—"

def fmt_amount(a):
    if a is None:
        return None
    try:
        if a >= 1000:
            s = f"{a:,.2f}"
        elif a >= 1:
            s = f"{a:.4f}".rstrip("0").rstrip(".")
        else:
            s = f"{a:.6f}".rstrip("0").rstrip(".")
        return s
    except:
        return str(a)

def get_settings():
    return state.get("settings", DEFAULT_SETTINGS)


def builtin_button_enabled(side: str) -> bool:
    return get_settings().get(f"btn_{side}_enabled", True) is not False


def extra_buttons() -> list[dict]:
    return normalize_extra_buttons(get_settings().get("extra_buttons"))


def extra_button(ident: str) -> dict | None:
    return next((button for button in extra_buttons() if button["id"] == ident), None)

# ── exact-ad links ──────────────────────────────────────────────────────────
def ad_templates() -> dict:
    """Default deep-link templates merged with per-exchange overrides
    (settings ▸ 🔗 Ad links, or the AD_LINK_TEMPLATES env var as JSON)."""
    raw = get_settings().get("ad_link_templates")
    overrides = dict(raw) if isinstance(raw, dict) else {}
    env_json = os.getenv("AD_LINK_TEMPLATES")
    if env_json:
        try:
            extra = json.loads(env_json)
            if isinstance(extra, dict):
                overrides.update(extra)
        except Exception as e:
            log.warning("AD_LINK_TEMPLATES is not valid JSON: %s", e)
    return resolve_templates(overrides)

def link_mode() -> str:
    """Use merchant profiles by default; ad-link templates remain opt-in."""
    mode = get_settings().get("btn_link_mode")
    return mode if mode in ("ad", "profile") else DEFAULT_SETTINGS["btn_link_mode"]

def side_links(side: str, m: Merchant, r: dict | None = None) -> dict:
    """Every URL we know for one side of one merchant.

    ``ad``      exact advertisement the price came from (empty when unknown)
    ``profile`` public merchant profile (canonical URL for OKX)
    ``market``  exchange market page for this pair/side
    ``best``    selected target first (default: profile → ad → market)
    """
    r = r or {}
    asset, fiat = (m.asset or ASSET), (m.fiat or FIAT)
    ad_id = r.get(f"{side}_ad_id")
    exact = ad_link(m.exchange, ad_id, asset, fiat, side,
                    templates=ad_templates(), nick=m.nickname or m.merchant_id,
                    profile_url=m.profile_url or "") if ad_id else None
    profile = m.profile_url or ""
    mkt = market_link(m.exchange, asset, fiat, side) or ""
    if link_mode() == "ad":
        best = exact or profile or mkt
    else:
        best = profile or exact or mkt
    return {"ad": exact or "", "profile": profile, "market": mkt, "best": best,
            "ad_id": str(ad_id or "")}

def link_values(side: str, m: Merchant, r: dict | None = None) -> dict:
    """Placeholders available in button-URL / body templates."""
    r = r or {}
    links = side_links(side, m, r)
    price = r.get(side)
    values = {
        "URL": links["profile"], "PROFILE_URL": links["profile"],
        "AD_URL": links["ad"], "LINK_URL": links["best"],
        "AD_ID": links["ad_id"], "PRICE": fmt(price),
        "AMOUNT": fmt_amount(r.get(f"{side}_amount")) or "",
        "NICK": m.nickname or m.merchant_id or "",
        "EXCHANGE": m.exchange, "EXCHANGE_TITLE": m.exchange.title(),
        "ICON": ICON.get(m.exchange, "💱"),
        "ASSET": (m.asset or ASSET).upper(), "ASSET_LOWER": (m.asset or ASSET).lower(),
        "FIAT": (m.fiat or FIAT).upper(), "FIAT_LOWER": (m.fiat or FIAT).lower(),
        "PAIR": f"{(m.asset or ASSET)}/{(m.fiat or FIAT)}",
        "SIDE": side.upper(), "side": side,
        "TAKER_SIDE": taker_side(side).upper(),
    }
    values["LINK"] = f'<a href="{links["best"]}">{fmt(price)}</a>' if links["best"] else fmt(price)
    return values

# ── panel ──
BOT_USERNAME = None  # filled in post_init

def set_group_button():
    label = "👥 Change group" if state["group"] else "👥 Set group"
    if BOT_USERNAME:
        return B(label, url=f"https://t.me/{BOT_USERNAME}?startgroup=setgroup")
    return B(label, callback_data="setgroup_help")

def panel():
    a = state["auto"]
    s = get_settings()
    liq_icon = "💧"
    return KB([
        [B("📊 Post prices now", callback_data="post"),
         B(f"{'🟢' if a else '🔴'} Auto: {'ON' if a else 'OFF'}", callback_data="auto")],
        [B("📋 Merchants", callback_data="list"), set_group_button()],
        [B("⚙️ Settings", callback_data="settings"), B("📝 Custom Msg", callback_data="custom_menu")],
        [B(f"{liq_icon} Liquidity: {'ON' if s.get('show_liquidity') else 'OFF'}", callback_data="toggle_liquidity"),
         B(f"🔘 Buttons: {'ON' if s.get('show_buttons') else 'OFF'}", callback_data="toggle_buttons")],
        [B("🔘 Manage buttons", callback_data="buttons_menu")],
        [B("👁 Preview", callback_data="preview"), B("🔄 Refresh", callback_data="panel")]
    ])

def settings_kb():
    s = get_settings()
    return KB([
        [B(f"💧 Liquidity: {'ON ✅' if s.get('show_liquidity') else 'OFF ❌'}", callback_data="toggle_liquidity"),
         B(f"🔘 All buttons: {'ON ✅' if s.get('show_buttons') else 'OFF ❌'}", callback_data="toggle_buttons")],
        [B("🔘 Manage buttons", callback_data="buttons_menu")],
        [B(f"🎯 Exact ad links: {'ON ✅' if link_mode() == 'ad' else 'OFF ❌'}", callback_data="toggle_link_mode"),
         B(f"🔗 Link prices: {'ON ✅' if s.get('price_links', True) else 'OFF ❌'}", callback_data="toggle_price_links")],
        [B("🔗 Ad link templates", callback_data="adlink_menu")],
        [B(f"🗑 Auto-delete prev: {'ON ✅' if s.get('auto_delete') else 'OFF ❌'}", callback_data="toggle_autodelete"),
         B(f"⏰ Delete after {s.get('delete_after_hours',24)}h", callback_data="toggle_delete_hours")],
        [B(f"🚪 Del Join/Left msgs: {'ON ✅' if s.get('delete_join_left', True) else 'OFF ❌'}", callback_data="toggle_joinleft")],
        [B("📝 Edit Header", callback_data="edit_header"), B("📝 Edit Body", callback_data="edit_body")],
        [B("📝 Edit Footer", callback_data="edit_footer"), B("🗑 Clear Custom Msg", callback_data="clear_custom")],
        [B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="panel")]
    ])

def buttons_menu_kb():
    s = get_settings()
    rows = [
        [B(f"🔘 All buttons: {'ON ✅' if s.get('show_buttons') else 'OFF ❌'}", callback_data="toggle_buttons"),
         B(f"🔄 Order: {order_label()}", callback_data="toggle_btn_order")],
        [B(f"🎯 Target: {'EXACT AD 🎯' if link_mode() == 'ad' else 'PROFILE 👤'}", callback_data="toggle_link_mode")],
        [B("🟢 Edit BUY label", callback_data="edit_buy_label"),
         B("🔴 Edit SELL label", callback_data="edit_sell_label")],
        [B("🔗 BUY link", callback_data="edit_buy_url"),
         B("🔗 SELL link", callback_data="edit_sell_url")],
    ]
    rows.append([
        B(f"{'🗑 Remove' if builtin_button_enabled(side) else '➕ Restore'} {side.upper()}",
          callback_data=f"{'remove' if builtin_button_enabled(side) else 'restore'}_{side}_button")
        for side in ("buy", "sell")
    ])
    rows += [
        [B("➕ Add button", callback_data="extra_add"),
         B(f"🧩 Extra buttons ({len(extra_buttons())})", callback_data="extra_buttons")],
        [B("🔗 Ad link templates", callback_data="adlink_menu")],
        [B("♻️ Reset buttons to default", callback_data="reset_buttons")],
        [B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="settings")]
    ]
    return KB(rows)


def extra_buttons_text():
    return (
        f"🧩 <b>Extra buttons ({len(extra_buttons())}/{MAX_EXTRA_BUTTONS})</b>\n\n"
        "Add your own profile, support, channel or website links.\n"
        "They appear after the remaining Buy/Sell buttons for <b>each merchant</b>, two per row.\n\n"
        "Tap a button below to edit its label/link or delete it.\n"
        "Use <code>{URL}</code> as the link to open each merchant's profile.\n"
        f"The full post is capped at {MAX_REPORT_BUTTONS} buttons; reduce merchants or extras if needed.\n"
        "The All buttons switch hides these too."
    )


def extra_buttons_kb():
    rows = [[B(f"✏️ {button['label'][:40]}", callback_data=f"extra_button:{button['id']}")]
            for button in extra_buttons()]
    rows += [[B("➕ Add button", callback_data="extra_add")],
             [B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="buttons_menu")]]
    return KB(rows)


def extra_button_text(button: dict):
    visibility = "" if get_settings().get("show_buttons", True) else (
        "\n\n⚠️ All buttons are OFF. Turn them on in 🔘 Manage buttons to show this button.")
    return (
        f"🧩 <b>Custom button</b>\n\n"
        f"Label: <code>{html_escape(button['label'])}</code>\n"
        f"Link: <code>{html_escape(button['url'])}</code>\n\n"
        "Shown for each merchant after the remaining Buy/Sell buttons.\n"
        "Changes appear in the next price post; use 👁 Preview to check them."
        + visibility
    )


def extra_button_kb(ident: str):
    return KB([
        [B("✏️ Edit label", callback_data=f"extra_label:{ident}"),
         B("🔗 Edit link", callback_data=f"extra_url:{ident}")],
        [B("🗑 Delete button", callback_data=f"extra_remove:{ident}")],
        [B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="extra_buttons")],
    ])


def extra_button_prompt(field: str) -> str:
    if field == "label":
        return ("🧩 <b>Send the button label</b>\n\n"
                "Use 1–60 characters of plain text, for example: 👤 My profile or 💬 Support.\n"
                "Extra-button labels are literal text, not price templates.\n\n"
                "Send /cancel to stop without saving.")
    return ("🔗 <b>Send the button link</b>\n\n"
            "Send an https://, http:// or tg:// URL (up to 2048 characters).\n"
            "Or send <code>{URL}</code> to open each merchant's public P2P profile.\n\n"
            "Example: <code>https://t.me/your_support</code>\n"
            "Send /cancel to stop without saving.")


def adlink_menu_text():
    """Explain the selected target and the optional ad-link templates."""
    s = get_settings()
    templates = ad_templates()
    mode = link_mode()
    lines = [
        "🔗 <b>Ad links — where the buttons take people</b>",
        "",
        f"🎯 Exact ad links: <b>{'ON ✅' if mode == 'ad' else 'OFF ❌'}</b>",
        ("   Buy/Sell buttons and the prices in the post open the <b>exact ad</b> "
         "the price was taken from." if mode == "ad" else
         "   Both buttons and linked prices open the merchant's public P2P profile."),
        f"🔗 Link prices in text: <b>{'ON ✅' if s.get('price_links', True) else 'OFF ❌'}</b>",
        "",
        "<b>Templates per exchange</b> (placeholders: <code>{AD_ID}</code> "
        "<code>{ASSET}</code> <code>{FIAT}</code> <code>{SIDE}</code> "
        "<code>{TAKER_SIDE}</code> <code>{URL}</code> <code>{NICK}</code>):",
    ]
    for ex in EXCHANGE_NAMES:
        tpl = templates.get(ex, "")
        tag = "🎯 exact ad" if template_is_exact(tpl) else "↪️ market page + ad hint"
        lines.append(f"{ICON.get(ex, '💱')} <b>{ex.title()}</b> — {tag}\n<code>{html_escape(tpl)}</code>")
    lines += [
        "",
        "Tap an exchange to change its template, or ♻️ to go back to the defaults.",
        "A template can also be set from the environment: "
        "<code>AD_LINK_TEMPLATES={\"okx\": \"https://…{AD_ID}\"}</code>.",
        "",
        "ℹ️ Binance publishes a real single-ad link, so those buttons open exactly "
        "one ad. OKX / Bybit / Bitget load the right market, side and pair and pass "
        "the ad id as a hint (Bybit's own share links expire after 30 minutes, so a "
        "permanent ad link is not possible there).",
    ]
    return "\n".join(lines)

def adlink_menu_kb():
    templates = ad_templates()
    rows = []
    for ex in EXCHANGE_NAMES:
        tag = "🎯" if template_is_exact(templates.get(ex, "")) else "↪️"
        rows.append([B(f"{tag} {ICON.get(ex, '💱')} {ex.title()}", callback_data=f"edit_adlink:{ex}")])
    rows.append([B(f"🎯 Exact ad links: {'ON ✅' if link_mode() == 'ad' else 'OFF ❌'}", callback_data="toggle_link_mode")])
    rows.append([B("♻️ Reset templates", callback_data="reset_adlinks")])
    rows.append([B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="settings")])
    return KB(rows)

def reset_adlinks():
    state["settings"]["ad_link_templates"] = {}
    state["settings"]["btn_link_mode"] = DEFAULT_SETTINGS["btn_link_mode"]
    save()

def custom_menu_kb():
    return KB([
        [B("📝 Edit Header", callback_data="edit_header"), B("📝 Edit Body", callback_data="edit_body")],
        [B("📝 Edit Footer", callback_data="edit_footer"), B("🗑 Clear All Custom", callback_data="clear_custom")],
        [B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="panel")]
    ])

def group_label():
    if not state["group"]: return None
    t = state.get("group_title")
    return f"{t} ({state['group']})" if t else str(state["group"])

def panel_text():
    g = group_label() or "not set — tap 👥 Set group below"
    s = get_settings()
    liq = "ON" if s.get("show_liquidity") else "OFF"
    btns = "ON" if s.get("show_buttons") else "OFF"
    autodel = "ON" if s.get("auto_delete") else "OFF"
    joinleft = "ON" if s.get("delete_join_left", True) else "OFF"
    header = s.get("custom_header") or "(default)"
    body = s.get("custom_body") or "(default)"
    footer = s.get("custom_footer") or "(none)"
    header_short = (header[:60] + "…") if len(header) > 60 else header
    body_short = (body[:60] + "…") if len(body) > 60 else body
    footer_short = (footer[:60] + "…") if len(footer) > 60 else footer
    last_msg = f"Last msg: {state.get('last_msg_id')}" if state.get('last_msg_id') else "No group msg yet"
    return (
        f"🤖 <b>P2P Price Bot</b>\n"
        f"Group: <code>{g}</code>\n"
        f"Merchants: {len(state['merchants'])} · Pair: {ASSET}/{FIAT} · every {INTERVAL}s\n"
        f"💧 Liquidity: <b>{liq}</b> · 🔘 Buttons: <b>{btns}</b> · 🗑 AutoDel: <b>{autodel}</b>\n"
        f"🔄 Btn order: <b>{order_label()}</b> · 🎯 Links: <b>{'EXACT AD' if link_mode() == 'ad' else 'PROFILE'}</b>\n"
        f"🚪 Del Join/Left msgs: <b>{joinleft}</b>\n"
        f"📝 Header: <code>{header_short}</code>\n"
        f"📝 Body: <code>{body_short}</code>\n"
        f"📝 Footer: <code>{footer_short}</code>\n"
        f"{last_msg}\n\n"
        f"➕ <b>Paste a merchant's public URL here to add it.</b>\n"
        f"Use ⚙️ Settings to toggle options and 📝 Custom Msg to customize the full post (header, body, footer)."
    )

def settings_text():
    s = get_settings()
    liq = "ON ✅" if s.get("show_liquidity") else "OFF ❌"
    btns = "ON ✅" if s.get("show_buttons") else "OFF ❌"
    autodel = "ON ✅" if s.get("auto_delete") else "OFF ❌"
    joinleft = "ON ✅" if s.get("delete_join_left", True) else "OFF ❌"
    del_hours = s.get("delete_after_hours", 24)
    header = s.get("custom_header") or "<i>(default: 📊 P2P {ASSET}/{FIAT})</i>"
    body = s.get("custom_body") or "<i>(default: exchange · merchant with sell/buy lines)</i>"
    footer = s.get("custom_footer") or "<i>(none)</i>"
    return (
        f"⚙️ <b>Settings</b>\n\n"
        f"💧 Show liquidity amount: <b>{liq}</b>\n"
        f"   When ON, shows available amount next to price.\n\n"
        f"🔘 Show buttons in group: <b>{btns}</b>\n"
        f"   When ON, group messages include the buttons you have configured.\n"
        f"   Order: <b>{order_label()}</b> — tap 🔘 Manage buttons to add or remove\n"
        f"   buttons, or edit their labels and links.\n\n"
        f"🎯 Buy/Sell buttons & prices open: <b>{'the EXACT ad 🎯' if link_mode() == 'ad' else 'the merchant profile 👤'}</b>\n"
        f"   Toggle it here or with the 🔘 Manage buttons menu; the deep-link\n"
        f"   templates live in 🔗 Ad link templates.\n\n"
        f"🔗 Clickable prices in the post: <b>{'ON ✅' if s.get('price_links', True) else 'OFF ❌'}</b>\n\n"
        f"🗑 Auto-delete previous message: <b>{autodel}</b>\n"
        f"   When ON, deletes previous price message on refresh/update.\n\n"
        f"⏰ Auto-delete after: <b>{del_hours}h</b>\n"
        f"   Message will be deleted after {del_hours} hours (0 = never).\n\n"
        f"🚪 Delete Join/Left messages: <b>{joinleft}</b>\n"
        f"   When ON, the bot deletes Telegram's \"user joined the group\" and\n"
        f"   \"user left the group\" service messages in your group.\n"
        f"   ⚠️ Bot must be a group admin with 'Delete messages' permission.\n\n"
        f"📝 Custom Header:\n{header}\n\n"
        f"📝 Custom Body (per merchant):\n{body}\n\n"
        f"📝 Custom Footer:\n{footer}\n\n"
        f"Header/Footer placeholders: <code>{{ASSET}}</code>, <code>{{FIAT}}</code>, <code>{{PAIR}}</code>\n"
        f"Body placeholders: <code>{{ICON}}</code> <code>{{EXCHANGE}}</code> <code>{{NICK}}</code> <code>{{SELL}}</code> <code>{{BUY}}</code> "
        f"<code>{{SELL_AMOUNT}}</code> <code>{{BUY_AMOUNT}}</code> <code>{{LINK}}</code> <code>{{URL}}</code> <code>{{ERROR}}</code> and header ones.\n"
        f"Ad-link placeholders: <code>{{SELL_URL}}</code> <code>{{BUY_URL}}</code> <code>{{SELL_AD_ID}}</code> <code>{{BUY_AD_ID}}</code> "
        f"<code>{{SELL_LINK}}</code> <code>{{BUY_LINK}}</code>.\n"
        f"HTML allowed: &lt;b&gt;, &lt;i&gt;, &lt;code&gt;, &lt;a&gt; etc."
    )

def html_escape(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def buttons_menu_text():
    s = get_settings()
    on = "ON ✅" if s.get("show_buttons") else "OFF ❌"
    buy_tpl = s.get("btn_buy_label") or DEFAULT_BUY_LABEL
    sell_tpl = s.get("btn_sell_label") or DEFAULT_SELL_LABEL
    default_url = "(merchant profile URL)" if link_mode() == "profile" else "(ad link template)"
    buy_url = s.get("btn_buy_url") or default_url
    sell_url = s.get("btn_sell_url") or default_url
    order_txt = "🟢 <b>BUY left</b> · 🔴 <b>SELL right</b>" if buttons_order() == "buy_sell" else "🔴 <b>SELL left</b> · 🟢 <b>BUY right</b>"
    labels = {"buy": buy_tpl, "sell": sell_tpl}
    sides = ("buy", "sell") if buttons_order() == "buy_sell" else ("sell", "buy")
    preview_labels = [labels[side] for side in sides if builtin_button_enabled(side)]
    preview_labels += [button["label"] for button in extra_buttons()]
    preview_row = "\n".join(" ".join(f"[ {html_escape(label)} ]" for label in preview_labels[i:i + 2])
                            for i in range(0, len(preview_labels), 2))
    if not preview_row:
        preview_row = "<i>No buttons. Restore BUY/SELL or add a custom button.</i>"
    return (
        f"🔘 <b>Buy / Sell buttons &amp; extras</b>\n\n"
        f"These are the inline buttons under the price post in your group.\n\n"
        f"🔘 All buttons: <b>{on}</b>\n"
        f"🟢 BUY: <b>{'included' if builtin_button_enabled('buy') else 'removed'}</b> · "
        f"🔴 SELL: <b>{'included' if builtin_button_enabled('sell') else 'removed'}</b>\n"
        f"🧩 Extra buttons: <b>{len(extra_buttons())}/{MAX_EXTRA_BUTTONS}</b>\n"
        f"Use Remove/Restore for BUY or SELL, or ➕ Add button for your own link.\n"
        f"🔄 Buy/Sell order: {order_txt}\n\n"
        f"🟢 <b>BUY label:</b>\n<code>{html_escape(buy_tpl)}</code>\n"
        f"🔴 <b>SELL label:</b>\n<code>{html_escape(sell_tpl)}</code>\n\n"
        f"🔗 BUY link: <code>{html_escape(buy_url)}</code>\n"
        f"🔗 SELL link: <code>{html_escape(sell_url)}</code>\n\n"
        f"🎯 Target: <b>{'the exact ad of the shown price' if link_mode() == 'ad' else 'the merchant profile page'}</b>\n"
        + ("   Each button opens the ad the price was read from "
           "(🟡 Binance = one specific ad, others = market page + ad hint).\n"
           if link_mode() == "ad" else
           "   Both buttons open the merchant's public P2P profile.\n"
           "   Users choose an ad there; no order is placed automatically.\n")
        + "   Custom BUY/SELL links override the selected target.\n\n"
        f"<b>Configured buttons (per merchant, two per row):</b>\n{preview_row}\n\n"
        f"<b>BUY/SELL label placeholders:</b>\n"
        f"<code>{{PRICE}}</code> <code>{{NICK}}</code> <code>{{FULLNICK}}</code> <code>{{EXCHANGE}}</code> "
        f"<code>{{ICON}}</code> <code>{{AMOUNT}}</code> <code>{{ASSET}}</code> <code>{{FIAT}}</code> "
        f"<code>{{PAIR}}</code> <code>{{SIDE}}</code>\n"
        f"Max 60 chars — the nickname is dropped automatically if the label gets too long.\n"
        f"Tap 👁 Preview to see the real buttons."
    )

def custom_menu_text():
    s = get_settings()
    header = s.get("custom_header") or "<i>(default)</i>"
    body = s.get("custom_body") or "<i>(default)</i>"
    footer = s.get("custom_footer") or "<i>(none)</i>"
    return (
        f"📝 <b>Custom Message</b>\n\n"
        f"Customize the full group post. The <b>Header</b> appears once on top, the <b>Body</b> "
        f"is repeated for every merchant, and the <b>Footer</b> appears once at the bottom.\n\n"
        f"<b>Current Header:</b>\n{header}\n\n"
        f"<b>Current Body (per merchant):</b>\n{body}\n\n"
        f"<b>Current Footer:</b>\n{footer}\n\n"
        f"Tap Edit to change. You can use:\n"
        f"• <code>{{ASSET}}</code> / <code>{{FIAT}}</code> / <code>{{PAIR}}</code>\n"
        f"• Body only: <code>{{ICON}}</code> <code>{{EXCHANGE}}</code> <code>{{NICK}}</code> <code>{{SELL}}</code> <code>{{BUY}}</code> "
        f"<code>{{SELL_AMOUNT}}</code> <code>{{BUY_AMOUNT}}</code> <code>{{LINK}}</code> <code>{{URL}}</code> <code>{{ERROR}}</code>\n"
        f"• HTML formatting, new lines supported\n"
        f"• Send /cancel to abort editing\n\n"
        f"Example header:\n<code>📊 P2P {{ASSET}}/{{FIAT}} - Best Rates 🔥</code>\n"
        f"Example body:\n<code>{{ICON}} {{NICK}} — Sell: {{SELL}} | Buy: {{BUY}} {{FIAT}}</code>\n"
        f"Example footer:\n<code>⚡️ Updated every {INTERVAL}s | Contact @youradmin</code>"
    )

def _set_group(chat):
    state["group"] = chat.id
    state["group_title"] = chat.title or ""
    state["last"] = {}  # force a fresh post to the new group
    save()

async def notify_admins(bot, text):
    for a in ADMINS:
        try: await bot.send_message(a, text, parse_mode="HTML", reply_markup=panel())
        except Exception: pass

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat = u.effective_chat
    if chat.type in ("group", "supergroup"):
        if c.args and c.args[0] == "setgroup":
            if not is_admin(u): return
            _set_group(chat)
            await u.message.reply_text(f"✅ <b>{chat.title}</b> will receive price updates.", parse_mode="HTML")
            await notify_admins(c.bot, f"✅ Group set to <b>{chat.title}</b>")
        return
    if is_admin(u): await u.message.reply_html(panel_text(), reply_markup=panel())
    else: await u.message.reply_text("⛔ You are not authorized. Ask the bot admin to add your ID.")

async def setgroup(u: Update, c):
    if not is_admin(u): return
    if u.effective_chat.type == "private":
        return await u.message.reply_html("Use the 👥 <b>Set group</b> button, or send /setgroup inside your group.",
                                          reply_markup=panel())
    _set_group(u.effective_chat)
    await u.message.reply_text("✅ This group will receive price updates.")

async def on_my_chat_member(u: Update, c):
    m = u.my_chat_member
    chat = m.chat
    if chat.type not in ("group", "supergroup"): return
    was, now = m.old_chat_member.status, m.new_chat_member.status
    joined = was in ("left", "kicked") and now in ("member", "administrator")
    if joined and m.from_user and m.from_user.id in ADMINS and state["group"] != chat.id:
        _set_group(chat)
        try: await c.bot.send_message(chat.id, f"✅ <b>{chat.title}</b> will receive price updates.", parse_mode="HTML")
        except Exception: pass
        await notify_admins(c.bot, f"✅ Group set to <b>{chat.title}</b>")
    elif now in ("left", "kicked") and state["group"] == chat.id:
        state["group"] = None; state["group_title"] = ""; save()
        await notify_admins(c.bot, f"⚠️ Bot was removed from <b>{chat.title}</b> — group unset.")

# ── delete "X joined / left the group" service messages ──
async def on_join_left(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Auto-delete Telegram's join/leave service messages in the group."""
    if not get_settings().get("delete_join_left", DEFAULT_SETTINGS["delete_join_left"]):
        return
    msg, chat = u.effective_message, u.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    # only in the registered group (if one is set)
    if state.get("group") and chat.id != state["group"]:
        return
    try:
        await msg.delete()
        kind = "joined" if msg.new_chat_members else "left"
        member = msg.new_chat_members[0] if msg.new_chat_members else msg.left_chat_member
        log.info("Deleted '%s the group' service message for %s in %s",
                 kind, getattr(member, "full_name", "?"), chat.id)
    except Exception as e:
        log.warning("Could not delete join/left msg in %s: %s "
                    "(make the bot a group admin with 'Delete messages' permission)", chat.id, e)

# ── custom message helpers ──
def apply_template(text: str) -> str:
    if not text:
        return ""
    return (text
            .replace("{ASSET}", ASSET).replace("{FIAT}", FIAT)
            .replace("{asset}", ASSET.lower()).replace("{fiat}", FIAT.lower())
            .replace("{Asset}", ASSET.title()).replace("{Fiat}", FIAT.title())
            .replace("{PAIR}", f"{ASSET}/{FIAT}").replace("{pair}", f"{ASSET}/{FIAT}"))

def apply_body_template(tpl: str, m: Merchant, r: dict) -> str:
    """Render a custom per-merchant body block with placeholders.
    Single-pass substitution: inserted values are never re-scanned."""
    if not tpl:
        return ""
    nick = m.nickname or m.merchant_id
    link = f'<a href="{m.profile_url}">{nick}</a>' if m.profile_url else nick
    sell_amt = fmt_amount(r.get("sell_amount")) or "—"
    buy_amt = fmt_amount(r.get("buy_amount")) or "—"
    sell, buy = fmt(r.get("sell")), fmt(r.get("buy"))
    err = r.get("error") or ""
    sell_links, buy_links = side_links("sell", m, r), side_links("buy", m, r)
    sell_link = f'<a href="{sell_links["best"]}">{sell}</a>' if sell_links["best"] else sell
    buy_link = f'<a href="{buy_links["best"]}">{buy}</a>' if buy_links["best"] else buy
    mapping = {
        "ASSET": ASSET, "asset": ASSET.lower(), "Asset": ASSET.title(),
        "FIAT": FIAT, "fiat": FIAT.lower(), "Fiat": FIAT.title(),
        "PAIR": f"{ASSET}/{FIAT}", "pair": f"{ASSET}/{FIAT}",
        "ICON": ICON.get(m.exchange, "💱"),
        "EXCHANGE": m.exchange.title(), "exchange": m.exchange, "Exchange": m.exchange.title(),
        "NICK": nick, "nick": nick, "Nick": (nick[:1].upper() + nick[1:]) if nick else nick,
        "URL": m.profile_url or "", "url": m.profile_url or "",
        "LINK": link, "Link": link,
        "SELL": sell, "Sell": sell, "BUY": buy, "Buy": buy,
        "SELL_AMOUNT": sell_amt, "BUY_AMOUNT": buy_amt,
        "SELL_LIQ": sell_amt, "BUY_LIQ": buy_amt,
        # selected profile/ad targets and optional ad links (see adlinks.py)
        "SELL_URL": sell_links["best"], "BUY_URL": buy_links["best"],
        "SELL_AD_URL": sell_links["ad"], "BUY_AD_URL": buy_links["ad"],
        "SELL_AD_ID": sell_links["ad_id"], "BUY_AD_ID": buy_links["ad_id"],
        "SELL_LINK": sell_link, "BUY_LINK": buy_link,
        "ERROR": err, "error": err,
    }
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in keys) + r")\}")
    return pattern.sub(lambda mm: mapping[mm.group(1)], tpl)

# ── add merchant by pasting URL / handle custom msg input ──
async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u) or u.effective_chat.type != "private": return
    txt = u.message.text.strip()

    awaiting = edit_get(u, "awaiting_custom")
    if isinstance(awaiting, str) and (awaiting in ("extra_add_label", "extra_add_url")
                                      or awaiting.startswith(("extra_label:", "extra_url:"))):
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("❌ Cancelled.", reply_markup=buttons_menu_kb())
        creating = awaiting.startswith("extra_add_")
        field = "label" if awaiting == "extra_add_label" or awaiting.startswith("extra_label:") else "url"
        button = None if creating else extra_button(awaiting.split(":", 1)[1])
        if not creating and button is None:
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("This button was removed. No changes saved.",
                                              reply_markup=extra_buttons_kb())
        if field == "label":
            value = clean_extra_button_label(txt)
            if not value:
                return await u.message.reply_text("❌ Send a non-empty label of 1–60 characters, or /cancel.")
            if creating:
                # Store the draft and next step together: the next message may
                # reach a different Vercel instance. Nothing is published yet.
                edits_of(u).update({"awaiting_custom": "extra_add_url", "extra_button_label": value})
                save()
                return await u.message.reply_html(
                    f"Label: <b>{html_escape(value)}</b>\n\n" + extra_button_prompt("url"),
                    reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]]))
        else:
            value = txt
            if not valid_extra_button_url(value):
                return await u.message.reply_text(
                    "❌ Use a valid https://, http:// or tg:// link, or {URL} for the merchant profile. "
                    "Maximum 2048 characters; no spaces or embedded login credentials. Try again or /cancel.")
        items = extra_buttons()
        if creating:
            if len(items) >= MAX_EXTRA_BUTTONS:
                edit_pop(u, "awaiting_custom")
                return await u.message.reply_text(f"You can add up to {MAX_EXTRA_BUTTONS} extra buttons. Remove one first.",
                                                  reply_markup=extra_buttons_kb())
            label = clean_extra_button_label(edit_get(u, "extra_button_label"))
            if not label:
                edit_pop(u, "awaiting_custom")
                return await u.message.reply_text("The button draft is missing. Tap Add button to start again.",
                                                  reply_markup=buttons_menu_kb())
            button = {"id": secrets.token_hex(6), "label": label, "url": value}
            items.append(button)
        else:
            for item in items:
                if item["id"] == button["id"]:
                    item[field] = value
                    button = item
                    break
        state["settings"]["extra_buttons"] = items
        edit_pop(u, "awaiting_custom")  # saves the complete button and clears its draft atomically
        return await u.message.reply_html("✅ Button saved.\n\n" + extra_button_text(button),
                                           reply_markup=extra_button_kb(button["id"]))

    if awaiting in ("header", "body", "footer"):
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            await u.message.reply_text("❌ Cancelled.", reply_markup=panel())
            return
        state["settings"][f"custom_{awaiting}"] = txt
        save()
        edit_pop(u, "awaiting_custom")
        await u.message.reply_html(f"✅ Custom {awaiting} saved:\n<code>{txt[:500]}</code>", reply_markup=panel())
        await u.message.reply_html(panel_text(), reply_markup=panel())
        return

    if awaiting and awaiting.startswith("adlink:"):
        ex = awaiting.split(":", 1)[1]
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            await u.message.reply_text("❌ Cancelled.", reply_markup=panel())
            return
        if ex not in EXCHANGE_NAMES:
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("❌ Unknown exchange.", reply_markup=panel())
        value = "" if txt.lower() in ("default", "reset", "-", "none") else txt
        if value and not value.startswith(("http://", "https://")):
            await u.message.reply_text("❌ The template must start with https:// — try again, or /cancel.")
            return
        if value and "{AD_ID}" not in value and edit_get(u, "adlink_warned") != ex:
            # no ad id → the link will land on the market page, not one exact ad
            edit_set(u, "adlink_warned", ex)
            await u.message.reply_html(
                "⚠️ That template has no <code>{AD_ID}</code> placeholder, so buttons would "
                "open the market page instead of one exact ad.\n\n"
                "Send the same text again to save it anyway, or /cancel.")
            return
        overrides = dict(state["settings"].get("ad_link_templates") or {})
        if value:
            overrides[ex] = value
        else:
            overrides.pop(ex, None)
        state["settings"]["ad_link_templates"] = overrides
        save()
        edit_pop(u, "awaiting_custom")
        edit_pop(u, "adlink_warned")
        shown = value or AD_LINK_TEMPLATES.get(ex, "")
        sample = render_template(shown, {
            "AD_ID": "1234567890", "ASSET": ASSET, "ASSET_LOWER": ASSET.lower(),
            "FIAT": FIAT, "FIAT_LOWER": FIAT.lower(), "SIDE": "sell", "SIDE_UPPER": "SELL",
            "TAKER_SIDE": "buy", "ACTION_TYPE": "1",
            "URL": "https://merchant-profile", "NICK": "Merchant",
        })
        await u.message.reply_html(
            f"✅ {ICON.get(ex, '💱')} {ex.title()} ad link template saved:\n"
            f"<code>{html_escape(shown)}</code>\n\n"
            f"Example link for a SELL ad:\n<code>{html_escape(sample)}</code>")
        await u.message.reply_html(adlink_menu_text(), reply_markup=adlink_menu_kb())
        return

    if awaiting in ("buy_label", "sell_label", "buy_url", "sell_url"):
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            await u.message.reply_text("❌ Cancelled.", reply_markup=panel())
            return
        side, kind = awaiting.split("_")          # buy/sell , label/url
        value = "" if txt.lower() in ("default", "reset", "-", "none") else txt
        if kind == "url" and value and not re.match(r"^(https?://|tg://|\{URL\})", value):
            await u.message.reply_text("❌ Link must start with https:// (or use {URL}). Try again, or /cancel.")
            return
        if kind == "label" and len(value) > 200:
            await u.message.reply_text("❌ Label template too long (max 200 chars). Try again, or /cancel.")
            return
        state["settings"][f"btn_{side}_{kind}"] = value
        save()
        edit_pop(u, "awaiting_custom")
        icon = "🟢" if side == "buy" else "🔴"
        shown = value or ("(default)" if kind == "url" else
                          (DEFAULT_BUY_LABEL if side == "buy" else DEFAULT_SELL_LABEL) + "  (default)")
        await u.message.reply_html(f"✅ {icon} {side.upper()} button {kind} saved:\n<code>{html_escape(shown[:300])}</code>")
        await u.message.reply_html(buttons_menu_text(), reply_markup=buttons_menu_kb())
        return

    m = parse_url(txt, ASSET, FIAT)
    if not m:
        return await u.message.reply_text("❌ Not a supported merchant URL (Binance / Bybit / OKX / Bitget).   /start")
    msg = await u.message.reply_text("⏳ Checking merchant…")
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as cl:
        r = await fetch(cl, m)
    state["merchants"][m.key] = asdict(m); save()
    extra = ""
    s = get_settings()
    if s.get("show_liquidity"):
        if r.get("sell_amount") is not None:
            extra += f"\nSell liq: {fmt_amount(r['sell_amount'])} {m.asset}"
        if r.get("buy_amount") is not None:
            extra += f"\nBuy liq: {fmt_amount(r['buy_amount'])} {m.asset}"
    if r.get("error"):
        extra += f"\n⚠️ {r['error']}"
    await msg.edit_text(f"✅ Added {ICON[m.exchange]} {m.exchange.title()} · {m.nickname or m.merchant_id}\n"
                        f"Sell: {fmt(r['sell'])} · Buy: {fmt(r['buy'])}{extra}")
    await u.message.reply_html(panel_text(), reply_markup=panel())

# ── prices ──
async def get_prices():
    ms = merchants()
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as cl:
        res = await asyncio.gather(*(fetch(cl, m) for m in ms))
    out = {}
    for m, r in zip(ms, res):
        state["merchants"][m.key] = asdict(m)
        out[m.key] = r
    save(); return out

def report(prices):
    s = get_settings()
    custom_header = s.get("custom_header", "").strip()
    if custom_header:
        header_raw = apply_template(custom_header)
        lines = [header_raw, ""]
    else:
        lines = [f"📊 <b>P2P {ASSET}/{FIAT}</b>\n"]

    custom_body = s.get("custom_body", "").strip()
    for m in merchants():
        r = prices.get(m.key)
        if not r:
            continue
        if custom_body:
            block = apply_body_template(custom_body, m, r).strip()
            if block:
                lines.append(block)
            continue
        nick_display = m.nickname or m.merchant_id
        lines.append(f"{ICON[m.exchange]} <b>{m.exchange.title()}</b> · "
                     f"<a href=\"{m.profile_url}\">{nick_display}</a>")
        if r.get("error"):
            lines.append(f"   ⚠️ {r['error']}\n")
            continue
        sell_price = fmt(r.get("sell"))
        buy_price = fmt(r.get("buy"))
        sell_amt = fmt_amount(r.get("sell_amount")) if s.get("show_liquidity") else None
        buy_amt = fmt_amount(r.get("buy_amount")) if s.get("show_liquidity") else None

        # Prices follow the selected target, just like default Buy/Sell buttons.
        if s.get("price_links", True):
            sell_url = side_links("sell", m, r)["best"]
            buy_url = side_links("buy", m, r)["best"]
            if sell_url:
                sell_price = f'<a href="{sell_url}">{sell_price}</a>'
            if buy_url:
                buy_price = f'<a href="{buy_url}">{buy_price}</a>'

        sell_line = f"   🔴 Best SELL (you buy): <b>{sell_price}</b>"
        if sell_amt:
            sell_line += f"  💧 {sell_amt} {m.asset}"
        buy_line = f"   🟢 Best BUY  (you sell): <b>{buy_price}</b>"
        if buy_amt:
            buy_line += f"  💧 {buy_amt} {m.asset}"

        # keep the text lines in the same order as the Buy/Sell buttons
        if buttons_order() == "buy_sell":
            lines.append(buy_line)
            lines.append(sell_line + "\n")
        else:
            lines.append(sell_line)
            lines.append(buy_line + "\n")

    custom_footer = s.get("custom_footer", "").strip()
    if custom_footer:
        lines.append(apply_template(custom_footer))

    return "\n".join(lines).strip()

# ── Buy / Sell button helpers ──
def buy_label_tpl():
    return (get_settings().get("btn_buy_label") or "").strip() or DEFAULT_BUY_LABEL

def sell_label_tpl():
    return (get_settings().get("btn_sell_label") or "").strip() or DEFAULT_SELL_LABEL

def buttons_order():
    return "sell_buy" if get_settings().get("buttons_order") == "sell_buy" else "buy_sell"

def order_label():
    return "🟢 Buy ⬅️ | Sell ➡️ 🔴" if buttons_order() == "buy_sell" else "🔴 Sell ⬅️ | Buy ➡️ 🟢"

def render_btn_label(tpl: str, m: Merchant, price, amount, side: str) -> str:
    """Render a Buy/Sell button caption. Single-pass placeholder substitution."""
    nick = (m.nickname or m.merchant_id or "")
    mapping = {
        "PRICE": fmt(price), "price": fmt(price),
        "AMOUNT": fmt_amount(amount) or "—", "LIQ": fmt_amount(amount) or "—",
        "NICK": nick[:14], "nick": nick[:14], "FULLNICK": nick,
        "EXCHANGE": m.exchange.title(), "exchange": m.exchange,
        "ICON": ICON.get(m.exchange, "💱"),
        "ASSET": ASSET, "asset": ASSET.lower(),
        "FIAT": FIAT, "fiat": FIAT.lower(),
        "PAIR": f"{ASSET}/{FIAT}", "pair": f"{ASSET}/{FIAT}",
        "SIDE": side.upper(), "side": side.lower(),
    }
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in keys) + r")\}")
    label = pattern.sub(lambda mm: mapping[mm.group(1)], tpl)
    label = " ".join(label.split())
    if len(label) > 60:
        # too long → drop the merchant nickname first, then hard-truncate
        short = pattern.sub(lambda mm: "" if mm.group(1).lower() in ("nick", "fullnick") else mapping[mm.group(1)], tpl)
        label = " ".join(short.split()) or label
        if len(label) > 60:
            label = label[:59].rstrip() + "…"
    return label or side.upper()

def btn_url(side: str, m: Merchant, r: dict | None = None) -> str:
    """Link behind a Buy/Sell button.

    A custom URL wins; otherwise use the selected target (merchant profile by
    default). Try the other target if unavailable, then the market page.
    """
    custom = (get_settings().get(f"btn_{side}_url") or "").strip()
    if custom:
        # {URL} {AD_URL} {AD_ID} {PRICE} {NICK} {EXCHANGE} {ASSET} {FIAT} {SIDE} …
        return render_template(custom, link_values(side, m, r))
    return side_links(side, m, r)["best"]

def report_keyboard(prices):
    s = get_settings()
    if not s.get("show_buttons", True):
        return None
    rows, count = [], 0
    custom_buttons = extra_buttons()
    for m in merchants():
        r = prices.get(m.key)
        if not r:
            continue
        sell, buy = r.get("sell"), r.get("buy")
        buy_btn = sell_btn = None
        # NOTE: a merchant's BUY ad is where the user sells, and vice-versa —
        # the labels keep the exchange wording, only their order is configurable.
        # Both buttons open the merchant profile by default (btn_link_mode).
        if buy is not None and builtin_button_enabled("buy"):
            buy_url = btn_url("buy", m, r)
            if buy_url:
                buy_btn = B(render_btn_label(buy_label_tpl(), m, buy, r.get("buy_amount"), "buy"),
                            url=buy_url)
        if sell is not None and builtin_button_enabled("sell"):
            sell_url = btn_url("sell", m, r)
            if sell_url:
                sell_btn = B(render_btn_label(sell_label_tpl(), m, sell, r.get("sell_amount"), "sell"),
                             url=sell_url)
        pair = [buy_btn, sell_btn] if buttons_order() == "buy_sell" else [sell_btn, buy_btn]
        merchant_buttons = [button for button in pair if button]
        for button in custom_buttons:
            url = m.profile_url if button["url"] in PROFILE_BUTTON_URLS else button["url"]
            # A missing/broken profile must not invalidate the entire keyboard.
            if valid_extra_button_url(url) and url not in PROFILE_BUTTON_URLS:
                merchant_buttons.append(B(button["label"], url=url))
        remaining = MAX_REPORT_BUTTONS - count
        visible = merchant_buttons[:remaining]
        rows.extend(visible[i:i + 2] for i in range(0, len(visible), 2))
        count += len(visible)
        if len(merchant_buttons) > remaining:
            log.warning("Price-post keyboard capped at %s buttons; reduce merchants or extra buttons",
                        MAX_REPORT_BUTTONS)
            break
    return KB(rows) if rows else None

# ── deletion helpers ──
async def delete_last_group_message(bot):
    """Delete previous price message in group if exists"""
    gid = state.get("group")
    mid = state.get("last_msg_id")
    if not gid or not mid:
        return False
    try:
        await bot.delete_message(chat_id=gid, message_id=mid)
        log.info(f"Deleted previous group message {mid} in {gid}")
        state["last_msg_id"] = None
        state["last_msg_time"] = None
        save()
        return True
    except Exception as e:
        # message may already be deleted or bot not admin
        log.debug(f"Could not delete msg {mid}: {e}")
        # if message not found, clear state to avoid repeated attempts
        if "not found" in str(e).lower() or "message to delete not found" in str(e).lower() or "BadRequest" in str(type(e)):
            state["last_msg_id"] = None
            state["last_msg_time"] = None
            save()
        return False

async def post(bot, force=False):
    if not state["group"] or not state["merchants"]: return False
    prices = await get_prices()
    s = get_settings()
    # the ad id is part of the snapshot: when the cheapest/most expensive ad of a
    # merchant changes, the post (and its exact-ad buttons) must be refreshed too
    if s.get("show_liquidity"):
        snap = {k: [v.get("sell"), v.get("buy"), v.get("sell_amount"), v.get("buy_amount"),
                    v.get("sell_ad_id"), v.get("buy_ad_id")] for k, v in prices.items()}
    else:
        snap = {k: [v.get("sell"), v.get("buy"), v.get("sell_ad_id"), v.get("buy_ad_id")]
                for k, v in prices.items()}
    snap["_header"] = s.get("custom_header","")
    snap["_body"] = s.get("custom_body","")
    snap["_footer"] = s.get("custom_footer","")
    snap["_liq"] = s.get("show_liquidity")
    snap["_btn"] = s.get("show_buttons")
    snap["_btn_order"] = buttons_order()
    snap["_btn_buy"] = s.get("btn_buy_label", "")
    snap["_btn_sell"] = s.get("btn_sell_label", "")
    snap["_btn_buy_url"] = s.get("btn_buy_url", "")
    snap["_btn_sell_url"] = s.get("btn_sell_url", "")
    snap["_btn_buy_enabled"] = builtin_button_enabled("buy")
    snap["_btn_sell_enabled"] = builtin_button_enabled("sell")
    snap["_extra_buttons"] = extra_buttons()  # detached copy: edits must change the snapshot
    snap["_link_mode"] = link_mode()
    snap["_ad_templates"] = ad_templates()
    snap["_price_links"] = bool(s.get("price_links", True))
    if not force and snap == state["last"]: return False
    state["last"] = snap; save()

    # Delete previous message if auto_delete enabled (refresh button or update time)
    if s.get("auto_delete", True):
        await delete_last_group_message(bot)

    text = report(prices)
    kb = report_keyboard(prices)
    try:
        sent = await bot.send_message(state["group"], text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        # store new message id and time
        state["last_msg_id"] = sent.message_id
        state["last_msg_time"] = int(time.time())
        save()
        log.info(f"Posted new price message {sent.message_id} to group {state['group']}")
    except Exception as e:
        log.warning(f"Failed to post to group: {e}")
        return False
    return True

async def auto_post_task(bot) -> bool:
    """Post prices when auto mode is on (and the snapshot changed)."""
    if not state.get("auto"):
        return False
    return bool(await post(bot))

async def cleanup_task(bot) -> bool:
    """Delete the group message once it is older than delete_after_hours."""
    s = get_settings()
    hours = s.get("delete_after_hours", 24)
    if hours <= 0:
        return False  # disabled
    last_time = state.get("last_msg_time")
    if not last_time or not state.get("last_msg_id") or not state.get("group"):
        return False
    now = int(time.time())
    if now - last_time < hours * 3600:
        return False
    log.info(f"Message {state['last_msg_id']} is older than {hours}h, auto-deleting")
    try:
        await bot.delete_message(chat_id=state["group"], message_id=state["last_msg_id"])
        state["last_msg_id"] = None
        state["last_msg_time"] = None
        save()
        return True
    except Exception as e:
        log.debug(f"Cleanup delete failed: {e}")
        # clear if not found
        if "not found" in str(e).lower():
            state["last_msg_id"] = None
            state["last_msg_time"] = None
            save()
        return False

# ── scheduled work ──
async def job(c: ContextTypes.DEFAULT_TYPE):
    """PTB JobQueue callback — posts prices when they change."""
    try:
        await auto_post_task(c.bot)
    except Exception as e:
        log.warning("auto post failed: %s", e)

async def cleanup_job(c: ContextTypes.DEFAULT_TYPE):
    """PTB JobQueue callback — deletes stale group messages."""
    try:
        await cleanup_task(c.bot)
    except Exception as e:
        log.warning("cleanup failed: %s", e)

# ── buttons ──
def list_kb():
    rows = [[B(f"❌ {ICON[m.exchange]} {m.exchange.title()} · {m.nickname or m.merchant_id}",
               callback_data=f"del:{m.key}")] for m in merchants()]
    return KB(rows + [[B("⬅️ Back", callback_data="panel")]])

async def on_button(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    if not is_admin(u): return await q.answer()
    d = q.data

    if d == "post":
        ok = await post(c.bot, force=True)
        await q.answer("✅ Posted!" if ok else "⚠️ Set group (/setgroup) and add merchants first", show_alert=not ok)

    elif d == "auto":
        state["auto"] = not state["auto"]; save(); await q.answer(f"Auto {'ON' if state['auto'] else 'OFF'}")

    elif d == "setgroup_help":
        return await q.answer("Add the bot to your group, then send /setgroup there.", show_alert=True)

    elif d == "list":
        await q.answer()
        return await q.edit_message_text("📋 <b>Merchants</b> — tap to remove" if state["merchants"]
                                         else "📋 No merchants yet. Paste a URL to add one.",
                                         parse_mode="HTML", reply_markup=list_kb())

    elif d.startswith("del:"):
        state["merchants"].pop(d[4:], None); state["last"].pop(d[4:], None); save()
        await q.answer("🗑 Removed")
        return await q.edit_message_text("📋 <b>Merchants</b> — tap to remove" if state["merchants"]
                                         else "📋 No merchants yet.",
                                         parse_mode="HTML", reply_markup=list_kb())

    elif d == "settings":
        await q.answer()
        return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())

    elif d == "custom_menu":
        await q.answer()
        return await q.edit_message_text(custom_menu_text(), parse_mode="HTML", reply_markup=custom_menu_kb())

    elif d == "buttons_menu":
        if edit_get(u, "awaiting_custom"):
            edit_pop(u, "awaiting_custom")
        await q.answer()
        return await q.edit_message_text(buttons_menu_text(), parse_mode="HTML", reply_markup=buttons_menu_kb())

    elif d in ("remove_buy_button", "remove_sell_button", "restore_buy_button", "restore_sell_button"):
        action, side, _ = d.split("_")
        state["settings"][f"btn_{side}_enabled"] = action == "restore"
        save()
        await q.answer(f"{side.upper()} button {'restored' if action == 'restore' else 'removed'}")
        return await q.edit_message_text(buttons_menu_text(), parse_mode="HTML", reply_markup=buttons_menu_kb())

    elif d == "extra_buttons":
        if edit_get(u, "awaiting_custom"):
            edit_pop(u, "awaiting_custom")
        await q.answer()
        return await q.edit_message_text(extra_buttons_text(), parse_mode="HTML", reply_markup=extra_buttons_kb())

    elif d == "extra_add":
        if len(extra_buttons()) >= MAX_EXTRA_BUTTONS:
            return await q.answer(f"Limit: {MAX_EXTRA_BUTTONS} extra buttons. Delete one first.", show_alert=True)
        edit_set(u, "awaiting_custom", "extra_add_label")
        await q.answer()
        return await q.edit_message_text(extra_button_prompt("label"), parse_mode="HTML",
                                          reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]]))

    elif d.startswith(("extra_button:", "extra_label:", "extra_url:", "extra_remove:", "extra_delete:")):
        action, ident = d.split(":", 1)
        button = extra_button(ident)
        if button is None:
            return await q.answer("This button no longer exists. Open Manage buttons again.", show_alert=True)
        if action == "extra_button":
            if edit_get(u, "awaiting_custom"):
                edit_pop(u, "awaiting_custom")
            await q.answer()
            return await q.edit_message_text(extra_button_text(button), parse_mode="HTML",
                                              reply_markup=extra_button_kb(ident))
        if action in ("extra_label", "extra_url"):
            field = "label" if action == "extra_label" else "url"
            edit_set(u, "awaiting_custom", d)
            await q.answer()
            return await q.edit_message_text(
                extra_button_prompt(field) + f"\n\nCurrent: <code>{html_escape(button[field])}</code>",
                parse_mode="HTML", reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]]))
        if action == "extra_remove":
            await q.answer()
            return await q.edit_message_text(
                f"🗑 Delete <b>{html_escape(button['label'])}</b> from future price posts?",
                parse_mode="HTML", reply_markup=KB([
                    [B("🗑 Delete", callback_data=f"extra_delete:{ident}"),
                     B("Keep button", callback_data=f"extra_button:{ident}")]]))
        state["settings"]["extra_buttons"] = [item for item in extra_buttons() if item["id"] != ident]
        edit_pop(u, "awaiting_custom")
        await q.answer("Button deleted")
        return await q.edit_message_text(extra_buttons_text(), parse_mode="HTML", reply_markup=extra_buttons_kb())

    elif d == "adlink_menu":
        await q.answer()
        return await q.edit_message_text(adlink_menu_text(), parse_mode="HTML", reply_markup=adlink_menu_kb())

    elif d == "toggle_link_mode":
        state["settings"]["btn_link_mode"] = "profile" if link_mode() == "ad" else "ad"
        save()
        await q.answer("Buttons open the EXACT ad 🎯" if link_mode() == "ad"
                       else "Buttons open the merchant profile 👤")
        if "Ad links" in (q.message.text or ""):
            return await q.edit_message_text(adlink_menu_text(), parse_mode="HTML", reply_markup=adlink_menu_kb())
        if "Buy / Sell buttons" in (q.message.text or ""):
            return await q.edit_message_text(buttons_menu_text(), parse_mode="HTML", reply_markup=buttons_menu_kb())
        try:
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except Exception:
            pass

    elif d == "toggle_price_links":
        state["settings"]["price_links"] = not state["settings"].get("price_links", True)
        save()
        await q.answer(f"Clickable prices {'ON' if state['settings']['price_links'] else 'OFF'}")
        try:
            if "Ad links" in (q.message.text or ""):
                return await q.edit_message_text(adlink_menu_text(), parse_mode="HTML", reply_markup=adlink_menu_kb())
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except Exception:
            pass

    elif d == "reset_adlinks":
        reset_adlinks()
        await q.answer("♻️ Ad link templates reset")
        return await q.edit_message_text(adlink_menu_text(), parse_mode="HTML", reply_markup=adlink_menu_kb())

    elif d.startswith("edit_adlink:"):
        ex = d.split(":", 1)[1]
        if ex not in EXCHANGE_NAMES:
            return await q.answer()
        edit_set(u, "awaiting_custom", f"adlink:{ex}")
        tpl = ad_templates().get(ex, "")
        await q.answer()
        return await q.edit_message_text(
            f"{ICON.get(ex, '💱')} <b>{ex.title()} ad link template</b>\n\n"
            "Placeholders:\n"
            "• <code>{AD_ID}</code> — the ad id from the exchange API\n"
            "• <code>{TAKER_SIDE}</code> / <code>{SIDE}</code> — buy / sell\n"
            "• <code>{ASSET}</code> <code>{ASSET_LOWER}</code> <code>{FIAT}</code> <code>{FIAT_LOWER}</code>\n"
            "• <code>{URL}</code> <code>{NICK}</code> <code>{EXCHANGE}</code> <code>{ACTION_TYPE}</code>\n\n"
            f"Current:\n<code>{html_escape(tpl)}</code>\n\n"
            "Send the new template (must start with <code>https://</code>), "
            "send <code>default</code> to restore the built-in one, or /cancel to abort.",
            parse_mode="HTML",
            reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]])
        )

    elif d == "toggle_btn_order":
        state["settings"]["buttons_order"] = "sell_buy" if buttons_order() == "buy_sell" else "buy_sell"
        save()
        await q.answer("Buy left / Sell right" if buttons_order() == "buy_sell" else "Sell left / Buy right")
        try:
            return await q.edit_message_text(buttons_menu_text(), parse_mode="HTML", reply_markup=buttons_menu_kb())
        except Exception:
            pass

    elif d in ("edit_buy_label", "edit_sell_label"):
        side = "buy" if d == "edit_buy_label" else "sell"
        edit_set(u, "awaiting_custom", f"{side}_label")
        icon = "🟢" if side == "buy" else "🔴"
        default_tpl = DEFAULT_BUY_LABEL if side == "buy" else DEFAULT_SELL_LABEL
        cur = state["settings"].get(f"btn_{side}_label") or f"{default_tpl}   (default)"
        await q.answer()
        return await q.edit_message_text(
            f"{icon} <b>Send the new {side.upper()} button label</b>\n\n"
            "Placeholders:\n"
            "• <code>{PRICE}</code> — the price\n"
            "• <code>{NICK}</code> — merchant nickname (max 14 chars) · <code>{FULLNICK}</code> — full\n"
            "• <code>{EXCHANGE}</code> <code>{ICON}</code> — exchange name / emoji\n"
            "• <code>{AMOUNT}</code> — available liquidity\n"
            f"• <code>{{ASSET}}</code> = {ASSET} · <code>{{FIAT}}</code> = {FIAT} · <code>{{PAIR}}</code> = {ASSET}/{FIAT}\n"
            "• <code>{SIDE}</code> = BUY / SELL\n\n"
            f"Current:\n<code>{html_escape(cur)}</code>\n\n"
            f"Example:\n<code>{icon} {side.upper()} {{PRICE}} {{FIAT}} · {{NICK}}</code>\n\n"
            "Send <code>default</code> to restore the default label, or /cancel to abort.",
            parse_mode="HTML",
            reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]])
        )

    elif d in ("edit_buy_url", "edit_sell_url"):
        side = "buy" if d == "edit_buy_url" else "sell"
        edit_set(u, "awaiting_custom", f"{side}_url")
        icon = "🟢" if side == "buy" else "🔴"
        default_url = "(merchant profile URL)" if link_mode() == "profile" else "(ad link template)"
        cur = state["settings"].get(f"btn_{side}_url") or default_url
        await q.answer()
        return await q.edit_message_text(
            f"{icon} <b>Send the new {side.upper()} button link</b>\n\n"
            "Without a custom link, the button follows the selected Target (default: merchant profile).\n"
            "You can send your own link (e.g. your support chat or a referral page).\n\n"
            "Placeholders: <code>{URL}</code> <code>{NICK}</code> <code>{EXCHANGE}</code> "
            "<code>{ASSET}</code> <code>{FIAT}</code>\n\n"
            f"Current:\n<code>{html_escape(cur)}</code>\n\n"
            "Send <code>default</code> to clear the override and use the selected Target, or /cancel to abort.",
            parse_mode="HTML",
            reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]])
        )

    elif d in ("reset_buttons", "reset_buttons_confirm"):
        if d == "reset_buttons" and extra_buttons():
            await q.answer()
            return await q.edit_message_text(
                "♻️ Reset all buttons? This deletes the extra buttons and restores BUY/SELL with profile links.",
                reply_markup=KB([[B("Reset all buttons", callback_data="reset_buttons_confirm"),
                                   B("Cancel", callback_data="buttons_menu")]]))
        for k in ("btn_buy_label", "btn_sell_label", "btn_buy_url", "btn_sell_url",
                  "btn_buy_enabled", "btn_sell_enabled", "extra_buttons", "show_buttons",
                  "buttons_order", "btn_link_mode"):
            state["settings"][k] = deepcopy(DEFAULT_SETTINGS[k])
        edit_pop(u, "awaiting_custom")
        await q.answer("♻️ Buttons reset to default")
        return await q.edit_message_text(buttons_menu_text(), parse_mode="HTML", reply_markup=buttons_menu_kb())

    elif d == "toggle_liquidity":
        state["settings"]["show_liquidity"] = not state["settings"].get("show_liquidity", False)
        save()
        await q.answer(f"Liquidity {'ON' if state['settings']['show_liquidity'] else 'OFF'}")
        try:
            if "Settings" in q.message.text_html or "Settings" in (q.message.text or ""):
                return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except:
            pass

    elif d == "toggle_buttons":
        state["settings"]["show_buttons"] = not state["settings"].get("show_buttons", True)
        save()
        await q.answer(f"Buttons {'ON' if state['settings']['show_buttons'] else 'OFF'}")
        cur_txt = q.message.text or ""
        try:
            if "Buy / Sell buttons" in cur_txt:
                return await q.edit_message_text(buttons_menu_text(), parse_mode="HTML", reply_markup=buttons_menu_kb())
            if "Settings" in cur_txt or "⚙️" in cur_txt:
                return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except Exception:
            pass

    elif d == "toggle_autodelete":
        state["settings"]["auto_delete"] = not state["settings"].get("auto_delete", True)
        save()
        await q.answer(f"Auto-delete {'ON' if state['settings']['auto_delete'] else 'OFF'}")
        try:
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except:
            pass

    elif d == "toggle_delete_hours":
        # cycle through 0, 6, 12, 24, 48
        options = [0, 6, 12, 24, 48]
        cur = state["settings"].get("delete_after_hours", 24)
        try:
            idx = options.index(cur)
            nxt = options[(idx + 1) % len(options)]
        except:
            nxt = 24
        state["settings"]["delete_after_hours"] = nxt
        save()
        await q.answer(f"Delete after {nxt}h" if nxt else "Never auto-delete")
        try:
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except:
            pass

    elif d == "edit_header":
        edit_set(u, "awaiting_custom", "header")
        await q.answer()
        return await q.edit_message_text(
            "📝 <b>Send new custom HEADER now</b>\n\n"
            "You can use:\n"
            f"• <code>{{ASSET}}</code> = {ASSET}\n"
            f"• <code>{{FIAT}}</code> = {FIAT}\n"
            "• HTML tags like &lt;b&gt;bold&lt;/b&gt;\n\n"
            "Current:\n"
            f"<code>{state['settings'].get('custom_header') or '(default)'}</code>\n\n"
            "Send the new header text, or /cancel to abort.",
            parse_mode="HTML",
            reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]])
        )

    elif d == "edit_body":
        edit_set(u, "awaiting_custom", "body")
        await q.answer()
        return await q.edit_message_text(
            "📝 <b>Send new custom BODY now</b>\n\n"
            "The body is repeated once per merchant in the group post.\n\n"
            "Placeholders:\n"
            "• <code>{ICON}</code> <code>{EXCHANGE}</code> <code>{NICK}</code>\n"
            "• <code>{SELL}</code> <code>{BUY}</code> — best prices\n"
            "• <code>{SELL_AMOUNT}</code> <code>{BUY_AMOUNT}</code> — liquidity\n"
            "• <code>{LINK}</code> — clickable merchant name\n"
            "• <code>{URL}</code> — merchant profile link\n"
            "• <code>{ERROR}</code> — fetch error (if any)\n"
            f"• <code>{{ASSET}}</code> = {ASSET} · <code>{{FIAT}}</code> = {FIAT} · <code>{{PAIR}}</code> = {ASSET}/{FIAT}\n"
            "• HTML tags and new lines are supported\n\n"
            "Example (default look):\n"
            "<code>{ICON} &lt;b&gt;{EXCHANGE}&lt;/b&gt; · {LINK}\n"
            "🔴 Sell: &lt;b&gt;{SELL}&lt;/b&gt; 💧 {SELL_AMOUNT} {ASSET}\n"
            "🟢 Buy: &lt;b&gt;{BUY}&lt;/b&gt; 💧 {BUY_AMOUNT} {ASSET}</code>\n\n"
            "Current:\n"
            f"<code>{state['settings'].get('custom_body') or '(default)'}</code>\n\n"
            "Send the new body text, or /cancel to abort.",
            parse_mode="HTML",
            reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]])
        )

    elif d == "edit_footer":
        edit_set(u, "awaiting_custom", "footer")
        await q.answer()
        return await q.edit_message_text(
            "📝 <b>Send new custom FOOTER now</b>\n\n"
            "You can use:\n"
            f"• <code>{{ASSET}}</code> = {ASSET}\n"
            f"• <code>{{FIAT}}</code> = {FIAT}\n"
            "• HTML tags\n\n"
            "Current:\n"
            f"<code>{state['settings'].get('custom_footer') or '(none)'}</code>\n\n"
            "Send the new footer text, or /cancel to abort.",
            parse_mode="HTML",
            reply_markup=KB([[B("❌ Cancel", callback_data="cancel_edit")]])
        )

    elif d == "toggle_joinleft":
        state["settings"]["delete_join_left"] = not state["settings"].get("delete_join_left", True)
        save()
        await q.answer(f"Delete join/left msgs {'ON' if state['settings']['delete_join_left'] else 'OFF'}")
        try:
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except:
            pass

    elif d == "clear_custom":
        state["settings"]["custom_header"] = ""
        state["settings"]["custom_body"] = ""
        state["settings"]["custom_footer"] = ""
        save()
        await q.answer("🗑 Custom message cleared")
        return await q.edit_message_text(custom_menu_text(), parse_mode="HTML", reply_markup=custom_menu_kb())

    elif d == "cancel_edit":
        edit_pop(u, "awaiting_custom")
        await q.answer("Cancelled")
        return await q.edit_message_text(panel_text(), parse_mode="HTML", reply_markup=panel())

    elif d == "preview":
        await q.answer("Generating preview…")
        if state["merchants"]:
            prices = await get_prices()
        else:
            prices = {}
        text = report(prices) if prices else (
            f"{apply_template(state['settings'].get('custom_header')) or f'📊 P2P {ASSET}/{FIAT}'}\n\n"
            f"<i>No merchants yet. Add one to see prices.</i>\n\n"
            f"{apply_template(state['settings'].get('custom_footer')) or ''}"
        )
        kb = report_keyboard(prices) if prices else None
        try:
            await c.bot.send_message(q.message.chat_id, f"👁 <b>Preview - how it will look in group:</b>\n\n{text}",
                                     parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        except Exception as e:
            await c.bot.send_message(q.message.chat_id, f"Preview error: {e}\n\n{text[:3000]}", parse_mode="HTML")
        return

    elif d == "panel":
        await q.answer()

    else:
        await q.answer()
        return

    try: await q.edit_message_text(panel_text(), parse_mode="HTML", reply_markup=panel())
    except Exception: pass

async def cancel_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u): return
    if edit_get(u, "awaiting_custom"):
        edit_pop(u, "awaiting_custom")
        await u.message.reply_text("❌ Editing cancelled.", reply_markup=panel())
    else:
        await u.message.reply_text("Nothing to cancel.", reply_markup=panel())

async def preview_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u) or u.effective_chat.type != "private": return
    if state["merchants"]:
        prices = await get_prices()
        text = report(prices)
        kb = report_keyboard(prices)
    else:
        text = f"{apply_template(state['settings'].get('custom_header')) or f'📊 P2P {ASSET}/{FIAT}'}\n\n<i>No merchants yet.</i>"
        kb = None
    await u.message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)

async def error_handler(update, context):
    log.warning("Update %s caused error %s", update, context.error)

# ── run ──
async def post_init(application):
    global BOT_USERNAME
    me = await application.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Logged in as @%s", BOT_USERNAME)

def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("preview", preview_cmd))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS
                                   | filters.StatusUpdate.LEFT_CHAT_MEMBER, on_join_left))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    return app

def build_application(polling: bool = True) -> Application:
    """Build the PTB application.

    ``polling=True``  long polling with the in-process JobQueue — the systemd,
                      Docker and local installs.
    ``polling=False`` serverless (Vercel): no updater and no JobQueue, because
                      Telegram pushes the updates to /api/webhook and a Vercel
                      Cron drives the periodic work (see serverless.py).
    """
    builder = Application.builder().token(TOKEN).post_init(post_init)
    if not polling:
        builder = builder.updater(None).job_queue(None)
    return register_handlers(builder.build())

def main():
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sig, lambda *_: sys.exit(0))
        except: pass

    app = build_application()
    app.job_queue.run_repeating(job, interval=INTERVAL, first=5)
    # cleanup job: check every 10 minutes if message older than 24h
    app.job_queue.run_repeating(cleanup_job, interval=600, first=60)
    print(f"🚀 Bot running · {ASSET}/{FIAT} · every {INTERVAL}s · Ctrl+C to stop")
    print(f"   Admins: {', '.join(map(str, ADMINS))} · Group: {state['group'] or 'not set'} · Merchants: {len(state['merchants'])}")
    print(f"   State: {STORE.describe()}")
    print(f"   Buttons: {'exact ad 🎯' if link_mode() == 'ad' else 'merchant profile 👤'}"
          f" · clickable prices: {'ON' if get_settings().get('price_links', True) else 'OFF'}")
    if not state["group"]:
        print("   → Open the bot in Telegram, /start, tap 👥 Set group")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
