#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
#  P2P Merchant Price Bot — One-Click Local Installer
#  Works anywhere: macOS, Windows (WSL/Git Bash), Linux without
#  systemd, shared hosting, laptops. Uses venv + nohup + autostart
#  via launchd (macOS) or cron @reboot (Linux).
#
#  Usage (curl or wget — whichever your machine has):
#    curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-local.sh | bash
#    wget -qO-  https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-local.sh | bash
#    git clone https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot.git
#    cd OKX_Telegram_P2P_Price_Bot && bash install-local.sh
#    bash install-local.sh --token 123:ABC --admins 123456 --asset USDT --fiat USD
#    bash install-local.sh --reconfigure    # re-run setup wizard
#    bash install-local.sh --update         # pull + update + restart
#    bash install-local.sh --stop           # stop bot
#    bash install-local.sh --uninstall      # stop + remove autostart (keep data)
# ─────────────────────────────────────────────────────────────

# ── Where the source lives ───────────────────────────────────────────────
# NOTE: this repository used to be called "sarakmacbook/exchange".  GitHub
# redirects the human-facing pages, but *not* raw.githubusercontent.com, so a
# stale slug answers the download with a 404 page, and bash then dies with:
#     line 7: syntax error near unexpected token `newline'
# Override after a rename without editing this file:  P2P_REPO_SLUG=owner/name
REPO_SLUG="${P2P_REPO_SLUG:-sarakmacbook/OKX_Telegram_P2P_Price_Bot}"
REPO_URL="https://github.com/${REPO_SLUG}.git"
RAW_URL="https://raw.githubusercontent.com/${REPO_SLUG}/main"
TARBALL_URL="https://codeload.github.com/${REPO_SLUG}/tar.gz/refs/heads/main"
DEFAULT_DIR="$HOME/exchange-local"

INSTALL_DIR="$DEFAULT_DIR"
BOT_LOG="bot.log"
PID_FILE="bot.pid"

# CLI overrides (also read from ENV: BOT_TOKEN, ADMIN_IDS, ASSET, FIAT, INTERVAL)
ARG_TOKEN="${BOT_TOKEN:-}"
ARG_ADMINS="${ADMIN_IDS:-}"
ARG_ASSET="${ASSET:-}"
ARG_FIAT="${FIAT:-}"
ARG_INTERVAL="${INTERVAL:-}"
RECONFIGURE=0
DO_UPDATE=0
DO_STOP=0
DO_UNINSTALL=0
NO_AUTOSTART=0
NON_INTERACTIVE=0

# ── colours ──
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; BOLD=''; DIM=''; NC=''
fi
info()  { echo -e "${CYAN}ℹ️  $*${NC}"; }
ok()    { echo -e "${GREEN}✅ $*${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $*${NC}"; }
err()   { echo -e "${RED}❌ $*${NC}" >&2; }
step()  { echo -e "\n${BOLD}━━ $* ━━${NC}"; }

usage() {
  cat <<EOF
${BOLD}P2P Bot — One-Click Local Installer (no systemd required)${NC}
Usage: bash install-local.sh [OPTIONS]

Options:
  --dir PATH              Install directory (default: $DEFAULT_DIR)
  --token TOKEN           Bot token from @BotFather (else prompted)
  --admins IDS            Telegram user ID(s) comma-separated (else prompted)
  --asset SYMBOL          Asset, e.g. USDT (default: USDT)
  --fiat CODE             Fiat,  e.g. USD  (default: USD)
  --interval SEC          Check interval seconds (default: 60)
  --reconfigure           Re-run setup even if config.json exists
  --update                Pull latest code, update deps & restart
  --stop                  Stop the bot (keep data & autostart)
  --uninstall             Stop bot & remove autostart (keep data)
  --no-autostart          Install without launchd/cron autostart
  --non-interactive       Never prompt (fail if no --token/--admins)
  --help                  Show this help

Env alternatives: BOT_TOKEN, ADMIN_IDS, ASSET, FIAT, INTERVAL

Examples:
  curl -fsSL $RAW_URL/install-local.sh | bash
  wget -qO-  $RAW_URL/install-local.sh | bash
  curl -fsSL $RAW_URL/install-local.sh | bash -s -- --token 123:ABC --admins 123456
  wget -qO-  $RAW_URL/install-local.sh | bash -s -- --token 123:ABC --admins 123456
  bash install-local.sh --asset BTC --fiat EUR --interval 30

Downloads use curl, wget or python3 — whichever is installed
(force one with:  DOWNLOADER=wget bash install-local.sh ...)
EOF
}

