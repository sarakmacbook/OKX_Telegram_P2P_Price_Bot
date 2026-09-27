#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
#  P2P Merchant Price Bot — One-Click VPS Installer
#  Ubuntu 20.04 / 22.04 / 24.04 — run as root or regular user with sudo
#
#  Usage (curl or wget — whichever your box has):
#    curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
#    wget -qO-  https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
#    curl -fsSL .../install.sh | bash -s -- --token 123:ABC --admins 123456 --asset USDT --fiat USD
#    wget -qO-  .../install.sh | bash -s -- --token 123:ABC --admins 123456 --asset USDT --fiat USD
#    git clone https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot.git
#    cd OKX_Telegram_P2P_Price_Bot && sudo bash install.sh
#    sudo bash install.sh --token 123:ABC --admins 123456,789012
#    sudo bash install.sh --reconfigure   # re-ask ALL 5 questions (blank prompts)
#    sudo bash install.sh --update        # pull + restart
#    sudo bash install.sh --uninstall     # uninstall — asks: erase everything
#                                          # or save config.json + data.json
#
#  --reconfigure is always interactive: it never takes BOT_TOKEN / ADMIN_IDS
#  from the environment as defaults, it asks every question again from blank,
#  backs up the old config.json and prints what actually changed.
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
DEFAULT_DIR="$HOME/exchange"
if [[ $EUID -eq 0 ]]; then DEFAULT_DIR="/opt/p2p-bot"; fi

INSTALL_DIR="$DEFAULT_DIR"
SERVICE_NAME="p2p-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# CLI overrides (also read from ENV: BOT_TOKEN, ADMIN_IDS, ASSET, FIAT, INTERVAL)
ARG_TOKEN="${BOT_TOKEN:-}"
ARG_ADMINS="${ADMIN_IDS:-}"
ARG_ASSET="${ASSET:-}"
ARG_FIAT="${FIAT:-}"
ARG_INTERVAL="${INTERVAL:-}"
RECONFIGURE=0
DO_UPDATE=0
DO_UNINSTALL=0
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
${BOLD}P2P Bot — One-Click Installer${NC}
Usage: bash install.sh [OPTIONS]

Options:
  --dir PATH              Install directory (default: $DEFAULT_DIR)
  --token TOKEN           Bot token from @BotFather (else prompted)
  --admins IDS            Telegram user ID(s) comma-separated (else prompted)
  --asset SYMBOL          Asset, e.g. USDT (default: USDT)
  --fiat CODE             Fiat,  e.g. USD  (default: USD)
  --interval SEC          Check interval seconds (default: 60)
  --reconfigure           Ask all 5 setup questions again (blank prompts —
                          current values are shown masked for reference only,
                          never used as defaults). Old config.json is backed up
                          to config.json.bak and the service is restarted.
                          Aliases: --reconfig, --setup
  --non-interactive       Never prompt; use flags/env only (fails if incomplete)
  --update                Pull latest code & restart service
  --uninstall             Uninstall — asks: erase EVERYTHING install.sh did
                          or save config.json + data.json to a backup folder
  --help                  Show this help

Env alternatives: BOT_TOKEN, ADMIN_IDS, ASSET, FIAT, INTERVAL

Examples:
  curl -fsSL $RAW_URL/install.sh | bash
  wget -qO-  $RAW_URL/install.sh | bash
  curl -fsSL $RAW_URL/install.sh | bash -s -- --token 123:ABC --admins 123456
  wget -qO-  $RAW_URL/install.sh | bash -s -- --token 123:ABC --admins 123456
  sudo bash install.sh --dir /opt/p2p-bot --asset BTC --fiat EUR
  sudo bash install.sh --reconfigure          # interactive: answer all 5 again

Downloads use curl, wget or python3 — whichever is installed
(force one with:  DOWNLOADER=wget bash install.sh ...)
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
    --reconfigure|--reconfig|--setup) RECONFIGURE=1; shift;;
    --update)     DO_UPDATE=1; shift;;
    --uninstall)  DO_UNINSTALL=1; shift;;
    --non-interactive) NON_INTERACTIVE=1; shift;;
    --help|-h)    usage; exit 0;;
    --) shift; break;;
    *) err "Unknown option: $1"; usage; exit 1;;
  esac
