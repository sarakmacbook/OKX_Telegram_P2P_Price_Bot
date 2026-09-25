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

> ⚠️ Without a KV store the bot still deploys, but every request may land in a
> different instance with an empty local disk, so it "forgets" the group, the
> merchants and the prices. `/api/webhook` shows
> **State store: file — NOT persistent ❌** when that is the case.

## 2. Deploy

### Option 1 — the dashboard

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fsarakmacbook%2FOKX_Telegram_P2P_Price_Bot&env=BOT_TOKEN,ADMIN_IDS,KV_REST_API_URL,KV_REST_API_TOKEN&envDescription=BOT_TOKEN%20and%20ADMIN_IDS%20are%20required%3B%20KV_REST_API_URL%2FTOKEN%20come%20from%20the%20KV%20or%20Upstash%20integration&project-name=p2p-price-bot)

1. **Import** the repository into Vercel.
2. **Environment variables**: `BOT_TOKEN`, `ADMIN_IDS` (and the two `KV_*` values,
   if the KV store is not connected yet).
3. **Deploy**.

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
| `P2P_STATE_KEY` | – | key the state lives under (default `p2p-price-bot:state`) |
| `PUBLIC_URL` | – | public URL used to register the webhook; auto-detected from `VERCEL_PROJECT_PRODUCTION_URL`/`VERCEL_URL`, set it for a custom domain or if the page says it cannot tell |
| `WEBHOOK_SECRET` | – | secret Telegram must send with every update; derived from `BOT_TOKEN` when empty |
| `CRON_SECRET` | – (recommended) | locks `/api/tick` down |
| `WEBHOOK_DROP_PENDING` | – | `1` (default) drops updates Telegram queued while you were offline, `0` replays them after a redeploy |
| `ASSET`, `FIAT`, `INTERVAL` | – | pair + description only (on Vercel the cron sets the real interval) |
| `AD_LINK_TEMPLATES` | – | per-exchange deep-link overrides (JSON), same as the polling installs |

Changing a variable only affects the **next** deployment — after editing
**Settings → Environment Variables**, redeploy (`npx vercel --prod` or *Redeploy*
in the dashboard).

## 7. Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/` | `GET` | redirects to `/api/webhook`, so the deployment URL itself opens the status page |
| `/api/webhook` | `POST` | Telegram updates — verified with `X-Telegram-Bot-Api-Secret-Token` (or `?secret=…`, handy for manual tests); everything else is rejected with 403 |
| `/api/webhook` | `GET` | status page (JSON for scripts, HTML in a browser); registers the webhook, `?register=1` forces it, `?register=0` only reports |
| `/api/webhook?check=1` | `GET` | status page + a live price fetch for every merchant — open this when prices are empty to see the per-merchant error |
| `/api/tick` | `GET` | one cron round: keep the webhook registered → post prices if they changed → delete stale group messages |
| anything else | – | `404` (JSON) — the deployment is one catch-all function, so the router answers what the platform's 404 used to |

## 8. How it works under the hood

* **One function, one entry point**: Vercel's Python runtime builds a Python
  project as a **single application** — it loads one top-level `app` and rewrites
  *every* request to it (files in `api/` are no longer functions of their own).
  `api/app.py` is that entry point and does the routing the platform used to do:
  `/api/webhook` → `api/webhook.py`, `/api/tick` → `api/tick.py`, `/` → the status
  page, anything else → 404. `pyproject.toml` declares it:

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
| Page shows ⚙️ *Setup needed* (or a 500 JSON with a *hint*) | `BOT_TOKEN`/`ADMIN_IDS`/KV missing or added **after** the last deploy → follow the steps on the page, then redeploy |
| Prices are `—`, or `⚠️ …451…` / *restricted* / *forbidden* | the exchange geo-blocks the function's region → keep `"regions": ["fra1"]` in `vercel.json` (EU) and redeploy; diagnose with `/api/webhook?check=1` |
| Deployment URL shows a Vercel 404 | only `/` (redirects to the status page) and `/api/*` exist — check the URL |
| `State store: file — NOT persistent ❌` | no KV store connected → add the integration, redeploy |
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
* `config.json`, `data.json` and `.env` are git-ignored; on Vercel everything
  secret lives in the project's environment variables.
