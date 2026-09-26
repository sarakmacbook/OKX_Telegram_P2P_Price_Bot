#!/usr/bin/env python3
"""Terminal setup — the other way to hand the bot its required settings.

    python setup_cli.py                 asks for what is missing
    python setup_cli.py --skip          nothing to do now: prints the web UI, exits 0
    python setup_cli.py --show          what is stored (redacted), and where
    python setup_cli.py --clear         forget the stored settings
    python setup_cli.py --token 123:ABC --admins 123456789 --yes     no prompts
    python setup_cli.py --state-backend file                           use data.json
    python setup_cli.py --state-backend redis --kv-url URL --kv-token TOKEN

Every answer is optional: **press Enter on any question to skip it** and add it
later in the browser, on the first-start page (``/api/setup``) — the wizard
prints that address when it is done, and ``--skip`` skips the whole thing.

Both paths write to the same store (``runtime_config``): the connected
KV/Redis under the key ``p2p-price-bot:config``, or ``runtime_config.json`` next
to the bot's data file.  That is what makes a Vercel deployment work without a
redeploy, and what lets you start in the terminal and finish in the browser (or
the other way round) without losing anything.

``--vercel-env`` additionally writes the values into the deployment's environment
variables through the Vercel CLI — the only way to set ``CRON_SECRET``, which
Vercel's own cron sends from there.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime_config                                     # noqa: E402
import storage                                            # noqa: E402

SETUP_PATH = "/api/setup"
ENV_ONLY = ("CRON_SECRET",)                # Vercel's cron reads these from the deployment
PUSHABLE = ("BOT_TOKEN", "ADMIN_IDS", "ASSET", "FIAT", "INTERVAL",
            "P2P_STATE_BACKEND", "KV_REST_API_URL", "KV_REST_API_TOKEN", "CRON_SECRET")


def env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names`` (the same aliases serverless.py uses)."""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return default


def web_ui_url() -> str:
    """The setup page of this deployment, when its address is known."""
    base = env("PUBLIC_URL", "P2P_PUBLIC_URL", "WEBHOOK_URL",
               "VERCEL_PROJECT_PRODUCTION_URL").rstrip("/")
    if not base:
        host = env("VERCEL_URL").split("/")[0].strip()
        base = f"https://{host}" if host else ""
    if not base:
        return f"https://<your-app>.vercel.app{SETUP_PATH}"
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return f"{base.rstrip('/')}{SETUP_PATH}"


def skip_message(reason: str = "") -> None:
    """What the user sees when the terminal part is skipped — the browser is next."""
    print("\n🌐 Skipped" + (f" — {reason}" if reason else "") + ".")
    print("   Finish the setup in the browser instead; the page stores what you enter,")
    print("   no redeploy needed:\n")
    print(f"     {web_ui_url()}\n")
    print("   …or come back to the terminal at any time:  python setup_cli.py")
    print("   See what is already stored with:            python setup_cli.py --show\n")


