"""Telegram bot: P2P merchant price feed."""

from __future__ import annotations

import os, sys, json, asyncio, logging, argparse, signal, time, re, secrets
from copy import deepcopy
from dataclasses import asdict
from urllib.parse import urlsplit
from pathlib import Path
import httpx
from telegram import (Update, ChatPermissions, BotCommand, BotCommandScopeAllPrivateChats,
                      BotCommandScopeChat, InlineKeyboardButton as _TelegramButton,
                      InlineKeyboardMarkup as KB)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ChatMemberHandler, ContextTypes, filters)
from exchanges import Merchant, parse_url, fetch, HEADERS
from adlinks import (EXCHANGE_NAMES, AD_LINK_TEMPLATES, ad_link, market_link,
                     resolve_templates, render_template, template_is_exact, taker_side)
from storage import build_store, database_link, database_connected, probe_store

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

# ── destinations: the price post goes to every chat that is set ─────────────
# "group" is the classic group/supergroup; "channel" is an optional extra
# channel the bot posts the same report into.  Both keep their own last message
# id (auto-delete / cleanup) and their own change snapshot.
DESTINATIONS = ("group", "channel")
LAST_ID = {"group": "last_msg_id", "channel": "channel_last_msg_id"}
LAST_TIME = {"group": "last_msg_time", "channel": "channel_last_msg_time"}
SNAPSHOT = {"group": "last", "channel": "channel_last"}
CHAT_ID = {"group": "group", "channel": "channel"}
CHAT_TITLE = {"group": "group_title", "channel": "channel_title"}

# ── auto-forward: repost what an admin sends in the private chat ────────────
FORWARD_TARGETS = ("off", "group", "channel", "both")
FORWARD_LABELS = {"off": "OFF", "group": "GROUP", "channel": "CHANNEL", "both": "GROUP + CHANNEL"}
MAX_FORWARD_HISTORY = 20          # how many 📤 Undo buttons stay valid
FORWARD_HISTORY_TTL = 48 * 3600   # …and for how long (seconds)
MAX_CAPTCHA_LENGTH = 1000         # the custom anti-scam challenge text

# ── ↪️ relay: which chats are forwarded *into* the group ────────────────────
# The admin picks the source chats: any channel or group the bot can read.
# With nothing picked the configured channel is relayed, which is what the bot
# did before this option existed — an upgrade changes nobody's setup.
FORWARD_SOURCE_TYPES = ("channel", "group", "supergroup")
FORWARD_SOURCE_ICONS = {"channel": "📢", "group": "👥", "supergroup": "👥"}
MAX_FORWARD_SOURCES = 10          # how many chats may be relayed at once
MAX_SOURCE_TITLE = 120            # titles are only ever shown in a menu
OWN_MESSAGE_LIMIT = 30            # bot messages in a source chat not to relay back
# /start payloads that register a chat (deep links: startgroup= / startchannel=)
SETUP_ACTIONS = ("setgroup", "setchannel", "forwardfrom")

# ── anti-scam verification (captcha) ────────────────────────────────────────
# A new member is muted and has to type a random word.  While pending they may
# only send plain text (no links, media or stickers), so a scammer cannot post
# anything useful even before the bot removes them.
CAPTCHA_ATTEMPT_CHOICES = (1, 2, 3, 5, 10)
CAPTCHA_TIMEOUT_CHOICES = (1, 2, 5, 10, 30)        # minutes
CAPTCHA_ACTIONS = ("restrict", "kick", "ban")
CAPTCHA_ACTION_TITLES = {"restrict": "mute until I approve 🔇",
                         "kick": "kick (they can rejoin) 👢",
                         "ban": "ban permanently 🚫"}
CAPTCHA_ACTION_SHORT = {"restrict": "Mute", "kick": "Kick", "ban": "Ban"}
CAPTCHA_LOCK_TTL = 48 * 3600      # how long a failed record stays for the admin
DEFAULT_CAPTCHA_MESSAGE = (
    "🛡 <b>Anti-scam check</b>\\n\\n"
    "{MENTION} welcome to <b>{GROUP}</b>! Scammer bots are everywhere, so type this word "
    "to unlock the group:\\n\\n"
    "<code>{WORD}</code>\\n\\n"
    "⏳ You have {MINUTES} minutes · {LEFT} attempts left."
)
# Short, unambiguous words: the challenge must be easy to type on a phone.
CAPTCHA_WORDS = (
    "amber", "anchor", "apple", "beacon", "bison", "blossom", "bridge", "canyon",
    "cedar", "cobalt", "comet", "coral", "crystal", "dahlia", "delta", "desert",
    "ember", "falcon", "fjord", "forest", "garden", "glacier", "harbor", "hazel",
    "horizon", "iris", "ivory", "jade", "jungle", "kettle", "lagoon", "lantern",
    "lilac", "meadow", "mesa", "mint", "moss", "nectar", "nimbus", "ocean",
    "olive", "onyx", "orchid", "panda", "pebble", "pepper", "petal", "pine",
    "prairie", "quartz", "quiver", "raven", "reef", "river", "saffron", "sage",
    "savanna", "scarlet", "sierra", "silver", "summit", "sunset", "thunder",
    "tiger", "topaz", "tulip", "tundra", "valley", "velvet", "willow", "zephyr",
)

# Chat permissions used by the anti-scam check.  ``PENDING`` deliberately keeps
# plain text enabled — Telegram mutes *everything* when can_send_messages is
# False, and then nobody could ever type the word.
PENDING_PERMISSIONS = ChatPermissions(can_send_messages=True)
MUTED_PERMISSIONS = ChatPermissions.no_permissions()
MEMBER_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)

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
    # ── look of the buttons and the post ──
    "button_icons": {},            # {"🟢": {"icon": "<custom emoji id>", "style": "success"}}
    "post_photo": "",              # banner for the group post: file_id or https URL
    "post_photo_kind": "photo",    # how to send it: "photo" (send_photo) | "animation" (GIF)
    "post_photo_size": {},         # what the uploaded file measures: {width, height, duration}
    "post_photo_hd": True,         # deliver the banner at its own size (📐 Full HD)
    # ── reposting what the admin sends in the private chat ──
    "forward_target": "group",     # "off" | "group" | "channel" | "both"
    # ── ↪️ reposting new messages from the selected chats into the group ──
    # which chats: state["forward_sources"] (empty = the configured channel)
    "channel_to_group": True,
    "group_to_channel": False,     # opt in: ordinary group posts to price channel
    # ── anti-scam verification for new members ──
    "captcha_enabled": True,       # mute new members until they type a random word
    "captcha_message": "",         # empty = DEFAULT_CAPTCHA_MESSAGE
    "captcha_attempts": 3,         # wrong words before the member is muted/removed
    "captcha_timeout": 5,          # minutes before a pending challenge expires
    "captcha_action": "restrict",  # what happens then: "restrict" | "kick" | "ban"
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


# ── button icons (custom emoji images) & the post banner ────────────────────
# Telegram can show a custom emoji — a premium, often animated "image" — in
# front of a button label, and colour the button (Bot API 9.4).  Only PTB 22.7
# names those fields, but 21.6 forwards them verbatim through ``api_kwargs``, so
# the pinned dependency already supports them.
#
# The icon is chosen by the emoji a label starts with: an admin sets it once for
# 🟢 and every button whose label starts with 🟢 shows it.  That is what makes
# this work for *every* button in the bot — the group post, the panel, the menus
# — without a per-button setting for each of them.
BUTTON_STYLES = ("success", "danger", "primary")
STYLE_TITLES = {"success": "green 🟢", "danger": "red 🔴", "primary": "blue 🔵"}
CAPTION_LIMIT = 1024              # Telegram's limit for a photo caption

# Leading emoji of a label, including the variation selectors and ZWJ sequences
# Telegram builds emoji from ("⚙️" is U+2699 U+FE0F, "👁" is a lone pictograph).
_EMOJI_RUN_RE = re.compile(
    r"^((?:[0-9#*]\uFE0F?\u20E3"
    r"|[\U0001F000-\U0001FAFF\u2300-\u23FF\u2460-\u24FF\u25A0-\u25FF"
    r"\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u2900-\u297F"
    r"\u00A9\u00AE\u203C\u2049\u2122\u2139\u3030\u303D\u3297\u3299"
    r"\uFE0F\u200D\u20E3\U000E0020-\U000E007F])+)\s*",
    re.UNICODE,
)


def leading_emoji(text: str) -> str:
    """The emoji a button label starts with (``"⚙️ Settings"`` → ``"⚙️"``)."""
    match = _EMOJI_RUN_RE.match((text or "").lstrip())
    return match.group(1) if match else ""


def icon_key(value: str) -> str:
    """The lookup key for a label or a stored emoji — ``""`` when unusable.

    Also keeps the key small enough for ``callback_data`` (64 bytes), which is
    where it travels while an admin edits it.
    """
    key = leading_emoji(value)
    return key if key and len(key.encode()) <= 48 else ""


def clean_icon_id(value) -> str:
    """A custom emoji id — a plain number, exactly as Telegram reports it."""
    text = str(value or "").strip()
    return text if text.isdigit() and 1 <= len(text) <= 25 else ""


def normalize_button_icons(value) -> dict:
    """Copy the valid saved icon records; malformed state must not break a post."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, entry in value.items():
        key = icon_key(key) if isinstance(key, str) else ""
        if not key:
            continue
        if isinstance(entry, str):                     # tolerate a bare id
            entry = {"icon": entry}
        if not isinstance(entry, dict):
            continue
        icon = clean_icon_id(entry.get("icon"))
        style = entry.get("style")
        style = style if style in BUTTON_STYLES else ""
        if icon or style:
            result[key] = {"icon": icon, "style": style}
    return result


def button_icons() -> dict:
    return normalize_button_icons(get_settings().get("button_icons"))


def button_icon(label: str, override: str | None = None) -> tuple[str, str]:
    """``(custom emoji id, style)`` for a label — ``("", "")`` when nothing is set."""
    key = icon_key(override) if override is not None else icon_key(label)
    entry = button_icons().get(key) if key else None
    return (entry["icon"], entry["style"]) if entry else ("", "")


# Telegram only shows button icons to bots with a Fragment username, or in
# messages sent to chats when the bot owner has Premium.  A server that refuses
# them must not cost us the price post: remember the refusal for this process
# and keep sending plain buttons (the price post matters more than its looks).
# The memory is the icon map that failed, so editing 🖼 Button icons tries again
# instead of staying plain until a restart.
_rejected_icons: dict | None = None


def icons_available() -> bool:
    """Whether buttons may carry icons — False while the refused set is unchanged."""
    if _rejected_icons is None:
        return True
    try:
        return button_icons() != _rejected_icons
    except Exception:                                  # pragma: no cover - defensive
        return False


def disable_button_icons(reason) -> None:
    global _rejected_icons
    if _rejected_icons is None:
        log.warning("Telegram refused the button icons (%s) — sending plain buttons from now "
                    "on. Remove the icon in 🖼 Button icons, or get the bot a Fragment username "
                    "/ a Premium owner.", reason)
    try:
        _rejected_icons = button_icons()
    except Exception:                                  # pragma: no cover - defensive
        _rejected_icons = {}


def _looks_like_icon_refusal(error) -> bool:
    text = str(error).lower()
    return ("icon_custom_emoji" in text or "custom emoji" in text
            or ("button" in text and "emoji" in text))


def B(text, *, icon=None, style=None, **kwargs):
    """``InlineKeyboardButton`` plus the configured custom-emoji icon / colour.

    Every button in the bot is built through this wrapper, so one 🖼 Button icons
    entry restyles every button that starts with that emoji — group post and
    admin panel alike.  Never raises: a broken setting leaves a plain button.
    """
    try:
        icon_id, saved_style = button_icon(text, icon)
    except Exception as e:                             # pragma: no cover - defensive
        log.warning("Button icon lookup failed (%s) — using a plain button", e)
        icon_id, saved_style = "", ""
    extras = {}
    if icons_available():
        if icon_id:
            extras["icon_custom_emoji_id"] = icon_id
        chosen = style or saved_style
        if chosen in BUTTON_STYLES:
            extras["style"] = chosen
    if extras:
        kwargs["api_kwargs"] = {**(kwargs.get("api_kwargs") or {}), **extras}
    return _TelegramButton(text, **kwargs)


# ── the post banner ─────────────────────────────────────────────────────────
#: how a banner is delivered — a still picture goes out with ``send_photo``, a
#: GIF (or any other silent animation) with ``send_animation``, which Telegram
#: plays inline in the post.  ``post_photo`` holds *what* to send,
#: ``post_photo_kind`` *how* — an animation file id only works with the method
#: it came from — and ``post_photo_size`` *how big*, so the post shows the file
#: at the size it was uploaded at instead of a preview (📐 Full HD).
BANNER_KINDS = ("photo", "animation")
BANNER_KIND_LABELS = {"photo": "photo", "animation": "GIF"}
#: the ``awaiting_custom`` keys the banner screens use (``banner_photo`` is the
#: old name of ``banner_media``, still honoured for a prompt left open across
#: an upgrade)
BANNER_AWAITING = ("banner_media", "banner_photo", "banner_url", "banner_gif_url")
#: a URL ending in one of these is delivered as an animation
ANIMATION_URL_SUFFIXES = (".gif", ".mp4")


def valid_photo_url(value: str) -> bool:
    """An http(s) image URL Telegram can download for ``send_photo``."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in '<>"\\' for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme in ("http", "https") and parsed.hostname
                    and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def clean_banner(value) -> str:
    """A banner is a Telegram file id or an http(s) image URL — nothing else."""
    text = str(value or "").strip()
    if not text or len(text) > 2048:
        return ""
    if text.startswith(("http://", "https://")):
        return text if valid_photo_url(text) else ""
    return text if re.fullmatch(r"[A-Za-z0-9_\-=]{10,200}", text) else ""


def clean_banner_kind(value) -> str:
    """``"animation"`` for a GIF, ``"photo"`` for anything else (the default)."""
    kind = str(value or "").strip().lower()
    return kind if kind in BANNER_KINDS else "photo"


def banner_kind() -> str:
    """How the current banner has to be sent: ``"photo"`` or ``"animation"``."""
    return clean_banner_kind(get_settings().get("post_photo_kind"))


def banner_kind_label(kind: str = "") -> str:
    """``"GIF"`` or ``"photo"`` — what the admin is shown for the current banner."""
    return BANNER_KIND_LABELS.get(clean_banner_kind(kind or banner_kind()), "photo")


def banner_kind_from_url(url) -> str:
    """A link that ends in ``.gif`` / ``.mp4`` is an animation, the rest a photo."""
    try:
        path = urlsplit(str(url or "")).path.lower()
    except ValueError:
        return "photo"
    return "animation" if path.endswith(ANIMATION_URL_SUFFIXES) else "photo"


#: An HD banner is delivered with the measurements of the file the admin sent:
#: ``sendAnimation`` then plays the GIF at its own size, instead of the small
#: preview Telegram picks for an animation that carries no size info.  A still
#: photo needs nothing (``sendPhoto`` always uses the largest copy of the
#: upload) and a URL — like a GIF that arrived as a plain file — has no
#: measured size here, so no size is claimed for it: a wrong one, shown as a
#: stretched or letterboxed banner, is worse than none.
BANNER_SIZE_LIMITS = {"width": (1, 10000), "height": (1, 10000)}
#: how long a silent GIF/MP4 may claim to play for — a decoration, so an absurd
#: value is dropped instead of throwing the whole size away
BANNER_DURATION_LIMIT = 600


