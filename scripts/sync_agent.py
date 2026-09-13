#!/usr/bin/env python3
"""
Push a fresh set of dashboard payloads from this Mac to the deployed backend.

Garmin blocks datacenter IPs for both login and token refresh, so the deployed
service can never renew its own session — it goes blank every day or two. This
machine has no such restriction, so it does the Garmin work and ships the
results. The server falls back to whatever was last pushed.

Run it from cron/launchd every few hours. Nothing here needs the server to be
reachable to succeed at the Garmin half, so a failed push is retried simply by
running again.

    SYNC_SECRET=... python3 scripts/sync_agent.py
    SYNC_SECRET=... python3 scripts/sync_agent.py --dry-run
"""

from __future__ import annotations

import os
import sys
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = os.environ.get("RACE_COACH_URL", "https://race-coach-api.onrender.com")
TIMEOUT = 180

# Keys must match what the server's serve() calls look up, or the fallback
# silently never fires.
def collect() -> dict:
    import main

    jobs = {
        "overview:week": lambda: main._overview_live("week"),
        "overview:month": lambda: main._overview_live("month"),
        "training:0": lambda: main._training_live(0),
        "sleep": lambda: main._sleep_live(),
        "fitness": lambda: main._fitness_live(),
    }

    payloads, failed = {}, []
    for key, fn in jobs.items():
        started = time.time()
        try:
            value = fn()
        except Exception as e:
            failed.append(f"{key}: {type(e).__name__}: {e}")
            print(f"  {key:16} FAILED  {type(e).__name__}: {str(e)[:80]}")
            continue
        # A payload carrying Garmin's empty-body error is worse than no
        # payload: pushing it would overwrite a good snapshot with a broken
        # one, which is precisely the outage this exists to prevent.
        from snapshot import looks_broken
        if looks_broken(value):
            failed.append(f"{key}: live call returned an error payload")
            print(f"  {key:16} BROKEN  (not pushed)")
            continue
        payloads[key] = value
        print(f"  {key:16} ok      {time.time() - started:.1f}s")

    return payloads, failed


def push(payloads: dict, secret: str) -> dict:
    req = urllib.request.Request(
        f"{BACKEND}/api/sync/push",
        data=json.dumps({"payloads": payloads}, default=str).encode(),
        headers={"Content-Type": "application/json", "X-Sync-Secret": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main_() -> int:
    here = Path(__file__).resolve().parent.parent / "backend"
    if not here.is_dir():
        print(f"backend/ not found at {here}")
        return 2
    sys.path.insert(0, str(here))
    os.chdir(here)

    try:
        from dotenv import load_dotenv
        load_dotenv(here / ".env")
    except Exception:
        pass

    dry = "--dry-run" in sys.argv
    secret = os.environ.get("SYNC_SECRET")
    if not secret and not dry:
        print("SYNC_SECRET is not set — cannot push. Set it here and on the server.")
        return 2

    print(f"collecting from Garmin ({BACKEND})")
    payloads, failed = collect()

    if not payloads:
        print("nothing collected — leaving the existing snapshot alone")
        return 1

    if dry:
        print(f"\ndry run — would push {len(payloads)} payloads: {sorted(payloads)}")
        return 0

    result = push(payloads, secret)
    print(f"\npush: {result}")
    if failed:
        print("partial: " + "; ".join(failed))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main_())