class Prompts:
    """The five questions, with an injectable ``input`` so tests can answer them."""

    def __init__(self, reader=None, echo: bool = True):
        self.reader = reader or input
        self.echo = echo

    def _read(self, prompt: str, secret: bool = False) -> str:
        if secret and self.echo and sys.stdin.isatty():
            try:
                return getpass.getpass(prompt).strip()
            except (EOFError, KeyboardInterrupt, ValueError):
                print()
                return ""
        try:
            return self.reader(prompt).strip()
        except EOFError:
            return ""

    def yes_no(self, question: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self._read(f"{question} {suffix} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("  ❌ Answer y or n.")

    def ask(self, label: str, variable: str, *, current: str = "", secret: bool = False,
            check=None, hint: str = "") -> str:
        """One optional question: Enter keeps/skips, a wrong answer is asked again."""
        stored = runtime_config.mask(current) if current and secret else current
        shown = f" [{stored}]" if stored and not secret else ""
        if hint:
            print(f"  {hint}")
        while True:
            value = self._read(f"{label} ({variable}){shown}: ", secret=secret)
            if not value:
                return ""                      # skipped — the web UI can do it later
            if check is None:
                return value
            clean, error = check(value)
            if not error:
                return clean
            print(f"  ❌ {error}")


def _checks():
    """Validators reused from runtime_config, so browser and terminal agree."""
    def token(value: str):
        return runtime_config._clean_token(value)

    def admins(value: str):
        return runtime_config._clean_admins(value)

    def code(value: str):
        clean, errors = runtime_config.validate({"ASSET": value})
        return (clean.get("ASSET", ""), errors[0] if errors else "")

    def interval(value: str):
        clean, errors = runtime_config.validate({"INTERVAL": value})
        return (clean.get("INTERVAL", ""), errors[0] if errors else "")

    return token, admins, code, interval


def push_to_vercel_env(values: dict, target: str = "production") -> list[str]:
    """``vercel env add`` for each value — the terminal way to set deployment variables.

    Best effort: the Vercel CLI must be installed and logged in (``vercel login``),
    and the project linked (``vercel link``).  Every failure is reported as text,
    never as an exception, because the store already has the values by then.
    """
    lines: list[str] = []
    # ``vercel env add`` rejects duplicates. Integrations commonly create
    # KV_REST_API_URL/TOKEN before this wizard runs, so discover existing names
    # and leave their values untouched instead of turning a successful setup
    # into a confusing error.
    existing: set[str] = set()
    try:
        listing = subprocess.run(["vercel", "env", "ls", target],
                                 capture_output=True, timeout=60)
        output = (listing.stdout + listing.stderr).decode(errors="replace")
        existing = {name for name in PUSHABLE if name in output}
    except Exception:
        pass
    for name, value in values.items():
        if name not in PUSHABLE or not value:
            continue
        if name in existing:
            lines.append(f"↷ {name} already exists on Vercel ({target}); left unchanged")
            continue
        try:
            done = subprocess.run(["vercel", "env", "add", name, target],
                                  input=str(value).encode(), capture_output=True, timeout=60)
        except FileNotFoundError:
            return ["The Vercel CLI is not installed — npm i -g vercel, or set the variables "
                    "in the dashboard."]
        except Exception as exc:                # pragma: no cover - defensive
            return [f"vercel env add {name} failed: {type(exc).__name__}: {exc}"]
        if done.returncode == 0:
            lines.append(f"✓ {name} → Vercel ({target})")
        else:
            detail = (done.stderr or done.stdout or b"").decode(errors="replace").strip()
            lines.append(f"✗ {name} → {detail.splitlines()[-1] if detail else 'rejected'}")
    return lines


def show() -> int:
    """``--show`` — where the settings are, and what they look like redacted."""
    info = runtime_config.summary(ROOT)
    stored = runtime_config.load(ROOT)
    print("\n📦 Stored settings\n" + "-" * 46)
    print(f"  store      {info['store']}")
    print(f"  persistent {'yes' if info['persistent'] else 'no — a restart forgets them'}")
    for name in runtime_config.SUPPORTED:
        from_env = (os.getenv(name) or "").strip()
        value = from_env or stored.get(name, "")
        where = "environment" if from_env and name not in runtime_config.injected() else (
            "stored" if stored.get(name) else "—")
        # Only the token is a secret: the rest are the owner's own settings, and
        # this is their terminal.
        shown = runtime_config.mask(value) if name in runtime_config.SECRETS else value
        print(f"  {name:<12} {(shown or 'not set'):<20} {where}")
    for name in ENV_ONLY:
        print(f"  {name:<12} {(runtime_config.mask(env(name)) or 'not set'):<20} "
              "environment only")
    print(f"\n  web UI     {web_ui_url()}\n")
    return 0


def wizard(args: argparse.Namespace) -> int:
    """Ask the questions, store the answers (Enter skips any of them)."""
    prompts = Prompts()
    token_check, admins_check, code_check, interval_check = _checks()
    stored = runtime_config.load(ROOT)
    info = runtime_config.describe(ROOT)

    print("\n🤖 P2P Price Bot — required settings\n" + "-" * 46)
    print("  Two ways to finish, both write to the same place:")
    print("    • here in the terminal (this wizard)")
    print(f"    • later in the browser:  {web_ui_url()}")
    print("  Press Enter on any question to skip it and add it in the browser later.\n")
    print(f"  settings will be stored in: {info['store']}"
          f"{' (temporary — no KV/Redis connected)' if not info['persistent'] else ''}")

    if not args.yes and not prompts.yes_no("\nEnter the settings now?", default=True):
        skip_message("you chose the web UI")
        return 0

    values: dict[str, str] = {}
    answers = {
        "BOT_TOKEN": args.token or prompts.ask(
            "Bot token from @BotFather", "BOT_TOKEN", current=stored.get("BOT_TOKEN", ""),
            secret=True, check=token_check,
            hint="  Get it from @BotFather → /newbot."),
        "ADMIN_IDS": args.admins or prompts.ask(
            "Your Telegram ID(s), comma-separated", "ADMIN_IDS",
            current=stored.get("ADMIN_IDS", ""), check=admins_check,
            hint="  @userinfobot tells you your numeric ID."),
    }
    if args.asset or args.fiat or args.interval or args.yes:
        answers["ASSET"] = args.asset
        answers["FIAT"] = args.fiat
        answers["INTERVAL"] = args.interval
    else:
        print("\n  Optional (Enter keeps the current value):")
        answers["ASSET"] = prompts.ask("Asset", "ASSET", current=env("ASSET", default="USDT"),
                                       check=code_check)
        answers["FIAT"] = prompts.ask("Fiat currency", "FIAT", current=env("FIAT", default="USD"),
                                      check=code_check)
        answers["INTERVAL"] = prompts.ask("Check prices every N seconds", "INTERVAL",
                                          current=env("INTERVAL", default="60"),
                                          check=interval_check)

    # Database selection is intentionally a flag rather than an extra prompt,
    # preserving the quick Enter-to-skip wizard flow.  The setup page exposes
    # the same choice as a select control.
    if args.state_backend:
        answers["P2P_STATE_BACKEND"] = args.state_backend

    kv_url, kv_token = args.kv_url, args.kv_token
    if not storage.redis_config() and not args.yes:
        print("\n  State store — needed on Vercel, optional on a VPS/Docker install.")
        print("  (Upstash for Redis → REST API → endpoint + token.)")
        kv_url = kv_url or prompts.ask("KV/Redis REST URL", "KV_REST_API_URL",
                                       current=env("KV_REST_API_URL"))
        kv_token = kv_token or prompts.ask("KV/Redis REST token", "KV_REST_API_TOKEN",
                                           current=env("KV_REST_API_TOKEN"), secret=True)
        if bool(kv_url) != bool(kv_token):
            print("  ❌ The URL and the token belong together — both were ignored.")
            kv_url = kv_token = ""

    values = {name: str(value).strip() for name, value in answers.items()
              if value is not None and str(value).strip()}
    if not values and not kv_url:
        skip_message("nothing was entered")
        return 0

    cleaned, errors = runtime_config.validate(values)
    if errors:
        for error in errors:
            print(f"  ❌ {error}")
        return 1

    if (cleaned.get("P2P_STATE_BACKEND") == "redis"
            and not (storage.redis_config() or (kv_url and kv_token))):
        print("  ❌ Redis was selected as the state database, but no KV/Redis REST URL and token were supplied.")
        return 1

    if cleaned.get("BOT_TOKEN") and not args.no_verify:
        ok, detail = runtime_config.verify_bot_token(cleaned["BOT_TOKEN"])
        print(f"  {'✓' if ok else ('?' if ok is None else '❌')} {detail}")
        if ok is False:
            print("  Nothing was saved — pass --no-verify to store it anyway.")
            return 1

    saved = runtime_config.save(cleaned, ROOT)
    missing = [name for name in cleaned if saved.get(name) != cleaned[name]]
    if missing:
        print(f"  ⚠️ Could not write {', '.join(missing)} to {info['store']}")
    for name, value in cleaned.items():
        os.environ[name] = value
    if kv_url and kv_token:
        reachable, detail = _probe(kv_url, kv_token)
        print(f"  {'✓' if reachable else '❌'} KV/Redis: {detail}")
        if reachable:
            os.environ["KV_REST_API_URL"], os.environ["KV_REST_API_TOKEN"] = kv_url, kv_token
            storage.RedisStore(kv_url, kv_token, key=runtime_config.config_key()).save(cleaned)
        else:
            print("  The KV pair was not stored — fix the endpoint and run this again.")
    runtime_config.invalidate()

    print(f"\n✅ Saved {', '.join(n for n in runtime_config.SUPPORTED if n in cleaned) or 'the KV connection'} "
          f"to {runtime_config.describe(ROOT)['store']}")
    if not runtime_config.describe(ROOT)["persistent"]:
        print("   ⚠️ That store is temporary: connect Upstash for Redis (or Vercel KV) to keep")
        print("      the settings — and the group, merchants and prices — across restarts.")

    if args.vercel_env:
        print("\n☁️ Vercel environment variables:")
        pushable = {**cleaned, "KV_REST_API_URL": kv_url, "KV_REST_API_TOKEN": kv_token}
        for line in push_to_vercel_env(pushable, args.vercel_target):
            print(f"   {line}")
        print("   Redeploy for the new variables to apply (Deployments → ⋯ → Redeploy).")

    still_missing = [check for check in ("BOT_TOKEN", "ADMIN_IDS") if not env(check)]
    if still_missing or not storage.redis_config():
        print(f"\n🌐 Finish the rest in the browser: {web_ui_url()}")
        if still_missing:
            print(f"   still missing: {', '.join(still_missing)}")
    else:
        print("\n✅ Nothing else is required — open the status page to register the webhook.")
    print()
    return 0


def _probe(url: str, token: str) -> tuple[bool, str]:
    import httpx

    try:
        reply = httpx.post(url, json=["GET", runtime_config.config_key()], timeout=6.0,
                           headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"})
        reply.raise_for_status()
        return True, "the store answered"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_cli.py",
        description="Store the settings the bot needs — or skip and finish in the web UI.",
        epilog=f"Skip everything and do it in the browser later: python setup_cli.py --skip "
               f"(the page is {SETUP_PATH} on your deployment).")
    parser.add_argument("--token", help="Bot token from @BotFather")
    parser.add_argument("--admins", help="Telegram user ID(s), comma-separated")
    parser.add_argument("--asset", help="Asset, e.g. USDT")
    parser.add_argument("--fiat", help="Fiat, e.g. USD")
    parser.add_argument("--interval", help="Check interval in seconds")
    parser.add_argument("--state-backend", choices=storage.BACKENDS,
                        help="State database: auto (default), file, or redis")
    parser.add_argument("--kv-url", dest="kv_url", help="KV/Redis REST URL")
    parser.add_argument("--kv-token", dest="kv_token", help="KV/Redis REST token")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Do not ask anything — store what the flags/env provide")
    parser.add_argument("--skip", action="store_true",
                        help="Store nothing now; print the web UI address and exit")
    parser.add_argument("--show", action="store_true",
                        help="Print what is stored (redacted), and where")
    parser.add_argument("--clear", action="store_true",
                        help="Forget the stored settings (environment variables are kept)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Do not check the token with Telegram before storing it")
    parser.add_argument("--vercel-env", action="store_true",
                        help="Also write the values to the deployment environment "
                             "(needs the Vercel CLI: npm i -g vercel)")
    parser.add_argument("--vercel-target", default="production",
                        help="Vercel target for --vercel-env (default: production)")
    args, _unknown = parser.parse_known_args(argv)

    if args.show:
        return show()
    if args.clear:
        runtime_config.clear(ROOT)
        print("🧹 Stored settings forgotten (environment variables are untouched).")
        return 0
    if args.skip:
        skip_message("you asked to do it in the web UI")
        return 0
    return wizard(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:                     # Ctrl-C mid-question = "later, in the web UI"
        skip_message("interrupted")
        sys.exit(130)
