## ⚡ Quick Install (copy/paste)

```bash
# with curl (recommended: -f aborts instead of saving an error page)
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh -o install.sh
bash install.sh

# with wget
wget -q https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh -O install.sh
bash install.sh
```


```bash
wget https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/uninstall.sh

bash uninstall.sh
```

Or run it without saving anything to disk:

```bash
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
# …or: wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
```

> ⚠️ **Download scripts from `raw.githubusercontent.com`, never from `github.com/…/blob/…`.**
> A `blob` URL is GitHub's HTML *viewer page*, so `wget …/blob/main/install.sh` stores ~700 KB of
> HTML in a file called `install.sh` — and running that fails with
> ``line 7: syntax error near unexpected token `newline'`` / `` `<!DOCTYPE html>'``.
> The same page comes back for a **renamed repository**, because `raw.githubusercontent.com`
> does not follow renames. See [Troubleshooting](#-troubleshooting).

## ⚡ Quick Uninstall (copy/paste)

```bash
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/uninstall.sh | bash
# …or: wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/uninstall.sh | bash
# …or, from a checkout:
bash uninstall.sh
```

The uninstaller **asks what to remove**:

| Choice | What happens |
|---|---|
| **1) Erase EVERYTHING** | Removes everything `install.sh` created: systemd service, bot process, cron entry, the whole install directory — **including `config.json` and `data.json`** |
| **2) Keep my data** | Same, but `config.json` + `data.json` (+ `.env`) are saved to a `~/p2p-bot-backup-<date>/` folder first, so a later reinstall starts where you left off |
| **3) Cancel** | Removes nothing |

Non-interactive: `bash uninstall.sh --full` (erase everything), `bash uninstall.sh --keep-data` (save the json data), add `--yes` to skip the confirmation. Without a terminal it always keeps the data. apt packages (`python3`, `git`, `curl`, …) are never removed — other software may need them.


# 🤖 P2P Merchant Price Bot

Telegram bot that watches your favourite **P2P merchants** on **Binance · Bybit · OKX · Bitget** and posts their best **sell** / **buy** prices to your Telegram group — automatically, every time the price changes.

---

## ⚡ One-Click Install

