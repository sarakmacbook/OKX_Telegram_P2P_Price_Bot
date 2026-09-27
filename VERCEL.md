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
> or the KV pair is missing, opening the deployment shows a first-start setup UI
> with a redacted checklist, **a form that stores what is missing**, and the exact
> Vercel steps. It registers the webhook automatically once everything is there.
> A local file store is still supported for VPS/Docker installs, but it cannot be
> used as persistent state on Vercel.

### Three ways to add them — pick one, or mix

| Way | How | Needs a redeploy? |
|---|---|---|
| **🌐 Browser** | open `https://<your-app>.vercel.app/api/setup`, fill the form, **💾 Save settings** | **no** — applied on the next request |
| **⌨️ Terminal** | `python setup_cli.py` (Enter skips a question; `--skip` skips everything and prints the page above) | **no** — same store as the browser |
| **▲ Dashboard / CLI** | *Settings → Environment Variables*, or `npx vercel env add BOT_TOKEN` (`python setup_cli.py --vercel-env` does it for you) | yes — variables apply to new deployments only |

The browser form and the terminal wizard write to the **same place** — the
connected KV/Redis under the key `p2p-price-bot:config`, or `runtime_config.json`
next to the bot's data file when there is no Redis. So you can start in the
terminal and finish in the browser (or the other way round), and
`python setup_cli.py --show` prints what is stored, redacted. Environment
variables always win over stored values, and `CRON_SECRET` stays
environment-only (Vercel's cron sends it from there).

> 🔒 The form is public, so it closes as soon as the deployment is configured —
> and if you set `SETUP_SECRET`, every submission must carry it. To change a
> setting later, use the 🔧 **Reconfigure** button described just below. See §10.

### Changing a setting later — the 🔧 Reconfigure button

A running deployment is reconfigured **without a redeploy and without the
Vercel dashboard**:

1. Open `https://<your-app>.vercel.app/api/setup` (or press **⚙️ Reconfigure
   setup** on the status page).
2. Press **🔧 Reconfigure — send me a link**.
3. The bot sends you a **one-time link** in Telegram, to the admin(s) in
   `ADMIN_IDS`. It works once and expires after 15 minutes.
4. Open that link: the form is editable again. Save, and the running instance
   picks the new values up immediately.

Sending `/setup` to the bot in Telegram does the same thing without opening the
browser at all.

Why a link instead of an open form? `/api/setup` is a public URL — a form that
unlocked itself would let anybody who guesses it point your bot at their own
admin id. The link is only ever delivered to the admins, so pressing the button
costs nothing and proving the link proves the admin. `SETUP_SECRET` still works
exactly as before if you prefer a fixed secret.

> ⚠️ Two cases the link cannot cover, because both need a working bot token:
> Telegram is unreachable, or the token was revoked. The page says so and points
> at `python setup_cli.py` — which writes to the same store — and at
> `SETUP_SECRET` in the environment.

The form also opens by itself whenever the deployment **cannot work**: a missing
token, or — the one that used to be invisible — a database whose credentials are
set but which no longer answers (see §9). In both states there is nothing to
protect and everything to repair, so the page that reports the problem is the
page that fixes it.

### The database step is skipped when a database is already there

The setup page does not guess from the environment alone: it **detects** the
database before it asks for one, and skips the step — and the "How to insert the
database" guide — the moment it finds one. It looks in two places, in this
order:

| # | Where it looks | What it answers |
|---|---|---|
| 1 | **This deployment's environment** — a complete `KV_REST_API_URL` + `KV_REST_API_TOKEN` pair (or the Upstash/Redis aliases) | *"already connected — the bot is using it"* — nothing to enter, nothing to redeploy |
| 2 | **Vercel itself** — `GET /v1/storage/stores` for this project, then the project's environment-variable *names* (values are never fetched) | *"connected to this project, but the credentials have not reached this deployment yet"* |

Case 2 is the one that used to send owners off to create a second database: the
store **is** connected, only the environment variables are missing because they
only land on the **next** deployment. The page says exactly that — press
**Connect to this project** again in *Vercel → Storage*, then **Redeploy** — and
it names the store it found, so you can see the step was skipped for a reason.

The Vercel look-up is read-only, cached for five minutes, and **best effort**:

* it needs `VERCEL_PROJECT_ID` (Vercel exposes it when *System Environment
  Variables* are enabled) **and** a credential — `VERCEL_TOKEN` (or
  `P2P_VERCEL_TOKEN`), which you add yourself;
* with neither, no call is made at all and the page simply says it could not
  check, instead of pretending nothing is connected;
* a refused token, an offline host or an API error is reported in the checklist
  and in `GET /api/setup` JSON (`status.database.error`) — never raised.

Nothing secret is ever part of the answer: the detection returns the store
**name**, the provider and where it was found. The checklist and the diagnostics
JSON show `status.database` (`found`, `wired`, `source`, `where`, `skipped`), and
the status page has a **Database detected** row.

> 🔁 A database that is *detected but no longer answers* is not skipped — that is
> the outage the checklist reports as **Not responding**, and the page keeps the
> form and the guide open so it can be repaired (see §9).

## 2. Deploy

### Option 1 — the dashboard

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot&env=BOT_TOKEN,ADMIN_IDS&envDescription=BOT_TOKEN%20and%20ADMIN_IDS%20are%20required%3B%20connect%20Upstash%20for%20Redis%20or%20Vercel%20KV%20after%20creating%20the%20project&project-name=p2p-price-bot)