done

# ── helpers ──
have_sudo() { [[ $EUID -eq 0 ]] || sudo -n true 2>/dev/null || sudo -v 2>/dev/null; }
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ── download helper: curl → wget → python3 (uses whichever exists) ──
# Override with:  DOWNLOADER=wget bash install.sh
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

is_systemd() {
  need_cmd systemctl && [[ -d /run/systemd/system ]] && [[ "$(ps -p 1 -o comm= 2>/dev/null || echo init)" == "systemd" ]]
}

# Detect if we're running via  curl | bash  /  wget | bash  (no local repo)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo ".")"
HAS_LOCAL_REPO=0
if [[ -f "$SCRIPT_DIR/bot.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
  HAS_LOCAL_REPO=1
fi

# ── Handle uninstall early ────────────────────────────────────────────────
# The real uninstaller asks whether to erase EVERYTHING install.sh created or
# to save config.json + data.json into a backup folder first.  It lives next
# to this script in the repo; when install.sh itself was piped (curl | bash)
# and there is no local copy, it is fetched from the same source.
# No --dir is forced here: uninstall.sh finds the install directory itself
# (from the systemd service, the current directory, or the usual locations).
if [[ $DO_UNINSTALL -eq 1 ]]; then
  UNINSTALLER="$SCRIPT_DIR/uninstall.sh"
  if [[ ! -f "$UNINSTALLER" ]] && detect_downloader; then
    _tmp_un="$(mktemp "${TMPDIR:-/tmp}/p2p-uninstall.XXXXXX.sh")" || _tmp_un=""
    if [[ -n "${_tmp_un:-}" ]] && fetch "$RAW_URL/uninstall.sh" "$_tmp_un"; then
      UNINSTALLER="$_tmp_un"
    fi
  fi
  if [[ -f "$UNINSTALLER" ]]; then
    exec bash "$UNINSTALLER"
  fi
  # last resort (no uninstaller available): remove the service inline
  echo "Uninstalling $SERVICE_NAME ..."
  if systemctl list-units --type=service 2>/dev/null | grep -q "$SERVICE_NAME"; then
    sudo systemctl stop "$SERVICE_NAME" || true
    sudo systemctl disable "$SERVICE_NAME" || true
    sudo rm -f "$SERVICE_FILE"
    sudo systemctl daemon-reload || true
    ok "Service removed."
  else
    warn "Service not found."
  fi
  echo "Data kept in $INSTALL_DIR (remove manually if wanted: rm -rf $INSTALL_DIR)"
  echo "For the full uninstaller (asks: erase everything or keep the json data):"
  echo "  curl -fsSL $RAW_URL/uninstall.sh | bash"
  exit 0
fi

banner() {
cat <<'BANNER'
 ____  ____  ____      ____        _
|  _ \|  _ \|  _ \    | __ )  ___ | |_
| |_) | |_) | |_) |   |  _ \ / _ \| __|
|  __/|  __/|  __/    | |_) | (_) | |_
|_|   |_|   |_|       |____/ \___/ \__|
  P2P Merchant Price Bot — One-Click VPS Installer
BANNER
echo -e "${DIM}Ubuntu 20.04 / 22.04 / 24.04 · Binance · Bybit · OKX · Bitget${NC}\n"
}

# ── Reopen stdin from /dev/tty if piped (curl | bash / wget | bash) so prompts work ──
# If there is no controlling TTY, keep stdin as-is (args/env mode).
if [[ ! -t 0 ]]; then
  { exec < /dev/tty; } 2>/dev/null || true
fi

# ── 1. banner & checks ──
banner

if [[ $DO_UPDATE -eq 1 ]]; then
  info "Update mode — will pull latest code and restart."
fi

step "1/6  Checking system"
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  echo "  OS: $PRETTY_NAME  ($VERSION_ID)  arch: $(uname -m)"
  if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]]; then
    warn "Not Ubuntu/Debian ($ID) — will try to install anyway."
  fi
else
  warn "/etc/os-release not found — continuing anyway."
fi