Pick the installer that matches your machine — all four ask for your **bot token** (from [@BotFather](https://t.me/BotFather) → `/newbot`) and your **Telegram ID** (from [@userinfobot](https://t.me/userinfobot)), then start the bot.

| Installer | Best for | What it does |
|---|---|---|
| `install.sh` | Ubuntu/Debian **VPS with systemd** | venv + systemd service (auto-restart & reboot-safe) |
| `install-docker.sh` | Any machine **with Docker**, incl. macOS | Docker Compose container (`restart: unless-stopped`) |
| `install-local.sh` | **macOS / Linux without systemd / WSL** | venv + nohup + launchd (macOS) or cron `@reboot` autostart |
| **python3 one-liner** | **Any machine with Python 3** (no curl / wget needed) | Downloads + runs `install-local.sh` in one command |
| **Vercel** — [Option E](#option-e--vercel-serverless-nothing-to-keep-running) | **Serverless hosting, nothing to keep running** (free plan) | Webhook + Vercel Cron (`api/`, `vercel.json`), state in a KV/Redis store |

> **curl or wget — your choice.** Every one-liner below is shown with both `curl` and `wget`; they are interchangeable. Inside the scripts the same applies: downloads automatically use **curl → wget → python3**, whichever exists on the box, and `git` is optional (a tarball is fetched instead when git is missing). Force a specific tool with `DOWNLOADER=wget`.

### Option A — VPS with systemd (recommended)

Paste this on a fresh Ubuntu VPS (20.04 / 22.04 / 24.04):

```bash
# with curl
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash

# with wget
wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
```

### Option B — Docker (macOS, Windows, any Linux)

```bash
# with curl
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-docker.sh | bash

# with wget
wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-docker.sh | bash
```

The script checks/installs Docker, creates `config.json` + `.env`, and runs `docker compose up -d --build`.

### Option C — Local / no systemd (laptops, WSL, shared hosting)

```bash
# with curl
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-local.sh | bash

# with wget
wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-local.sh | bash
```

### Option D — Python 3 (no curl / no wget)

One-click install with nothing but **Python 3** installed. It downloads `install-local.sh` and runs it:

```bash
python3 -c "import urllib.request as u;print(u.urlopen('https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-local.sh').read().decode())" | bash
```

> This is the same local / no-systemd install as **Option C**, just launched by Python instead of `curl` or `wget`.

### Option E — Vercel (serverless, nothing to keep running)

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot&env=BOT_TOKEN,ADMIN_IDS,KV_REST_API_URL,KV_REST_API_TOKEN&envDescription=BOT_TOKEN%20and%20ADMIN_IDS%20are%20required%3B%20KV_REST_API_URL%2FTOKEN%20come%20from%20the%20KV%20or%20Upstash%20integration&project-name=p2p-price-bot)

Vercel has no long-running process, so there the bot runs in **webhook mode**:
Telegram pushes every update to `/api/webhook` and a **Vercel Cron** calls
`/api/tick` to post the prices — the same bot, the same panel, the same handlers.

1. **Import** this repository into Vercel ([deploy button](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot) or `npx vercel`).
2. **Add a KV/Redis store** (Vercel → Storage → *Upstash for Redis* → connect to the project) and the **environment variables** `BOT_TOKEN` + `ADMIN_IDS`.
3. **Deploy**, then open `https://<your-app>.vercel.app/api/webhook` — that page registers the Telegram webhook and shows the status.
4. In Telegram: `/start` → **👥 Set group** → paste a merchant URL → **🟢 Auto: ON**.

> ⏰ **Free (Hobby) plan**: crons may only run **once a day** — a faster schedule
> fails the deployment. On **Pro** set `"schedule": "* * * * *"` in
> [`vercel.json`](vercel.json) for live prices, or keep the free plan and let an
> external scheduler call `/api/tick` (see [VERCEL.md](VERCEL.md#5-the-cron--how-often-prices-are-checked)).

Full walkthrough, environment variables, endpoints and troubleshooting:
**[VERCEL.md](VERCEL.md)**.

<details>
<summary>No curl and no wget? (python3 / PowerShell / manual)</summary>

**python3 (any Linux/macOS with Python 3):** use **Option D** above — one command, no curl/wget needed.

**Windows PowerShell** (then run it with WSL or Git Bash):

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install-local.sh -OutFile install-local.sh
bash install-local.sh
```

**Fully manual — download the archive, no git needed:**

```bash
mkdir -p ~/exchange && wget -qO- https://codeload.github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/tar.gz/refs/heads/main | tar -xz --strip-components=1 -C ~/exchange
cd ~/exchange && bash install-local.sh        # or: sudo bash install.sh
```

(With curl instead of wget: `curl -fsSL https://codeload.github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/tar.gz/refs/heads/main | tar -xz --strip-components=1 -C ~/exchange`)
</details>

<details>
<summary>No prompts (for automation) — any installer</summary>

```bash
# curl
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash -s -- \
  --token "123456:ABC-your-token" --admins "123456789" --asset USDT --fiat USD --interval 60

# wget
wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash -s -- \
  --token "123456:ABC-your-token" --admins "123456789" --asset USDT --fiat USD --interval 60
```
</details>

<details>
<summary>Docker without the installer</summary>

```bash
git clone https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot.git && cd OKX_Telegram_P2P_Price_Bot
cp .env.example .env && nano .env      # BOT_TOKEN + ADMIN_IDS
docker compose up -d --build
```
</details>

---

## 📱 Setup in Telegram (1 minute)

1. Open your bot → `/start`
2. Tap **👥 Set group** → pick your group → done. The bot joins and registers the group automatically.
3. **Paste a merchant URL** into the bot chat to add it:
   - `https://p2p.binance.com/en/advertiserDetail?advertiserNo=…`
   - `https://www.bybit.com/en/fiat/trade/otc/profile/…`
   - `https://www.okx.com/p2p/market?publicUserId=…`
   - `https://www.bitget.com/p2p/merchant/…`
4. Tap **🟢 Auto: ON** — prices are posted whenever they change.

### Panel buttons

| Button | What it does |
|---|---|
| 📊 **Post prices now** | Posts all merchant prices to the group immediately |
| 🟢/🔴 **Auto** | Toggle automatic posting on price change |
| 📋 **Merchants** | List merchants — tap one to remove |
| 👥 **Set group** | One click: choose the group that receives updates |
| ⚙️ **Settings** | Liquidity, Buy/Sell buttons, auto-delete timers, **join/left cleanup** |
| 🟢🔴 **Buy/Sell buttons** | Swap Buy/Sell order and edit their labels + links |
| 📝 **Custom Msg** | Customize the **full** post: header, body (per-merchant template), footer |
| 👁 **Preview** | See exactly how the group post will look |
| 🔄 **Refresh** | Refresh the panel |

### 📝 Custom message — header, body & footer

Tap **📝 Custom Msg** (or ⚙️ Settings → Edit) to fully customize the group post:

- **Header** — shown once on top (default: `📊 P2P {ASSET}/{FIAT}`).
- **Body** — a template repeated **once per merchant**. Leave it default or write your own.
- **Footer** — shown once at the bottom (default: none).

**Body placeholders** (also `{ASSET}`, `{FIAT}`, `{PAIR}` work everywhere):

| Placeholder | Replaced with |
|---|---|
| `{ICON}` | Exchange emoji (🟡 🟣 ⚫ 🔵) |
| `{EXCHANGE}` | Exchange name, e.g. `Binance` |
| `{NICK}` | Merchant nickname |
| `{LINK}` | Clickable merchant name (`<a>` to the profile) |
| `{URL}` | Raw merchant profile URL |
| `{SELL}` / `{BUY}` | Best sell / buy price |
| `{SELL_AMOUNT}` / `{BUY_AMOUNT}` | Available liquidity (if the merchant has ads) |
| `{SELL_URL}` / `{BUY_URL}` | Link to the exact ad behind that price (profile/market fallback) |
| `{SELL_LINK}` / `{BUY_LINK}` | The price itself as a clickable link |
| `{SELL_AD_ID}` / `{BUY_AD_ID}` | The ad ids the prices came from |
| `{ERROR}` | Fetch error text, if any |

HTML (`<b>`, `<i>`, `<code>`, `<a href>`) and new lines are supported. Example 3-line body:

```
{ICON} <b>{EXCHANGE}</b> · {LINK}
🔴 Sell: <b>{SELL}</b> 💧 {SELL_AMOUNT} {ASSET}
🟢 Buy: <b>{BUY}</b> 💧 {BUY_AMOUNT} {ASSET}
```

Use **👁 Preview** to check the result before it goes to the group.

### 🟢🔴 Buy / Sell buttons — order, labels & links

Under every group post the bot shows one row of inline buttons per merchant. By default it is
**🟢 BUY on the left · 🔴 SELL on the right** — and everything about them is editable from the
**private chat with the bot**: tap **🟢🔴 Buy/Sell buttons** on the panel (or ⚙️ Settings →
**🟢🔴 Edit Buy/Sell buttons**).

| Menu item | What it does |
|---|---|
| 🔘 **Buttons: ON/OFF** | Show or hide the buttons in the group post |
| 🔄 **Order** | Switch between `🟢 Buy ⬅️ \| Sell ➡️ 🔴` and `🔴 Sell ⬅️ \| Buy ➡️ 🟢` (the price lines in the text follow the same order) |
| 🟢 **Edit BUY label** | Send your own caption for the Buy button |
| 🔴 **Edit SELL label** | Send your own caption for the Sell button |
| 🎯 **Target** | Switch between **the exact ad** of the shown price (default) and the merchant profile page |
| 🔗 **BUY / SELL link** | Optional custom URL — overrides the target for that side (`{AD_URL}`, `{AD_ID}`, `{PRICE}`, `{URL}`, `{NICK}`, … available) |
| 🔗 **Ad link templates** | Edit the deep-link template of each exchange (Binance / Bybit / OKX / Bitget) |
| ♻️ **Reset buttons to default** | Back to the defaults below |

Defaults:

```
🟢 BUY {PRICE} {NICK}      🔴 SELL {PRICE} {NICK}
```

**Label placeholders**

| Placeholder | Replaced with |
|---|---|
| `{PRICE}` | Best price for that side |
| `{NICK}` / `{FULLNICK}` | Merchant nickname (max 14 chars) / full nickname |
| `{EXCHANGE}` / `{ICON}` | Exchange name / emoji (🟡 🟣 ⚫ 🔵) |
| `{AMOUNT}` | Available liquidity for that side |
| `{ASSET}` / `{FIAT}` / `{PAIR}` | e.g. `USDT`, `USD`, `USDT/USD` |
| `{SIDE}` | `BUY` or `SELL` |

Telegram limits a button caption to ~64 characters — if your template renders longer, the bot drops
the nickname automatically and truncates as a last resort. Link placeholders: `{AD_URL}` (the
exact ad), `{AD_ID}`, `{URL}` (merchant profile), `{PRICE}`, `{NICK}`, `{EXCHANGE}`, `{ASSET}`,
`{FIAT}`, `{SIDE}`. Send `default` while editing to restore the
default label/link, or `/cancel` to abort. Use **👁 Preview** to see the real buttons before posting.

### 🎯 Buy/Sell buttons that open the **exact ad**

Every price in the post comes from one specific ad: the cheapest **SELL** ad and the highest
**BUY** ad of that merchant. The bot remembers those ad ids and turns the buttons (and,
optionally, the prices in the text) into links that open **that very ad**, not just the
merchant's profile.

```
📊 P2P USDT/USD

⚫ Okx · Fast_sonic
   🟢 Best BUY  (you sell): 1.001          ← button → that exact BUY ad
   🔴 Best SELL (you buy):  0.999          ← button → that exact SELL ad
[🟢 BUY 1.001 Fast_sonic] [🔴 SELL 0.999 Fast_sonic]
```

* ⚙️ **Settings → 🎯 Exact ad links** switches the buttons (and prices) between
  **the exact ad** and the **merchant profile page**. Default: exact ad.
* ⚙️ **Settings → 🔗 Link prices** makes the prices inside the post clickable too.
* ⚙️ **Settings → 🔗 Ad link templates** — one template per exchange, editable from Telegram
  (or via the `AD_LINK_TEMPLATES` env var). Placeholders: `{AD_ID}` `{ASSET}` `{ASSET_LOWER}`
  `{FIAT}` `{FIAT_LOWER}` `{SIDE}` `{TAKER_SIDE}` `{ACTION_TYPE}` `{URL}` `{NICK}`.

Built-in templates and how precise they are:

| Exchange | Template | Opens |
|---|---|---|
| 🟡 **Binance** | `c2c.binance.com/en/adv?code={AD_ID}` | **the exact ad** (Binance's documented ad link) |
| ⚫ **OKX** | `okx.com/p2p-markets/{FIAT}/{TAKER_SIDE}-{ASSET}?adId={AD_ID}` | the right market/side/pair + ad hint |
| 🟣 **Bybit** | `bybit.com/en/p2p/{TAKER_SIDE}/{ASSET}/{FIAT}?actionType=…&adId={AD_ID}` | the right market/side/pair + ad hint |
| 🔵 **Bitget** | `bitget.com/p2p-trade?fiatName={FIAT}&coinName={ASSET}&advId={AD_ID}` | the right market/pair + ad hint |

The exchanges that do not document an ad-level parameter simply ignore the extra `adId` / `advId`
hint, so the link still lands on the correct side and pair — and you can paste your own working
template in **🔗 Ad link templates** at any time (no code change, no redeploy). Bybit's own share
links expire after 30 minutes, which is why its default template points at the market page.

### 🚪 Auto-delete “joined / left the group” messages

The bot deletes Telegram’s **“X joined the group”** and **“X left the group”** service messages in your group — including when people join after being **accepted via a join request** — so your price feed stays clean.

- Toggle in ⚙️ **Settings → 🚪 Del Join/Left msgs** (ON by default).
- ⚠️ The bot must be a **group admin** with the **Delete messages** permission, otherwise it can't remove those messages.

---

## 🧯 Troubleshooting

### ``syntax error near unexpected token `newline'`` / `` `<!DOCTYPE html>' ``

```
./install.sh: line 7: syntax error near unexpected token `newline'
./install.sh: line 7:
`<!DOCTYPE html>'
```

Your `install.sh` is not a shell script at all — it is a saved **GitHub web page** (the file is
~700 KB of HTML, and `<!DOCTYPE html>` lands on line 7). That happens when:

| Cause | Why it breaks |
|---|---|
| `wget https://github.com/OWNER/REPO/blob/main/install.sh` | `/blob/main/…` is GitHub's HTML *viewer* page, not the file — use `raw.githubusercontent.com` (or add `?raw=true`) |
| the repository was **renamed** | `github.com` redirects, but `raw.githubusercontent.com` answers with a 404 page |
| a proxy / login wall returned a page | any HTML body looks like this once bash parses it |

Confirm it in one second:

```bash
head -n 1 install.sh    # correct: #!/usr/bin/env bash      · broken: blank / <!DOCTYPE html>
file install.sh         # "HTML document text" = wrong file, "shell script text" = fine
```

Then re-download it properly — `curl -f` (or `wget -q`) aborts instead of saving an error page:

```bash
rm -f install.sh uninstall.sh
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
# …or: wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh | bash
```

If you want the file on disk first (so you can read it before running it), keep the raw URL and
run it with `bash`, not `./` — then no `chmod` is needed either:

```bash
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/install.sh -o install.sh
bash install.sh
```

All three installers now verify every download: an HTML/error body is rejected and deleted with
an explanation instead of being written into the install directory.

### `Could not fetch the bot source into …`

`git clone` and the archive download both failed — usually no network or no `git`/`curl`/`wget`.
Install one of them, or point the scripts at a fork/renamed repo without editing them:

```bash
P2P_REPO_SLUG=your-name/your-repo bash install.sh
```

---

## 🧪 Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The suite covers the ad-link templates, the Buy/Sell button targets, clickable prices and the
state backends — no Telegram calls are made.

---

## 🛠️ Manage

**systemd (Option A):**

```bash
sudo systemctl status p2p-bot     # is it running?
sudo journalctl -u p2p-bot -f     # live logs
sudo systemctl restart p2p-bot    # restart
sudo bash install.sh --reconfigure   # change token / pair / interval
sudo bash install.sh --update        # pull latest + restart
```

**Docker (Option B):**

```bash
docker compose ps             # status
docker compose logs -f        # live logs
docker compose restart        # restart
bash install-docker.sh --reconfigure   # change token / pair / interval
bash install-docker.sh --update        # pull latest + rebuild + restart
bash install-docker.sh --down          # stop container (keep data)
```

**Local / no systemd (Option C):**

```bash
tail -f ~/exchange-local/bot.log   # live logs (or $INSTALL_DIR/bot.log)
bash install-local.sh --stop       # stop (start again by re-running install-local.sh)
bash install-local.sh --reconfigure  # change token / pair / interval
bash install-local.sh --update        # pull latest + restart
bash install-local.sh --uninstall     # stop + remove autostart (keep data)
```

**Vercel (Option E):**

```bash
npx vercel logs <deployment-url>            # live logs
npx vercel --prod                           # redeploy (after changing env vars)
curl -s https://<your-app>.vercel.app/api/webhook          # status page + (re)register the webhook
curl -s -H "Authorization: Bearer $CRON_SECRET" \
     https://<your-app>.vercel.app/api/tick                # run the cron job by hand
```

Prices are checked whenever the cron in `vercel.json` runs (once a day on the free
plan, every minute on Pro) — details in [VERCEL.md](VERCEL.md).

**Uninstall (asks: erase everything or keep your data):**

```bash
# systemd install — asks 1) erase EVERYTHING  2) keep config.json + data.json  3) cancel
curl -fsSL https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/uninstall.sh | bash
# …or the same with wget
wget -qO- https://raw.githubusercontent.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot/main/uninstall.sh | bash
# …or non-interactive:
bash uninstall.sh --full          # erase everything (incl. config.json + data.json)
bash uninstall.sh --keep-data     # erase everything but save the json data first
# docker install
bash install-docker.sh --down
# local install
bash install-local.sh --uninstall
```

With **2) Keep my data** the json files are copied to `~/p2p-bot-backup-<date>/` before the install directory is removed — reinstall later and drop them back in:

```bash
cp ~/p2p-bot-backup-*/config.json ~/p2p-bot-backup-*/data.json <install-dir>/
```

---

## 📁 Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot (panel, buttons, auto-poster) |
| `exchanges.py` | Binance / Bybit / OKX / Bitget adapters + URL parser |
| `adlinks.py` | Exact-ad deep-link templates (Binance / Bybit / OKX / Bitget) |
| `storage.py` | State backends: `data.json` file, optional Upstash/Redis REST, read-only fallback |
| `serverless.py` | Serverless mode: ASGI glue, webhook + cron helpers, one PTB app per warm container |
| `api/webhook.py` | Vercel function: Telegram updates (`POST`) + status page (`GET /api/webhook`) |
| `api/tick.py` | Vercel function: the cron job that replaces the JobQueue (`/api/tick`) |
| `vercel.json` | Vercel config: function limits + the `/api/tick` cron schedule |
| `VERCEL.md` | Step-by-step Vercel deployment guide |
| `tests/` | pytest suite (links, buttons, storage, serverless/webhook mode) |
| `.github/workflows/` | CI (tests) |
| `install.sh` | One-click installer — systemd VPS |
| `install-docker.sh` | One-click installer — Docker Compose |
| `install-local.sh` | One-click installer — macOS / no systemd |
| `uninstall.sh` | Interactive uninstaller — asks: erase everything or keep your data |
| `Dockerfile` / `docker-compose.yml` | Docker alternative |
| `config.json` | Auto-created: token, admins, pair, interval |
| `data.json` | Auto-created: group, merchants, last prices |

All three installers download what they need with **curl, wget or python3** (first one found — override with `DOWNLOADER=wget`), and fall back to the GitHub tarball when `git` is not installed.

## 📄 License

MIT