def _size_number(raw, low: int, high: int):
    """One field of a banner size: an int inside its bounds, ``0`` when absent.

    ``None`` means *unusable* — the caller then drops the whole size.
    """
    if raw is None or raw == "":
        return 0 if low == 0 else None
    if isinstance(raw, bool):
        return None
    try:
        number = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def clean_banner_size(value) -> dict:
    """A believable ``{width, height, duration}`` — or nothing at all."""
    if not isinstance(value, dict):
        return {}
    size = {}
    for key, (low, high) in BANNER_SIZE_LIMITS.items():
        number = _size_number(value.get(key), low, high)
        if number is None:
            return {}                      # half a size is worse than none
        size[key] = number
    duration = _size_number(value.get("duration"), 0, BANNER_DURATION_LIMIT)
    size["duration"] = duration or 0                       # only the play length, so never fatal
    return size


def banner_size() -> dict:
    """What the current banner measures (``{}`` when nothing is known about it)."""
    return clean_banner_size(get_settings().get("post_photo_size"))


def banner_hd() -> bool:
    """Post the banner at its own size — on, unless the admin turned it off."""
    return bool(get_settings().get("post_photo_hd", True))


def banner_hd_label() -> str:
    """``"ON ✅"`` / ``"OFF ❌"`` — how the 📐 switch reads on the banner screen."""
    return "ON ✅" if banner_hd() else "OFF ❌"


def toggle_banner_hd() -> bool:
    """📐 Full HD on/off for every future post; returns the new state."""
    state["settings"]["post_photo_hd"] = not banner_hd()
    save()
    return banner_hd()


def banner_size_label(size: dict = None) -> str:
    """``"1080×640"`` for the banner screen, ``""`` when the size is unknown."""
    size = banner_size() if size is None else clean_banner_size(size)
    return f"{size['width']}×{size['height']}" if size else ""


def banner_send_kwargs(kind: str = "") -> dict:
    """The ``sendAnimation`` fields that keep the banner in full HD.

    Empty when the banner is a photo (the API has no size field there), when 📐
    Full HD is off, or when nothing is known about the file's size.
    """
    if clean_banner_kind(kind or banner_kind()) != "animation" or not banner_hd():
        return {}
    size = banner_size()
    if not size:
        return {}
    kwargs = {"width": size["width"], "height": size["height"]}
    if size["duration"]:
        kwargs["duration"] = size["duration"]
    return kwargs


def set_banner(value, kind: str = "photo", size=None) -> None:
    """Store a banner, *how* it is delivered and *what it measures* (state only)."""
    banner = clean_banner(value)
    state["settings"]["post_photo"] = banner
    state["settings"]["post_photo_kind"] = clean_banner_kind(kind) if banner else "photo"
    state["settings"]["post_photo_size"] = clean_banner_size(size) if banner else {}


def post_banner() -> str:
    return clean_banner(get_settings().get("post_photo"))


# ── destinations: the group and the optional channel ────────────────────────
def chat_of(kind: str):
    """The chat id set for a destination (``"group"`` / ``"channel"``), or None."""
    return state.get(CHAT_ID.get(kind, "group"))

def chat_title_of(kind: str) -> str:
    return state.get(CHAT_TITLE.get(kind, "group_title")) or ""

def post_targets() -> list[tuple[str, int]]:
    """Every chat the price report is posted to, in order."""
    out = []
    for kind in DESTINATIONS:
        chat_id = chat_of(kind)
        # a group and its linked channel share nothing here: both are posted to
        if isinstance(chat_id, int) and chat_id not in [c for _, c in out]:
            out.append((kind, chat_id))
    return out

def destination_label(kind: str) -> str:
    """A human name for a destination, e.g. ``"channel @p2p_rates"``."""
    title = chat_title_of(kind)
    return f"{kind} {title}" if title else kind


# ── auto-forward: where an admin's private message is reposted ───────────────
def forward_target() -> str:
    target = get_settings().get("forward_target")
    return target if target in FORWARD_TARGETS else DEFAULT_SETTINGS["forward_target"]

def channel_to_group_enabled() -> bool:
    value = get_settings().get("channel_to_group")
    return value if isinstance(value, bool) else DEFAULT_SETTINGS["channel_to_group"]

def toggle_channel_to_group() -> bool:
    enabled = not channel_to_group_enabled()
    state["settings"]["channel_to_group"] = enabled
    save()
    return enabled

def group_to_channel_enabled() -> bool:
    value = get_settings().get("group_to_channel")
    return value if isinstance(value, bool) else DEFAULT_SETTINGS["group_to_channel"]

def toggle_group_to_channel() -> bool:
    enabled = not group_to_channel_enabled()
    state["settings"]["group_to_channel"] = enabled
    save()
    return enabled


# ── ↪️ relay sources: which channel/group is forwarded into the group ────────
def normalize_forward_sources(value) -> dict:
    """Keep the usable source records — a broken one must never stop a relay."""
    clean: dict = {}
    if not isinstance(value, dict):
        return clean
    for key, record in value.items():
        if not isinstance(record, dict) or record.get("type") not in FORWARD_SOURCE_TYPES:
            continue
        try:
            chat_id = int(record.get("chat_id", key))
        except (TypeError, ValueError):
            continue
        title = str(record.get("title") or "").strip()[:MAX_SOURCE_TITLE]
        try:
            added = int(record.get("added") or 0)
        except (TypeError, ValueError):
            added = 0
        clean[str(chat_id)] = {"chat_id": chat_id, "title": title,
                               "type": record["type"], "added": added}
        if len(clean) >= MAX_FORWARD_SOURCES:
            break
    return clean

def forward_sources() -> dict:
    """The chats the admin selected, keyed by chat id ({} = relay the channel)."""
    return normalize_forward_sources(state.get("forward_sources"))

def forward_source_records() -> list[dict]:
    """The selected sources in the order they were added (oldest first)."""
    records = list(forward_sources().values())
    records.sort(key=lambda r: r.get("added") or 0)
    return records

def forward_source_ids() -> set[int]:
    """Every chat whose messages are relayed into the group."""
    ids = {record["chat_id"] for record in forward_sources().values()}
    if not ids and isinstance(state.get("channel"), int):
        ids = {state["channel"]}      # nothing chosen yet → the classic behaviour
    return ids

def is_forward_source(chat_id) -> bool:
    """Whether messages from this chat are relayed — also the PTB filter's answer.

    The destination group is never a source: relaying it into itself is
    pointless, and matching it here would take its messages away from the
    🛡 anti-scam handler (PTB runs only the first handler that matches).
    """
    if not channel_to_group_enabled() or not isinstance(state.get("group"), int):
        return False
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return False
    return chat_id != state["group"] and chat_id in forward_source_ids()

def add_forward_source(chat_id, title="", kind="") -> bool:
    """Select a channel/group to forward from; False when it already was one."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return False
    sources = forward_sources()
    if str(chat_id) in sources:
        return False
    if len(sources) >= MAX_FORWARD_SOURCES:
        log.warning("Forward source limit reached (%s) — %s was not added",
                    MAX_FORWARD_SOURCES, chat_id)
        return False
    sources[str(chat_id)] = {"chat_id": chat_id, "title": str(title or "").strip()[:MAX_SOURCE_TITLE],
                             "type": kind if kind in FORWARD_SOURCE_TYPES else "channel",
                             "added": int(time.time())}
    state["forward_sources"] = sources
    save()
    return True

def remove_forward_source(chat_id) -> bool:
    """Stop forwarding from a chat; False when it was not selected."""
    sources = forward_sources()
    try:
        removed = sources.pop(str(int(chat_id)), None) is not None
    except (TypeError, ValueError):
        return False
    state["forward_sources"] = sources
    save()
    return removed

def source_selected(chat_id) -> bool:
    """Whether this chat was picked by the admin (not just the channel default)."""
    try:
        return str(int(chat_id)) in forward_sources()
    except (TypeError, ValueError):
        return False

# The bot answers inside a source chat ("✅ forwarding from here"), and Telegram
# hands that answer back as an ordinary update — without a note of who sent it,
# because a channel post has no ``from_user``.  So the ids the bot posted itself
# are remembered for a moment and skipped by the relay.
def own_messages() -> dict:
    records = state.get("own_messages")
    return records if isinstance(records, dict) else {}

def remember_own_message(chat_id, message_id) -> None:
    """Note a message the bot posted into a ↪️ source chat."""
    try:
        key = f"{int(chat_id)}:{int(message_id)}"
    except (TypeError, ValueError):
        return
    records = own_messages()
    records[key] = int(time.time())
    for old in sorted(records, key=lambda k: records[k])[:-OWN_MESSAGE_LIMIT]:
        records.pop(old, None)
    state["own_messages"] = records
    save()

def is_own_message(chat_id, message_id) -> bool:
    try:
        return f"{int(chat_id)}:{int(message_id)}" in own_messages()
    except (TypeError, ValueError):
        return False

def forward_source_label(record: dict) -> str:
    """``"📢 Rates channel"`` — or the chat id when Telegram gave no title."""
    icon = FORWARD_SOURCE_ICONS.get(record.get("type"), "💬")
    title = record.get("title") or ""
    return f"{icon} {title or record.get('chat_id')}"

def forward_source_summary() -> str:
    """Short answer to "from where?" for a button label."""
    sources = forward_sources()
    if not sources:
        return "the channel" if state.get("channel") else "none"
    if len(sources) == 1:
        record = next(iter(sources.values()))
        title = record.get("title") or str(record.get("chat_id"))
        return title if len(title) <= 22 else title[:21] + "…"
    return f"{len(sources)} chats"

def relay_header(chat) -> str:
    """``📢 <b>News</b>`` — the source chat's name, written atop every relay.

    "Forward from channel to group with channel name": the name is part of the
    message the group receives, not only Telegram's own "Forwarded from" note.
    The live title from the update wins; the stored ones are the fallback.
    """
    record = forward_sources().get(str(getattr(chat, "id", ""))) or {}
    title = str(getattr(chat, "title", "") or "").strip()
    if not title:
        title = str(record.get("title") or "").strip()
    if not title and getattr(chat, "id", None) == state.get("channel"):
        title = str(state.get("channel_title") or "").strip()
    icon = FORWARD_SOURCE_ICONS.get(getattr(chat, "type", "") or record.get("type", ""), "💬")
    return f"{icon} <b>{html_escape(title or str(getattr(chat, 'id', '')))}</b>"

def cycle_forward_target() -> str:
    """📤 Auto-forward button: group → channel → both → off."""
    order = ("group", "channel", "both", "off")
    chosen = order[(order.index(forward_target()) + 1) % len(order)]
    state["settings"]["forward_target"] = chosen
    save()
    return chosen

def forward_targets() -> list[tuple[str, int]]:
    """Where a forwarded admin message goes (empty = forwarding is off)."""
    target = forward_target()
    wanted = {"group": ("group",), "channel": ("channel",), "both": DESTINATIONS}.get(target, ())
    return [(kind, chat_id) for kind, chat_id in post_targets() if kind in wanted]

def forward_label() -> str:
    return FORWARD_LABELS[forward_target()]

def remember_forward(token: str, messages: list[dict]) -> None:
    """Keep what a 📤 repost created so 🗑 Undo can delete it again."""
    history = state.setdefault("forwards", {})
    if not isinstance(history, dict):
        history = {}
    history[token] = {"messages": messages, "ts": int(time.time())}
    for old in sorted(history, key=lambda k: history[k].get("ts") or 0)[:-MAX_FORWARD_HISTORY]:
        history.pop(old, None)
    state["forwards"] = history


# ── anti-scam verification (captcha) settings ───────────────────────────────
def clean_captcha_message(value) -> str:
    """The custom challenge text: plain HTML Telegram can send, or \"\" = default."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_CAPTCHA_LENGTH:
        return ""
    return text

def _clean_choice(value, choices, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number in choices else default

def captcha_message() -> str:
    return clean_captcha_message(get_settings().get("captcha_message")) or DEFAULT_CAPTCHA_MESSAGE

def captcha_attempts() -> int:
    return _clean_choice(get_settings().get("captcha_attempts"),
                         CAPTCHA_ATTEMPT_CHOICES, DEFAULT_SETTINGS["captcha_attempts"])

def captcha_timeout() -> int:
    """Minutes a new member gets to type the word."""
    return _clean_choice(get_settings().get("captcha_timeout"),
                         CAPTCHA_TIMEOUT_CHOICES, DEFAULT_SETTINGS["captcha_timeout"])

def captcha_action() -> str:
    action = get_settings().get("captcha_action")
    return action if action in CAPTCHA_ACTIONS else DEFAULT_SETTINGS["captcha_action"]

def captcha_enabled() -> bool:
    return get_settings().get("captcha_enabled", True) is not False

def new_captcha_word() -> str:
    """A random, easy-to-type word, e.g. ``"tiger4821"``."""
    return f"{secrets.choice(CAPTCHA_WORDS)}{secrets.randbelow(9000) + 1000}"

def captcha_key(chat_id, user_id) -> str:
    return f"{int(chat_id)}:{int(user_id)}"

def pending_captchas() -> dict:
    records = state.get("captcha")
    return records if isinstance(records, dict) else {}

def pending_captcha(chat_id, user_id) -> dict | None:
    record = pending_captchas().get(captcha_key(chat_id, user_id))
    return record if isinstance(record, dict) else None

def captcha_word_matches(answer: str, word: str) -> bool:
    """Case- and punctuation-insensitive: phones capitalise and add spaces."""
    def normalise(text):
        return "".join(ch for ch in str(text or "").lower() if ch.isalnum())
    return bool(word) and normalise(answer) == normalise(word)

def render_captcha(tpl: str, *, word: str, name: str, user_id, group: str,
                   minutes: int, left: int) -> str:
    """Fill the challenge template. Single pass — inserted text is not re-scanned."""
    mapping = {
        "WORD": word, "word": word.lower(),
        "NAME": html_escape(name), "name": name,
        "MENTION": f'<a href="tg://user?id={int(user_id)}">{html_escape(name)}</a>',
        "USER_ID": str(int(user_id)),
        "GROUP": html_escape(group or "the group"), "group": html_escape(group or "the group"),
        "MINUTES": str(minutes), "minutes": str(minutes),
        "ATTEMPTS": str(captcha_attempts()), "LEFT": str(max(left, 0)), "left": str(max(left, 0)),
    }
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in keys) + r")\}")
    return apply_template(pattern.sub(lambda m: mapping[m.group(1)], tpl))


def custom_emoji_id(message) -> str:
    """The custom emoji id inside a message: text, caption, or emoji sticker.

    Telegram reports custom emoji as ``MessageEntity.custom_emoji_id`` (in a
    forwarded message or caption) and on premium emoji *stickers*.
    """
    for entities in (getattr(message, "entities", None),
                     getattr(message, "caption_entities", None)):
        for entity in entities or []:
            found = clean_icon_id(getattr(entity, "custom_emoji_id", ""))
            if found:
                return found
    sticker = getattr(message, "sticker", None)
    return clean_icon_id(getattr(sticker, "custom_emoji_id", "")) if sticker else ""


async def send_report(bot, chat_id, text, kb, rebuild=None):
    """Send a price post — as a banner with the report as its caption.

    The banner is a photo (``send_photo``) or a GIF (``send_animation``); the
    report becomes its caption either way.  A GIF is sent with its own
    dimensions (📐 Full HD, on by default), so Telegram plays it at full size
    instead of a downscaled preview.  Telegram caps a caption at
    :data:`CAPTION_LIMIT` characters, so a longer report is posted as a normal
    text message and the banner is skipped (logged, never silently mangled).
    A banner that Telegram refuses (deleted file, unreachable URL) also falls
    back to the text post.

    ``rebuild`` is a callable that builds the keyboard again; it is used when
    Telegram rejects the button icons, so the post goes out with plain buttons
    instead of not going out at all.
    """
    banner, kind = post_banner(), banner_kind()
    if banner and len(text) > CAPTION_LIMIT:
        log.info("Report is %s characters — above the %s-character caption limit; "
                 "posting it without the banner", len(text), CAPTION_LIMIT)
        banner = ""

    async def deliver(keyboard):
        if banner:
            hd = banner_send_kwargs(kind)
            try:
                if kind == "animation":
                    return await bot.send_animation(chat_id, animation=banner, caption=text,
                                                    parse_mode="HTML", reply_markup=keyboard, **hd)
                return await bot.send_photo(chat_id, photo=banner, caption=text,
                                            parse_mode="HTML", reply_markup=keyboard)
            except Exception as e:
                log.warning("Could not send the %s banner (%s) — falling back to a text post",
                            kind, e)
        return await bot.send_message(chat_id, text, parse_mode="HTML",
                                      disable_web_page_preview=True, reply_markup=keyboard)

    try:
        return await deliver(kb)
    except Exception as e:
        if not (rebuild and icons_available() and _looks_like_icon_refusal(e)):
            raise
        disable_button_icons(e)
        return await deliver(rebuild())


