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

### Choose the state database

Set `P2P_STATE_BACKEND` in `.env` (or as an environment variable) to choose where
**groups, merchants, settings and last prices** are stored:

| Value | Behaviour | Best for |
|---|---|---|
| `auto` (default) | Uses Redis/KV when a complete REST URL + token is configured; otherwise uses `data.json`. | Most installs; keeps backwards-compatible automatic selection. |
| `file` | Always uses the local `data.json`, even if Redis credentials exist. | A single VPS/Docker bot that must stay local. |
| `redis` | Requires a Redis-compatible REST store (Vercel KV or Upstash). It does **not** fall back to a separate file if credentials are missing. | Vercel, multiple bot instances, or shared durable state. |

```env
# Local / Docker JSON file
P2P_STATE_BACKEND=file
P2P_DATA_DIR=/var/lib/p2p-bot

# Or a shared Vercel KV / Upstash Redis store
P2P_STATE_BACKEND=redis
KV_REST_API_URL=https://your-store.upstash.io
KV_REST_API_TOKEN=your-token
```

The setup page has the same **State database** selector. From a terminal use
`python setup_cli.py --state-backend file` or
`python setup_cli.py --state-backend redis --kv-url https://… --kv-token …`.
`P2P_STORAGE_BACKEND` is accepted as an alias. On Vercel select `redis`: file
state is temporary and is intentionally rejected as a persistent setup.

#### 🔌 Connect database link

When no shared database is connected, every surface says so **and carries the
link that fixes it** (Vercel → **Storage**, where *Connect to this project*
writes `KV_REST_API_URL` + `KV_REST_API_TOKEN`):

