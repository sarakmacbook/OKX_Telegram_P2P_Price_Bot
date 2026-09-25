"""vercel.json regression guard: the deployment must stay shippable.

* ``regions`` — the exchange P2P APIs geo-block US IPs (Binance answers HTTP 451
  there), while Vercel's default region is ``iad1`` (US). The functions must run
  in the EU. One region keeps the free Hobby plan working.
* ``redirects`` — opening the deployment root must land on the status page,
  not on Vercel's 404.
* ``crons`` — ``/api/tick`` replaces the polling JobQueue; the default schedule
  must stay Hobby-compatible (once a day — anything faster fails the deploy).
* ``maxDuration`` — must stay within the Hobby limit (300s).
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    return json.loads((ROOT / "vercel.json").read_text())


def test_functions_key_matches_the_api_files():
    config = load()
    assert "api/**/*.py" in config.get("functions", {})


def test_functions_run_in_the_eu_not_the_default_us_region():
    functions = load()["functions"]["api/**/*.py"]
    assert functions.get("regions") == ["fra1"], (
        "one EU region (Hobby-compatible): the exchanges geo-block US IPs, "
        "so Vercel's default iad1 serves no prices")


def test_max_duration_stays_within_the_hobby_limit():
    functions = load()["functions"]["api/**/*.py"]
    assert functions.get("maxDuration", 15) <= 300


def test_deployment_root_redirects_to_the_status_page():
    redirects = load().get("redirects", [])
    assert {"source": "/", "destination": "/api/webhook", "permanent": False} in redirects


def test_tick_cron_exists_and_is_a_valid_schedule():
    crons = load().get("crons", [])
    tick = [c for c in crons if c.get("path") == "/api/tick"]
    assert len(tick) == 1, "one cron must drive /api/tick (it replaces the JobQueue)"
    assert len(tick[0].get("schedule", "").split()) == 5, "a 5-field cron expression is required"