def empty_state():
    return {"group": None, "group_title": "", "channel": None, "channel_title": "",
            "auto": False, "merchants": {}, "last": {}, "channel_last": {}, "edits": {},
            "settings": deepcopy(DEFAULT_SETTINGS), "link_target_version": LINK_TARGET_VERSION,
            "last_msg_id": None, "last_msg_time": None,
            "channel_last_msg_id": None, "channel_last_msg_time": None,
            # anti-scam: {chat_id:user_id → {word, tries, msg_id, expires, …}}
            "captcha": {},
            # 📤 Undo for messages the admin had reposted to the group/channel
            "forwards": {},
            # ↪️ chats selected as relay sources {chat_id: {chat_id, title, type}}
            "forward_sources": {},
            # ↪️ messages the bot itself posted into a source chat ("chat:msg" → ts)
            "own_messages": {}}


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
    # icons and the post banner are read while building every keyboard
    data["settings"]["button_icons"] = normalize_button_icons(data["settings"].get("button_icons"))
    data["settings"]["post_photo"] = clean_banner(data["settings"].get("post_photo"))
    # a banner saved before GIF support existed is always a photo
    data["settings"]["post_photo_kind"] = clean_banner_kind(data["settings"].get("post_photo_kind"))
    # the size the banner was uploaded at, and whether it should be kept (📐 HD)
    size = clean_banner_size(data["settings"].get("post_photo_size"))
    data["settings"]["post_photo_size"] = size if data["settings"]["post_photo"] else {}
    if not isinstance(data["settings"].get("post_photo_hd"), bool):
        data["settings"]["post_photo_hd"] = DEFAULT_SETTINGS["post_photo_hd"]
    if data["settings"].get("btn_link_mode") not in ("ad", "profile"):
        data["settings"]["btn_link_mode"] = DEFAULT_SETTINGS["btn_link_mode"]
    # ── destinations, auto-forward and the anti-scam check ──
    if data["settings"].get("forward_target") not in FORWARD_TARGETS:
        data["settings"]["forward_target"] = DEFAULT_SETTINGS["forward_target"]
    if not isinstance(data["settings"].get("channel_to_group"), bool):
        data["settings"]["channel_to_group"] = DEFAULT_SETTINGS["channel_to_group"]
    if not isinstance(data["settings"].get("group_to_channel"), bool):
        data["settings"]["group_to_channel"] = DEFAULT_SETTINGS["group_to_channel"]
    data["settings"]["captcha_message"] = clean_captcha_message(data["settings"].get("captcha_message"))
    data["settings"]["captcha_attempts"] = _clean_choice(
        data["settings"].get("captcha_attempts"),
        CAPTCHA_ATTEMPT_CHOICES, DEFAULT_SETTINGS["captcha_attempts"])
    data["settings"]["captcha_timeout"] = _clean_choice(
        data["settings"].get("captcha_timeout"),
        CAPTCHA_TIMEOUT_CHOICES, DEFAULT_SETTINGS["captcha_timeout"])
    if data["settings"].get("captcha_action") not in CAPTCHA_ACTIONS:
        data["settings"]["captcha_action"] = DEFAULT_SETTINGS["captcha_action"]
    if not isinstance(data["settings"].get("captcha_enabled"), bool):
        data["settings"]["captcha_enabled"] = DEFAULT_SETTINGS["captcha_enabled"]
    if "last" not in data:
        data["last"] = {}
    if "channel_last" not in data or not isinstance(data.get("channel_last"), dict):
        data["channel_last"] = {}
    if "merchants" not in data:
        data["merchants"] = {}
    if "auto" not in data:
        data["auto"] = False
    if "group" not in data:
        data["group"] = None
    if "group_title" not in data:
        data["group_title"] = ""
    if "channel" not in data:
        data["channel"] = None
    if "channel_title" not in data:
        data["channel_title"] = ""
    if "last_msg_id" not in data:
        data["last_msg_id"] = None
    if "last_msg_time" not in data:
        data["last_msg_time"] = None
    if "channel_last_msg_id" not in data:
        data["channel_last_msg_id"] = None
    if "channel_last_msg_time" not in data:
        data["channel_last_msg_time"] = None
    if not isinstance(data.get("edits"), dict):
        data["edits"] = {}
    # Drop anything malformed: a stale record would lock a member out forever.
    clean = {}
    for key, record in (data.get("captcha") or {}).items() if isinstance(data.get("captcha"), dict) else ():
        if (isinstance(record, dict) and isinstance(key, str)
                and re.fullmatch(r"-?\d+:\d+", key) and record.get("word")):
            clean[key] = record
    data["captcha"] = clean
    if not isinstance(data.get("forwards"), dict):
        data["forwards"] = {}
    # ↪️ relay sources: keep the valid records, drop anything malformed
    data["forward_sources"] = normalize_forward_sources(data.get("forward_sources"))
    own = data.get("own_messages")
    data["own_messages"] = {k: v for k, v in own.items()
                            if isinstance(k, str) and re.fullmatch(r"-?\d+:\d+", k)} \
        if isinstance(own, dict) else {}
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


def db_is_persistent() -> bool:
    """Whether the group, merchants and prices live in a shared database."""
    return database_connected(STORE)


# A database is *configured* the moment a URL + token exist, which is not the
# same as it answering.  ``RedisStore.load()`` reports a dead store exactly like
# an empty one, so the bot used to show "connected ✅" while it had already
# forgotten everything.  The answer is cached: the panel calls it several times.
_DB_PROBE_TTL = 60.0
_db_probe: dict = {"at": 0.0, "ok": True, "detail": ""}


def db_health(force: bool = False) -> tuple[bool, str]:
    """``(answers, detail)`` for the store in use — never raises."""
    now = time.monotonic()
    if not force and now - _db_probe["at"] < _DB_PROBE_TTL:
        return bool(_db_probe["ok"]), str(_db_probe["detail"])
    ok, detail = probe_store(STORE)
    _db_probe.update(at=now, ok=ok, detail=detail)
    return bool(ok), str(detail)


def rebuild_store() -> str:
    """Re-select the state backend, carrying the in-memory state over.

    Credentials can appear after the process started — the setup page writes
    them into the shared store and ``python setup_cli.py`` into the environment.
    Whatever this process already knows is written into the newly selected store
    first, so switching databases never loses the group or the merchants.  Like
    every write in ``storage.py`` this is best-effort and never raises.
    """
    global STORE
    previous = STORE
    STORE = build_store(BASE_DIR)
    if previous.describe() != STORE.describe():
        STORE.save(dict(state))
        log.info("State store switched from %s to %s", previous.describe(), STORE.describe())
    return STORE.describe()


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

def set_channel_button():
    """Deep link that adds the bot to a channel with the rights it needs.

    A bot cannot be invited to a channel by hand from the chat itself — the
    ``startchannel`` link asks Telegram to add it *and* to grant the
    post/delete permissions the price post needs.
    """
    label = "📢 Change channel" if state["channel"] else "📢 Set channel"
    if BOT_USERNAME:
        return B(label, url=f"https://t.me/{BOT_USERNAME}?startchannel=setchannel"
                            "&admin=post_messages+edit_messages+delete_messages")
    return B(label, callback_data="setchannel_help")

def add_source_channel_button():
    """Deep link that adds the bot to a channel and makes it a ↪️ relay source.

    A bot only receives channel posts when it is an **admin** of the channel, so
    the link asks Telegram for the posting right as well.
    """
    if BOT_USERNAME:
        return B("📢 Add a channel", url=f"https://t.me/{BOT_USERNAME}?startchannel=forwardfrom"
                                         "&admin=post_messages")
    return B("📢 Add a channel", callback_data="fwd_src_help")

def add_source_group_button():
    """Deep link that adds the bot to a group and makes it a ↪️ relay source."""
    if BOT_USERNAME:
        return B("👥 Add a group", url=f"https://t.me/{BOT_USERNAME}?startgroup=forwardfrom")
    return B("👥 Add a group", callback_data="fwd_src_help")

def forward_sources_text() -> str:
    """The ↪️ Forward-from menu: what is relayed, and how to change it."""
    records = forward_source_records()
    group = group_label()
    lines = "\n".join(
        f"{index}. <code>{html_escape(forward_source_label(record))}</code>"
        for index, record in enumerate(records, 1))
    if not records:
        lines = ("<i>Nothing selected — new posts in the configured "
                 f"{html_escape(channel_label() or 'channel (not set)')}</i> are relayed,\n"
                 "<i>which is what the bot did before this option existed.</i>")
    return (
        f"↪️ <b>Forward from</b>\n\n"
        f"Into the group: <code>{html_escape(group or 'not set')}</code>\n"
        f"Forwarding: <b>{'ON ✅' if channel_to_group_enabled() else 'OFF ❌'}</b>\n\n"
        f"{lines}\n\n"
        f"Add the bot to any <b>channel</b> or <b>group</b> and tap a button below, or send "
        f"<code>/forwardfrom</code> inside that chat (<code>/stopforward</code> removes it again).\n"
        f"• In a <b>channel</b> the bot must be an <b>admin</b> — Telegram only sends channel "
        f"posts to admins.\n"
        f"• In a <b>group</b> the bot must be an <b>admin</b> too, or @BotFather → "
        f"<i>/setprivacy</i> → <b>Disable</b>, otherwise it cannot read the messages.\n"
        f"• Tap a chat below to stop forwarding from it "
        f"({len(records)}/{MAX_FORWARD_SOURCES} selected).\n"
        f"• Every relayed message carries the source chat's name at the top.\n"
        f"• The price post itself is never echoed back, and the destination group cannot be a "
        f"source."
    )

def forward_sources_kb():
    """One row per selected chat (tap = remove), then the ways to add one."""
    rows = [[B(forward_source_label(record), callback_data=f"fwd_src_del:{record['chat_id']}")]
            for record in forward_source_records()]
    rows.append([add_source_channel_button(), add_source_group_button()])
    if state.get("channel") and not source_selected(state["channel"]):
        rows.append([B("📢 Use the price channel", callback_data="fwd_src_use_channel")])
    rows.append([B(f"↪️ Forward to group: {'ON ✅' if channel_to_group_enabled() else 'OFF ❌'}",
                   callback_data="fwd_src_toggle")])
    rows.append([B("⬅️ Back", callback_data="settings")])
    return KB(rows)

def database_button():
    """The link to the page where a database is connected (Vercel → Storage)."""
    return B("🔌 Connect database ↗", url=database_link())

def panel():
    a = state["auto"]
    s = get_settings()
    liq_icon = "💧"
    rows = [
        [B("📊 Post prices now", callback_data="post"),
         B(f"{'🟢' if a else '🔴'} Auto: {'ON' if a else 'OFF'}", callback_data="auto")],
        [B("📋 Merchants", callback_data="list"), set_group_button()],
        [set_channel_button(), B("🛡 Anti-scam", callback_data="antiscam")],
        [B("⚙️ Settings", callback_data="settings"), B("📝 Custom Msg", callback_data="custom_menu")],
        [B(f"{liq_icon} Liquidity: {'ON' if s.get('show_liquidity') else 'OFF'}", callback_data="toggle_liquidity"),
         B(f"🔘 Buttons: {'ON' if s.get('show_buttons') else 'OFF'}", callback_data="toggle_buttons")],
        [B("🔘 Manage buttons", callback_data="buttons_menu")],
    ]
    # No shared database means this install forgets its group, merchants and
    # prices; put the way to fix that in front of the admin, not in a manual.
    if not db_is_persistent():
        rows.append([database_button(), B("❓ Why", callback_data="database")])
    rows.append([B("👁 Preview", callback_data="preview"), B("🔄 Refresh", callback_data="panel")])
    return KB(rows)

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
        [B(f"🗄 Database: {'connected ✅' if db_is_persistent() else 'connect ⚠️'}", callback_data="database")],
        [B("🖼 Button icons", callback_data="button_icons"),
         B("🖼 Post banner", callback_data="banner_menu")],
        [set_channel_button(), B("🛡 Anti-scam", callback_data="antiscam")],
        [B(f"📤 Auto-forward: {forward_label()}", callback_data="toggle_forward_target")],
        [B(f"↪️ Group → channel: {'ON ✅' if group_to_channel_enabled() else 'OFF ❌'}",
           callback_data="toggle_group_to_channel")],
        [B(f"↪️ Channel → group: {'ON ✅' if channel_to_group_enabled() else 'OFF ❌'}",
           callback_data="toggle_channel_to_group"),
         B(f"↪️ Forward from: {forward_source_summary()}", callback_data="forward_sources")],
        [B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="panel")]
    ])

