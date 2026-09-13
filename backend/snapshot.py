"""
Server-side cache of dashboard payloads, refreshed from a trusted machine.

Garmin puts login *and* token refresh behind Cloudflare, which blocks
datacenter IPs. A host like Render can resume a live session but can never
renew one, so its access token dies every day or two and the dashboard goes
blank. The athlete's own machine has no such restriction.

So the Garmin conversation moves there: a local agent produces these payloads
and pushes them here, and every read endpoint falls back to the stored copy
when its own live call fails. The failure mode becomes "data is a few hours
old" instead of "data is gone", and nobody re-pastes a token again.

Stored payloads are the API's own responses, verbatim — no schema of its own
to drift out of step with the endpoints.
"""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path

SNAPSHOT_FILE = Path(__file__).parent / "snapshot_cache.json"

# Payloads older than this are still served — stale data beats no data — but
# they are flagged so the UI can say so rather than quietly showing last week.
STALE_AFTER_HOURS = 12


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    if SNAPSHOT_FILE.exists():
        try:
            return json.loads(SNAPSHOT_FILE.read_text())
        except Exception as e:
            print(f"snapshot load failed: {e}")
    return {"pushed_at": None, "payloads": {}}


def save(payloads: dict) -> dict:
    """Replace the stored payloads. Returns a small summary for the caller."""
    data = {"pushed_at": _now(), "payloads": payloads}
    try:
        SNAPSHOT_FILE.write_text(json.dumps(data, default=str))
    except Exception as e:
        print(f"snapshot save failed: {e}")
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pushed_at": data["pushed_at"], "keys": sorted(payloads)}


def age_hours(pushed_at: str | None) -> float | None:
    if not pushed_at:
        return None
    try:
        then = dt.datetime.fromisoformat(pushed_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 3600
    except Exception:
        return None


def get(key: str) -> dict | None:
    """Stored payload for one endpoint, tagged with how old it is."""
    data = load()
    payload = (data.get("payloads") or {}).get(key)
    if payload is None:
        return None
    age = age_hours(data.get("pushed_at"))
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["_cached"] = True
        payload["_as_of"] = data.get("pushed_at")
        payload["_age_hours"] = round(age, 1) if age is not None else None
        payload["_stale"] = bool(age and age > STALE_AFTER_HOURS)
    return payload


def looks_broken(payload) -> bool:
    """
    True when a live payload came back empty or carrying Garmin's failure.

    Garmin answers a rejected session with an empty body, which surfaces as
    "Expecting value: line 1 column 1" nested somewhere inside an otherwise
    HTTP-200 response — so a status code is not enough to decide whether the
    live call actually produced anything worth serving.
    """
    if payload is None:
        return True
    if isinstance(payload, dict):
        if "error" in payload:
            return True
        blob = json.dumps(payload, default=str)
        if "Expecting value: line 1 column 1" in blob:
            return True
    return False


def serve(key: str, producer):
    """
    Live value when it works, the stored copy when it does not.

    Never raises on the fallback path: an endpoint that would otherwise 500 is
    exactly the case this exists for.
    """
    try:
        live = producer()
    except Exception as e:
        print(f"{key}: live call raised ({e}) — serving snapshot")
        live = None

    if not looks_broken(live):
        return live

    cached = get(key)
    if cached is not None:
        return cached
    return live if live is not None else {"error": f"No live data and no snapshot for {key}."}