# ── arg parse ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        INSTALL_DIR="$2"; shift 2;;
    --token)      ARG_TOKEN="$2"; shift 2;;
    --admins)     ARG_ADMINS="$2"; shift 2;;
    --asset)      ARG_ASSET="$2"; shift 2;;
    --fiat)       ARG_FIAT="$2"; shift 2;;
    --interval)   ARG_INTERVAL="$2"; shift 2;;
    --reconfigure) RECONFIGURE=1; shift;;
    --update)     DO_UPDATE=1; shift;;
    --stop)       DO_STOP=1; shift;;
    --uninstall)  DO_UNINSTALL=1; shift;;
    --no-autostart) NO_AUTOSTART=1; shift;;
    --non-interactive) NON_INTERACTIVE=1; shift;;
    --help|-h)    usage; exit 0;;
    --) shift; break;;
    *) err "Unknown option: $1"; usage; exit 1;;
  esac
done

# ── helpers ──
need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ── download helper: curl → wget → python3 (uses whichever exists) ──
# Override with:  DOWNLOADER=wget bash install-local.sh
DOWNLOADER="${DOWNLOADER:-}"
DOWNLOADER_OVERRIDE="$DOWNLOADER"
detect_downloader() {
  if [[ -n "$DOWNLOADER_OVERRIDE" ]]; then
    if need_cmd "$DOWNLOADER_OVERRIDE"; then DOWNLOADER="$DOWNLOADER_OVERRIDE"; return 0; fi
    warn "DOWNLOADER=$DOWNLOADER_OVERRIDE requested but not installed — falling back."
    DOWNLOADER_OVERRIDE=""
  fi
  if   need_cmd curl;    then DOWNLOADER="curl"
  elif need_cmd wget;    then DOWNLOADER="wget"
  elif need_cmd python3; then DOWNLOADER="python3"
  else DOWNLOADER=""; return 1
  fi
}

# ── download integrity guards ────────────────────────────────────────────
# Whenever a URL no longer points at a file, GitHub answers with a web page
# instead of refusing: a renamed repository (raw.githubusercontent.com does
# NOT follow renames) or a github.com/OWNER/REPO/blob/… link (the HTML viewer
# rather than the raw file).  Saving that page as install.sh / bot.py is what
# produces the classic error:
#     ./install.sh: line 7: syntax error near unexpected token `newline'
#     ./install.sh: line 7: `<!DOCTYPE html>'
# so every download is inspected before it is allowed to touch the install dir.
looks_like_web_page() {
  head -c 512 "$1" 2>/dev/null | tr -d '\r\n' \
    | LC_ALL=C grep -asiE '<!doctype html|<html[ >]|^404: not found|<\?xml' >/dev/null
}

looks_like_gzip() {   # a gzip archive starts with the magic bytes 1f 8b
  local magic
  need_cmd od || return 0   # no od available — let tar decide for itself
  magic="$(head -c 2 "$1" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')"
  [[ "$magic" == "1f8b" ]]
}

# guard_download DEST URL — reject empty, HTML or non-archive payloads
guard_download() {
  local dest="$1" url="$2" name
  name="$(basename "$dest")"
  if [[ ! -s "$dest" ]]; then
    err "Download failed — received an empty file from:"
    echo "    $url" >&2
    return 1
  fi
  case "$url" in
    *tar.gz*|*tgz*)                      # archives are verified by magic bytes only
      if ! looks_like_gzip "$dest"; then
        err "Refusing to unpack '$name' — not a gzip archive (probably an error page)."
        echo "    URL: $url" >&2
        return 1
      fi
      return 0
      ;;
  esac
  if looks_like_web_page "$dest"; then
    err "Refusing to install '$name' — the download is a web page, not a file."
    echo "    URL: $url" >&2
    echo "  Usual causes:" >&2
    echo "    1. the URL is a github.com/OWNER/REPO/blob/… page — use raw.githubusercontent.com" >&2
    echo "    2. the repository was renamed — raw URLs do not follow renames (they return 404)" >&2
    echo "  This installer expects the sources under:" >&2
    echo "    $RAW_URL" >&2
    echo "  Point the installer at the right repository with: P2P_REPO_SLUG=owner/repo" >&2
    return 1
  fi
  return 0
}