def database_kb():
    return KB([
        [database_button()],
        [B("🔄 Check connection", callback_data="db_check")],
        [B("⬅️ Back", callback_data="panel")],
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
        [B("🖼 Button icons", callback_data="button_icons"),
         B("🖼 Post banner", callback_data="banner_menu")],
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

def on_vercel() -> bool:
    """Whether this process runs as a Vercel function (same flags as serverless.py)."""
    return bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or os.getenv("VERCEL_URL")
                or os.getenv("VERCEL_PROJECT_PRODUCTION_URL"))


# ── 🛡 Anti-scam verification ───────────────────────────────────────────────
# New members are muted the moment they join and have to type a random word.
# Two details make this work with Telegram's permission model:
#   • can_send_messages=False mutes *everything*, so a pending member keeps text
#     enabled and is blocked from links, media and stickers instead — the bot
#     deletes every wrong word straight away.
#   • a failed/expired challenge is decided by the admin (mute / kick / ban),
#     never automatically, so a real user who mistyped is not thrown out.
def antiscam_text():
    pending = [r for r in pending_captchas().values() if not r.get("locked")]
    locked = [r for r in pending_captchas().values() if r.get("locked")]
    custom = bool(clean_captcha_message(get_settings().get("captcha_message")))
    message = captcha_message()
    shown = message if len(message) <= 300 else message[:299] + "…"
    return (
        f"🛡 <b>Anti-scam verification</b>\n\n"
        f"When somebody joins the group, the bot mutes them and asks for a random word. "
        f"Only after they type it can they post links, photos or stickers.\n\n"
        f"Verification: <b>{'ON ✅' if captcha_enabled() else 'OFF ❌'}</b>\n"
        f"Wrong words allowed: <b>{captcha_attempts()}</b> · time limit: "
        f"<b>{captcha_timeout()} min</b>\n"
        f"On failure: <b>{CAPTCHA_ACTION_TITLES[captcha_action()]}</b>\n"
        f"Waiting now: <b>{len(pending)}</b> · waiting for you: <b>{len(locked)}</b>\n\n"
        f"<b>Challenge message</b> ({'custom' if custom else 'default'}):\n"
        f"<code>{html_escape(shown)}</code>\n\n"
        f"Placeholders: <code>{{WORD}}</code> <code>{{MENTION}}</code> <code>{{NAME}}</code> "
        f"<code>{{GROUP}}</code> <code>{{MINUTES}}</code> <code>{{LEFT}}</code> "
        f"<code>{{ASSET}}</code> <code>{{FIAT}}</code>\n"
        f"<code>{{WORD}}</code> is required — without it nobody could ever pass.\n\n"
        f"ℹ️ The bot needs to be a group admin with <b>Restrict members</b> and "
        f"<b>Delete messages</b>; without them it simply skips the check."
    )

def antiscam_kb():
    return KB([
        [B(f"🛡 Verification: {'ON ✅' if captcha_enabled() else 'OFF ❌'}",
           callback_data="captcha_toggle")],
        [B("📝 Edit challenge message", callback_data="captcha_edit"),
         B("♻️ Reset message", callback_data="captcha_reset")],
        [B(f"🔢 Attempts: {captcha_attempts()}", callback_data="captcha_attempts"),
         B(f"⏰ Timeout: {captcha_timeout()} min", callback_data="captcha_timeout")],
        [B(f"🚫 On failure: {CAPTCHA_ACTION_SHORT[captcha_action()]}",
           callback_data="captcha_action")],
        [B("👁 Preview challenge", callback_data="captcha_preview")],
        [B("⬅️ Back", callback_data="panel")],
    ])

# ── 🖼 Button icons & post banner (admin screens) ───────────────────────────
# The labels of these two screens themselves.  They are listed here instead of
# reading their keyboards: those screens are built from this list, so building
# them here would recurse.
ICON_SCREEN_LABELS = ("🖼 Button icons", "🖼 Post banner", "👁 Send a test",
                      "📤 Send a photo", "🔗 Use an image URL", "🗑 Remove banner",
                      "📐 Post in full HD: ON ✅",
                      "🖼 Set / replace icon", "🎨 Colour: green 🟢", "🗑 Remove icon",
                      "🛡 Anti-scam", "📢 Set channel", "📢 Change channel",
                      "📝 Edit challenge message", "♻️ Reset message",
                      "👁 Preview challenge", "✅ Approve", "🚫 Kick")


def button_labels() -> list[str]:
    """Every button label the bot can show, in the order the menus show them.

    The icons screen is built from this, so it lists exactly the emoji this bot
    actually uses — panel, menus and group post — and never a hard-coded list.
    """
    labels = [buy_label_tpl(), sell_label_tpl()]
    labels += [button["label"] for button in extra_buttons()]
    for builder in (panel, settings_kb, buttons_menu_kb, extra_buttons_kb, adlink_menu_kb,
                    custom_menu_kb, database_kb, list_kb, antiscam_kb):
        try:
            keyboard = builder()
        except Exception as e:                        # pragma: no cover - defensive
            log.debug("Could not inspect %s: %s", getattr(builder, "__name__", builder), e)
            continue
        labels += [button.text for row in keyboard.inline_keyboard for button in row]
    return labels + list(ICON_SCREEN_LABELS)


def emoji_in_use() -> list[str]:
    """The distinct leading emoji of every button the bot shows."""
    keys: list[str] = []
    for label in button_labels():
        key = icon_key(label)
        if key and key not in keys:
            keys.append(key)
    return keys[:60]


def labels_for_emoji(key: str) -> list[str]:
    """The buttons that would show the icon saved under ``key``."""
    seen, found = set(), []
    for label in button_labels():
        if icon_key(label) == key and label not in seen:
            seen.add(label)
            found.append(label)
    return found


def button_icons_text():
    configured = button_icons()
    keys = emoji_in_use()
    with_icon = [key for key in keys if configured.get(key, {}).get("icon")]
    lines = [
        "🖼 <b>Button icons</b>",
        "",
        "Telegram can show a custom emoji — a premium, often animated emoji image — before a "
        "button's label, and colour the button. The icon is chosen by the emoji the label "
        "starts with, so one entry covers every button that uses it.",
        "",
        f"Emoji in use: <b>{len(keys)}</b> · with an icon: <b>{len(with_icon)}</b>",
        "",
        "Tap an emoji below to set, replace or remove its icon (forward the emoji, send it as "
        "a sticker, or paste the numeric id).",
        "",
        "⚠️ Telegram only shows button icons for bots that bought a username on Fragment, or "
        "when the bot owner has Premium — other clients keep the plain emoji.",
    ]
    return "\n".join(lines)


def button_icons_kb():
    configured = button_icons()
    rows, row = [], []
    for key in emoji_in_use():
        if len(f"icon_menu:{key}".encode()) > 64:      # Telegram's callback_data limit
            continue
        mark = " ✅" if configured.get(key, {}).get("icon") else ""
        row.append(B(f"{key}{mark}", callback_data=f"icon_menu:{key}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("🖼 Post banner", callback_data="banner_menu"),
                 B("👁 Preview", callback_data="preview")])
    rows.append([B("⬅️ Back", callback_data="settings")])
    return KB(rows)


def icon_editor_text(key: str):
    entry = button_icons().get(key, {})
    labels = labels_for_emoji(key)
    lines = [f"🖼 <b>Icon for {key} buttons</b>", ""]
    if labels:
        shown = ", ".join(f"<code>{html_escape(label)}</code>" for label in labels[:6])
        more = "" if len(labels) <= 6 else f" (+{len(labels) - 6} more)"
        lines += [f"Used by <b>{len(labels)}</b> button(s): {shown}{more}", ""]
    if entry.get("icon"):
        lines += [f"Current icon: <code>{html_escape(entry['icon'])}</code> · "
                  f"colour: <b>{STYLE_TITLES.get(entry.get('style'), 'default')}</b>", ""]
    elif entry.get("style"):
        lines += [f"Colour: <b>{STYLE_TITLES.get(entry['style'])}</b> — no icon yet.", ""]
    else:
        lines += ["Nothing set — these buttons show the plain emoji.", ""]
    lines += [
        "<b>Two ways to set the icon</b>",
        "• Forward (or send) a message that contains the custom emoji — the bot reads its id, or",
        "• paste the numeric id (custom emoji ids are numbers only).",
        "",
        "ℹ️ A normal emoji (😀) has no id; only Telegram <i>custom</i> emoji do.",
    ]
    return "\n".join(lines)


def icon_editor_kb(key: str):
    entry = button_icons().get(key, {})
    rows = [[B("🖼 Set / replace icon", callback_data=f"icon_set:{key}")]]
    rows.append([B(f"🎨 Colour: {STYLE_TITLES.get(entry.get('style'), 'default')}",
                   callback_data=f"icon_style:{key}")])
    if entry:
        rows.append([B("🗑 Remove icon", callback_data=f"icon_clear:{key}")])
    rows.append([B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="button_icons")])
    return KB(rows)


def icon_prompt(key: str):
    return (f"🖼 <b>Send the icon for {key} buttons</b>\n\n"
            "• Forward a message that contains the custom emoji, or send it as a sticker; the "
            "bot stores the emoji's id.\n"
            "• Or paste the id itself (numbers only, e.g. <code>5368324170671202286</code>).\n\n"
            "Send /cancel to stop without changing anything.")


def banner_hd_note(size: dict = None, kind: str = "") -> str:
    """The one line that explains what 📐 Full HD does to *this* banner."""
    size = banner_size() if size is None else clean_banner_size(size)
    if clean_banner_kind(kind or banner_kind()) == "photo":
        return "a photo is always the largest copy of your upload — 📐 is about a GIF"
    if banner_hd():
        return (f"the post passes the GIF's own {banner_size_label(size)} to Telegram, so it "
                "plays at full size instead of a preview" if size else
                "the GIF is posted as the file it is and Telegram reads its size — nothing to "
                "downscale")
    return ("the GIF's own size is held back, so Telegram may render a smaller preview of it"
            if size else "no size is passed, so Telegram may pick a smaller preview itself")


def banner_text():
    banner, kind, size = post_banner(), banner_kind(), banner_size()
    what = banner_kind_label(kind)
    measured = f" at {banner_size_label(size)}" if size else ""
    if banner.startswith(("http://", "https://")):
        shown = banner if len(banner) <= 80 else banner[:79] + "…"
        current = f"a {what} URL (<code>{html_escape(shown)}</code>)"
    elif banner:
        current = f"a Telegram {what}{measured} (<code>…{html_escape(banner[-10:])}</code>)"
    else:
        current = "<b>none</b> — the post is sent as text"
    return (
        "🖼 <b>Post banner</b>\n\n"
        f"Current: {current}\n\n"
        f"With a banner set, the price post is sent as a {what} with the report as its caption "
        "and the buttons underneath — your logo above the prices.\n\n"
        "Send a photo or a GIF in this chat, or set an https:// image/GIF URL.\n\n"
        f"📐 <b>Full HD: {banner_hd_label()}</b> — {banner_hd_note(size, kind)}\n\n"
        f"⚠️ Telegram caps a caption at {CAPTION_LIMIT} characters, so a longer report is posted "
        "as a plain text message instead (the banner is skipped)."
    )


def banner_kb():
    rows = [[B("📤 Send a photo or GIF", callback_data="banner_send")],
            [B("🔗 Use an image URL", callback_data="banner_url"),
             B("🎞 Use a GIF URL", callback_data="banner_gif_url")],
            [B(f"📐 Post in full HD: {banner_hd_label()}", callback_data="banner_hd")]]
    if post_banner():
        rows.append([B("👁 Send a test", callback_data="banner_test"),
                     B("🗑 Remove banner", callback_data="banner_clear")])
    rows.append([B("🖼 Button icons", callback_data="button_icons")])
    rows.append([B("👁 Preview", callback_data="preview"), B("⬅️ Back", callback_data="settings")])
    return KB(rows)


def database_text():
    """Where the state lives, and the link that makes it survive a redeploy."""
    persistent = db_is_persistent()
    if persistent:
        state_block = (
            "✅ The group, the merchants, the settings and the prices are kept there, "
            "so they survive restarts, redeploys and extra instances.\n\n"
            "Change the database URL/token on the setup page or with "
            "<code>python setup_cli.py</code>, then tap 🔄 Check connection."
        )
    elif on_vercel():
        state_block = (
            "⚠️ No shared database is connected: the state is kept on this host only. "
            "A redeploy, a cold instance or a second instance starts empty.\n\n"
            "<b>Connect one</b>\n"
            "1. Open the Vercel dashboard → <b>Storage</b> (link below).\n"
            "2. Add <b>Upstash for Redis</b> (or <b>Vercel KV</b>) → "
            "<b>Connect to this project</b>. That writes "
            "<code>KV_REST_API_URL</code> + <code>KV_REST_API_TOKEN</code> for you.\n"
            "3. <b>Redeploy</b> — environment variables only apply to new deployments — "
            "then tap 🔄 Check connection.\n\n"
            "No redeploy needed instead: paste the REST URL + token into the browser "
            "setup page (<code>/api/setup</code>) or answer <code>python setup_cli.py</code>, "
            "then tap 🔄 Check connection."
        )
    else:
        state_block = (
            "⚠️ No shared database is connected: the state is kept in a local file on this "
            "host, so a second instance (or a reinstalled machine) starts empty.\n\n"
            "<b>Connect one</b>\n"
            "1. Create a Redis-compatible REST database — a free Upstash one is enough "
            "(link below).\n"
            "2. Put its REST URL and token in <code>KV_REST_API_URL</code> + "
            "<code>KV_REST_API_TOKEN</code> (.env / the service environment), or run "
            "<code>python setup_cli.py</code> — it can store the pair for you.\n"
            "3. Restart the bot if you edited .env, then tap 🔄 Check connection."
        )
    # "configured" is not "answering": load() reports a database that went away
    # exactly like an empty one, so it is probed here instead.
    answers, detail = db_health(force=True) if persistent else (True, "")
    if persistent and not answers:
        label = "NOT answering ⚠️"
        outage = (f"\n\n🚨 <b>Configured, but not answering</b> — {html_escape(detail)}\n"
                  f"Until it responds the bot starts from an empty state every time and "
                  f"forgets the group, the merchants and the prices. Fix the URL and token "
                  f"on <code>/api/setup</code> or with <code>python setup_cli.py</code>, "
                  f"then tap 🔄 Check connection.")
    else:
        label = "connected ✅" if persistent else "NOT connected ⚠️"
        outage = ""
    return (
        f"🗄 <b>Database — where the bot keeps its state</b>\n\n"
        f"Current store: <code>{html_escape(STORE.describe())}</code>\n"
        f"Shared database: <b>{label}</b>\n\n"
        f"{state_block}{outage}\n\n"
        f"🔗 {html_escape(database_link())}"
    )

def group_label():
    if not state["group"]: return None
    t = state.get("group_title")
    return f"{t} ({state['group']})" if t else str(state["group"])