| Where | What you get |
|---|---|
| Telegram panel | a **🗄 Database** line in the status text and — while no store is connected — a **🔌 Connect database ↗** button (it disappears once one is connected) |
| `/database` (alias `/db`) | the state store in use, the connection steps and the same link, plus **🔄 Check connection** |
| ⚙️ Settings | a **🗄 Database** button showing `connected ✅` / `connect ⚠️` |
| `/api/setup` and the status page | a **🔌 Connect database ↗** button above the database panel / next to the *not persistent* warning; scripts reading `/api/webhook` JSON get the URL as `connect_database` |
| `/api/setup` when the bot is running | the **🔧 Reconfigure — send me a link** button: the bot DMs the admins a one-time link that reopens the setup form (see [Reconfiguring](#reconfiguring-a-running-deployment)) |

The destination is `P2P_DATABASE_LINK` (default
`https://vercel.com/dashboard/stores`) — point it at your own storage page,
an Upstash console or a self-hosted Redis:

```env
P2P_DATABASE_LINK=https://vercel.com/dashboard/stores
```

After connecting a database, tap **🔄 Check connection** in the bot (or reopen
the page): the bot re-reads the credentials and re-selects the store without a
restart.

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

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot&env=BOT_TOKEN,ADMIN_IDS&envDescription=BOT_TOKEN%20and%20ADMIN_IDS%20are%20required%3B%20connect%20Upstash%20for%20Redis%20or%20Vercel%20KV%20after%20creating%20the%20project&project-name=p2p-price-bot)

Vercel has no long-running process, so there the bot runs in **webhook mode**:
Telegram pushes every update to `/api/webhook` and a **Vercel Cron** calls
`/api/tick` to post the prices — the same bot, the same panel, the same handlers.

1. **Import** this repository into Vercel ([deploy button](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot) or `npx vercel`).
2. **Add a KV/Redis store** (Vercel → Storage → *Upstash for Redis* → connect to the project) and the **environment variables** `BOT_TOKEN` + `ADMIN_IDS`.
3. **Deploy**, then open `https://<your-app>.vercel.app/`. On a first deployment the URL shows a guided setup web UI: a redacted checklist for `BOT_TOKEN`, `ADMIN_IDS` and the KV pair, **plus a form that stores whatever is missing** — no redeploy needed. Once ready, the health page registers the Telegram webhook by itself. The page **detects** the database first (in the environment, then on Vercel itself) and **skips** the database step when one is already there — it says where it found it, and, if only the credentials are missing, that a redeploy delivers them instead of a second store.
4. Prefer a terminal? `python setup_cli.py` asks for the same values and writes them to the same store. Every question can be skipped with **Enter** — and `python setup_cli.py --skip` skips the whole thing and prints the web UI address, so you can finish in the browser later (`--show` prints what is stored, redacted).
5. In Telegram: `/start` → **👥 Set group** → paste a merchant URL → **🟢 Auto: ON**.

> 🌍 The functions run in `fra1` (EU): the exchange P2P APIs geo-block US IPs
> (Binance answers HTTP 451 there), so Vercel's default US region serves no
> prices. [`vercel.json`](vercel.json) already sets this for you.
>
> ⏰ **Free (Hobby) plan**: crons may only run **once a day** — a faster schedule
> fails the deployment. On **Pro** set `"schedule": "* * * * *"` in
> [`vercel.json`](vercel.json) for live prices, or keep the free plan and let an
> external scheduler call `/api/tick` (see [VERCEL.md](VERCEL.md#5-the-cron--how-often-prices-are-checked)).

Full walkthrough, environment variables, endpoints and troubleshooting:
**[VERCEL.md](VERCEL.md)**.

#### Reconfiguring a running deployment

Change the token, the admins, the pair or the database **without a redeploy and
without the dashboard**:

1. Open `https://<your-app>.vercel.app/api/setup` (or press **⚙️ Reconfigure
   setup** on the status page).
2. Press **🔧 Reconfigure — send me a link** — the bot DMs the admin(s) a
   one-time link (single use, 15 minutes). `/setup` in Telegram does the same.
3. Open the link, change what you want, **💾 Save settings**. The running
   instance picks the new values up immediately.

The form opens by itself whenever the deployment cannot work — a missing token,
or a database whose credentials are set but which no longer answers. That second
case used to hide behind a green "connected" tick while the bot silently forgot
its group, merchants and prices; the checklist now shows **Not responding** and
hands you the form to repair it.

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
   - `https://www.okx.com/p2p/ads-merchant?publicUserId=…`
   - `https://www.bitget.com/p2p/merchant/…`
4. Tap **🟢 Auto: ON** — prices are posted whenever they change.
5. Optional: **📢 Set channel** to post into a channel too. New posts from that
   channel are also auto-forwarded into the configured group by default; toggle
   this with **⚙️ Settings → ↪️ Channel → group**, and pick *which* channels or
   groups to forward from in **⚙️ Settings → ↪️ Forward from**. Check **🛡 Anti-scam** so
   newcomers have to type a random word before they can post.

### Panel buttons

| Button | What it does |
|---|---|
| 📊 **Post prices now** | Posts all merchant prices to the group **and the channel** immediately |
| 🟢/🔴 **Auto** | Toggle automatic posting on price change |
| 📋 **Merchants** | List merchants — tap one to remove |
| 👥 **Set group** | One click: choose the group that receives updates |
| 📢 **Set channel** | One click: add the bot to a channel and post the prices there too |
| 🛡 **Anti-scam** | Mute newcomers until they type a random word — see [🛡 Anti-scam verification](#-anti-scam-verification) |
| ⚙️ **Settings** | Liquidity, Buy/Sell buttons, auto-delete timers, **🧹 group cleanup** (join notices + the useless messages), **📤 private-message forwarding**, **↪️ forwarding into the group + which chats to forward from**, **🖼 button icons & post banner** |
| 🔘 **Manage buttons** | Add custom buttons, remove/restore Buy or Sell, and edit labels + links |
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
| `{URL}` | Public merchant profile URL (canonical profile route for OKX) |
| `{SELL}` / `{BUY}` | Best sell / buy price |
| `{SELL_AMOUNT}` / `{BUY_AMOUNT}` | Available liquidity (if the merchant has ads) |
| `{SELL_URL}` / `{BUY_URL}` | Selected link target: merchant profile by default, or optional ad link |
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

### 🔘 Manage buttons — add, remove, labels & links

Under every group post the bot shows configurable URL buttons for each merchant. By default it is
**🟢 BUY on the left · 🔴 SELL on the right**. In the bot's **private admin chat**, tap
**🔘 Manage buttons** on the panel (or **⚙️ Settings → 🔘 Manage buttons**). You can keep both,
remove either or both, and add your own buttons. **Buy and Sell still open the merchant's public
P2P profile by default**, where users can choose an ad themselves.

| Menu item | What it does |
|---|---|
| 🔘 **All buttons: ON/OFF** | Show or hide all configured buttons, including extras |
| 🔄 **Order** | Switch between `🟢 Buy ⬅️ \| Sell ➡️ 🔴` and `🔴 Sell ⬅️ \| Buy ➡️ 🟢` (the price lines in the text follow the same order) |
| 🟢 **Edit BUY label** | Send your own caption for the Buy button |
| 🔴 **Edit SELL label** | Send your own caption for the Sell button |
| 🗑 **Remove BUY / SELL** | Remove either built-in button from the post without losing its label or link |
| ➕ **Restore BUY / SELL** | Bring a removed built-in button back |
| ➕ **Add button** | Create an extra button: send its label, then its link |
| 🧩 **Extra buttons** | Select an extra button to edit its label/link or delete it (with confirmation) |
| 🎯 **Target** | Switch between **the merchant profile page** (default) and optional ad-link templates |
| 🔗 **BUY / SELL link** | Optional custom URL — overrides the target for that side (`{AD_URL}`, `{AD_ID}`, `{PRICE}`, `{URL}`, `{NICK}`, … available) |
| 🔗 **Ad link templates** | Edit the deep-link template of each exchange (Binance / Bybit / OKX / Bitget) |
| 🖼 **Button icons** | Put a custom-emoji image (and a colour) in front of any button label — see below |
| 🖼 **Post banner** | Post a photo **or a GIF** — with the prices in its caption and the media at its own full-HD size — see below |
| ♻️ **Reset buttons to default** | Restore both Buy/Sell buttons and profile links, clear extras and overrides, and turn buttons ON; asks for confirmation if extras would be deleted |

Defaults:

```
🟢 BUY {PRICE} {NICK}      🔴 SELL {PRICE} {NICK}
```

**Buy/Sell label placeholders** (extra-button labels are plain text)

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

#### Add your own buttons or replace Buy/Sell

1. Open **🔘 Manage buttons → ➕ Add button**.
2. Send a label, such as `👤 My P2P profile`, `💬 Support` or `📢 Channel` (1–60 characters).
3. Send its public `https://`, `http://` or `tg://` link (up to 2048 characters).
   Send **`{URL}`** (or `{PROFILE_URL}`) instead to use each merchant's P2P profile automatically.
4. Use **👁 Preview**, then **📊 Post prices now** to publish the updated buttons.

For a single **My P2P profile** button instead of Buy and Sell, remove **BUY** and **SELL**,
then add a button named `👤 My P2P profile` with link `{URL}`. Removing buttons does not remove
the prices from the message. If you remove both defaults and all extras, no keyboard is sent.

Extra buttons appear in their creation order after the remaining Buy/Sell buttons **for each
merchant**, laid out two per row. You can add up to **8 extra buttons**. The bot caps the full
post's keyboard at **100 buttons**; with many merchants, reduce the number of extras/merchants
if some buttons are omitted. The **All buttons** switch hides everything without deleting it.

A new button is saved only after both a valid label and link have been provided. Send `/cancel`
or tap **Cancel** to discard a draft. Drafts and completed buttons use the shared state store,
so they survive restarts and Vercel requests; two admins' drafts are kept separate. Only IDs
in `ADMIN_IDS` can manage the buttons. Extra labels are literal plain text (not HTML or templates),
and extra links support only a full URL or the exact `{URL}` / `{PROFILE_URL}` placeholder.

To change or remove an extra button, open **🧩 Extra buttons**, select it, then **Edit label**,
**Edit link**, or **Delete button**. Deletion asks for confirmation. Changes apply to the next
post (including auto-posts even when prices have not changed); existing Telegram messages keep
their old buttons until replaced.

### 🖼 Button icons — an image in front of a button label

Telegram can draw a **custom emoji** (a premium, often animated emoji image) before a button's
label, and colour the button **green / red / blue** (Bot API 9.4). Open
**⚙️ Settings → 🖼 Button icons** (or **🔘 Manage buttons → 🖼 Button icons**).

The icon is chosen by **the emoji the label starts with**, so one entry covers every button that
uses it — the group post, the panel and every menu:

| Set an icon for | …and it appears on |
|---|---|
| `🟢` | `🟢 BUY {PRICE} …`, `🟢 Edit BUY label`, and every other 🟢 button |
| `🔴` | `🔴 SELL {PRICE} …`, `🔴 Edit SELL label`, … |
| `⚙️`, `📋`, `👁`, `🖼` … | the matching buttons in the panel and the menus |

Tap an emoji, then set its icon in either of two ways:

1. **Forward (or send) the custom emoji** — a message that contains it, or the emoji as a
   premium sticker. The bot reads its id.
2. **Paste the numeric id** — e.g. `5368324170671202286`.

**🎨 Colour** cycles that emoji's buttons through *green → red → blue → default*, and
**🗑 Remove icon** clears the entry. A plain emoji like 😀 has no id — only Telegram *custom*
emoji do.

> ⚠️ Telegram only shows these icons for bots that bought a username on **Fragment**, or when the
> bot owner has **Telegram Premium**; other clients simply show the plain emoji. If your Telegram
> refuses the icons, the bot notices once, logs it, and keeps posting with plain buttons — a price
> post is never lost because of a decoration.

### 🖼 Post banner — a photo **or a GIF** in full HD, with the prices in its caption

**⚙️ Settings → 🖼 Post banner** puts a picture (your logo, a banner, a rate card — or an
animated GIF) above the prices: the post is then sent as a **photo or animation whose caption is
the report**, with the buttons under it.

* **📤 Send a photo or GIF** — send it in the chat; the bot stores Telegram's `file_id` (no
  re-uploading on every post). A GIF is posted with `sendAnimation`, so Telegram **plays it in
  the post**; send it as a GIF or as a `.gif` file, both work.
* **🔗 Use an image URL** — an `https://` JPG/PNG link instead (a link ending in `.gif` is posted
  as an animation automatically).
* **🎞 Use a GIF URL** — an `https://` GIF (or silent MP4) link, always posted as an animation.
* **📐 Post in full HD** — ON by default, and it is what makes an uploaded GIF stay sharp: the
  bot remembers the `width`, `height` and `duration` of the file you sent and passes them to
  `sendAnimation`, so Telegram renders the animation **at its own size** instead of picking a
  smaller preview. A photo banner needs no such fields — the bot always keeps the **largest** copy
  Telegram made of your upload. Turn 📐 off if you would rather Telegram choose the display size.
* **📎 A GIF sent as a file never loses the banner** — sending a GIF *without compression* keeps
  every byte of it, but the id Telegram gives that upload is a **document** id, and
  `sendAnimation` does not always play one. When it refuses, the bot posts the **file itself**
  with the prices in its caption: a banner that the client opens on a tap still beats a price
  post with no banner at all. 👁 Send a test tells you which of the two the group received.
* **👁 Send a test** / **👁 Preview** — see exactly what the group will get, including the size the
  GIF went out at.
* **🗑 Remove banner** — back to a plain text post.

> 📐 **What full HD does and does not do.** The bot never re-encodes, resamples or crops your
> media: it posts the file Telegram already has, at the size that file measures. What Telegram did
> to the upload *before* the bot could see it (a GIF sent as media is converted to a silent MP4,
> and a very large picture is downscaled into its photo copies) is out of the bot's hands — so
> upload the banner at the size you want your group to see, and tap 👁 Send a test to confirm.

Telegram caps a **caption at 1024 characters** (photo, animation or file alike), so a longer
report is posted as a plain text message and the banner is skipped (logged, and the test tells
you). A banner Telegram refuses for good — a deleted file, a dead URL, an upload that has gone
from its storage — also falls back to the text post. Deleting the previous message
(**🗑 Auto-delete prev**) and the `N`-hour auto-delete both cover the banner message too.

> 🎞 Switching a photo banner for a GIF (or back), or turning **📐 full HD** on or off, counts as a
> change, so the next price post goes out again even if the prices have not moved.

### 👤 Buy/Sell buttons open the **merchant profile**

Both **BUY** and **SELL** open the public P2P profile of the merchant in that row. For OKX,
the bot builds `https://www.okx.com/p2p/ads-merchant?publicUserId=…` from the saved merchant's
public ID. This also fixes older saved marketplace-style URLs; you do not need to re-add
the merchant. Linked prices and the merchant name use the same profile destination.

Users select an ad and complete the trade on OKX themselves. The bot does **not** automatically
click Buy/Sell or place an order, and it does not force the OKX app to open: app/browser
handling depends on the user's device and Telegram settings.

Existing installations switch from the old ad target to the profile target once on upgrade.
Custom BUY/SELL link overrides are preserved and still take priority. To clear one without
changing extra buttons, edit its **BUY / SELL link** and send `default`.
**🔘 Manage buttons → ♻️ Reset buttons to default** restores both built-in buttons and the
profile target, but also deletes extra buttons after confirmation.
After restarting/redeploying, use **📊 Post prices now** to publish the new links (old Telegram
messages retain their original buttons).

```
📊 P2P USDT/USD

⚫ Okx · Fast_sonic
   🟢 Best BUY  (you sell): 1.001          ← button → merchant profile
   🔴 Best SELL (you buy):  0.999          ← button → merchant profile
[🟢 BUY 1.001 Fast_sonic] [🔴 SELL 0.999 Fast_sonic]
```

* ⚙️ **Settings → 🎯 Exact ad links** can opt back into ad-link templates. It is **OFF**
  by default, so buttons and linked prices open the **merchant profile**. A choice made
  after the upgrade survives restarts and serverless requests.
* ⚙️ **Settings → 🔗 Link prices** makes the prices inside the post clickable too.
* ⚙️ **Settings → 🔗 Ad link templates** — one template per exchange, editable from Telegram
  (or via the `AD_LINK_TEMPLATES` env var). Placeholders: `{AD_ID}` `{ASSET}` `{ASSET_LOWER}`
  `{FIAT}` `{FIAT_LOWER}` `{SIDE}` `{TAKER_SIDE}` `{ACTION_TYPE}` `{URL}` `{NICK}`.

#### Optional ad-link templates

The bot still remembers the IDs behind each price (the cheapest merchant SELL ad and highest
merchant BUY ad). If you explicitly enable ad links, these templates are used instead of the
profile. They are also available as a fallback when no profile URL can be resolved.

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

### 🧹 Group cleanup — remove the join notices and every useless message

Your group is a price board, so the bot keeps it clean: it removes the messages
nobody needs and the price post stays the last thing anybody reads. Open the bot →
⚙️ **Settings → 🧹 Group cleanup** (or send `/cleanup`, alias `/clean`) — one switch
per rule, plus a master switch that turns the whole thing off:

| Rule | What disappears | Default |
|---|---|---|
| 🚪 **Join/left notices** | Telegram’s **“X joined the group”** / **“X left the group”** service messages — including people accepted **via a join request** | **ON** ✅ |
| 🧾 **Other service notices** | changed title/photo, pinned messages, invite links, video chats, forum topics, “group upgraded”… | OFF |
| 🔗 **Links & @usernames** | any member message holding a URL, a `t.me` link or an `@username` | OFF |
| 🖼 **Media & stickers** | stickers, GIFs, photos, videos, voice notes, audio, files, polls, locations | OFF |
| ↩️ **Forwarded messages** | anything forwarded from another chat, channel or bot | OFF |
| ⌨️ **Commands** | `/something` typed by an ordinary member | OFF |
| 🔇 **Strict: members post nothing** | **every** message from an ordinary member — the group reads like a channel | OFF |

**Never removed**, whatever you switch on:

- the bot’s own posts — the price report, the 🛡 anti-scam challenge, ↪️ relayed
  channel posts and anything you 📤 forwarded from the private chat;
- your messages, the other bot admins’, and the **group administrators’** (their
  list is read once and remembered for 10 minutes, so it costs no extra call per
  message);
- a new member who is answering the 🛡 **anti-scam check** — that flow owns their
  messages until it is done with them, so a verification answer is never eaten by
  🔇 strict mode.

**After a removal** the bot can stay **silent 🤫** (default), post a **short warning
in the group 💬** that deletes itself after 20 seconds, or **tell the admins 📩** in
private. The 🧹 screen also counts what it removed and shows the last reason.

- ⚠️ The bot must be a **group admin** with the **Delete messages** permission,
  otherwise it cannot remove anything (it says so in the log).
- Cleanup runs in your **registered group** only, and it looks at every message
  *next to* the other handlers — a photo the 🖼 banner editor also sees, or an
  answer the 🛡 check already took, is handled by both without fighting over it.
- Telegram only lets a bot delete messages younger than **48 hours**, so this
  cleans what arrives from now on; it cannot purge old history.
- With **↪️ Group → channel** switched on, a member’s message is mirrored into the
  channel *first* and then removed from the group — the channel keeps the post,
  the group stays clean.

---

## 📢 Post to a channel (in addition to the group)

The same price post can go to a **channel** as well as to your group — handy when
you want a public rate channel and a discussion group.

1. Open your bot → `/start` → **📢 Set channel** and pick your channel. Telegram adds
   the bot with the rights it needs (post / edit / delete messages).
   *Prefer the terminal?* Add the bot to the channel as an admin yourself, then send
   `/setchannel` inside the channel.
2. That is it: from now on **every price post goes to both** chats.

| | |
|---|---|
| Independent | The group and the channel each keep their **own** last message — the previous post is deleted in each chat separately, and the ⏰ auto-delete timer applies to both. |
| Adding later | Setting a channel does **not** repost to the group; only the new chat gets a post. |
| Removing | Remove the bot from a chat and it is unset automatically. |
| Permissions | In a channel the bot must be an **admin** with the right to post and delete messages. |

### ↪️ Forward posts into the group — you pick from where

New messages in the chats **you select** are forwarded into your group. This is
separate from the private-chat **📤 Auto-forward** option below.

**Every relayed message carries the channel name.** `📢 Channel` (or `👥 Group`)
is written above the text or the media caption, so the group always sees where a
post came from. Content Telegram refuses to copy that way (stickers, polls,
protected posts) goes out as a real forward instead — Telegram's own
"Forwarded from" header then shows the name.

| | |
|---|---|
| Switch | **⚙️ Settings → ↪️ Channel → group** (ON by default) |
| Pick the chats | **⚙️ Settings → ↪️ Forward from** — lists every selected chat, tap one to remove it (up to 10) |
| Default | Nothing selected → the **configured price channel** is relayed, which is what the bot did before this option existed |
| Add a channel | **📢 Add a channel** in that menu (a deep link that adds the bot as an admin), or send `/forwardfrom` inside the channel |
| Add a group | **👥 Add a group** in that menu, or send `/forwardfrom` inside the group |
| Stop | `/stopforward` inside that chat, or tap it in the menu |
| One tap | **📢 Use the price channel** adds the channel the prices go to |

Requirements — the bot has to be able to **read** the source chat and **write** to
the group:

- In a **channel** the bot must be an **administrator**: Telegram only sends
  channel posts to admins.
- In a **group** the bot must be an **admin** as well, *or* privacy mode must be
  off (@BotFather → `/setprivacy` → **Disable**) — otherwise it never sees the
  messages it should relay.
- The **destination group itself** cannot be a source (its messages stay with the
  🛡 anti-scam handler), and the relay can be turned off without losing the list.

Never relayed: **commands** (`/forwardfrom`, `/setchannel`, …), the bot's **own
price reports** and its **“✅ forwarding from here”** confirmations — so nothing
is echoed back into the group.

---

### ↪️ Forward group posts to the channel

In **⚙️ Settings → ↪️ Group → channel**, turn on the independent group-to-channel relay (OFF by default). Ordinary text and media from the configured group are copied to the configured price channel with the group name above them. Commands, join/leave notices, verification answers, and the bot’s own posts are not relayed. Both directions may be enabled at once without echoing the bot’s relayed copies. The bot needs permission to read group messages (admin or privacy mode disabled) and post in the channel.

## 📤 Auto-forward what you send the bot

Anything you send to the bot in your **private chat** that the menus did not ask for
is reposted to your group / channel — text, photo, video, sticker, file, voice note…

1. Send the message (or **forward** one into the bot chat).
2. The bot copies it into the destination and answers with a **🗑 Undo** button —
   tap it to delete the copy again (the undo stays available for 48 hours).
3. Choose where it goes in ⚙️ **Settings → 📤 Auto-forward**, which cycles:

| Setting | Meaning |
|---|---|
| `GROUP` (default) | Everything you send is reposted to the group |
| `CHANNEL` | Reposted to the channel instead |
| `GROUP + CHANNEL` | Reposted to both |
| `OFF` | Nothing is reposted — the bot only answers your menus again |

Not forwarded: **commands** (`/start`, `/cancel`, …), answers the bot asked for
(a banner photo or GIF, a button label, an edited header…) and **merchant URLs**, which are
still added as merchants.

> 📋 An **album** (several photos sent as one message) is reposted photo by photo;
> everything else keeps its caption and formatting.

---

## 🛡 Anti-scam verification

Scammer bots join P2P groups in waves and post “admin”, “support” and fake-deal
links within seconds. Turn the check on and **every new member is muted until they
type a random word**:

1. Someone joins → the bot mutes them (they can still **type**, but links, photos,
   stickers and polls are blocked) and posts a challenge with a random word.
2. They type the word → the bot unmutes them, deletes the challenge and welcomes them.
3. Wrong word → their message is deleted at once and the challenge shows how many
   attempts are left.
4. Out of attempts or out of time → the bot applies what you configured and **asks
   you** what to do with a ✅ **Approve** / 🚫 **Kick** message.

Open the bot → **🛡 Anti-scam**:

| Setting | Values |
|---|---|
| Verification | ON ✅ / OFF ❌ |
| Wrong words allowed | 1 · 2 · **3** · 5 · 10 |
| Time limit | 1 · 2 · **5** · 10 · 30 minutes |
| On failure | **Mute until I approve** 🔇 · Kick (they can rejoin) 👢 · Ban permanently 🚫 |

### Your own challenge message

Tap **📝 Edit challenge message** and write anything you like — it must contain
`{WORD}`, that is where the random word goes:

| Placeholder | Replaced with |
|---|---|
| `{WORD}` | The random word the member has to type (required) |
| `{MENTION}` | A clickable mention of the new member |
| `{NAME}` | Their name as plain text |
| `{GROUP}` | The group title |
| `{MINUTES}` | The time limit |
| `{LEFT}` / `{ATTEMPTS}` | Attempts left / total attempts |
| `{ASSET}` `{FIAT}` `{PAIR}` | e.g. `USDT`, `USD`, `USDT/USD` |

Default:

```
🛡 Anti-scam check

{MENTION} welcome to {GROUP}! Scammer bots are everywhere, so type this word to unlock the group:

{WORD}

⏳ You have {MINUTES} minutes · {LEFT} attempts left.
```

Send `default` while editing to go back to it. **👁 Preview challenge** shows you the
exact message with a sample word.

⚠️ The bot must be a **group admin** with **Restrict members** (to mute) and
**Delete messages** (to remove wrong answers). Without them it logs a warning and
simply skips the check instead of failing. Admins of the bot and other bots are
never challenged.

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

### The bot forgot its group / merchants, but every page says the database is fine

The database credentials are set, so the checklist *used* to show a green tick —
while `RedisStore.load()` was quietly returning "nothing saved" because the store
stopped answering (deleted or rotated Upstash database, wrong region, expired
token). The bot then starts from an empty state on every request.

`/api/setup` now probes the store instead of trusting the variables: it shows
**Not responding** with the connection error, keeps the deployment "not ready",
and reopens the form so you can paste working credentials — no redeploy. On a
VPS/Docker install, `python bot.py` logs the same and `python setup_cli.py --show`
prints where the settings are.

---

## 🧪 Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The suite covers the ad-link templates, the Buy/Sell button targets, clickable prices, the
group/channel destinations, 📤 auto-forward, the ↪️ forward-source selection, the 🛡 anti-scam
check and the state backends — no Telegram calls are made.

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
| `runtime_config.py` | The settings the setup page / `setup_cli.py` store (KV key or `runtime_config.json`), validated and applied before `bot.py` starts |
| `setup_cli.py` | Terminal setup wizard — every question skippable (`--skip`, `--show`, `--clear`, `--vercel-env`) |
| `serverless.py` | Serverless mode: ASGI glue, first-start setup UI + settings form, webhook + cron helpers, one PTB app per warm container |
| `api/app.py` | Vercel entry point: one ASGI app that routes `/api/webhook`, `/api/setup`, `/api/tick` and `/` |
| `api/webhook.py` | Telegram updates (`POST`) + status page (`GET /api/webhook`) |
| `api/setup.py` | First-start page + the form that stores the required settings (`GET`/`POST /api/setup`) |
| `api/tick.py` | The cron round that replaces the JobQueue (`/api/tick`) |
| `vercel.json` | Vercel config: function limits + the `/api/tick` cron schedule |
| `pyproject.toml` | Vercel build config: the Python entry point + dependencies (mirrors `requirements.txt`) |
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