if ! have_sudo && [[ $EUID -ne 0 ]]; then
  err "This installer needs sudo for apt + systemd."
  echo "  Run:  sudo bash install.sh"
  echo "  Or:   su -c 'bash install.sh'"
  exit 1
fi

if ! is_systemd; then
  warn "systemd not detected — will use nohup fallback (no auto-restart on reboot)."
  warn "For production, use a VPS with systemd (most Ubuntu VPS have it)."
fi

# ── 2. system deps ──
step "2/6  Installing system packages"
export DEBIAN_FRONTEND=noninteractive
if [[ -n "${SKIP_APT:-}" ]]; then
  info "SKIP_APT set — skipping apt update/install"
else
  $SUDO apt-get update -y || warn "apt update failed — continuing anyway (may need manual: sudo apt update)"
  $SUDO apt-get install -y python3 python3-pip python3-venv git curl wget ca-certificates || warn "apt install failed — continuing; ensure python3/venv/git are installed"
fi

if detect_downloader; then
  echo "  Downloader: $DOWNLOADER  (curl / wget / python3 — any of them works)"
else
  warn "No curl, wget or python3 available — source will be fetched with git clone only."
fi

# ensure python is 3.8+ (Ubuntu 20.04 has 3.8 — still works)
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "  Python: $PY_VER  ($(python3 --version))"
if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3,8) else 1)'; then
  err "Python $PY_VER too old — need 3.8+. Please upgrade Ubuntu."
  exit 1
fi

# ── 3. get / update source ──
step "3/6  Getting bot source"

# Resolve install dir — if has local repo and no --dir override, use script dir
if [[ $HAS_LOCAL_REPO -eq 1 && "$INSTALL_DIR" == "$DEFAULT_DIR" ]]; then
  # if script dir looks like a real install dir, prefer it
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
        fetch "$RAW_URL/bot.py"           "$INSTALL_DIR/bot.py"           || true
        fetch "$RAW_URL/exchanges.py"     "$INSTALL_DIR/exchanges.py"     || true
        fetch "$RAW_URL/adlinks.py"       "$INSTALL_DIR/adlinks.py"       || true
        fetch "$RAW_URL/storage.py"       "$INSTALL_DIR/storage.py"       || true
        fetch "$RAW_URL/requirements.txt" "$INSTALL_DIR/requirements.txt" || true
      }
    }
  fi
else
  # dir exists but empty
  if [[ $HAS_LOCAL_REPO -eq 1 ]]; then
    cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/ 2>/dev/null || true
  else
    git clone "$REPO_URL" "$INSTALL_DIR" 2>/dev/null || fetch_repo "$INSTALL_DIR" || true
  fi
fi

if [[ ! -f "$INSTALL_DIR/bot.py" ]]; then
  err "Could not fetch the bot source into $INSTALL_DIR."
  echo "  Install git, curl or wget, then re-run — or download manually:"
  echo "    wget -qO- $TARBALL_URL | tar -xz --strip-components=1 -C $INSTALL_DIR"
  exit 1
fi

cd "$INSTALL_DIR"
echo "  Files: $(ls -1 bot.py exchanges.py adlinks.py storage.py requirements.txt 2>/dev/null | tr '\n' ' ')"

# ── 4. venv & deps ──
step "4/6  Setting up Python environment"
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

# ── 5. config ──
step "5/6  Configuring bot"

CONFIG_FILE="$INSTALL_DIR/config.json"
NEED_SETUP=0
if [[ ! -f "$CONFIG_FILE" ]]; then NEED_SETUP=1; fi
if [[ $RECONFIGURE -eq 1 ]]; then NEED_SETUP=1; fi
# --update alone must not prompt, but  --update --reconfigure  still asks
if [[ $DO_UPDATE -eq 1 && $RECONFIGURE -eq 0 ]]; then NEED_SETUP=0; fi