def channel_label():
    if not state["channel"]: return None
    t = state.get("channel_title")
    return f"{t} ({state['channel']})" if t else str(state["channel"])

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
    pending = len([r for r in pending_captchas().values() if not r.get("locked")])
    locked = len([r for r in pending_captchas().values() if r.get("locked")])
    return (
        f"🤖 <b>P2P Price Bot</b>\n"
        f"Group: <code>{g}</code>\n"
        f"Channel: <code>{channel_label() or 'not set — tap 📢 Set channel'}</code>\n"
        f"Merchants: {len(state['merchants'])} · Pair: {ASSET}/{FIAT} · every {INTERVAL}s\n"
        f"🗄 Database: <b>{'connected ✅' if db_is_persistent() else 'NOT connected ⚠️'}</b>"
        f"{'' if db_is_persistent() else ' — tap 🔌 Connect database'}\n"
        f"💧 Liquidity: <b>{liq}</b> · 🔘 Buttons: <b>{btns}</b> · 🗑 AutoDel: <b>{autodel}</b>\n"
        f"🔄 Btn order: <b>{order_label()}</b> · 🎯 Links: <b>{'EXACT AD' if link_mode() == 'ad' else 'PROFILE'}</b>\n"
        f"🚪 Del Join/Left msgs: <b>{joinleft}</b>\n"
        f"📤 Auto-forward: <b>{forward_label()}</b> · ↪️ Channel → group: "
        f"<b>{'ON' if channel_to_group_enabled() else 'OFF'}</b> "
        f"(from <b>{html_escape(forward_source_summary())}</b>) · 🛡 Verification: "
        f"<b>{'ON' if captcha_enabled() else 'OFF'}</b>"
        f"{f' ({pending} waiting' + (f', {locked} for you' if locked else '') + ')' if pending or locked else ''}\n"
        f"📝 Header: <code>{header_short}</code>\n"
        f"📝 Body: <code>{body_short}</code>\n"
        f"📝 Footer: <code>{footer_short}</code>\n"
        f"{last_msg}\n\n"
        f"➕ <b>Paste a merchant's public URL here to add it.</b>\n"
        + ("📤 Anything else you send here is reposted to <b>{0}</b> "
           "(📤 Auto-forward in ⚙️ Settings).\n".format(forward_label())
           if forward_target() != "off" else "")
        + "Use ⚙️ Settings to toggle options and 📝 Custom Msg to customize the full post (header, body, footer)."
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
        f"🖼 Button icons: <b>{len(button_icons())}</b> emoji configured\n"
        f"   Custom emoji images shown before button labels — one entry per emoji,\n"
        f"   so it applies to every button that starts with it.\n\n"
        f"🖼 Post banner: <b>{'set ✅' if post_banner() else 'none ❌'}</b> · "
        f"📐 Full HD: <b>{banner_hd_label()}</b>\n"
        f"   A photo or GIF posted with the prices, which travel in its caption; 📐 keeps a GIF\n"
        f"   at the size it was uploaded at. Tap 🖼 Post banner to change either.\n\n"
        f"🗑 Auto-delete previous message: <b>{autodel}</b>\n"
        f"   When ON, deletes previous price message on refresh/update.\n\n"
        f"⏰ Auto-delete after: <b>{del_hours}h</b>\n"
        f"   Message will be deleted after {del_hours} hours (0 = never).\n\n"
        f"🚪 Delete Join/Left messages: <b>{joinleft}</b>\n"
        f"   When ON, the bot deletes Telegram's \"user joined the group\" and\n"
        f"   \"user left the group\" service messages in your group.\n"
        f"   ⚠️ Bot must be a group admin with 'Delete messages' permission.\n\n"
        f"📢 Channel: <b>{html_escape(channel_label() or 'not set')}</b>\n"
        f"   The price post also goes to a channel when one is set — the group and\n"
        f"   the channel each keep their own last message and auto-delete.\n\n"
        f"📤 Auto-forward: <b>{forward_label()}</b>\n"
        f"   Anything you send here (text, photo, video, sticker, file…) is reposted\n"
        f"   to that chat, with a 🗑 Undo button. Tap it to cycle:\n"
        f"   GROUP → CHANNEL → GROUP + CHANNEL → OFF.\n\n"
        f"↪️ Channel → group: <b>{'ON ✅' if channel_to_group_enabled() else 'OFF ❌'}</b>\n"
        f"   Forwarding from: <b>{html_escape(forward_source_summary())}</b>\n"
        f"   New posts in the selected channel(s)/group(s) are forwarded into the group,\n"
        f"   each topped with the source chat's name (📢 <b>Channel</b>).\n"
        f"   With nothing selected, the configured channel is relayed.\n"
        f"   The bot must be an admin in the source chat and able to send messages in the\n"
        f"   group. Tap ↪️ Forward from to pick the chats.\n\n"
        f"🛡 Anti-scam verification: <b>{'ON ✅' if captcha_enabled() else 'OFF ❌'}</b>\n"
        f"   New members must type a random word before they can post links or media\n"
        f"   ({captcha_attempts()} attempts, {captcha_timeout()} min, then "
        f"{CAPTCHA_ACTION_TITLES[captcha_action()]}).\n"
        f"   Tap 🛡 Anti-scam to change it and to write your own challenge message.\n\n"
        f"🗄 Database (state store): <b>{'connected ✅' if db_is_persistent() else 'NOT connected ⚠️'}</b>\n"
        f"   <code>{html_escape(STORE.describe())}</code>\n"
        f"   Tap 🗄 Database for the connection link and what to do with it.\n\n"
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
        f"🖼 Button icons: <b>{len(button_icons())}</b> emoji · "
        f"🖼 Post banner: <b>{'set ✅' if post_banner() else 'none'}</b>\n"
        f"Use Remove/Restore for BUY or SELL, ➕ Add button for your own link, or\n"
        f"🖼 Button icons to put a custom emoji image in front of any label.\n"
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

def _set_channel(chat):
    state["channel"] = chat.id
    state["channel_title"] = chat.title or ""
    state["channel_last"] = {}  # only the channel has to be posted to again
    save()

def _set_destination(kind: str, chat) -> bool:
    """Register a group or a channel; False when it was already the current one."""
    if chat_of(kind) == chat.id:
        return False
    (_set_group if kind == "group" else _set_channel)(chat)
    return True

def trusted(u) -> bool:
    """Only bot admins may drive the bot — but a channel post has no sender.

    Telegram (and PTB) report no ``from_user`` for channel posts, so a command
    typed inside a channel cannot be attributed to anybody.  Only channel admins
    can post there in the first place, which is the same level of trust.
    """
    user = u.effective_user
    return True if user is None else user.id in ADMINS

async def notify_admins(bot, text, reply_markup=None):
    for a in ADMINS:
        try:
            await bot.send_message(a, text, parse_mode="HTML",
                                   reply_markup=panel() if reply_markup is None else reply_markup)
        except Exception: pass

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat = u.effective_chat
    if chat.type in ("group", "supergroup", "channel"):
        wanted = {"group": "setgroup", "supergroup": "setgroup", "channel": "setchannel"}[chat.type]
        action = c.args[0] if c.args else None
        if action not in SETUP_ACTIONS:
            return
        if not trusted(u): return
        # …?startgroup=forwardfrom / …?startchannel=forwardfrom — the deep links
        # behind the ↪️ Forward-from menu: add the bot here *and* relay from here.
        if action == "forwardfrom":
            await register_forward_source(u, c, chat)
            return
        if action != wanted:
            return
        kind = "channel" if chat.type == "channel" else "group"
        _set_destination(kind, chat)
        # a channel post has no message to reply to — sending is enough there
        if u.effective_message:
            await u.effective_message.reply_text(
                f"✅ <b>{chat.title}</b> will receive price updates.", parse_mode="HTML")
        await notify_admins(c.bot, f"✅ {'Channel' if kind == 'channel' else 'Group'} set to "
                                   f"<b>{html_escape(chat.title or '')}</b>")
        return
    if is_admin(u): await u.message.reply_html(panel_text(), reply_markup=panel())
    else: await u.message.reply_text("⛔ You are not authorized. Ask the bot admin to add your ID.")

async def setgroup(u: Update, c):
    if not is_admin(u): return
    if u.effective_chat.type == "private":
        return await u.message.reply_html("Use the 👥 <b>Set group</b> button, or send /setgroup inside your group.",
                                          reply_markup=panel())
    if u.effective_chat.type == "channel":
        return await u.message.reply_html("This is a channel — use /setchannel (or the 📢 Set channel button).",
                                          reply_markup=panel())
    _set_group(u.effective_chat)
    await u.message.reply_text("✅ This group will receive price updates.")

async def setchannel(u: Update, c):
    """/setchannel — post the prices into this channel as well."""
    chat = u.effective_chat
    if chat.type == "private":
        if not is_admin(u): return
        return await u.message.reply_html(
            "Use the 📢 <b>Set channel</b> button, or send /setchannel inside your channel.",
            reply_markup=panel())
    if chat.type != "channel":
        return await u.message.reply_text("This chat is not a channel — use /setgroup here.")
    if not trusted(u):
        return
    _set_channel(chat)
    try:
        await u.effective_message.reply_text("✅ This channel will receive price updates.")
    except Exception:
        pass
    await notify_admins(c.bot, f"✅ Channel set to <b>{html_escape(chat.title or '')}</b>")

async def answer_in_source(u, c, text: str) -> None:
    """Reply inside a ↪️ source chat — and never relay that reply to the group."""
    chat, msg = u.effective_chat, message_of(u)
    try:
        sent = (await msg.reply_html(text) if getattr(msg, "reply_html", None)
                else await c.bot.send_message(chat.id, text, parse_mode="HTML"))
    except Exception as e:                     # no posting rights, chat gone, …
        log.debug("Could not answer in chat %s: %s", chat.id, e)
        return
    remember_own_message(chat.id, getattr(sent, "message_id", None))

async def register_forward_source(u, c, chat) -> bool:
    """↪️ Select this channel/group as a chat the bot forwards *from*."""
    if chat.type not in FORWARD_SOURCE_TYPES:
        await answer_in_source(u, c, "⚠️ Only a channel or a group can be a forward source.")
        return False
    if not state.get("group"):
        await answer_in_source(u, c, "⚠️ Set the destination group first — 👥 <b>Set group</b> "
                                     "in the bot, or /setgroup inside that group.")
        return False
    if chat.id == state["group"]:
        await answer_in_source(u, c, "⚠️ This is the group messages are forwarded <b>into</b> — "
                                     "choose another channel or group.")
        return False
    if source_selected(chat.id):
        await answer_in_source(u, c, f"ℹ️ Already forwarding from <b>{html_escape(chat.title or '')}</b>.")
        return False
    if not add_forward_source(chat.id, chat.title, chat.type):
        await answer_in_source(u, c, f"⚠️ The limit of {MAX_FORWARD_SOURCES} forward sources is "
                                     "reached — remove one in ⚙️ Settings → ↪️ Forward from.")
        return False
    turned_on = ""
    if not channel_to_group_enabled():        # selecting a source means "use it"
        state["settings"]["channel_to_group"] = True
        save()
        turned_on = "\n↪️ Forwarding was OFF — it is ON now."
    where = html_escape(group_label() or str(state["group"]))
    await answer_in_source(u, c, f"✅ New messages in <b>{html_escape(chat.title or str(chat.id))}</b> "
                                 f"are forwarded to <b>{where}</b>.{turned_on}")
    await notify_admins(c.bot, f"✅ ↪️ Forwarding from <b>{html_escape(chat.title or str(chat.id))}</b> "
                               f"({chat.type}) into <b>{where}</b>.",
                       reply_markup=forward_sources_kb())
    return True

async def forwardfrom(u: Update, c):
    """/forwardfrom — forward this channel's/group's messages into the group."""
    chat = u.effective_chat
    if chat.type == "private":
        if not is_admin(u): return
        return await u.message.reply_html(
            "↪️ <b>Forward from</b>\n\nSend /forwardfrom inside the channel or group whose "
            "messages should reach your group (/stopforward removes it again), or use the "
            "buttons below.", reply_markup=forward_sources_kb())
    if not trusted(u): return
    await register_forward_source(u, c, chat)

async def stopforward(u: Update, c):
    """/stopforward — this chat is no longer forwarded into the group."""
    chat = u.effective_chat
    if chat.type == "private":
        if not is_admin(u): return
        return await u.message.reply_html(
            "↪️ Send /stopforward inside the channel or group that should no longer be "
            "forwarded, or tap it in the list below.", reply_markup=forward_sources_kb())
    if not trusted(u): return
    if not remove_forward_source(chat.id):
        return await answer_in_source(u, c, "ℹ️ This chat was not selected as a forward source.")
    await answer_in_source(u, c, f"🗑 Stopped forwarding <b>{html_escape(chat.title or str(chat.id))}</b> "
                                 "into the group.")
    await notify_admins(c.bot, f"🗑 ↪️ No longer forwarding from "
                               f"<b>{html_escape(chat.title or str(chat.id))}</b>.",
                       reply_markup=forward_sources_kb())

async def on_my_chat_member(u: Update, c):
    m = u.my_chat_member
    chat = m.chat
    if chat.type not in ("group", "supergroup", "channel"): return
    kind = "channel" if chat.type == "channel" else "group"
    was, now = m.old_chat_member.status, m.new_chat_member.status
    joined = was in ("left", "kicked") and now in ("member", "administrator")
    if joined and m.from_user and m.from_user.id in ADMINS and _set_destination(kind, chat):
        try:
            sent = await c.bot.send_message(chat.id, f"✅ <b>{chat.title}</b> will receive price updates.", parse_mode="HTML")
            # the chat may be a ↪️ source as well — never relay the bot's own hello
            remember_own_message(chat.id, getattr(sent, "message_id", None))
        except Exception: pass
        await notify_admins(c.bot, f"✅ {'Channel' if kind == 'channel' else 'Group'} set to "
                                   f"<b>{html_escape(chat.title or '')}</b>")
    elif now in ("left", "kicked"):
        was_source = source_selected(chat.id)
        if was_source:
            remove_forward_source(chat.id)     # the bot cannot read that chat any more
        if chat_of(kind) == chat.id:
            state[CHAT_ID[kind]] = None
            state[CHAT_TITLE[kind]] = ""
            state[LAST_ID[kind]] = None
            state[LAST_TIME[kind]] = None
            state[SNAPSHOT[kind]] = {}
            forget_captchas(chat.id)
            save()
            await notify_admins(c.bot, f"⚠️ Bot was removed from <b>{html_escape(chat.title or '')}</b> "
                                       f"— {kind} unset.")
        elif was_source:
            await notify_admins(c.bot, f"⚠️ Bot was removed from <b>{html_escape(chat.title or '')}</b> "
                                       f"— no longer forwarded into the group.",
                                reply_markup=forward_sources_kb())


# ── 🛡 anti-scam verification ───────────────────────────────────────────────
def forget_captchas(chat_id=None, user_id=None) -> int:
    """Drop pending/failed records — of one chat, one member, or all of them."""
    records = pending_captchas()
    if chat_id is None and user_id is None:
        removed = len(records)
        state["captcha"] = {}
        return removed
    prefix = f"{int(chat_id)}:" if chat_id is not None else ""
    keys = [key for key in records
            if key.startswith(prefix) and (user_id is None or key == captcha_key(chat_id, user_id))]
    for key in keys:
        records.pop(key, None)
    return len(keys)

async def _safe(coroutine, *args, **kwargs):
    """One Telegram call that must never take the bot down with it."""
    try:
        return await coroutine(*args, **kwargs)
    except Exception as e:
        log.debug("Anti-scam call failed: %s", e)
        return None

async def captcha_challenge(bot, chat, user) -> bool:
    """Mute a new member and ask them for a word.  True = a challenge is pending."""
    if getattr(user, "is_bot", False) or user.id in ADMINS:
        return False
    word = new_captcha_word()
    try:
        # text-only: everything a scammer posts (links, media, stickers) is off
        await bot.restrict_chat_member(chat.id, user.id, PENDING_PERMISSIONS)
    except Exception as e:
        log.warning("Cannot restrict %s in %s (%s) — make the bot an admin with "
                    "'Restrict members'; skipping the anti-scam check for them", user.id, chat.id, e)
        return False
    name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(user.id)
    # a re-join while a challenge is still open replaces it (and its message)
    previous = pending_captcha(chat.id, user.id)
    if previous:
        await _safe(bot.delete_message, chat.id, previous.get("msg_id"))
    text = render_captcha(captcha_message(), word=word, name=name, user_id=user.id,
                          group=chat.title or "", minutes=captcha_timeout(),
                          left=captcha_attempts())
    record = {"word": word, "tries": 0, "name": name, "chat_title": chat.title or "",
              "expires": int(time.time()) + captcha_timeout() * 60, "msg_id": None}
    try:
        sent = await bot.send_message(chat.id, text, parse_mode="HTML",
                                      disable_web_page_preview=True)
        record["msg_id"] = sent.message_id
    except Exception as e:
        log.warning("Could not send the anti-scam challenge in %s: %s", chat.id, e)
        await _safe(bot.restrict_chat_member, chat.id, user.id, MEMBER_PERMISSIONS)
        return False
    state.setdefault("captcha", {})[captcha_key(chat.id, user.id)] = record
    save()
    log.info("Anti-scam challenge sent to %s (%s) in %s", name, user.id, chat.id)
    return True

async def captcha_pass(bot, chat, user, record, message=None):
    """Correct word: unmute, clean up the challenge, welcome the member."""
    key = captcha_key(chat.id, user.id)
    state.get("captcha", {}).pop(key, None)
    await _safe(bot.restrict_chat_member, chat.id, user.id, MEMBER_PERMISSIONS)
    await _safe(bot.delete_message, chat.id, record.get("msg_id"))
    if message is not None:
        await _safe(message.delete)
    note = await _safe(bot.send_message, chat.id,
                       f"✅ <b>{html_escape(record.get('name') or 'Welcome')}</b> verified — "
                       f"welcome to {html_escape(record.get('chat_title') or 'the group')}!",
                       parse_mode="HTML")
    save()
    log.info("%s (%s) passed the anti-scam check in %s", record.get("name"), user.id, chat.id)
    return note

async def captcha_lock(bot, chat_id, user_id, reason: str):
    """Out of attempts or out of time — apply what the admin configured."""
    record = pending_captcha(chat_id, user_id)
    if not record or record.get("locked"):
        return False
    record["locked"] = True
    record["locked_at"] = int(time.time())
    record["reason"] = reason
    action = captcha_action()
    if action == "kick":
        await _safe(bot.ban_chat_member, chat_id, user_id)
        await _safe(bot.unban_chat_member, chat_id, user_id)
    elif action == "ban":
        await _safe(bot.ban_chat_member, chat_id, user_id)
    else:
        await _safe(bot.restrict_chat_member, chat_id, user_id, MUTED_PERMISSIONS)
    # replace the challenge with a short notice, so the group sees what happens
    await _safe(bot.delete_message, chat_id, record.get("msg_id"))
    record["msg_id"] = None
    await notify_admins(bot, captcha_lock_text(record, chat_id, user_id),
                        reply_markup=captcha_decision_kb(chat_id, user_id))
    save()
    log.info("Anti-scam check failed for %s (%s) in %s: %s → %s",
             record.get("name"), user_id, chat_id, reason, action)
    return True

def captcha_lock_text(record: dict, chat_id, user_id) -> str:
    name = html_escape(record.get("name") or "A new member")
    where = html_escape(record.get("chat_title") or f"chat {chat_id}")
    why = ("ran out of attempts" if record.get("reason") == "attempts"
           else f"did not answer within {captcha_timeout()} minutes")
    action = CAPTCHA_ACTION_TITLES[captcha_action()]
    decision = ("They are <b>muted</b> in the group until you decide."
                if captcha_action() == "restrict" else f"Action taken: <b>{action}</b>.")
    return (f"🛡 <b>Anti-scam: {name}</b> {why} in <b>{where}</b>.\n\n"
            f"{decision}\n"
            f"{'Approve' if captcha_action() == 'restrict' else 'Unban'} them only if you "
            f"are sure they are a real user.")

def captcha_decision_kb(chat_id, user_id):
    approve = "✅ Approve" if captcha_action() == "restrict" else "✅ Unban"
    return KB([[B(approve, callback_data=f"cap_approve:{chat_id}:{user_id}"),
                B("🚫 Kick", callback_data=f"cap_kick:{chat_id}:{user_id}")]])

async def captcha_approve(bot, chat_id, user_id) -> str:
    """Admin says yes: full member rights again."""
    record = pending_captcha(chat_id, user_id) or {}
    if captcha_action() == "ban":
        await _safe(bot.unban_chat_member, chat_id, user_id)
    await _safe(bot.restrict_chat_member, chat_id, user_id, MEMBER_PERMISSIONS)
    await _safe(bot.delete_message, chat_id, record.get("msg_id"))
    forget_captchas(chat_id, user_id)
    save()
    return html_escape(record.get("name") or "The member")

async def captcha_kick(bot, chat_id, user_id) -> str:
    """Admin says no: remove them from the group (they may rejoin)."""
    record = pending_captcha(chat_id, user_id) or {}
    await _safe(bot.delete_message, chat_id, record.get("msg_id"))
    await _safe(bot.ban_chat_member, chat_id, user_id)
    await _safe(bot.unban_chat_member, chat_id, user_id)
    forget_captchas(chat_id, user_id)
    save()
    return html_escape(record.get("name") or "The member")

async def captcha_fail_attempt(bot, chat, user, record, message) -> bool:
    """A wrong word: delete it, count it, lock the member when they run out."""
    record["tries"] = int(record.get("tries") or 0) + 1
    if message is not None:
        await _safe(message.delete)          # a scammer's message never stays up
    left = captcha_attempts() - record["tries"]
    if left <= 0:
        await captcha_lock(bot, chat.id, user.id, "attempts")
        return True
    save()
    # refresh the challenge so the member knows how many tries are left
    try:
        await bot.edit_message_text(
            render_captcha(captcha_message(), word=record["word"],
                           name=record.get("name") or "", user_id=user.id,
                           group=record.get("chat_title") or "",
                           minutes=captcha_timeout(), left=left),
            chat_id=chat.id, message_id=record.get("msg_id"), parse_mode="HTML",
            disable_web_page_preview=True)
    except Exception as e:
        log.debug("Could not refresh the challenge in %s: %s", chat.id, e)
    return False

async def sweep_captcha(bot) -> int:
    """Expire timed-out challenges and forget ancient ones (no JobQueue needed)."""
    now, changed, locked = int(time.time()), False, 0
    for key, record in list(pending_captchas().items()):
        if not isinstance(record, dict):                       # pragma: no cover - load() cleans
            state["captcha"].pop(key, None); changed = True; continue
        age = now - int(record.get("locked_at") or record.get("expires") or now)
        if record.get("locked"):
            if age > CAPTCHA_LOCK_TTL:
                state["captcha"].pop(key, None); changed = True
            continue
        if now < int(record.get("expires") or 0):
            continue
        try:
            chat_id, user_id = (int(part) for part in key.split(":", 1))
        except ValueError:                                     # pragma: no cover - defensive
            state["captcha"].pop(key, None); changed = True; continue
        if await captcha_lock(bot, chat_id, user_id, "timeout"):
            locked += 1
        changed = True
    if changed:
        save()
    return locked

# ── 📤 auto-forward: repost what the admin sends here ───────────────────────
def message_of(u):
    """The message of an update — ``channel_post`` included (via effective_message)."""
    return getattr(u, "effective_message", None) or getattr(u, "message", None)

async def forward_to_targets(u, c) -> bool:
    """Copy an admin's private message into the group / channel.

    Returns True when it was reposted.  Anything the menus did not consume ends
    up here, so a message sent to the bot reaches the group in one tap.
    """
    msg, chat = message_of(u), u.effective_chat
    if not is_admin(u) or not msg or chat.type != "private":
        return False
    targets = forward_targets()
    if not targets:
        return False
    sent, failed = [], []
    for kind, chat_id in targets:
        try:
            copied = await c.bot.copy_message(chat_id=chat_id, from_chat_id=chat.id,
                                              message_id=msg.message_id)
            sent.append({"kind": kind, "chat_id": chat_id, "message_id": copied.message_id})
        except Exception as e:
            log.info("copy_message to %s failed (%s) — trying a real forward", kind, e)
            try:
                forwarded = await c.bot.forward_message(chat_id=chat_id, from_chat_id=chat.id,
                                                        message_id=msg.message_id)
                sent.append({"kind": kind, "chat_id": chat_id, "message_id": forwarded.message_id})
            except Exception as e2:
                log.warning("Could not forward the message to %s %s: %s", kind, chat_id, e2)
                failed.append(f"{kind} ({e2})")
    if not sent:
        await msg.reply_text("⚠️ Nothing was sent — check that the bot can still post in the "
                             f"{' and the '.join(k for k, _ in targets)}.\n" + "\n".join(failed))
        return True      # the message was meant for the group; do not re-use it
    token = secrets.token_hex(4)
    remember_forward(token, sent)
    names = " + ".join(dict.fromkeys(destination_label(item["kind"]) for item in sent))
    extra = "" if not failed else "\n\n⚠️ Not sent to: " + html_escape(", ".join(failed))
    await msg.reply_html(f"📤 Sent to <b>{html_escape(names)}</b>.{extra}",
                         reply_markup=KB([[B("🗑 Undo", callback_data=f"fwd_undo:{token}")]]))
    log.info("Forwarded message %s from admin %s to %s", msg.message_id, u.effective_user.id, names)
    return True

def is_own_post(msg, chat, bot=None) -> bool:
    """True for a message the bot itself posted — never echo it back.

    The scheduled price report already went to the group, so relaying the copy
    the bot posted into a destination chat would duplicate it.
    """
    message_id = getattr(msg, "message_id", None)
    if message_id is None:
        return False
    for chat_key, id_key in (("group", "last_msg_id"), ("channel", "channel_last_msg_id")):
        if chat.id == state.get(chat_key) and state.get(id_key) == message_id:
            return True
    if is_own_message(chat.id, message_id):
        return True
    sender, bot_id = getattr(msg, "from_user", None), getattr(bot, "id", None)
    return bool(sender is not None and bot_id is not None and sender.id == bot_id)

async def relay_to_group(u: Update, c: ContextTypes.DEFAULT_TYPE, destination=None) -> bool:
    """↪️ Forward one message from a selected channel/group into the group.

    Every relayed message is sent *with the channel name*: ``📢 <b>News</b>``
    is written above the text (or media caption), so the group always sees
    where the post came from.  Content Telegram refuses to copy with a new
    caption (stickers, polls, protected posts…) goes out as a real forward
    instead — Telegram's own "Forwarded from" header then shows the name.

    Returns True when the message reached the group.  The source chats are the
    ones the admin picked (⚙️ Settings → ↪️ Forward from); with none picked the
    configured channel is relayed, as before this option existed.
    """
    msg, chat = message_of(u), u.effective_chat
    if (not msg or not chat or chat.type not in FORWARD_SOURCE_TYPES
            or (destination is None and not is_forward_source(chat.id))):
        return False
    if is_own_post(msg, chat, getattr(c, "bot", None)):
        return False
    group = destination if destination is not None else state["group"]
    header = relay_header(chat)
    text = getattr(msg, "text", None)
    try:
        if text is not None:
            body = getattr(msg, "text_html", None) or html_escape(text)
            sent = await c.bot.send_message(chat_id=group, text=f"{header}\n\n{body}",
                                            parse_mode="HTML")
        else:
            caption = getattr(msg, "caption", None) or ""
            body = getattr(msg, "caption_html", None) or html_escape(caption)
            sent = await c.bot.copy_message(chat_id=group, from_chat_id=chat.id,
                                     message_id=msg.message_id,
                                     caption=f"{header}\n\n{body}" if body else header,
                                     parse_mode="HTML")
        remember_own_message(group, sent.message_id)
        log.info("Relayed %s message %s from %s to group %s with its name",
                 chat.type, msg.message_id, chat.id, group)
        return True
    except Exception as copy_error:
        # A real forward is the fallback for content Telegram will not copy
        # with a caption (stickers, polls, protected posts…) — its "Forwarded
        # from" header shows the source name too, so the name is there either
        # way.  Protected channel content is rejected by both API methods.
        try:
            sent = await c.bot.forward_message(chat_id=group, from_chat_id=chat.id,
                                        message_id=msg.message_id)
            remember_own_message(group, sent.message_id)
            log.info("Forwarded %s message %s from %s to group %s",
                     chat.type, msg.message_id, chat.id, group)
            return True
        except Exception as forward_error:
            log.warning("Could not relay %s message %s from %s to group %s: "
                        "copy failed (%s); forward failed (%s)",
                        chat.type, msg.message_id, chat.id, group,
                        copy_error, forward_error)
            return False

async def on_channel_post(u: Update, c: ContextTypes.DEFAULT_TYPE) -> bool:
    """↪️ Relay each new post of a selected channel into the group."""
    return await relay_to_group(u, c)

async def on_source_message(u: Update, c: ContextTypes.DEFAULT_TYPE) -> bool:
    """↪️ Relay each new message of a selected group into the group."""
    return await relay_to_group(u, c)

async def on_group_to_channel(u: Update, c: ContextTypes.DEFAULT_TYPE) -> bool:
    msg, chat = message_of(u), u.effective_chat
    if (not group_to_channel_enabled() or not msg or not chat
            or chat.id != state.get("group") or not isinstance(state.get("channel"), int)
            or state["channel"] == chat.id):
        return False
    user = u.effective_user
    if user and pending_captcha(chat.id, user.id):
        # Never publish verification answers or messages from a locked newcomer.
        await on_group_text(u, c)
        return False
    return await relay_to_group(u, c, destination=state["channel"])

class GroupToChannelFilter(filters.MessageFilter):
    def filter(self, message):
        return (group_to_channel_enabled() and state.get("channel") is not None
                and message.chat_id == state.get("group"))


class ForwardSourceFilter(filters.MessageFilter):
    """Matches a message sent in one of the ↪️ selected relay chats.

    The check has to live in the *filter*: PTB calls only the first handler that
    matches an update, so a handler that grabbed every group message would take
    the registered group's messages away from the 🛡 anti-scam handler.
    """

    def filter(self, message):
        return is_forward_source(getattr(message, "chat_id", None))


async def on_private_media(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Anything else an admin sends privately → 📤 repost it to group/channel."""
    await forward_to_targets(u, c)

async def undo_forward(u, c, token: str) -> bool:
    """🗑 Undo — delete what a 📤 repost created in the group / channel."""
    history = state.get("forwards") or {}
    entry = history.get(token) if isinstance(history, dict) else None
    if not isinstance(entry, dict):
        return False
    for item in entry.get("messages") or []:
        try:
            await c.bot.delete_message(chat_id=item.get("chat_id"), message_id=item.get("message_id"))
        except Exception as e:
            log.debug("Could not delete forwarded message %s: %s", item.get("message_id"), e)
    history.pop(token, None)
    state["forwards"] = history
    save()
    return True

# ── delete "X joined / left the group" service messages + run the check ─────
async def on_join_left(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Auto-delete Telegram's join/leave service messages and verify newcomers."""
    msg, chat = message_of(u), u.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    # only in the registered group (if one is set)
    if state.get("group") and chat.id != state["group"]:
        return
    if get_settings().get("delete_join_left", DEFAULT_SETTINGS["delete_join_left"]):
        try:
            await msg.delete()
            kind = "joined" if msg.new_chat_members else "left"
            member = msg.new_chat_members[0] if msg.new_chat_members else msg.left_chat_member
            log.info("Deleted '%s the group' service message for %s in %s",
                     kind, getattr(member, "full_name", "?"), chat.id)
        except Exception as e:
            log.warning("Could not delete join/left msg in %s: %s "
                        "(make the bot a group admin with 'Delete messages' permission)", chat.id, e)
    left = getattr(msg, "left_chat_member", None)
    if left is not None:
        # they gave up: drop the pending record and its challenge message
        record = pending_captcha(chat.id, left.id)
        if record:
            forget_captchas(chat.id, left.id)
            await _safe(c.bot.delete_message, chat.id, record.get("msg_id"))
            save()
        return
    await sweep_captcha(c.bot)
    if not captcha_enabled():
        return
    for member in getattr(msg, "new_chat_members", None) or []:
        await captcha_challenge(c.bot, chat, member)

async def on_group_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """A message in the group: is it somebody answering their anti-scam check?"""
    msg, chat, user = message_of(u), u.effective_chat, u.effective_user
    if not msg or not chat or chat.type not in ("group", "supergroup") or not user:
        return
    await sweep_captcha(c.bot)
    record = pending_captcha(chat.id, user.id)
    if not record or record.get("locked"):
        return
    if captcha_word_matches((msg.text or msg.caption or ""), record["word"]):
        await captcha_pass(c.bot, chat, user, record, msg)
    else:
        await captcha_fail_attempt(c.bot, chat, user, record, msg)

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

# ── button icons & banner input (the two ways an admin sets an icon) ──
def store_button_icon(key: str, emoji_id: str) -> dict:
    """Save one 🖼 icon entry (state only — the caller persists it)."""
    icons = button_icons()
    entry = dict(icons.get(key) or {"icon": "", "style": ""})
    entry["icon"] = clean_icon_id(emoji_id)
    entry.setdefault("style", "")
    if entry["icon"] or entry["style"]:
        icons[key] = entry
    else:
        icons.pop(key, None)
    state["settings"]["button_icons"] = icons
    return icons


def icon_saved_reply(key: str, emoji_id: str):
    return (f"✅ <b>Icon saved for {key} buttons</b>\n"
            f"<code>{html_escape(emoji_id)}</code>\n\n" + icon_editor_text(key))


def animation_file_id(message) -> str:
    """The file id of a GIF the admin sent — as an animation or as a GIF file.

    Telegram hands a GIF over as ``message.animation`` when it is sent as a GIF
    and as ``message.document`` (mime ``image/gif``) when it is sent as a file;
    both make a perfectly good animated banner.
    """
    animation = getattr(message, "animation", None)
    file_id = getattr(animation, "file_id", "")
    if file_id:
        return file_id
    document = getattr(message, "document", None)
    if document and (getattr(document, "mime_type", "") or "").lower() == "image/gif":
        return getattr(document, "file_id", "") or ""
    return ""


def largest_photo(sizes):
    """The biggest copy Telegram made of an uploaded picture.

    ``message.photo`` is a list of the same picture downscaled, the largest one
    conventionally last.  The order is not guaranteed, so the areas are compared
    and a tie keeps the later entry — storing anything but the largest copy is
    how a banner ends up blurry, since the earlier ones are previews.
    """
    best, best_area = None, -1
    for size in sizes or []:
        area = (getattr(size, "width", 0) or 0) * (getattr(size, "height", 0) or 0)
        if area >= best_area:
            best, best_area = size, area
    return best


def media_size(*media) -> dict:
    """What an uploaded GIF (or one copy of a photo) measures, as Telegram saw it.

    A GIF that arrived as a plain *file* is a document and carries no
    dimensions, so a banner built from one is stored without a size — the post
    then lets Telegram read the file itself rather than guessing.
    """
    for item in media:
        if item is None:
            continue
        size = clean_banner_size({"width": getattr(item, "width", None),
                                  "height": getattr(item, "height", None),
                                  "duration": getattr(item, "duration", None)})
        if size:
            return size
    return {}


async def save_banner_media(u: Update, c: ContextTypes.DEFAULT_TYPE, file_id: str, kind: str,
                            size=None):
    """Store what the admin just sent as the post banner and confirm it."""
    if not file_id: return
    set_banner(file_id, kind, size)
    edit_pop(u, "awaiting_custom")                             # saves
    await u.message.reply_html("✅ Banner saved — the next price post uses it.\n\n" + banner_text(),
                               reply_markup=banner_kb())


async def on_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """An admin sends a photo → the post banner, or 📤 a repost to the group."""
    if not is_admin(u) or u.effective_chat.type != "private": return
    if edit_get(u, "awaiting_custom") not in BANNER_AWAITING:
        await forward_to_targets(u, c)
        return
    photo = largest_photo(getattr(u.message, "photo", None))
    if photo is None: return
    # the largest size Telegram sent, with the size it stands for
    return await save_banner_media(u, c, getattr(photo, "file_id", "") or "", "photo",
                                   media_size(photo))


async def on_animation(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """An admin sends a GIF → the animated post banner, or 📤 a repost."""
    if not is_admin(u) or u.effective_chat.type != "private": return
    if edit_get(u, "awaiting_custom") not in BANNER_AWAITING:
        await forward_to_targets(u, c)
        return
    file_id = animation_file_id(u.message)
    if not file_id:                                            # nothing we could reuse
        await forward_to_targets(u, c)
        return
    # the GIF's own width/height/duration travel with it, so the post shows it full size
    return await save_banner_media(u, c, file_id, "animation",
                                   media_size(getattr(u.message, "animation", None)))


async def on_sticker(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """A premium emoji sticker is exactly what a button icon is — accept one."""
    if not is_admin(u) or u.effective_chat.type != "private": return
    awaiting = edit_get(u, "awaiting_custom")
    if not isinstance(awaiting, str) or not awaiting.startswith("icon:"):
        await forward_to_targets(u, c)   # not an icon → 📤 repost it instead
        return
    key = icon_key(awaiting.split(":", 1)[1])
    emoji_id = custom_emoji_id(u.message)
    if not key or not emoji_id:
        return await u.message.reply_text(
            "❌ That sticker is not a custom emoji. Send a premium emoji sticker, forward a "
            "message with the emoji, or paste its numeric id.")
    store_button_icon(key, emoji_id)
    edit_pop(u, "awaiting_custom")
    await u.message.reply_html(icon_saved_reply(key, emoji_id), reply_markup=icon_editor_kb(key))


# ── add merchant by pasting URL / handle custom msg input ──
async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u) or u.effective_chat.type != "private": return
    txt = u.message.text.strip()

    awaiting = edit_get(u, "awaiting_custom")

    if awaiting in ("banner_url", "banner_gif_url"):
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("❌ Cancelled.", reply_markup=banner_kb())
        if not valid_photo_url(txt):
            return await u.message.reply_text(
                "❌ Send an https:// (or http://) URL of an image (JPG/PNG) or a GIF, or /cancel.")
        # 🎞 GIF URL is an animation by choice; a bare image URL that happens to
        # end in .gif/.mp4 is one too, so a GIF link can never be posted flat.
        kind = "animation" if (awaiting == "banner_gif_url"
                               or banner_kind_from_url(txt) == "animation") else "photo"
        set_banner(txt, kind)
        save()
        edit_pop(u, "awaiting_custom")
        return await u.message.reply_html("✅ Banner saved — the next price post uses it.\n\n"
                                          + banner_text(), reply_markup=banner_kb())

    if isinstance(awaiting, str) and awaiting.startswith("icon:"):
        key = icon_key(awaiting.split(":", 1)[1])
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("❌ Cancelled.", reply_markup=button_icons_kb())
        if not key:
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("That emoji is no longer editable — open "
                                              "🖼 Button icons again.", reply_markup=button_icons_kb())
        emoji_id = custom_emoji_id(u.message) or clean_icon_id(txt)
        if not emoji_id:
            return await u.message.reply_text(
                "❌ No custom emoji found. Forward a message with the premium emoji (or send it "
                "as a sticker), or paste the numeric id — a normal emoji like 😀 has no id.")
        store_button_icon(key, emoji_id)
        edit_pop(u, "awaiting_custom")
        return await u.message.reply_html(icon_saved_reply(key, emoji_id),
                                          reply_markup=icon_editor_kb(key))
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

    if awaiting == "captcha_message":
        if txt.lower() == "/cancel":
            edit_pop(u, "awaiting_custom")
            return await u.message.reply_text("❌ Cancelled.", reply_markup=antiscam_kb())
        value = "" if txt.lower() in ("default", "reset", "-", "none") else txt
        if value and "{WORD}" not in value:
            # without the word nobody could ever pass the check
            return await u.message.reply_html(
                "❌ The challenge must contain <code>{WORD}</code> — that is the random word "
                "the member has to type. Try again, or /cancel.")
        if len(value) > MAX_CAPTCHA_LENGTH:
            return await u.message.reply_text(
                f"❌ Too long (max {MAX_CAPTCHA_LENGTH} characters). Try again, or /cancel.")
        state["settings"]["captcha_message"] = clean_captcha_message(value)
        save()
        edit_pop(u, "awaiting_custom")
        await u.message.reply_html("✅ Challenge message saved.\\n\\n" + antiscam_text(),
                                   reply_markup=antiscam_kb())
        return

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
        # not a merchant URL — 📤 repost it to the group / channel when that is on
        if await forward_to_targets(u, c):
            return
        return await u.message.reply_text(
            "❌ Not a supported merchant URL (Binance / Bybit / OKX / Bitget).   /start\n"
            "📤 Turn on auto-forward in ⚙️ Settings to repost messages like this one instead.")
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
async def delete_last_message(bot, kind: str = "group"):
    """Delete the previous price message in a destination (group or channel)."""
    chat_id = chat_of(kind)
    mid = state.get(LAST_ID[kind])
    if not chat_id or not mid:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=mid)
        log.info("Deleted previous %s message %s in %s", kind, mid, chat_id)
        state[LAST_ID[kind]] = None
        state[LAST_TIME[kind]] = None
        save()
        return True
    except Exception as e:
        # message may already be deleted or bot not admin
        log.debug("Could not delete msg %s: %s", mid, e)
        # if message not found, clear state to avoid repeated attempts
        if "not found" in str(e).lower() or "message to delete not found" in str(e).lower() or "BadRequest" in str(type(e)):
            state[LAST_ID[kind]] = None
            state[LAST_TIME[kind]] = None
            save()
        return False

async def delete_last_group_message(bot):
    """Delete the previous price message in the group (the classic behaviour)."""
    return await delete_last_message(bot, "group")

async def post(bot, force=False):
    if not state["merchants"]: return False
    targets = post_targets()
    if not targets: return False
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
    # changing the banner — or switching a photo banner for a GIF, or turning the
    # HD delivery off — must repost
    snap["_photo"] = (post_banner(), banner_kind(), banner_hd(), banner_size())
    snap["_button_icons"] = button_icons()
    # The group and the channel each keep their own copy of the snapshot, so a
    # channel added later gets its own post without reposting to the group.
    due = [kind for kind, _ in targets
           if force or snap != state.get(SNAPSHOT[kind])]
    if not due: return False

    text = report(prices)
    kb = report_keyboard(prices)
    ok = False
    for kind, chat_id in targets:
        if kind not in due:
            continue
        state[SNAPSHOT[kind]] = deepcopy(snap)
        save()
        # Delete previous message if auto_delete enabled (refresh button or update time)
        if s.get("auto_delete", True):
            await delete_last_message(bot, kind)
        try:
            # send_report adds the banner photo when one is configured and the
            # report fits in a caption (see CAPTION_LIMIT); rebuild() lets it
            # retry with plain buttons if Telegram refuses the icons
            sent = await send_report(bot, chat_id, text, kb,
                                     rebuild=lambda: report_keyboard(prices))
            # store new message id and time
            state[LAST_ID[kind]] = sent.message_id
            state[LAST_TIME[kind]] = int(time.time())
            save()
            log.info("Posted new price message %s to %s %s", sent.message_id, kind, chat_id)
            ok = True
        except Exception as e:
            log.warning("Failed to post to %s %s: %s", kind, chat_id, e)
    return ok

async def auto_post_task(bot) -> bool:
    """Post prices when auto mode is on (and the snapshot changed)."""
    if not state.get("auto"):
        return False
    return bool(await post(bot))

async def cleanup_task(bot) -> bool:
    """Delete group/channel messages once they are older than delete_after_hours."""
    s = get_settings()
    hours = s.get("delete_after_hours", 24)
    if hours <= 0:
        return False  # disabled
    now, done = int(time.time()), False
    for kind in DESTINATIONS:
        chat_id, last_time = chat_of(kind), state.get(LAST_TIME[kind])
        if not chat_id or not last_time or not state.get(LAST_ID[kind]):
            continue
        if now - last_time < hours * 3600:
            continue
        log.info("%s message %s is older than %sh, auto-deleting",
                 kind.capitalize(), state[LAST_ID[kind]], hours)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=state[LAST_ID[kind]])
            state[LAST_ID[kind]] = None
            state[LAST_TIME[kind]] = None
            save()
            done = True
        except Exception as e:
            log.debug("Cleanup delete failed: %s", e)
            # clear if not found
            if "not found" in str(e).lower():
                state[LAST_ID[kind]] = None
                state[LAST_TIME[kind]] = None
                save()
    return done

# ── scheduled work ──
async def job(c: ContextTypes.DEFAULT_TYPE):
    """PTB JobQueue callback — posts prices when they change."""
    try:
        await auto_post_task(c.bot)
    except Exception as e:
        log.warning("auto post failed: %s", e)

async def cleanup_job(c: ContextTypes.DEFAULT_TYPE):
    """PTB JobQueue callback — deletes stale posts, expires anti-scam checks."""
    try:
        await cleanup_task(c.bot)
    except Exception as e:
        log.warning("cleanup failed: %s", e)
    try:
        await sweep_captcha(c.bot)
    except Exception as e:
        log.warning("anti-scam sweep failed: %s", e)

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
        await q.answer("✅ Posted!" if ok else "⚠️ Set a group/channel and add merchants first",
                       show_alert=not ok)

    elif d == "auto":
        state["auto"] = not state["auto"]; save(); await q.answer(f"Auto {'ON' if state['auto'] else 'OFF'}")

    elif d == "setgroup_help":
        return await q.answer("Add the bot to your group, then send /setgroup there.", show_alert=True)

    elif d == "setchannel_help":
        return await q.answer("Add the bot to your channel as an admin (it needs the right to "
                              "post and delete messages), then send /setchannel there.",
                              show_alert=True)

    elif d == "antiscam":
        if edit_get(u, "awaiting_custom"):
            edit_pop(u, "awaiting_custom")
        await q.answer()
        return await q.edit_message_text(antiscam_text(), parse_mode="HTML", reply_markup=antiscam_kb())

    elif d == "captcha_toggle":
        state["settings"]["captcha_enabled"] = not captcha_enabled()
        save()
        await q.answer(f"Verification {'ON' if captcha_enabled() else 'OFF'}")
        return await q.edit_message_text(antiscam_text(), parse_mode="HTML", reply_markup=antiscam_kb())

    elif d == "captcha_edit":
        edit_set(u, "awaiting_custom", "captcha_message")
        await q.answer()
        return await q.edit_message_text(
            "🛡 <b>Send the challenge message</b>\n\n"
            "Shown to every new member while they are muted. It <b>must</b> contain "
            "<code>{WORD}</code> — that is where the random word goes.\n\n"
            "Placeholders: <code>{WORD}</code> <code>{MENTION}</code> <code>{NAME}</code> "
            "<code>{GROUP}</code> <code>{MINUTES}</code> <code>{LEFT}</code> "
            "<code>{ASSET}</code> <code>{FIAT}</code> <code>{PAIR}</code>\n"
            "HTML allowed: &lt;b&gt; &lt;i&gt; &lt;code&gt; &lt;a&gt;\n\n"
            "Example:\n"
            "<code>🛡 {MENTION} type <b>{WORD}</b> to join {GROUP} — {MINUTES} min</code>\n\n"
            "Send /cancel to abort; send <code>default</code> to go back to the built-in text.",
            parse_mode="HTML", reply_markup=KB([[B("❌ Cancel", callback_data="antiscam")]]))

    elif d == "captcha_reset":
        state["settings"]["captcha_message"] = ""
        save()
        await q.answer("♻️ Challenge message reset")
        return await q.edit_message_text(antiscam_text(), parse_mode="HTML", reply_markup=antiscam_kb())

    elif d == "captcha_attempts":
        choices = CAPTCHA_ATTEMPT_CHOICES
        state["settings"]["captcha_attempts"] = choices[
            (choices.index(captcha_attempts()) + 1) % len(choices)]
        save()
        await q.answer(f"Attempts: {captcha_attempts()}")
        return await q.edit_message_text(antiscam_text(), parse_mode="HTML", reply_markup=antiscam_kb())

    elif d == "captcha_timeout":
        choices = CAPTCHA_TIMEOUT_CHOICES
        state["settings"]["captcha_timeout"] = choices[
            (choices.index(captcha_timeout()) + 1) % len(choices)]
        save()
        await q.answer(f"Timeout: {captcha_timeout()} min")
        return await q.edit_message_text(antiscam_text(), parse_mode="HTML", reply_markup=antiscam_kb())

    elif d == "captcha_action":
        state["settings"]["captcha_action"] = CAPTCHA_ACTIONS[
            (CAPTCHA_ACTIONS.index(captcha_action()) + 1) % len(CAPTCHA_ACTIONS)]
        save()
        await q.answer(f"On failure: {CAPTCHA_ACTION_SHORT[captcha_action()]}")
        return await q.edit_message_text(antiscam_text(), parse_mode="HTML", reply_markup=antiscam_kb())

    elif d == "captcha_preview":
        await q.answer("Sending a preview…")
        return await captcha_preview(u, c)

    elif d == "toggle_forward_target":
        chosen = cycle_forward_target()
        await q.answer(f"📤 Auto-forward: {FORWARD_LABELS[chosen]}")
        try:
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except Exception:
            pass

    elif d == "toggle_group_to_channel":
        enabled = toggle_group_to_channel()
        await q.answer(f"↪️ Group → channel: {'ON' if enabled else 'OFF'}")
        return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())

    elif d == "toggle_channel_to_group":
        enabled = toggle_channel_to_group()
        await q.answer(f"↪️ Channel → group: {'ON' if enabled else 'OFF'}")
        try:
            return await q.edit_message_text(settings_text(), parse_mode="HTML", reply_markup=settings_kb())
        except Exception:
            pass

    elif d == "forward_sources":
        if edit_get(u, "awaiting_custom"):
            edit_pop(u, "awaiting_custom")
        await q.answer()
        return await q.edit_message_text(forward_sources_text(), parse_mode="HTML",
                                         reply_markup=forward_sources_kb())

    elif d == "fwd_src_help":
        return await q.answer("Add the bot to that channel or group (as an admin), then send "
                              "/forwardfrom inside it.", show_alert=True)

    elif d == "fwd_src_toggle" or d == "fwd_src_use_channel" or d.startswith("fwd_src_del:"):
        # ↪️ Forward-from menu: switch the relay, add the price channel, drop one
        if d == "fwd_src_toggle":
            await q.answer(f"↪️ Forward to group: {'ON' if toggle_channel_to_group() else 'OFF'}")
        elif d == "fwd_src_use_channel":
            added = add_forward_source(state.get("channel"), state.get("channel_title"), "channel")
            await q.answer("📢 Forwarding from the price channel" if added else "Already selected")
        else:
            try:
                source_id = int(d.split(":", 1)[1])
            except (TypeError, ValueError):
                return await q.answer("Bad request", show_alert=True)
            dropped = remove_forward_source(source_id)
            await q.answer("🗑 No longer forwarded from there" if dropped else "Already gone")
        try:
            return await q.edit_message_text(forward_sources_text(), parse_mode="HTML",
                                             reply_markup=forward_sources_kb())
        except Exception:                        # unchanged text — the answer said it already
            pass

    elif d.startswith("fwd_undo:"):
        done = await undo_forward(u, c, d.split(":", 1)[1])
        await q.answer("🗑 Deleted" if done else "Already gone")
        if done:
            try:
                return await q.edit_message_text("🗑 Deleted — the message is gone from the "
                                                 "group/channel.", parse_mode="HTML")
            except Exception:
                pass
        return

    elif d.startswith("cap_approve:") or d.startswith("cap_kick:"):
        approve = d.startswith("cap_approve:")
        _, chat_id, user_id = d.split(":")
        try:
            chat_id, user_id = int(chat_id), int(user_id)
        except ValueError:
            return await q.answer("Bad request", show_alert=True)
        who = (await captcha_approve(c.bot, chat_id, user_id) if approve
               else await captcha_kick(c.bot, chat_id, user_id))
        await q.answer("✅ Approved" if approve else "🚫 Removed", show_alert=True)
        try:
            verb = "approved" if approve else "kicked"
            return await q.edit_message_text(f"{'✅' if approve else '🚫'} <b>{who}</b> {verb}.",
                                             parse_mode="HTML")
        except Exception:
            pass

    elif d == "list":
        await q.answer()
        return await q.edit_message_text("📋 <b>Merchants</b> — tap to remove" if state["merchants"]
                                         else "📋 No merchants yet. Paste a URL to add one.",
                                         parse_mode="HTML", reply_markup=list_kb())

    elif d.startswith("del:"):
        state["merchants"].pop(d[4:], None)
        for key in SNAPSHOT.values():            # group and channel both repost
            state[key].pop(d[4:], None)
        save()
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

    elif d == "button_icons":
        if edit_get(u, "awaiting_custom"):
            edit_pop(u, "awaiting_custom")
        await q.answer()
        return await q.edit_message_text(button_icons_text(), parse_mode="HTML",
                                         reply_markup=button_icons_kb())

    elif d.startswith("icon_menu:"):
        key = icon_key(d.split(":", 1)[1])
        if not key:
            return await q.answer("Unknown emoji", show_alert=True)
        await q.answer()
        return await q.edit_message_text(icon_editor_text(key), parse_mode="HTML",
                                         reply_markup=icon_editor_kb(key))

    elif d.startswith("icon_set:"):
        key = icon_key(d.split(":", 1)[1])
        if not key:
            return await q.answer("Unknown emoji", show_alert=True)
        edit_set(u, "awaiting_custom", f"icon:{key}")
        await q.answer("Forward the emoji or paste its id")
        return await q.edit_message_text(icon_prompt(key), parse_mode="HTML",
                                         reply_markup=KB([[B("❌ Cancel", callback_data=f"icon_menu:{key}")]]))

    elif d.startswith("icon_style:"):
        key = icon_key(d.split(":", 1)[1])
        if not key:
            return await q.answer("Unknown emoji", show_alert=True)
        icons = button_icons()
        current = (icons.get(key) or {}).get("style", "")
        following = list(BUTTON_STYLES) + [""]
        chosen = following[(following.index(current) + 1) % len(following)]
        entry = dict(icons.get(key) or {"icon": ""})
        entry["style"] = chosen
        entry.setdefault("icon", "")
        if entry.get("icon") or entry.get("style"):
            icons[key] = entry
        else:
            icons.pop(key, None)
        state["settings"]["button_icons"] = icons
        save()
        await q.answer(f"Colour: {STYLE_TITLES.get(chosen, 'default')}")
        return await q.edit_message_text(icon_editor_text(key), parse_mode="HTML",
                                         reply_markup=icon_editor_kb(key))

    elif d.startswith("icon_clear:"):
        key = icon_key(d.split(":", 1)[1])
        icons = button_icons()
        icons.pop(key, None)
        state["settings"]["button_icons"] = icons
        save()
        await q.answer("🗑 Icon removed")
        return await q.edit_message_text(icon_editor_text(key), parse_mode="HTML",
                                         reply_markup=icon_editor_kb(key))

    elif d == "banner_menu":
        if edit_get(u, "awaiting_custom"):
            edit_pop(u, "awaiting_custom")
        await q.answer()
        return await q.edit_message_text(banner_text(), parse_mode="HTML", reply_markup=banner_kb())

    elif d == "banner_send":
        edit_set(u, "awaiting_custom", "banner_media")
        await q.answer("Send the photo or GIF")
        return await q.edit_message_text(
            "🖼 <b>Send the photo or GIF for the post banner</b>\n\n"
            "Send a picture and it sits above the prices as a photo; send a GIF and the post "
            "plays it as an animation. Either way the bot stores Telegram's file id, so the "
            "post reuses it without re-uploading.\n\n"
            "📐 <b>Full HD</b> keeps the file you sent: a GIF travels with its own width, height "
            "and duration, and a photo is stored as the largest copy Telegram made of it. The "
            "post your group gets is the one you uploaded, not a preview.\n\n"
            "Send /cancel to stop.",
            parse_mode="HTML", reply_markup=KB([[B("❌ Cancel", callback_data="banner_menu")]]))

    elif d == "banner_url":
        edit_set(u, "awaiting_custom", "banner_url")
        await q.answer("Send the image URL")
        return await q.edit_message_text(
            "🔗 <b>Send the image URL</b>\n\n"
            "An <code>https://</code> link to a picture (JPG/PNG) Telegram can download — for "
            "example a file you host yourself or a CDN link. A link that ends in "
            "<code>.gif</code> is posted as an animation.\n\nSend /cancel to stop.",
            parse_mode="HTML", reply_markup=KB([[B("❌ Cancel", callback_data="banner_menu")]]))

    elif d == "banner_gif_url":
        edit_set(u, "awaiting_custom", "banner_gif_url")
        await q.answer("Send the GIF URL")
        return await q.edit_message_text(
            "🎞 <b>Send the GIF URL</b>\n\n"
            "An <code>https://</code> link to a GIF (or a silent MP4) — it is posted with "
            "<code>sendAnimation</code>, so Telegram plays it in the post, with the prices as "
            "its caption.\n\n"
            "A GIF the group can reach at its full size is a GIF you host yourself: Telegram "
            "downloads the link, so the banner is exactly what the URL serves.\n\n"
            "Send /cancel to stop.",
            parse_mode="HTML", reply_markup=KB([[B("❌ Cancel", callback_data="banner_menu")]]))

    elif d == "banner_hd":
        enabled = toggle_banner_hd()
        await q.answer("📐 Full HD ON — the banner keeps its own size" if enabled
                       else "📐 Full HD OFF")
        return await q.edit_message_text(banner_text(), parse_mode="HTML", reply_markup=banner_kb())

    elif d == "banner_clear":
        set_banner("", "photo")
        save()
        await q.answer("🗑 Banner removed")
        return await q.edit_message_text(banner_text(), parse_mode="HTML", reply_markup=banner_kb())

    elif d == "banner_test":
        await q.answer("Sending a test…")
        return await banner_test(u, c)

    elif d == "database":
        await q.answer()
        return await q.edit_message_text(database_text(), parse_mode="HTML",
                                         reply_markup=database_kb(),
                                         disable_web_page_preview=True)

    elif d == "db_check":
        # Credentials are usually added outside the bot (Vercel Storage, the
        # setup page, setup_cli.py), so re-read them and re-select the backend.
        try:
            import runtime_config
            runtime_config.apply(BASE_DIR)
        except Exception as e:
            log.warning("Stored settings unavailable: %s", e)
        rebuild_store()
        refresh_state()
        await q.answer(f"State store: {'connected ✅' if db_is_persistent() else 'not connected ⚠️'}")
        return await q.edit_message_text(database_text(), parse_mode="HTML",
                                         reply_markup=database_kb(),
                                         disable_web_page_preview=True)

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
        body = f"👁 <b>Preview - how it will look in group:</b>\n\n{text}"
        try:
            # the banner and its caption limit apply here too, so the preview
            # shows what the group will actually get
            await send_report(c.bot, q.message.chat_id, body, kb,
                              rebuild=lambda: report_keyboard(prices))
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

