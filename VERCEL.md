# ▲ Deploy on Vercel (serverless)

The bot runs on Vercel **without a server to keep alive**. Vercel has no
long-running process, so the bot switches to **webhook mode** there:

| | VPS / Docker / local (`install.sh`) | Vercel |
|---|---|---|
| Telegram updates | bot polls Telegram (`getUpdates`) | Telegram **pushes** them to `/api/webhook` |
| Prices every N seconds | in-process JobQueue | Vercel **Cron** calls `/api/tick` |
| State (`data.json`) | file next to the bot | **KV / Redis REST** (required — no disk) |
| Cost | your server | free tier is usually enough |

Everything else is identical: the same handlers, the same panel, the same
`data.json`-style state — just stored in a KV store instead of a file.

```
Telegram ──►  https://<your-app>.vercel.app/api/webhook   (every update)
Vercel Cron ─► https://<your-app>.vercel.app/api/tick      (post prices, cleanup)
                        │
                        └──► KV/Redis REST store (group, merchants, settings, last prices)
```

---

## 1. What you need

* the **bot token** from [@BotFather](https://t.me/BotFather) → `/newbot`
* your **Telegram ID** from [@userinfobot](https://t.me/userinfobot)
* a **KV / Redis REST store** (2 minutes, free): Vercel dashboard → your project →
  **Storage** → *Upstash for Redis* (or the *Vercel KV* integration) → **Connect to
  project**. Vercel then sets `KV_REST_API_URL` + `KV_REST_API_TOKEN` for you.
  Just clicking *Create* is enough — the bot only stores one small JSON document.

> ⚠️ KV/Redis is required for a Vercel deployment. If `BOT_TOKEN`, `ADMIN_IDS`,
> or the KV pair is missing, the first-start page shows a redacted checklist and
> a form for the Telegram credentials and optional bot settings. **Database
> credentials and backend selection are not editable in the Web UI.** Connect
> the initial KV/Redis store through Vercel Storage or the deployment
> environment; Telegram database management becomes available after the bot is
> running. A local file store is supported on VPS/Docker, but is not persistent
> on Vercel.

### Three ways to add settings — pick one, or mix

| Way | How | Needs a redeploy? |
|---|---|---|
| **🌐 Browser** | open `https://<your-app>.vercel.app/api/setup` and fill the Telegram / optional bot settings form | **no** — applied on the next request |
| **⌨️ Terminal** | `python setup_cli.py` for bot settings; optional `--vercel-env` pushes environment values (including `--kv-url` / `--kv-token`) | **no** for stored bot settings; **yes** for changed environment variables |
| **▲ Dashboard / CLI** | *Settings → Environment Variables*, or `npx vercel env add BOT_TOKEN`; connect the first KV store in *Storage* | **yes** — environment variables apply to new deployments only |

The browser form and the terminal wizard write bot configuration to the same
place — the connected KV/Redis under `p2p-price-bot:config`, or
`runtime_config.json` next to the bot's data file when there is no Redis. The
terminal wizard can also select the backend and provide the KV REST pair; use
`--vercel-env` to save deployment variables to Vercel, then redeploy. Environment
variables always win over stored values, and `CRON_SECRET` stays
environment-only (Vercel's cron sends it from there).

> 🔒 The form is public, so it closes as soon as the deployment is configured —
> and if you set `SETUP_SECRET`, every submission must carry it. To change a
> bot setting later, use the 🔧 **Reconfigure** button described just below.
> To change database settings, use `/database` in the bot's private Telegram
> chat (see §6).

### Changing bot settings later — the 🔧 Reconfigure button

The setup form edits the Telegram token/admin IDs and optional bot settings. It
does not edit database credentials. To change a bot setting on a running
serverless deployment:

1. Open `https://<your-app>.vercel.app/api/setup` (or press **⚙️ Reconfigure
   setup** on the status page).
2. Press **🔧 Reconfigure — send me a link**.
3. The bot sends the admins a **one-time link** in Telegram. It works once and
   expires after 15 minutes.
4. Open the link, change the bot settings, and save. The instance picks them up
   immediately.

Sending `/setup` to the bot in Telegram does the same thing without opening the
browser. The link is delivered only to configured admins; `SETUP_SECRET` remains
available if you prefer a fixed secret.

> ⚠️ If Telegram is unreachable or the token was revoked, the link cannot be
> delivered. Use `python setup_cli.py` or set the token in the deployment
> environment instead.

### Manage a custom database in Telegram

Once the bot is running, send `/database` in a private chat or tap **⚙️ Settings
→ 🗄 Database**. **➕ Set / change custom database** asks for a Redis-compatible
REST URL and token, tests the credentials, then migrates the live bot state
before switching. Credentials are stored separately from deployment-managed
KV variables. **↩️ Use deployment database** verifies the fallback before
switching back.

The Vercel first-start database is a bootstrap requirement: connect it through
**Vercel → Storage** or set the deployment environment variables before Telegram
can manage a custom store. If a Telegram-selected custom store stops answering
while the deployment store remains healthy, use **↩️ Use deployment database**;
the bot verifies the fallback before switching. If the bootstrap store itself
stops answering, restore its deployment credentials and redeploy so Telegram
updates can reach the bot again.

### Read-only database detection

The setup checklist detects a database without asking for credentials. It checks
the deployment environment first, then—when a Vercel token is configured—Vercel's
storage list and the project environment-variable *names* (values are never
fetched):

| # | Where it looks | What it answers |
|---|---|---|
| 1 | **This deployment's active configuration** — a deployment-managed KV/Redis pair or a custom pair selected in Telegram | *"a database is wired into this deployment"* |
| 2 | **Vercel itself** — `GET /v1/storage/stores` for this project, then the project's environment-variable names | *"a store is connected, but this deployment may need a redeploy to receive its credentials"* |

A database detected on Vercel but not wired into the deployment is not a reason
to create a second one: connect it to the project again and redeploy. The
checklist and diagnostics JSON expose `status.database` (`found`, `wired`,
`source`, `where`, plus a legacy `skipped` flag) without exposing URLs or tokens.
The setup page has no database fields or connection buttons; detection is only a
read-only status report.

The Vercel lookup is read-only, cached for five minutes, and best effort:

* it needs `VERCEL_PROJECT_ID` (Vercel exposes it when *System Environment
  Variables* are enabled) and a credential — `VERCEL_TOKEN` (or
  `P2P_VERCEL_TOKEN`), which you add yourself;
* with neither, no call is made and the page says it could not check;
* a refused token, offline host or API error is reported in `GET /api/setup`
  JSON (`status.database.error`) — never raised.

> 🔁 A database that no longer answers is shown as **Not responding**. The web
> UI cannot repair its credentials; restore them in the deployment environment.
> Use `/database` to switch to a different custom store when the bot is running.

## 2. Deploy

### Option 1 — the dashboard

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot&env=BOT_TOKEN,ADMIN_IDS&envDescription=BOT_TOKEN%20and%20ADMIN_IDS%20are%20required%3B%20connect%20Upstash%20for%20Redis%20or%20Vercel%20KV%20after%20creating%20the%20project&project-name=p2p-price-bot)

1. **Import** the repository into Vercel.
2. **Connect storage** — [**Storage**](https://vercel.com/dashboard/stores) →
   **Upstash for Redis** (or Vercel KV) → **Connect to this project**, then
   confirm `KV_REST_API_URL` and `KV_REST_API_TOKEN` exist in the project's
   environment variables. The web UI only reports whether a database is
   connected; it has no connect link or credential fields. After the bot starts,
   custom Redis REST stores can be managed from Telegram with `/database`.
3. Set `BOT_TOKEN` and `ADMIN_IDS` in **Settings → Environment Variables**.
4. **Deploy**. If anything is missing, the deployment URL opens the first-start
   web UI and tells you which redacted checklist item still needs attention.

### Option 2 — the Vercel CLI

```bash
git clone https://github.com/sarakmacbook/OKX_Telegram_P2P_Price_Bot.git
cd OKX_Telegram_P2P_Price_Bot
npx vercel                       # link the folder to a Vercel project
npx vercel env add BOT_TOKEN production
npx vercel env add ADMIN_IDS production
npx vercel env add KV_REST_API_URL production        # only if not set by the KV integration
npx vercel env add KV_REST_API_TOKEN production
npx vercel env add CRON_SECRET production            # recommended: locks down /api/tick
npx vercel --prod                # deploy with all variables in place
```

`ASSET`, `FIAT` (default `USDT`/`USD`) and `INTERVAL` are optional there — the
interval only describes the polling installs, on Vercel the **cron schedule**
decides how often prices are checked.

## 3. Register the webhook (one click)

Open your deployment in a browser (the root URL redirects there, so either works):

```
https://<your-app>.vercel.app/
https://<your-app>.vercel.app/api/webhook
```

That page **registers the Telegram webhook** and shows the health of the
deployment: webhook URL, pending updates, state store, group, merchants, auto
posting. It re-registers automatically whenever the URL or the secret changed,
and `/api/tick` does the same on every cron run — so there is nothing manual to
keep in sync.

Or from the command line:

```bash
curl -s https://<your-app>.vercel.app/api/webhook | head -40   # JSON status (also registers)
curl -s "https://<your-app>.vercel.app/api/webhook?register=1"  # force a re-register
```

The page also knocks on the deployment's own public URL and tells you whether
Telegram can actually get in: a webhook can be *registered* and still be
unreachable (Deployment Protection answering `401`, a domain that answers `404`,
DNS that answers nothing), and that is the case where the bot goes silent while
every screen still looks healthy. See §9.

## 4. Use it from Telegram

1. Open your bot → `/start` → tap **👥 Set group** and pick your group.
2. Paste a merchant URL (`p2p.binance.com/…`, `bybit.com/…`, `okx.com/…`, `bitget.com/…`).
3. Tap **🟢 Auto: ON**. From then on every cron run posts the prices when they changed.

To inspect or manage the active store, send `/database` in a private chat with
the bot or open **⚙️ Settings → 🗄 Database**. The panel no longer offers an
external database-connect link. If the deployment has no initial KV store,
connect one in **Vercel → Storage**, confirm the environment pair, and redeploy;
after the bot starts, `/database` can configure a custom REST store.

Prefer a **separate bot token for preview deployments**: each preview has its own
domain and would otherwise re-register the same bot's webhook away from production.

## 5. The cron — how often prices are checked

The schedule lives in [`vercel.json`](vercel.json):

```json
{ "crons": [ { "path": "/api/tick", "schedule": "0 0 * * *" } ] }
```

| Vercel plan | Minimum interval | Precision | What to put in `vercel.json` |
|---|---|---|---|
| **Hobby** (free) | once per day | ±59 min | `"0 0 * * *"` — anything faster **fails the deploy** |
| **Pro** | once per minute | per minute | `"* * * * *"` (or `"*/5 * * * *"` for 5 minutes) |

So on the free plan the bot posts **once a day** by default. Two ways around it:

* upgrade to **Pro** and change the schedule to `* * * * *`;
* keep Vercel free and let an **external scheduler** call the tick endpoint —
  e.g. [cron-job.org](https://cron-job.org) every 5 minutes, or a GitHub Actions
  schedule in your fork:

  ```yaml
  # .github/workflows/tick.yml  (only needed on the Hobby plan)
  name: tick
  on:
    schedule: [{ cron: "*/5 * * * *" }]
    workflow_dispatch:
  jobs:
    tick:
      runs-on: ubuntu-latest
      steps:
        - run: |
            curl -fsS -H "Authorization: Bearer ${{ secrets.CRON_SECRET }}" \
              "https://<your-app>.vercel.app/api/tick"
  ```

Every run posts only when a price (or the ad behind it) actually changed, so a
1-minute cron does not spam the group. Each run is remembered in the state store
(`tick`), and that is what the status page row **Last cron run** and the
`🕒 Cron:` line in the Telegram panel show — `never`, or how long ago the last
run was with how many runs so far. A cron that never ran, or stopped more than a
day ago, is reported there as a warning instead of leaving you with a bot that
silently posts nothing; a failure inside one step (the webhook check, the post,
the cleanup) no longer cancels the other steps, because on the free plan this may
be the only run in 24 hours. `/api/tick` is protected by
`CRON_SECRET`: Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`
automatically; other callers must pass the same value in that header or as
`?secret=<CRON_SECRET>`. If `CRON_SECRET` is unset the endpoint is open (and says so
in its answer) — anyone who knows the URL could then trigger a post, so set it.

## 6. Environment variables

| Variable | Required | What it does |
|---|---|---|
| `BOT_TOKEN` | ✅ | token from @BotFather |
| `ADMIN_IDS` | ✅ | your Telegram ID(s), comma-separated |
| `KV_REST_API_URL` + `KV_REST_API_TOKEN` | ✅ | state store (set automatically by the Upstash/KV integration) |
| `UPSTASH_REDIS_REST_URL` + `…_TOKEN`, `REDIS_REST_URL` + `…_TOKEN` | alt. | other Redis-REST providers, same idea |
| `P2P_STATE_BACKEND` | – (`auto`) | choose `auto`, `redis`, or `file`; use `redis` on Vercel. `file` is intentionally not accepted as persistent Vercel state even when a KV pair exists (`P2P_STORAGE_BACKEND` is an alias) |
| `P2P_STATE_KEY` | – | key the state lives under (default `p2p-price-bot:state`) |
| `P2P_DATABASE_LINK` | – (legacy) | destination returned as `connect_database` in JSON status/error responses for scripts. The Web UI and Telegram database panel no longer show an external connect link; use Vercel Storage for the bootstrap store and `/database` for a custom Telegram-managed store. Default `https://vercel.com/dashboard/stores` |
| `SETUP_SECRET` | – (recommended) | locks the `/api/setup` form: every submission must carry it (form field, `?secret=`, or `Authorization: Bearer`). Without it the form is open while the deployment is unconfigured and closes once it is ready |
| `VERCEL_TOKEN` (or `P2P_VERCEL_TOKEN`) | – | optional, read-only lookup lets the checklist report which database is connected to the project and whether this deployment has its credentials (needs `VERCEL_PROJECT_ID`, which Vercel exposes when *System Environment Variables* are enabled) |
| `P2P_VERCEL_PROJECT_ID`, `P2P_VERCEL_TEAM_ID` | – | override the project/team the page asks about (defaults: `VERCEL_PROJECT_ID` / `VERCEL_PROJECT_NAME` and `VERCEL_TEAM_ID`) |
| `P2P_CONFIG_KEY` | – | key the settings saved by the form / `setup_cli.py` live under (default `p2p-price-bot:config`) |
| `P2P_RUNTIME_CONFIG_FILE` | – | file those settings live in when there is no Redis (default `runtime_config.json` next to the data file) |
| `PUBLIC_URL` | – | public URL used to register the webhook; auto-detected from `VERCEL_PROJECT_PRODUCTION_URL`/`VERCEL_URL`, set it for a custom domain or if the page says it cannot tell |
| `WEBHOOK_SECRET` | – | secret Telegram must send with every update; derived from `BOT_TOKEN` when empty |
| `CRON_SECRET` | – (recommended) | locks `/api/tick` down |
| `WEBHOOK_DROP_PENDING` | – | `0` (default) preserves queued updates, including /start; set `1` only to deliberately discard them |
| `P2P_WEBHOOK_PROBE` | – | `1`/`0` to force the "can Telegram reach this deployment?" knock on or off. On by default **on Vercel only**; off elsewhere, so a local `uvicorn` never calls out. Set `0` if you do not want the deployment to request its own URL. |
| `ASSET`, `FIAT`, `INTERVAL` | – | pair + description only (on Vercel the cron sets the real interval) |
| `AD_LINK_TEMPLATES` | – | per-exchange deep-link overrides (JSON), same as the polling installs |

Changing an environment variable only affects the **next** deployment — after
editing **Settings → Environment Variables**, redeploy (`npx vercel --prod` or
*Redeploy* in the dashboard). The setup page cannot select a database backend
or accept credentials. Set `P2P_STATE_BACKEND=redis` in the environment (or use
the terminal wizard) for the deployment-managed store; after startup, a custom
store is selected and persisted through Telegram's private `/database` menu.

## 7. Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/` | `GET` | redirects to `/api/webhook`, so the deployment URL itself opens the status page |
| `/api/webhook` | `POST` | Telegram updates — verified with `X-Telegram-Bot-Api-Secret-Token` (or `?secret=…`, handy for manual tests); everything else is rejected with 403 |
| `/api/webhook` | `GET` | first-start setup UI when Telegram settings or the required Vercel KV store are missing; otherwise the health page (JSON for scripts, HTML in a browser) that registers the webhook; `?register=1` forces it, `?register=0` only reports. The page reports database health but has no database credential controls. It also shows **Reachable** (the result of knocking on the public URL — `yes`, `no` with the reason, or `not checked`) and **Last cron run**, and prints the fix for whatever it finds. |
| `/api/webhook?check=1` | `GET` | status page + a live price fetch for every merchant — open this when prices are empty to see the per-merchant error |
| `/api/health` | `GET` | static marker (`{"ok": true, "app": "p2p-price-bot"}`) proving a request reached *this* deployment. It reads no state and calls nothing, so it is the right target for an external uptime check (and the URL the status page knocks on) |
| `/api/setup` | `GET` | readiness checklist plus a form for Telegram credentials and optional bot settings. Database health is read-only here; database configuration is not editable in the browser. |
| `/api/setup` | `POST` | stores bot settings (JSON or form-encoded) and applies them at once; rejects database credential/backend fields, returns 403 when the deployment is healthy and no authorization is sent, or 400 with a readable reason when a value is wrong or Telegram rejects the token |
| `/api/setup` | `POST` `action=send-link` | the 🔧 **Reconfigure** button: sends the admins a one-time link in Telegram that reopens the bot-settings form (see §6) |
| `/api/tick` | `GET` | one cron round: keep the webhook registered → post prices if they changed → delete stale group messages, expired 🛡 anti-scam checks and 🧹 group-cleanup warnings |
| anything else | – | `404` (JSON) — the deployment is one catch-all function, so the router answers what the platform's 404 used to |

## 8. How it works under the hood

* **One function, one entry point**: Vercel's Python runtime builds a Python
  project as a **single application** — it loads one top-level `app` and rewrites
  *every* request to it (files in `api/` are no longer functions of their own).
  `api/app.py` is that entry point and does the routing the platform used to do:
  `/api/webhook` → `api/webhook.py`, `/api/tick` → `api/tick.py`,
  `/api/setup` → `api/setup.py`, `/` → the status page, anything else → 404.
  `pyproject.toml` declares it:

  ```toml
  [tool.vercel]
  entrypoint = "api.app:app"
  ```

  Without that declaration (or a file named `app.py`/`index.py`/… in the project
  root, `src/`, `app/` or `api/`) the build stops with
  `No python entrypoint found in default locations`.
* **`pyproject.toml` also carries the dependencies** — it takes precedence over
  `requirements.txt` on Vercel, so both lists must stay identical (a test fails
  when they drift). `requirements.txt` remains the source for the VPS, Docker and
  local installs, which never read `pyproject.toml`.
* **`api/webhook.py`** and **`api/tick.py`** still export their own
  `async def app(scope, receive, send)` (Vercel detects ASGI by that exact
  signature), so each endpoint also runs standalone — `uvicorn api.webhook:app`.
* **`serverless.py`** glues the two worlds together: it imports `bot.py`, builds a
  PTB `Application` with `updater(None)` and `job_queue(None)` (no polling, no
  in-process scheduler), initializes it once per warm container and hands it each
  update. `Bot.initialize()` verifies the token with `getMe` — a wrong
  `BOT_TOKEN` therefore shows up as a 500 with a hint instead of a silent bot.
* **No JobQueue**: `/api/tick` calls the very same functions the polling
  scheduler uses (`auto_post_task`, `cleanup_task`), so behaviour is identical.
  Its steps are isolated from each other: if the webhook check fails the price
  post still happens, and anything that failed is listed in the answer's
  `errors` (and in the runtime logs) instead of being swallowed.
* **A registered webhook is not a working one**: Telegram's POST can be refused
  *before* it reaches the function (Deployment Protection, a missing DNS record,
  a domain that was never connected), which is exactly how the bot goes silent
  while every page looks fine. `/api/webhook` and `/api/tick` therefore knock on
  the deployment's own public URL (`GET <PUBLIC_URL>/api/health`, cached for
  5 minutes, 1 minute after a failure) and the status page turns the answer into
  a fix. On Vercel the knock is on by default; outside Vercel it runs only with
  `P2P_WEBHOOK_PROBE=1`, so a local `uvicorn` never calls out.
* **State freshness**: before every update/tick the state is re-read from the KV
  store, because Vercel may run several instances of the same deployment at the
  same time.
* **Multi-step admin input** (e.g. *Edit header* → send the text, or *Add merchant*)
  is kept in that shared store as well, so the two messages may safely land on
  different instances. See `refresh_state` / `edit_get` / `edit_set` in `bot.py`.
* **`config.json` is not used** on Vercel (read-only filesystem): the bot builds
  its configuration from the environment variables.
* **Region**: the functions run in `fra1` (EU — `"regions"` in `vercel.json`).
  Vercel's default region is `iad1` (US), and the exchange P2P APIs geo-block US
  IPs — Binance answers HTTP 451 there — so from the default region every price
  fetch fails and the bot posts nothing. A single region keeps the free Hobby
  plan working; the status page shows the region it actually runs in. If prices
  ever go empty after touching regions, `/api/webhook?check=1` names the cause
  (look for 451 / restricted / forbidden).
* **HTTP errors are visible**: a refused exchange request (451, 403, …) is stored
  as the merchant's ⚠️ error instead of a silent `—` — in the group post, in the
  add-merchant reply and in `?check=1`.

## 9. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `vercel build` fails with *No python entrypoint found in default locations* | the entry point moved, or `pyproject.toml` lost its `[tool.vercel] entrypoint = "api.app:app"` → restore it (the file must be `api/app.py` and export a top-level `app`), then redeploy |
| `vercel build` installs nothing / `ModuleNotFoundError: telegram` | `pyproject.toml` outranks `requirements.txt` on Vercel — the two dependency lists must match (see §8) |
| Page shows ⚙️ *Setup needed* (or a 500 JSON with a *hint*) | Telegram token/admin settings can be saved in the page form or with `python setup_cli.py`; Vercel KV must be connected through **Storage** or the deployment environment, then redeployed. Database credentials cannot be entered on the page. |
| `POST /api/setup` answers `403 … the form is locked` | the deployment is configured and healthy → press **🔧 Reconfigure — send me a link** to get a one-time link in Telegram, send `SETUP_SECRET` (set it in the environment first), or change bot settings with `python setup_cli.py` / in the dashboard. Use `/database` in Telegram for custom-store changes. |
| The bot forgot its group, merchants and prices, but every page says the database is connected | the configured store no longer answers (for example, a deleted/rotated Upstash database or wrong region). `/api/setup` shows **Not responding** but cannot edit database credentials. If a Telegram-selected custom store is down and the deployment store is reachable, use `/database` → **↩️ Use deployment database**. If the bootstrap store is down, restore its Vercel KV/Redis variables in the deployment environment and redeploy. |
| 🔧 Reconfigure says *the bot could not reach Telegram* | the token is wrong, revoked, or the admin never pressed *Start* on the bot — so the link cannot be delivered. Fix the token with `python setup_cli.py`, or set `SETUP_SECRET` in the environment and open `/api/setup?secret=…` |
| The form saved the settings but the next cold start forgot them | they went to the instance's temporary storage because no KV/Redis is connected → connect Upstash/KV, add the pair to the environment and redeploy |
| Prices are `—`, or `⚠️ …451…` / *restricted* / *forbidden* | the exchange geo-blocks the function's region → keep `"regions": ["fra1"]` in `vercel.json` (EU) and redeploy; diagnose with `/api/webhook?check=1` |
| Deployment URL shows a Vercel 404 | only `/` (redirects to the status page) and `/api/*` exist — check the URL |
| Setup UI says the state store is missing (or an older deployment shows `State store: file — NOT persistent ❌`) | add/connect the Upstash or Vercel KV integration, confirm `KV_REST_API_URL` + `KV_REST_API_TOKEN`, then redeploy |
| `webhook not registered: Cannot tell where this deployment is reachable` | neither `VERCEL_PROJECT_PRODUCTION_URL`/`VERCEL_URL` nor `PUBLIC_URL` is set (e.g. running the file outside Vercel) → set `PUBLIC_URL` |
| Bot answers nothing in Telegram | open `/api/webhook` — it re-registers, shows the last webhook error from Telegram **and checks from the outside** whether the deployment can be reached at all. Read the **Reachable** row and the ⚠️ warning: `401`/`403` means Vercel Deployment Protection is in front of this address (see below), `404` means the domain is not connected, `could not be reached` means DNS/TLS. Then make sure the group was set and **Auto** is ON |
| `Reachable: no — HTTP 401` / Telegram's `Wrong response from the webhook: 401` | this deployment's URL asks anonymous visitors to log in, and Telegram cannot. Vercel Authentication covers **every preview URL and the auto-generated `*-<hash>.vercel.app` production address**; the project's real production domain stays public. So: keep Deployment Protection on *Standard Protection* (not *All Deployments*), point the bot at your own production domain — `PUBLIC_URL=https://your-domain` in the environment — redeploy, then open `/api/webhook?register=1` |
| `Reachable: no — HTTP 404` | the URL answers, but nothing is deployed there: the domain is not connected to this project (Vercel → project → **Settings → Domains**) or the deployment was removed. Connect the domain and set `PUBLIC_URL` to it |
| `Reachable: no — could not be reached at all` | DNS does not resolve / TLS fails / the request times out. Fix the domain, or set `PUBLIC_URL` to the address the bot should be reachable at |
| `Reachable: not checked` | the probe is off (`P2P_WEBHOOK_PROBE=0`), no public URL is known, or this is not a Vercel deployment. Set `PUBLIC_URL`, or `P2P_WEBHOOK_PROBE=1` to force the check |
| `Last cron run: never` / a ⚠️ warning that it was `… ago` | the cron never ran (or stopped): on the free plan it runs once a day, so give it 24 hours, check **Vercel → your project → Cron Jobs** for failures, and/or add the external 5-minute scheduler of §5 |
| Cron answer contains an `errors` list | each entry names one step that failed (webhook check, price post, cleanup) — the other steps still ran. The same lines are in the runtime logs |
| Prices only once a day | free Hobby plan → Pro cron or an external scheduler calling `/api/tick` (see §5) |
| Telegram keeps retrying one update | check the runtime logs; anything that fails returns a 500 on purpose so Telegram retries |
| Logs | Vercel dashboard → your project → **Logs**, or `npx vercel logs <deployment-url>` |
| Reset everything | delete the KV key (`p2p-price-bot:state`) or send `/start` and set the group again |

## 10. Security

* `POST /api/webhook` only accepts bodies carrying the secret token; the default
  secret is a SHA-256 of your `BOT_TOKEN`, so it is unguessable without the token.
* The registration URL comes **only** from environment variables / Vercel's own
  `VERCEL_*` variables — never from the request's `Host` header, so a forged
  request cannot point your bot's updates at somebody else's server.
* `CRON_SECRET` keeps `/api/tick` (which can post to your group) private.
* `POST /api/setup` accepts bot settings while the deployment is **not**
  configured, while something it needs is **broken**, and whenever the request
  carries `SETUP_SECRET` or a one-time reconfigure link. Database fields are
  rejected even with authorization; database credentials are managed separately
  through Telegram. A healthy deployment cannot be re-pointed at somebody else's
  admin ID by a stranger who finds the URL.
* The one-time reconfigure link is 256 bits of randomness, is stored **only as a
  SHA-256 fingerprint** (so a leak of the database cannot rebuild it), expires
  after 15 minutes, is spent by the save it authorizes, and is never shown to
  the person who pressed the button — it is delivered to the admins in Telegram,
  which is what makes it safe for the button to be public.
* The Telegram token submitted through setup is checked with Telegram (`getMe`)
  before storage, is stored in your own KV/Redis (never in code or logs), and is
  never echoed back — the checklist shows a redacted copy only.
* Custom Redis REST credentials are collected only in the admin's private
  Telegram chat, tested before switching, and never repeated in bot replies.
  The bot carries live state to the new store and verifies the deployment
  fallback before switching back.
* `config.json`, `data.json`, `runtime_config.json` and `.env` are git-ignored; on
  Vercel everything secret lives in the project's environment variables or in the
  KV store the deployment already trusts.

### Setup activates Telegram delivery

Saving a complete Vercel setup now registers the webhook before returning. No
extra status-page visit is needed. Failed activation is reported as a warning;
the settings remain saved. The webhook must use a public production domain,
not a deployment URL protected by Vercel login. Set `PUBLIC_URL` to that domain
if the automatically selected domain is not publicly accessible.