# ── validators (shared by the wizards and the non-interactive path) ──
valid_token()    { [[ "$1" == *":"* && ${#1} -gt 20 ]]; }
valid_admins()   { local v="${1//[[:space:]]/}"; [[ "$v" =~ ^[0-9]+(,[0-9]+)*$ ]]; }
valid_symbol()   { [[ "$1" =~ ^[A-Za-z0-9]{2,12}$ ]]; }
valid_interval() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 5 && 10#$1 <= 86400 )); }

# ── what is configured right now (reference + backup only, NEVER a default) ──
EX_TOKEN=""; EX_ADMINS=""; EX_ASSET=""; EX_FIAT=""; EX_INTERVAL=""
read_existing_config() {
  [[ -f "$CONFIG_FILE" ]] || return 0
  local out
  out="$(python3 - "$CONFIG_FILE" <<'PY' 2>/dev/null || true
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for k in ("token", "admins", "asset", "fiat", "interval"):
    v = c.get(k, "")
    print("" if v is None else v)
PY
)"
  [[ -n "$out" ]] || return 0
  EX_TOKEN="$(sed -n '1p' <<<"$out")"
  EX_ADMINS="$(sed -n '2p' <<<"$out")"
  EX_ASSET="$(sed -n '3p' <<<"$out")"
  EX_FIAT="$(sed -n '4p' <<<"$out")"
  EX_INTERVAL="$(sed -n '5p' <<<"$out")"
}

# mask_token TOK — show enough to recognise a token without printing it whole
mask_token() {
  local t="${1:-}"
  if [[ -z "$t" ]]; then echo "(none)"
  elif (( ${#t} <= 14 )); then echo "****"
  else echo "${t:0:9}…${t: -4}"
  fi
}

backup_config() {
  [[ -f "$CONFIG_FILE" ]] || return 0
  cp -p "$CONFIG_FILE" "$CONFIG_FILE.bak" 2>/dev/null || return 0
  chmod 600 "$CONFIG_FILE.bak" 2>/dev/null || true
  info "Previous config backed up → $CONFIG_FILE.bak"
}

# write_config — the single writer for config.json (used by every path below)
write_config() {
  local tok="${ARG_TOKEN:-}" admins="${ARG_ADMINS:-}"
  local asset="${ARG_ASSET:-USDT}" fiat="${ARG_FIAT:-USD}" interval="${ARG_INTERVAL:-60}"
  [[ -n "$tok" && -n "$admins" ]] || { err "Both a bot token and admin ID(s) are required."; return 1; }
  valid_token "$tok"       || { err "Token looks invalid (should contain ':' and be longer than 20 chars)."; return 1; }
  valid_admins "$admins"   || { err "Admins should be comma-separated numeric IDs."; return 1; }
  valid_symbol "$asset"    || { err "Asset should be 2-12 letters/numbers (e.g. USDT)."; return 1; }
  valid_symbol "$fiat"     || { err "Fiat should be 2-12 letters/numbers (e.g. USD)."; return 1; }
  admins="${admins//[[:space:]]/}"
  asset="${asset^^}"; fiat="${fiat^^}"
  interval="${interval//[^0-9]/}"; [[ -z "$interval" ]] && interval=60
  valid_interval "$interval" || { err "Interval must be a whole number of seconds, 5-86400."; return 1; }

  backup_config
  cat > "$CONFIG_FILE" <<EOF
{
 "token": "$tok",
 "admins": "$admins",
 "asset": "$asset",
 "fiat": "$fiat",
 "interval": $interval
}
EOF
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
  ARG_TOKEN="$tok"; ARG_ADMINS="$admins"; ARG_ASSET="$asset"; ARG_FIAT="$fiat"; ARG_INTERVAL="$interval"
  return 0
}

# Non-interactive fast path: flags/env only (no TTY, or --non-interactive)
try_write_from_args() {
  [[ -n "${ARG_TOKEN:-}" && -n "${ARG_ADMINS:-}" ]] || return 1
  write_config || return 1
  ok "Config written from CLI/env → $CONFIG_FILE"
  echo "  asset=$ARG_ASSET fiat=$ARG_FIAT interval=${ARG_INTERVAL}s admins=$ARG_ADMINS"
}

# ── prompt helpers ──
# These set the global ANSWER instead of echoing into $(...): a command
# substitution would swallow the ❌ messages into the captured value, and would
# make Ctrl-D / a closed tty loop forever.
trim() { local v="$1"; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"; printf '%s' "$v"; }

# ask_req PROMPT VALIDATOR HINT — used by --reconfigure: no default, blank rejected
ask_req() {
  local prompt="$1" validator="$2" hint="$3"
  while true; do
    ANSWER=""
    if ! read -r -p "$prompt: " ANSWER; then
      echo ""
      err "Input ended (Ctrl-D) — aborting. $CONFIG_FILE was not modified."
      exit 1
    fi
    ANSWER="$(trim "$ANSWER")"
    if [[ -z "$ANSWER" ]]; then echo -e "${RED}  ❌ Blank is not accepted — $hint${NC}"; continue; fi
    if "$validator" "$ANSWER"; then return 0; fi
    echo -e "${RED}  ❌ Invalid — $hint${NC}"
  done
}

# ask_def PROMPT VALIDATOR HINT [DEFAULT] — used by first-time setup: Enter keeps DEFAULT
ask_def() {
  local prompt="$1" validator="$2" hint="$3" default="${4:-}"
  local shown="$prompt: "
  [[ -n "$default" ]] && shown="$prompt [$default]: "
  while true; do
    ANSWER=""
    if ! read -r -p "$shown" ANSWER; then
      echo ""
      ANSWER="$(trim "$default")"
      if [[ -n "$ANSWER" ]] && "$validator" "$ANSWER"; then
        warn "Input ended (Ctrl-D) — keeping default: $ANSWER"; return 0
      fi
      err "Input ended (Ctrl-D) and there is no usable default — aborting."
      exit 1
    fi
    ANSWER="$(trim "$ANSWER")"
    [[ -z "$ANSWER" ]] && ANSWER="$(trim "$default")"
    if [[ -z "$ANSWER" ]]; then echo -e "${RED}  ❌ Blank is not accepted — $hint${NC}"; continue; fi
    if "$validator" "$ANSWER"; then return 0; fi
    echo -e "${RED}  ❌ Invalid — $hint${NC}"
  done
}

# report_changes — old → new diff shown after a reconfigure
report_changes() {
  [[ -n "$EX_TOKEN$EX_ADMINS" ]] || return 0
  echo -e "  ${BOLD}Changes:${NC}"
  if [[ "$EX_TOKEN" != "$ARG_TOKEN" ]]; then
    echo -e "    token     $(mask_token "$EX_TOKEN") → $(mask_token "$ARG_TOKEN")"
  else echo -e "    ${DIM}token     unchanged ($(mask_token "$ARG_TOKEN"))${NC}"; fi
  if [[ "$EX_ADMINS" != "$ARG_ADMINS" ]]; then
    echo -e "    admins    ${EX_ADMINS} → ${ARG_ADMINS}"
  else echo -e "    ${DIM}admins    unchanged (${ARG_ADMINS})${NC}"; fi
  if [[ "$EX_ASSET" != "$ARG_ASSET" || "$EX_FIAT" != "$ARG_FIAT" ]]; then
    echo -e "    pair      ${EX_ASSET}/${EX_FIAT} → ${ARG_ASSET}/${ARG_FIAT}"
  else echo -e "    ${DIM}pair      unchanged (${ARG_ASSET}/${ARG_FIAT})${NC}"; fi
  if [[ "$EX_INTERVAL" != "$ARG_INTERVAL" ]]; then
    echo -e "    interval  ${EX_INTERVAL}s → ${ARG_INTERVAL}s"
  else echo -e "    ${DIM}interval  unchanged (${ARG_INTERVAL}s)${NC}"; fi
}

read_existing_config

if [[ $NEED_SETUP -eq 1 ]]; then
  if [[ $RECONFIGURE -eq 1 && -t 0 && $NON_INTERACTIVE -eq 0 ]]; then
    # ── reconfigure: always ask, always blank (env/CLI values are NOT defaults) ──
    echo ""
    echo -e "${BOLD}🔧 Reconfigure — all 5 questions are asked again${NC}"
    if [[ -n "$EX_TOKEN$EX_ADMINS" ]]; then
      echo -e "${DIM}  Current → token=$(mask_token "$EX_TOKEN")  admins=${EX_ADMINS:-(none)}  pair=${EX_ASSET:-?}/${EX_FIAT:-?}  interval=${EX_INTERVAL:-?}s${NC}"
    else
      warn "No readable $CONFIG_FILE yet — answering these questions will create one."
    fi
    echo -e "${DIM}  Nothing is pre-filled: type every value again — blank answers are rejected.${NC}"
    echo -e "${DIM}  Get bot token from @BotFather on Telegram → /newbot${NC}"
    echo -e "${DIM}  Get your user ID from @userinfobot on Telegram${NC}"
    echo ""
    ask_req "Bot token from @BotFather" valid_token "must look like 123456:ABC-... (contains ':' and is 20+ chars)"; ARG_TOKEN="$ANSWER"
    ask_req "Your Telegram user ID(s), comma-separated" valid_admins "numeric IDs, e.g. 123456 or 123456,789012"; ARG_ADMINS="$ANSWER"
    ask_req "Asset (e.g. USDT)" valid_symbol "2-12 letters/numbers, e.g. USDT"; ARG_ASSET="$ANSWER"
    ask_req "Fiat currency (e.g. USD)" valid_symbol "2-12 letters/numbers, e.g. USD"; ARG_FIAT="$ANSWER"
    ask_req "Check prices every N seconds (5-86400)" valid_interval "a whole number of seconds between 5 and 86400"; ARG_INTERVAL="$ANSWER"
    write_config || { err "Could not save configuration — nothing was changed."; exit 1; }
    ok "Saved to $CONFIG_FILE"
    report_changes
  elif try_write_from_args; then
    :
  elif [[ ! -t 0 || $NON_INTERACTIVE -eq 1 ]]; then
    err "No terminal to ask on, and no complete --token/--admins given."
    echo ""
    echo "  Provide them via flags or env:"
    echo "    curl -fsSL $RAW_URL/install.sh | bash -s -- --token 123:ABC --admins 123456"
    echo "    wget -qO-  $RAW_URL/install.sh | bash -s -- --token 123:ABC --admins 123456"
    echo "    BOT_TOKEN=123:ABC ADMIN_IDS=123456 bash install.sh"
    echo ""
    echo "  Or run interactively on the VPS:"
    echo "    bash install.sh"
    echo "    sudo bash install.sh --reconfigure   # asks all 5 questions again"
    exit 1
  else
    # ── first-time interactive wizard (flag/env values act as defaults) ──
    echo ""
    echo -e "${BOLD}🤖 First-time setup — you will be asked 5 questions${NC}"
    echo -e "${DIM}  Get bot token from @BotFather on Telegram → /newbot${NC}"
    echo -e "${DIM}  Get your user ID from @userinfobot on Telegram${NC}"
    echo ""
    ask_def "Bot token from @BotFather" valid_token "must look like 123456:ABC-... (contains ':' and is 20+ chars)" "${ARG_TOKEN:-}"; ARG_TOKEN="$ANSWER"
    ask_def "Your Telegram user ID(s), comma-separated" valid_admins "numeric IDs, e.g. 123456 or 123456,789012" "${ARG_ADMINS:-}"; ARG_ADMINS="$ANSWER"
    ask_def "Asset" valid_symbol "2-12 letters/numbers, e.g. USDT" "${ARG_ASSET:-USDT}"; ARG_ASSET="$ANSWER"
    ask_def "Fiat currency" valid_symbol "2-12 letters/numbers, e.g. USD" "${ARG_FIAT:-USD}"; ARG_FIAT="$ANSWER"
    ask_def "Check prices every N seconds" valid_interval "a whole number of seconds between 5 and 86400" "${ARG_INTERVAL:-60}"; ARG_INTERVAL="$ANSWER"
    write_config || { err "Could not save configuration."; exit 1; }
    ok "Saved to $CONFIG_FILE"
  fi
else
  ok "Config exists — keeping $CONFIG_FILE (use --reconfigure to change)"
  cat "$CONFIG_FILE" | python3 -m json.tool 2>/dev/null | sed 's/"token": ".*"/"token": "***"/' || cat "$CONFIG_FILE"
fi

# ensure data.json exists (seeded with the same defaults bot.py uses —
# see DEFAULT_SETTINGS in bot.py so the two never drift apart)
if [[ ! -f "$INSTALL_DIR/data.json" ]]; then
  cat > "$INSTALL_DIR/data.json" <<'EOF'
{"group": null, "auto": false, "merchants": {}, "last": {}, "settings": {"show_liquidity": false, "show_buttons": true, "custom_header": "", "custom_body": "", "custom_footer": "", "auto_delete": true, "delete_after_hours": 24, "delete_join_left": true, "cleanup_enabled": true, "cleanup_service": false, "cleanup_links": false, "cleanup_media": false, "cleanup_forwards": false, "cleanup_commands": false, "cleanup_strict": false, "cleanup_notify": "off", "buttons_order": "buy_sell", "btn_buy_label": "", "btn_sell_label": "", "btn_buy_url": "", "btn_sell_url": "", "btn_link_mode": "ad", "ad_link_templates": {}, "price_links": true}, "last_msg_id": null, "last_msg_time": null}
EOF
fi
chmod 600 "$CONFIG_FILE" 2>/dev/null || true
chmod 600 "$INSTALL_DIR/data.json" 2>/dev/null || true

# chown to correct user if installed as root to /opt
if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  chown -R "$SUDO_USER:$SUDO_USER" "$INSTALL_DIR" 2>/dev/null || true
  REAL_USER="$SUDO_USER"
  REAL_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
  REAL_USER="$(whoami)"
  REAL_HOME="$HOME"
fi
# if install dir is /opt/p2p-bot but user is ubuntu, keep ownership to that user
if [[ "$INSTALL_DIR" == "/opt/p2p-bot" ]]; then
  # determine owner: SUDO_USER or current
  OWNER="${SUDO_USER:-$(whoami)}"
  $SUDO chown -R "$OWNER:$OWNER" "$INSTALL_DIR" 2>/dev/null || true
  REAL_USER="$OWNER"
fi

# ── 6. systemd service ──
step "6/6  Installing service"

if is_systemd; then
  echo "  Creating $SERVICE_FILE ..."
  # detect python binary
  PYBIN="$INSTALL_DIR/venv/bin/python"
  if [[ ! -x "$PYBIN" ]]; then PYBIN="$(command -v python3)"; fi

  $SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=P2P Merchant Price Bot — Binance/Bybit/OKX/Bitget
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYBIN $INSTALL_DIR/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# Optional: load env file if you prefer env vars over config.json
# EnvironmentFile=-$INSTALL_DIR/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=p2p-bot

# Hardening (uncomment if needed — keep permissive for VPS simplicity)
# NoNewPrivileges=yes
# PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

  # restart or start
  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Restarting $SERVICE_NAME ..."
    $SUDO systemctl restart "$SERVICE_NAME"
  else
    info "Starting $SERVICE_NAME ..."
    $SUDO systemctl start "$SERVICE_NAME"
  fi

  sleep 3
  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service is running (systemd)"
  else
    # Check if it's restarting (RestartSec=5 → might be inactive briefly)
    sleep 2
    if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
      ok "Service is running (systemd) — after restart delay"
    else
      warn "Service not active yet — check logs (token/network?):"
      $SUDO journalctl -u "$SERVICE_NAME" -n 40 --no-pager 2>/dev/null | tail -n 40 || $SUDO systemctl status "$SERVICE_NAME" --no-pager 2>/dev/null | tail -n 40 || true
      echo ""
      warn "Bot is installed and will keep retrying (Restart=always)."
      warn "Check: sudo journalctl -u $SERVICE_NAME -f"
      warn "Or run manually: $INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py"
      warn "Common causes: invalid token, no internet, or Telegram blocked"
      # don't exit 1 — let install succeed so user can fix token and restart
    fi
  fi

else
  # fallback: nohup
  warn "systemd unavailable — starting with nohup fallback..."
  PYBIN="$INSTALL_DIR/venv/bin/python"
  # kill old
  pkill -f "$INSTALL_DIR/bot.py" 2>/dev/null || true
  nohup "$PYBIN" "$INSTALL_DIR/bot.py" > "$INSTALL_DIR/bot.log" 2>&1 &
  echo $! > "$INSTALL_DIR/bot.pid"
  # add @reboot cron
  (crontab -l 2>/dev/null | grep -v "$INSTALL_DIR/bot.py"; echo "@reboot $PYBIN $INSTALL_DIR/bot.py >> $INSTALL_DIR/bot.log 2>&1") | crontab -
  ok "Started via nohup (PID $(cat "$INSTALL_DIR/bot.pid")) — logs: $INSTALL_DIR/bot.log"
  info "Reboot persistence via cron @reboot"
fi

# ── done ──
echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  🎉 Installed! Bot is running.${NC}"
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BOLD}Install dir:${NC} $INSTALL_DIR"
echo -e "  ${BOLD}Service:${NC}     $SERVICE_NAME  (systemctl)"
echo -e "  ${BOLD}Config:${NC}      $CONFIG_FILE"
echo -e "  ${BOLD}Data:${NC}        $INSTALL_DIR/data.json"
echo ""
echo -e "  ${BOLD}Next steps on Telegram:${NC}"
echo -e "   1. Open your bot → send ${CYAN}/start${NC} (in private chat)"
echo -e "   2. Tap ${CYAN}👥 Set group${NC} → pick your group (the bot joins & registers it)"
echo -e "      …or add the bot to the group and send ${CYAN}/setgroup${NC} there"
echo -e "   3. Back in private chat → ${CYAN}paste a merchant URL${NC} to add it"
echo -e "      Binance / Bybit / OKX / Bitget public merchant links supported"
echo -e "   4. Tap ${CYAN}🟢 Auto: ON${NC} — prices post automatically whenever they change"
echo ""
echo -e "  ${BOLD}Panel buttons:${NC}"
echo -e "   ${CYAN}📊 Post now${NC} · ${CYAN}🟢 Auto ON/OFF${NC} · ${CYAN}📋 Merchants${NC} · ${CYAN}👥 Set group${NC}"
echo -e "   ${CYAN}⚙️ Settings${NC} · ${CYAN}📝 Custom Msg${NC} · ${CYAN}💧 Liquidity${NC} · ${CYAN}🔘 Buy/Sell buttons${NC}"
echo -e "   ${CYAN}👁 Preview${NC} · ${CYAN}🔄 Refresh${NC}"
echo ""
echo -e "  ${BOLD}Extras:${NC}"
echo -e "   · ${CYAN}📝 Custom Msg${NC} customizes the whole post — header, body (per merchant), footer"
echo -e "   · ${CYAN}🧹 Group cleanup${NC} (in ⚙️ Settings, or /cleanup) removes \"X joined/left the group\""
echo -e "     notices and the useless messages: service notices, 🔗 links, 🖼 media,"
echo -e "     ↩️ forwards, ⌨️ commands — or 🔇 strict, where members post nothing at all"
echo -e "     — the bot's own posts, the admins' and a member's 🛡 answer are never removed"
echo -e "     — the bot must be a group admin with the ${DIM}Delete messages${NC} permission"
echo -e "   · ⚙️ Settings also has auto-delete timers, liquidity and Buy/Sell buttons"
echo ""
echo -e "  ${BOLD}Useful commands:${NC}"
if is_systemd; then
echo -e "   ${DIM}sudo systemctl status $SERVICE_NAME${NC}     — check status"
echo -e "   ${DIM}sudo journalctl -u $SERVICE_NAME -f${NC}      — live logs"
echo -e "   ${DIM}sudo systemctl restart $SERVICE_NAME${NC}     — restart"
echo -e "   ${DIM}sudo systemctl stop $SERVICE_NAME${NC}        — stop"
fi
echo -e "   ${DIM}sudo bash $INSTALL_DIR/install.sh --reconfigure${NC}  — re-ask all 5 setup questions"
echo -e "   ${DIM}sudo bash $INSTALL_DIR/install.sh --update${NC}       — update to latest"
echo -e "   ${DIM}sudo bash $INSTALL_DIR/install.sh --uninstall${NC}    — uninstall (asks what to keep)"
echo ""
if is_systemd; then
  echo -e "${DIM}── last 15 log lines ──${NC}"
  $SUDO journalctl -u "$SERVICE_NAME" -n 15 --no-pager 2>/dev/null | tail -n 15 || cat "$INSTALL_DIR/bot.log" 2>/dev/null | tail -n 15 || true
fi
echo ""