async def database_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """/database — the state store, and the link that makes it persistent."""
    if not is_admin(u): return
    await u.message.reply_html(database_text(), reply_markup=database_kb(),
                               disable_web_page_preview=True)

def preview_payload():
    """The exact text + keyboard the group post would use (no network calls)."""
    if state["merchants"]:
        return None, None                       # prices are fetched by the callers
    text = (f"{apply_template(state['settings'].get('custom_header')) or f'📊 P2P {ASSET}/{FIAT}'}"
            "\n\n<i>No merchants yet — add one by pasting a merchant URL here.</i>")
    return text, None


async def preview_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u) or u.effective_chat.type != "private": return
    if state["merchants"]:
        prices = await get_prices()
        text = report(prices)
        kb = report_keyboard(prices)
    else:
        text, kb = preview_payload()
    # Same delivery path as the group post, so the banner (and the caption
    # fallback) is previewed honestly.
    await send_report(c.bot, u.effective_chat.id, text, kb)


async def captcha_preview(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Send the admin the anti-scam challenge with a sample word."""
    chat_id = u.effective_user.id
    word = new_captcha_word()
    text = render_captcha(captcha_message(), word=word, name="New Member", user_id=chat_id,
                          group=state.get("group_title") or "your group",
                          minutes=captcha_timeout(), left=captcha_attempts())
    await c.bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    await c.bot.send_message(
        chat_id,
        "👆 That is what a new member sees in the group while they are muted "
        "(the word is different every time).\n"
        f"After <b>{captcha_attempts()}</b> wrong words or <b>{captcha_timeout()} minutes</b> "
        f"the bot <b>{CAPTCHA_ACTION_TITLES[captcha_action()]}</b> and asks you what to do.",
        parse_mode="HTML", reply_markup=antiscam_kb())


async def banner_test(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Send the admin the post exactly as the group receives it, banner and size included."""
    if state["merchants"]:
        prices = await get_prices()
        text = report(prices)
        kb = report_keyboard(prices)
    else:
        text, kb = preview_payload()
    sent = await send_report(c.bot, u.effective_user.id, text, kb)
    if getattr(sent, "photo", None) or getattr(sent, "animation", None):
        note = "✅ That is the post with your banner."
        hd = banner_send_kwargs()
        if hd:                                  # what Telegram was told to render it at
            note += f"\n📐 Full HD — the GIF goes out at its own {hd['width']}×{hd['height']}."
        elif banner_hd():
            note += "\n📐 Full HD — the file is posted as it is, at whatever size it holds."
        else:
            note += "\n📐 Full HD is OFF, so Telegram may show a smaller preview of the GIF."
    else:
        note = ("ℹ️ No banner was used: either none is set, or the report is longer than "
                f"{CAPTION_LIMIT} characters so Telegram would reject the caption.")
    await c.bot.send_message(u.effective_user.id, note, reply_markup=banner_kb())

async def error_handler(update, context):
    log.warning("Update %s caused error %s", update, context.error)

# ── run ──
PRIVATE_COMMANDS = [
    BotCommand("start", "Open the bot control panel"),
]
ADMIN_PRIVATE_COMMANDS = [
    BotCommand("start", "Open the bot control panel"),
    BotCommand("setgroup", "Set the group for price updates"),
    BotCommand("setchannel", "Set a channel for price updates"),
    BotCommand("forwardfrom", "Set up forwarding from this chat"),
    BotCommand("stopforward", "Stop forwarding from this chat"),
    BotCommand("preview", "Preview the price report"),
    BotCommand("database", "Check the database connection"),
    BotCommand("cancel", "Cancel the current action"),
]


async def configure_bot_commands(bot, *, include_setup: bool = False):
    """Publish Telegram's private-chat command menu for users and admins.

    The default private menu only exposes /start. Each configured admin gets a
    more useful, private menu with the commands the bot actually handles.
    Failures are non-fatal: an unavailable Bot API must not stop the bot.
    """
    try:
        await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        log.warning("Could not publish the private /start command: %s", exc)

    commands = list(ADMIN_PRIVATE_COMMANDS)
    if include_setup:
        commands.append(BotCommand("setup", "Get a one-time reconfiguration link"))
    for admin_id in ADMINS:
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            log.warning("Could not publish private commands for admin %s: %s", admin_id, exc)


async def post_init(application):
    global BOT_USERNAME
    me = await application.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Logged in as @%s", BOT_USERNAME)
    await configure_bot_commands(application.bot)

def register_handlers(app):
    # A command handler only listens to *messages* by default, and a channel
    # post is not one — the /start and /setchannel that arrive when the bot is
    # added to a channel would never be seen without this filter.
    with_channel_posts = filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS
    app.add_handler(CommandHandler("start", start, filters=with_channel_posts))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("setchannel", setchannel, filters=with_channel_posts))
    # ↪️ pick the chats whose messages are relayed into the group
    app.add_handler(CommandHandler(["forwardfrom", "setforward"], forwardfrom,
                                   filters=with_channel_posts))
    app.add_handler(CommandHandler(["stopforward", "unforward"], stopforward,
                                   filters=with_channel_posts))
    # Relay ordinary posts from the selected channels; setup commands above
    # stay in the channel and are never copied into the group.
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS & ~filters.COMMAND,
                                   on_channel_post))
    # The same for a selected *group*: the filter matches only those chats, so
    # the registered group keeps its 🛡 anti-scam handler (PTB calls only the
    # first matching handler of a group).
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGES & ForwardSourceFilter()
                                   & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                                   on_source_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & GroupToChannelFilter()
                                   & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                                   on_group_to_channel))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("preview", preview_cmd))
    app.add_handler(CommandHandler(["database", "db"], database_cmd))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS
                                   | filters.StatusUpdate.LEFT_CHAT_MEMBER, on_join_left))
    # the group: answers to the 🛡 anti-scam challenge
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT
                                   & ~filters.COMMAND, on_group_text))
    # banner media (a photo or a GIF) and premium-emoji stickers
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    # a GIF arrives as an animation when it is sent as a GIF and as a document
    # when it is sent as a file — both are accepted
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE
                                   & (filters.ANIMATION | filters.Document.GIF), on_animation))
    app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    # private chat: merchant URLs, menu answers and 📤 auto-forward
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT
                                   & ~filters.COMMAND, on_text))
    # 📤 auto-forward for everything else an admin sends (video, file, voice…)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND
                                   & (filters.VIDEO | filters.Document.ALL | filters.AUDIO
                                      | filters.VOICE | filters.VIDEO_NOTE | filters.ANIMATION
                                      | filters.CONTACT | filters.LOCATION | filters.POLL
                                      | filters.Dice.ALL | filters.VENUE | filters.GAME),
                                   on_private_media))
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
    print(f"   Channel: {state['channel'] or 'not set'} · 📤 Auto-forward: {forward_label()}")
    print(f"   ↪️ Forward to group: {'ON' if channel_to_group_enabled() else 'OFF'}"
          f" · from: {forward_source_summary()}")
    print(f"   🛡 Anti-scam: {'ON' if captcha_enabled() else 'OFF'}"
          f" ({captcha_attempts()} attempts, {captcha_timeout()} min → {captcha_action()})")
    print(f"   State: {STORE.describe()}")
    print(f"   Buttons: {'exact ad 🎯' if link_mode() == 'ad' else 'merchant profile 👤'}"
          f" · clickable prices: {'ON' if get_settings().get('price_links', True) else 'OFF'}")
    if not state["group"] and not state["channel"]:
        print("   → Open the bot in Telegram, /start, tap 👥 Set group (and 📢 Set channel)")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