# download_to URL DEST — transfer with the detected tool (curl / wget / python3)
download_to() {
  local url="$1" dest="$2"
  case "$DOWNLOADER" in
    curl)
      curl -fsSL --connect-timeout 15 --retry 3 "$url" -o "$dest" ;;
    wget)
      wget -q --timeout=20 --tries=3 -O "$dest" "$url" ;;
    python3)
      python3 - "$url" "$dest" <<'PY'
import shutil, sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={"User-Agent": "p2p-bot-installer"})
with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
    shutil.copyfileobj(resp, fh)
PY
      ;;
    *)
      return 1 ;;
  esac
}

# fetch URL DEST — download URL into file DEST (never keeps an HTML error page)
fetch() {
  local url="$1" dest="$2"
  detect_downloader || { err "No downloader found — install curl, wget or python3."; return 1; }
  if ! download_to "$url" "$dest"; then
    err "Download failed ($DOWNLOADER exited non-zero):"
    echo "    $url" >&2
    rm -f "$dest"
    return 1
  fi
  if ! guard_download "$dest" "$url"; then
    rm -f "$dest"
    return 1
  fi
  return 0
}

# fetch_repo DEST — download the repo tarball and unpack it into DEST
# (fallback when git is missing or the clone fails)
fetch_repo() {
  local dest="$1" tmp
  need_cmd tar || { warn "tar not found — cannot unpack the source tarball."; return 1; }
  tmp="$(mktemp)" || return 1
  if fetch "$TARBALL_URL" "$tmp"; then
    mkdir -p "$dest"
    if tar -xzf "$tmp" -C "$dest" --strip-components=1; then
      rm -f "$tmp"
      ok "Source downloaded via ${DOWNLOADER} tarball → $dest"
      return 0
    fi
    err "Could not unpack the archive from $TARBALL_URL"
  fi
  rm -f "$tmp"
  return 1
}

# Detect if we're running from a local repo checkout
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo ".")"
HAS_LOCAL_REPO=0
if [[ -f "$SCRIPT_DIR/bot.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
  HAS_LOCAL_REPO=1
fi

# Reopen stdin from /dev/tty if piped (curl | bash / wget | bash) so prompts work.
# If there is no controlling TTY, keep stdin as-is (args/env mode).
if [[ ! -t 0 ]]; then
  exec < /dev/tty 2>/dev/null || true
fi

banner() {
cat <<'BANNER'
 ____  ____  ____      ____        _
|  _ \|  _ \|  _ \    | __ )  ___ | |_
| |_) | |_) | |_) |   |  _ \ / _ \| __|
|  __/|  __/|  __/    | |_) | (_) | |_
|_|   |_|   |_|       |____/ \___/ \__|
  P2P Merchant Price Bot — One-Click Local Installer
BANNER
echo -e "${DIM}No systemd needed · macOS / Linux / WSL · venv + nohup${NC}\n"
}

banner

# ── 1. checks ──
step "1/5  Checking system"
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  echo "  OS: $PRETTY_NAME ($VERSION_ID)  arch: $(uname -m)"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  echo "  OS: macOS  arch: $(uname -m)"
else
  echo "  OS: $(uname -s)  arch: $(uname -m)"
fi

if ! need_cmd python3; then err "python3 not found. Install Python 3.8+ first."; exit 1; fi
if ! need_cmd git; then warn "git not found — source will be downloaded with curl/wget/python3 instead."; fi
if detect_downloader; then
  echo "  Downloader: $DOWNLOADER  (curl / wget / python3 — any of them works)"
else
  warn "No curl, wget or python3 found — install one of them (git clone is the only other way)."
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "  Python: $PY_VER  ($(python3 --version))"
if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3,8) else 1)'; then
  err "Python $PY_VER too old — need 3.8+. Please install a newer Python."
  exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
  warn "python3-venv missing — trying to install it..."
  if [[ "$(uname -s)" == "Linux" ]] && need_cmd apt-get && { [[ $EUID -eq 0 ]] || sudo -n true 2>/dev/null; }; then
    SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -y || true
    $SUDO apt-get install -y python3-venv || { err "Could not install python3-venv."; exit 1; }
  else
    err "python3-venv is required. On Debian/Ubuntu: sudo apt-get install python3-venv"
    exit 1
  fi
fi

# ── 2. get source ──
step "2/5  Getting bot source"
# Resolve install dir — if local repo & no --dir override, use script dir
if [[ $HAS_LOCAL_REPO -eq 1 && "$INSTALL_DIR" == "$DEFAULT_DIR" ]]; then
  if [[ "$SCRIPT_DIR" != "." && "$SCRIPT_DIR" != "/tmp" ]]; then
    INSTALL_DIR="$SCRIPT_DIR"
  fi