1. **Import** the repository into Vercel.
2. **Connect storage** — [**Storage**](https://vercel.com/dashboard/stores) →
   **Upstash for Redis** (or Vercel KV) → **Connect to this project**, then
   confirm `KV_REST_API_URL` and `KV_REST_API_TOKEN` exist in the project's
   environment variables. The deployment itself links there whenever no store is
   connected: the **🔌 Connect database ↗** button on `/api/setup` and on the
   status page, and — in Telegram — the same button in the panel, in ⚙️ Settings
   and behind `/database`.
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

## 4. Use it from Telegram

1. Open your bot → `/start` → tap **👥 Set group** and pick your group.
2. Paste a merchant URL (`p2p.binance.com/…`, `bybit.com/…`, `okx.com/…`, `bitget.com/…`).
3. Tap **🟢 Auto: ON**. From then on every cron run posts the prices when they changed.

If the panel still shows **🔌 Connect database ↗**, no KV store reached the
deployment: tap it (or send `/database`) to open **Vercel → Storage**, connect
Upstash for Redis to the project, redeploy, then tap **🔄 Check connection** — the
bot re-reads the credentials and switches store without a restart.

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
1-minute cron does not spam the group. `/api/tick` is protected by
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
| `P2P_DATABASE_LINK` | – | destination of the **🔌 Connect database** link the bot (`/database`, the panel, ⚙️ Settings) and the setup/status pages show when no KV store is connected. Default `https://vercel.com/dashboard/stores`; set it to your team's storage page, an Upstash console or your own Redis docs |
| `SETUP_SECRET` | – (recommended) | locks the `/api/setup` form: every submission must carry it (form field, `?secret=`, or `Authorization: Bearer`). Without it the form is open while the deployment is unconfigured and closes once it is ready |
| `VERCEL_TOKEN` (or `P2P_VERCEL_TOKEN`) | – | lets the setup page ask Vercel which database is already connected to the project, so the step can be skipped (needs `VERCEL_PROJECT_ID`, which Vercel exposes when *System Environment Variables* are enabled) |
| `P2P_VERCEL_PROJECT_ID`, `P2P_VERCEL_TEAM_ID` | – | override the project/team the page asks about (defaults: `VERCEL_PROJECT_ID` / `VERCEL_PROJECT_NAME` and `VERCEL_TEAM_ID`) |
| `P2P_CONFIG_KEY` | – | key the settings saved by the form / `setup_cli.py` live under (default `p2p-price-bot:config`) |
| `P2P_RUNTIME_CONFIG_FILE` | – | file those settings live in when there is no Redis (default `runtime_config.json` next to the data file) |
| `PUBLIC_URL` | – | public URL used to register the webhook; auto-detected from `VERCEL_PROJECT_PRODUCTION_URL`/`VERCEL_URL`, set it for a custom domain or if the page says it cannot tell |
| `WEBHOOK_SECRET` | – | secret Telegram must send with every update; derived from `BOT_TOKEN` when empty |
| `CRON_SECRET` | – (recommended) | locks `/api/tick` down |
| `WEBHOOK_DROP_PENDING` | – | `0` (default) preserves queued updates, including /start; set `1` only to deliberately discard them |
| `ASSET`, `FIAT`, `INTERVAL` | – | pair + description only (on Vercel the cron sets the real interval) |
| `AD_LINK_TEMPLATES` | – | per-exchange deep-link overrides (JSON), same as the polling installs |

Changing a variable only affects the **next** deployment — after editing
**Settings → Environment Variables**, redeploy (`npx vercel --prod` or *Redeploy*
in the dashboard). The setup page can select the backend for the current instance,
but add `P2P_STATE_BACKEND=redis` to the deployment environment as well when you
want that choice to survive every cold start.

## 7. Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/` | `GET` | redirects to `/api/webhook`, so the deployment URL itself opens the status page |
| `/api/webhook` | `POST` | Telegram updates — verified with `X-Telegram-Bot-Api-Secret-Token` (or `?secret=…`, handy for manual tests); everything else is rejected with 403 |
| `/api/webhook` | `GET` | first-start setup UI when `BOT_TOKEN`/`ADMIN_IDS`/KV are missing; otherwise the health page (JSON for scripts, HTML in a browser) that registers the webhook; `?register=1` forces it, `?register=0` only reports |
| `/api/webhook?check=1` | `GET` | status page + a live price fetch for every merchant — open this when prices are empty to see the per-merchant error |
| `/api/setup` | `GET` | the first-start page: readiness checklist + a form for what is missing (rendered whether or not the bot can start, so it is also where you change a setting later) |
| `/api/setup` | `POST` | stores the submitted settings (JSON or form-encoded) and applies them at once; 403 when the deployment is healthy and no authorization is sent, 400 with a readable reason when a value is wrong or Telegram rejects the token |
| `/api/setup` | `POST` `action=send-link` | the 🔧 **Reconfigure** button: sends the admins a one-time link in Telegram that reopens the form (see §6) |
| `/api/tick` | `GET` | one cron round: keep the webhook registered → post prices if they changed → delete stale group messages |
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
| Page shows ⚙️ *Setup needed* (or a 500 JSON with a *hint*) | `BOT_TOKEN`/`ADMIN_IDS`/KV missing or added **after** the last deploy → add them in the form on the page (or `python setup_cli.py`) — no redeploy needed; only environment variables need one |
| `POST /api/setup` answers `403 … the form is locked` | the deployment is configured and healthy → press **🔧 Reconfigure — send me a link** to get a one-time link in Telegram, send `SETUP_SECRET` (set it in the environment first), or change the value with `python setup_cli.py` / in the dashboard |
| The bot forgot its group, merchants and prices, but every page says the database is connected | the database credentials are set yet the store no longer answers (deleted/rotated Upstash database, wrong region). `/api/setup` now shows **Not responding** instead of a green tick and reopens the form — paste working credentials (or connect a new database) and save |
| 🔧 Reconfigure says *the bot could not reach Telegram* | the token is wrong, revoked, or the admin never pressed *Start* on the bot — so the link cannot be delivered. Fix the token with `python setup_cli.py`, or set `SETUP_SECRET` in the environment and open `/api/setup?secret=…` |
| The form saved the settings but the next cold start forgot them | they went to the instance's temporary storage because no KV/Redis is connected → connect Upstash/KV, add the pair to the environment and redeploy |
| Prices are `—`, or `⚠️ …451…` / *restricted* / *forbidden* | the exchange geo-blocks the function's region → keep `"regions": ["fra1"]` in `vercel.json` (EU) and redeploy; diagnose with `/api/webhook?check=1` |
| Deployment URL shows a Vercel 404 | only `/` (redirects to the status page) and `/api/*` exist — check the URL |
| Setup UI says the state store is missing (or an older deployment shows `State store: file — NOT persistent ❌`) | add/connect the Upstash or Vercel KV integration, confirm `KV_REST_API_URL` + `KV_REST_API_TOKEN`, then redeploy |
| `webhook not registered: Cannot tell where this deployment is reachable` | neither `VERCEL_PROJECT_PRODUCTION_URL`/`VERCEL_URL` nor `PUBLIC_URL` is set (e.g. running the file outside Vercel) → set `PUBLIC_URL` |
| Bot answers nothing in Telegram | open `/api/webhook` — it re-registers and shows the last webhook error from Telegram; make sure the group was set and **Auto** is ON |
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
* `POST /api/setup` (the settings form) accepts a submission while the
  deployment is **not** configured, while something it needs is **broken**, and
  whenever the request carries an authorization: `SETUP_SECRET`, or a one-time
  reconfigure link. A healthy, configured deployment can therefore never be
  re-pointed at somebody else's admin id by a stranger who finds the URL.
* The one-time reconfigure link is 256 bits of randomness, is stored **only as a
  SHA-256 fingerprint** (so a leak of the database cannot rebuild it), expires
  after 15 minutes, is spent by the save it authorizes, and is never shown to
  the person who pressed the button — it is delivered to the admins in Telegram,
  which is what makes it safe for the button to be public.
* The token you submit is checked with Telegram (`getMe`) before it is stored, it
  is stored in your own KV/Redis (never in the code or the logs), and it is never
  echoed back — the checklist shows a redacted copy only.
* `config.json`, `data.json`, `runtime_config.json` and `.env` are git-ignored; on
  Vercel everything secret lives in the project's environment variables or in the
  KV store the deployment already trusts.

### Setup activates Telegram delivery

Saving a complete Vercel setup now registers the webhook before returning. No
extra status-page visit is needed. Failed activation is reported as a warning;
the settings remain saved. The webhook must use a public production domain,
not a deployment URL protected by Vercel login. Set `PUBLIC_URL` to that domain
if the automatically selected domain is not publicly accessible.