fi
echo "  Install dir: $INSTALL_DIR"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Existing repo found — pulling latest..."
  if need_cmd git; then
    git -C "$INSTALL_DIR" pull --ff-only || warn "git pull failed — continuing with existing files."
  else
    warn "git not installed — cannot pull; keeping the files already on disk."
  fi
elif [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/bot.py" ]]; then
  if [[ $DO_UPDATE -eq 1 ]]; then
    info "No git repo here — refreshing files from the GitHub archive..."
    fetch_repo "$INSTALL_DIR" || warn "Refresh failed — continuing with existing files."
  else
    info "Existing install dir without git — keeping files."
  fi
elif [[ ! -d "$INSTALL_DIR" ]]; then
  if [[ $HAS_LOCAL_REPO -eq 1 && "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    info "Copying local files to $INSTALL_DIR ..."
    mkdir -p "$INSTALL_DIR"
    cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/ 2>/dev/null || true
    cp -r "$SCRIPT_DIR"/.git "$INSTALL_DIR"/ 2>/dev/null || true
  else
    info "Cloning $REPO_URL → $INSTALL_DIR ..."
    git clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null || {
      # fallback: git missing/blocked → tarball via curl / wget / python3
      warn "git clone failed — downloading the source archive instead..."
      mkdir -p "$INSTALL_DIR"
      fetch_repo "$INSTALL_DIR" || {
        warn "Archive download failed too — fetching the core files one by one..."
        fetch "$RAW_URL/bot.py"              "$INSTALL_DIR/bot.py"              || true
        fetch "$RAW_URL/exchanges.py"        "$INSTALL_DIR/exchanges.py"        || true
        fetch "$RAW_URL/adlinks.py"          "$INSTALL_DIR/adlinks.py"          || true
        fetch "$RAW_URL/storage.py"          "$INSTALL_DIR/storage.py"          || true
        fetch "$RAW_URL/database_adapters.py" "$INSTALL_DIR/database_adapters.py" || true
        fetch "$RAW_URL/runtime_config.py"   "$INSTALL_DIR/runtime_config.py"   || true
        fetch "$RAW_URL/telegram_store.py"   "$INSTALL_DIR/telegram_store.py"   || true
        fetch "$RAW_URL/requirements.txt"    "$INSTALL_DIR/requirements.txt"    || true
      }
    }
  fi
else
  if [[ $HAS_LOCAL_REPO -eq 1 ]]; then
    cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/ 2>/dev/null || true
  else
    git clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null || fetch_repo "$INSTALL_DIR" || true
  fi
fi

if [[ ! -f "$INSTALL_DIR/bot.py" ]]; then
  err "Could not fetch the bot source into $INSTALL_DIR."
  echo "  Install git, curl or wget, then re-run — or download manually:"
  echo "    mkdir -p $INSTALL_DIR"
  echo "    wget -qO- $TARBALL_URL | tar -xz --strip-components=1 -C $INSTALL_DIR"
  exit 1
fi

cd "$INSTALL_DIR"
echo "  Files: $(ls -1 bot.py exchanges.py adlinks.py storage.py requirements.txt 2>/dev/null | tr '\n' ' ')"

# ── 3. venv & deps ──
step "3/5  Setting up Python environment"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  python3 -m venv "$INSTALL_DIR/venv"
  ok "Created venv at $INSTALL_DIR/venv"
else
  info "venv already exists — reusing."
fi
# shellcheck disable=SC1091
source "$INSTALL_DIR/venv/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
ok "Dependencies installed ($(pip freeze | wc -l) packages)"

# ── 4. config ──
step "4/5  Configuring bot"

CONFIG_FILE="$INSTALL_DIR/config.json"
DATA_FILE="$INSTALL_DIR/data.json"
NEED_SETUP=0
if [[ ! -f "$CONFIG_FILE" ]]; then NEED_SETUP=1; fi
if [[ $RECONFIGURE -eq 1 ]]; then NEED_SETUP=1; fi
if [[ $DO_UPDATE -eq 1 ]]; then NEED_SETUP=0; fi

valid_token() { [[ "$1" == *":"* && ${#1} -gt 20 ]]; }
valid_admins() { [[ "$1" =~ ^[0-9,\ ]+$ ]]; }

write_config() {
  local tok="${ARG_TOKEN:-}" admins="${ARG_ADMINS:-}" asset="${ARG_ASSET:-USDT}" fiat="${ARG_FIAT:-USD}" interval="${ARG_INTERVAL:-60}"
  if [[ -n "$tok" && -n "$admins" ]]; then
    if ! valid_token "$tok"; then err "Token looks invalid (should contain ':' and be longer)."; return 1; fi
    if ! valid_admins "$admins"; then err "Admins should be comma-separated numeric IDs."; return 1; fi
    admins="$(echo "$admins" | tr -d ' ')"
    asset="${asset^^}"; fiat="${fiat^^}"
    interval="${interval//[^0-9]/}"; [[ -z "$interval" ]] && interval=60
    cat > "$CONFIG_FILE" <<EOF
{
 "token": "$tok",
 "admins": "$admins",
 "asset": "$asset",
 "fiat": "$fiat",
 "interval": $interval
}
EOF
    chmod 600 "$CONFIG_FILE"
    ok "Config written → $CONFIG_FILE"
    echo "  asset=$asset fiat=$fiat interval=${interval}s admins=$admins"
    return 0
  fi
  return 1
}

ask() {
  local prompt="$1" default="${2:-}" var
  if [[ -n "$default" ]]; then prompt="$prompt [$default]"; fi
  read -r -p "$prompt: " var || true
  var="${var:-$default}"
  echo "$var"
}

if [[ $NEED_SETUP -eq 1 ]]; then
  if write_config; then
    :
  elif [[ ! -t 0 || $NON_INTERACTIVE -eq 1 ]]; then
    err "config.json not found and no --token/--admins provided in non-interactive mode."
    echo ""
    echo "  Provide them via flags or env:"
    echo "    bash install-local.sh --token 123:ABC --admins 123456"
    echo "    BOT_TOKEN=123:ABC ADMIN_IDS=123456 bash install-local.sh"
    echo "    curl -fsSL $RAW_URL/install-local.sh | bash -s -- --token 123:ABC --admins 123456"
    echo "    wget -qO-  $RAW_URL/install-local.sh | bash -s -- --token 123:ABC --admins 123456"
    echo ""
    echo "  Or run interactively:  bash install-local.sh"
    exit 1
  else
    echo ""
    echo -e "${BOLD}🤖 First-time setup — you will be asked 5 questions${NC}"
    echo -e "${DIM}  Get bot token from @BotFather on Telegram → /newbot${NC}"
    echo -e "${DIM}  Get your user ID from @userinfobot on Telegram${NC}"
    echo ""
    while true; do
      TOK_INPUT="$(ask "Bot token from @BotFather" "${ARG_TOKEN:-}")"
      if valid_token "$TOK_INPUT"; then ARG_TOKEN="$TOK_INPUT"; break; else echo -e "${RED}  ❌ Token should look like 123456:ABC... — try again${NC}"; fi
    done
    while true; do
      ADM_INPUT="$(ask "Your Telegram user ID(s), comma-separated" "${ARG_ADMINS:-}")"
      if valid_admins "$ADM_INPUT"; then ARG_ADMINS="$ADM_INPUT"; break; else echo -e "${RED}  ❌ Should be numbers like 123456 or 123,456${NC}"; fi
    done
    ARG_ASSET="$(ask "Asset" "${ARG_ASSET:-USDT}")"
    ARG_FIAT="$(ask "Fiat currency" "${ARG_FIAT:-USD}")"
    ARG_INTERVAL="$(ask "Check prices every N seconds" "${ARG_INTERVAL:-60}")"
    write_config || { err "Could not save configuration."; exit 1; }
  fi
else
  ok "Config exists — keeping $CONFIG_FILE (use --reconfigure to change)"
  cat "$CONFIG_FILE" | python3 -m json.tool 2>/dev/null | sed 's/"token": ".*"/"token": "***"/' || cat "$CONFIG_FILE"
fi

# ensure data.json exists
if [[ ! -f "$DATA_FILE" ]]; then
  echo '{"group": null, "auto": false, "merchants": {}, "last": {}}' > "$DATA_FILE"
fi
chmod 600 "$CONFIG_FILE" 2>/dev/null || true
chmod 600 "$DATA_FILE" 2>/dev/null || true

PYBIN="$INSTALL_DIR/venv/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="$(command -v python3)"

# ── 5. start (nohup + autostart) ──
step "5/5  Starting bot"

# stop previous instance first
if [[ -f "$PID_FILE" ]]; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
fi
pkill -f "$INSTALL_DIR/bot.py" 2>/dev/null || true

if [[ $DO_STOP -eq 1 ]]; then
  ok "Bot stopped (data kept). Start again: bash $INSTALL_DIR/install-local.sh"
  exit 0
fi

if [[ $DO_UNINSTALL -eq 1 ]]; then
  # remove autostart entries
  if [[ "$(uname -s)" == "Darwin" ]]; then
    PLIST="$HOME/Library/LaunchAgents/com.p2p-bot.plist"
    rm -f "$PLIST" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true
    ok "launchd agent removed"
  fi
  if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v "$INSTALL_DIR/bot.py" || true) | crontab - 2>/dev/null || true
    ok "cron @reboot entry removed"
  fi
  ok "Bot uninstalled. Data kept in $INSTALL_DIR (delete manually: rm -rf $INSTALL_DIR)"
  exit 0
fi

nohup "$PYBIN" "$INSTALL_DIR/bot.py" >> "$INSTALL_DIR/$BOT_LOG" 2>&1 &
echo $! > "$PID_FILE"
sleep 3

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  ok "Bot started (PID $(cat "$PID_FILE")) — logs: $INSTALL_DIR/$BOT_LOG"
else
  warn "Bot process exited quickly — last log lines:"
  tail -n 15 "$INSTALL_DIR/$BOT_LOG" 2>/dev/null || true
  warn "Common cause: missing/invalid token or network (Telegram blocked)."
  warn "Fix token:  bash $INSTALL_DIR/install-local.sh --reconfigure"
fi

# autostart
if [[ $NO_AUTOSTART -eq 0 && "$(uname -s)" == "Darwin" ]]; then
  PLIST_DIR="$HOME/Library/LaunchAgents"
  PLIST="$PLIST_DIR/com.p2p-bot.plist"
  mkdir -p "$PLIST_DIR"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.p2p-bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYBIN</string>
    <string>$INSTALL_DIR/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$INSTALL_DIR/$BOT_LOG</string>
  <key>StandardErrorPath</key><string>$INSTALL_DIR/$BOT_LOG</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST" 2>/dev/null || true
  ok "launchd autostart installed (com.p2p-bot)"
elif [[ $NO_AUTOSTART -eq 0 ]] && command -v crontab >/dev/null 2>&1; then
  (crontab -l 2>/dev/null | grep -v "$INSTALL_DIR/bot.py"; echo "@reboot $PYBIN $INSTALL_DIR/bot.py >> $INSTALL_DIR/$BOT_LOG 2>&1") | crontab -
  ok "cron @reboot autostart installed"
else
  info "Autostart skipped (--no-autostart or no crontab)."
fi

# ── done ──
echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  🎉 Installed! Bot is running.${NC}"
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BOLD}Install dir:${NC} $INSTALL_DIR"
echo -e "  ${BOLD}PID file:${NC}    $PID_FILE"
echo -e "  ${BOLD}Logs:${NC}        $INSTALL_DIR/$BOT_LOG"
echo -e "  ${BOLD}Config:${NC}      $CONFIG_FILE"
echo ""
echo -e "  ${BOLD}Next steps on Telegram:${NC}"
echo -e "   1. Open your bot → send ${CYAN}/start${NC} (in private chat)"
echo -e "   2. Add bot to your group → send ${CYAN}/setgroup${NC} inside the group"
echo -e "   3. Back in private chat → ${CYAN}paste a merchant URL${NC} to add it"
echo -e "   4. Tap ${CYAN}🟢 Auto: ON${NC} — prices post automatically"
echo ""
echo -e "  ${BOLD}Useful commands:${NC}"
echo -e "   ${DIM}tail -f $INSTALL_DIR/$BOT_LOG${NC}         — live logs"
echo -e "   ${DIM}bash $INSTALL_DIR/install-local.sh --stop${NC}        — stop"
echo -e "   ${DIM}bash $INSTALL_DIR/install-local.sh${NC}              — start"
echo -e "   ${DIM}bash $INSTALL_DIR/install-local.sh --reconfigure${NC}  — change token/pair"
echo -e "   ${DIM}bash $INSTALL_DIR/install-local.sh --update${NC}       — update to latest"
echo -e "   ${DIM}bash $INSTALL_DIR/install-local.sh --uninstall${NC}    — remove autostart (keep data)"
echo ""
