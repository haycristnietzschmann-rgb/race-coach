"""
Push planned sessions to Garmin Connect as **structured workouts** (warm-up /
repeat[interval + recovery] / cool-down) and drop them on the training calendar
by date, so they're selectable on the watch.

The installed garminconnect has no workout API, so this talks to Garmin's
workout-service / schedule endpoints directly through the already-authenticated
session (client.api.connectapi / client.api.garth). Those endpoints are
community-reverse-engineered — every call is wrapped and returns a plain result
dict the frontend can display, success or failure.
"""
from __future__ import annotations

import os
import re
import datetime as dt

from garmin_client import get_client

# Garmin enum ids (workout-service)
_SPORT = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running"},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling"},
}
_STEP = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recovery": {"stepTypeId": 5, "stepTypeKey": "recovery"},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat"},
    "other": {"stepTypeId": 7, "stepTypeKey": "other"},
}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_END_DIST = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
_END_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
_TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
_TARGET_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}


# ------------------------------------------------------------ prescription parse

def _mid(lo_hi: str) -> float:
    """'45–60' -> 52.5 ; '3' -> 3"""
    parts = re.split(r"[–-]", lo_hi)
    nums = [float(p) for p in parts if p.strip().replace(".", "").isdigit()]
    return sum(nums) / len(nums) if nums else 0.0


def _to_seconds(value: str, unit: str) -> int:
    v = _mid(value)
    u = unit.lower()
    if u.startswith("h"):
        return int(v * 3600)
    if u.startswith("m") and u != "m":            # min / mins
        return int(v * 60)
    if u == "m":                                   # ambiguous 'm' after a distance handled elsewhere
        return int(v * 60)
    return int(v)                                  # sec / s


def _pace_to_mps(txt: str):
    """'5:20/km' -> (low_mps, high_mps) with a small window."""
    m = re.search(r"(\d+):(\d{2})\s*/\s*km", txt)
    if not m:
        return None
    sec_per_km = int(m.group(1)) * 60 + int(m.group(2))
    mps = 1000.0 / sec_per_km
    return round(mps * 0.97, 3), round(mps * 1.03, 3)


def _exec_step(order: int, kind: str, end_val: int, end_cond=None, target=None,
               t1=None, t2=None, desc: str = "") -> dict:
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP[kind],
        "endCondition": end_cond or _END_TIME,
        "endConditionValue": end_val,
        "targetType": target or _TARGET_NONE,
    }
    if t1 is not None:
        step["targetValueOne"] = t1
    if t2 is not None:
        step["targetValueTwo"] = t2
    if desc:
        step["description"] = desc[:512]
    return step


def parse_prescription(text: str, sport: str) -> list[dict]:
    """Turn a free-text prescription into Garmin workout steps. Best-effort:
    whatever can't be parsed becomes a single timed step carrying the text, so
    a workout is always produced."""
    text = (text or "").strip()
    steps: list[dict] = []
    order = 1
    low = text.lower()
    is_long = any(w in low for w in ("long ride", "long run", "endurance", "z2", "mostly z2", "aerobic"))
    warm = 900 if sport == "cycling" else 600
    cool = 600 if sport == "cycling" else 300

    # repeat: "6×3 min ... / 2 min jog"  or  "3x10min @ threshold, 5 min easy"
    rep = re.search(
        r"(\d+)\s*(?:[–-]\s*\d+\s*)?[×x]\s*"
        r"(\d+(?:[–-]\d+)?)\s*(min|mins|minutes|sec|secs|s|h)\b"
        r"(?:[^,/]*?(?:@\s*[\d:]+\s*/\s*km)?)?"
        r"(?:\s*[,/]\s*(\d+(?:[–-]\d+)?)\s*(min|mins|sec|secs|s)\s*"
        r"(?:jog|easy|float|recovery|rest|spin|walk)?)?",
        low,
    )
    total = re.search(r"(\d+(?:[–-]\d+)?(?:\.\d+)?)\s*(h|hour|hours|min|mins|minutes)\b", low)
    dist = re.search(r"(\d+(?:[–-]\d+)?)\s*km\b", low)
    pace = _pace_to_mps(low)

    if rep:
        reps = int(rep.group(1))
        work_s = _to_seconds(rep.group(2), rep.group(3))
        rec_s = _to_seconds(rep.group(4), rep.group(5)) if rep.group(4) else 90
        rep_total = reps * (work_s + rec_s)

        # "2-2.5 h aerobic with 2x20 min SS" / "50 min Z2 + 6x20s strides":
        # a stated total duration AROUND the reps -> steady base, then the set.
        total_s = _to_seconds(total.group(1), total.group(2)) if total else 0
        if total_s and total_s > rep_total + warm:
            steps.append(_exec_step(order, "warmup", warm, desc="Warm-up easy")); order += 1
            steady = max(300, total_s - rep_total - warm - cool)
            steps.append(_exec_step(order, "interval", steady, desc="Steady aerobic base")); order += 1
        else:
            steps.append(_exec_step(order, "warmup", warm, desc="Warm-up easy")); order += 1

        inner = [
            _exec_step(order + 1, "interval", work_s,
                       target=_TARGET_PACE if pace else _TARGET_NONE,
                       t1=pace[0] if pace else None, t2=pace[1] if pace else None,
                       desc=text),
            _exec_step(order + 2, "recovery", rec_s, desc="Easy recovery"),
        ]
        steps.append({
            "type": "RepeatGroupDTO",
            "stepOrder": order,
            "stepType": _STEP["repeat"],
            "numberOfIterations": reps,
            "smartRepeat": False,
            "workoutSteps": inner,
        })
        order += 3
        steps.append(_exec_step(order, "cooldown", cool, desc="Cool-down easy"))
        return steps

    # steady session with a stated duration or distance
    if total:
        secs = _to_seconds(total.group(1), total.group(2))
        if not is_long:
            steps.append(_exec_step(order, "warmup", warm, desc="Warm-up")); order += 1
            secs = max(300, secs - warm - cool)
        steps.append(_exec_step(order, "interval", secs,
                                target=_TARGET_PACE if pace else _TARGET_NONE,
                                t1=pace[0] if pace else None, t2=pace[1] if pace else None,
                                desc=text)); order += 1
        if not is_long:
            steps.append(_exec_step(order, "cooldown", cool, desc="Cool-down"))
        return steps

    if dist:
        km = _mid(dist.group(1))
        steps.append(_exec_step(order, "interval", int(km * 1000), end_cond=_END_DIST,
                                target=_TARGET_NONE, desc=text))
        return steps

    # fallback: one lap-button step carrying the whole instruction
    return [_exec_step(1, "other", 0, end_cond=_END_LAP, desc=text or "See plan")]


def _renumber(steps: list[dict], start: int = 1) -> int:
    n = start
    for s in steps:
        s["stepOrder"] = n
        n += 1
        if s.get("type") == "RepeatGroupDTO":
            n = _renumber(s.get("workoutSteps", []), n)
    return n


def build_workout_payload(name: str, sport_key: str, steps: list[dict]) -> dict:
    _renumber(steps)
    sport = _SPORT.get(sport_key, _SPORT["running"])
    return {
        "workoutName": name[:80],
        "description": "Pushed from Training Coach",
        "sportType": sport,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": sport,
            "workoutSteps": steps,
        }],
    }


# ------------------------------------------------------------------ Garmin calls

def _api_post(path: str, payload):
    """POST to a connectapi path, tolerating the couple of shapes the client
    wrapper can return."""
    api = get_client().api
    try:
        return api.connectapi(path, method="POST", json=payload)
    except TypeError:
        pass
    # older wrapper: go through garth directly
    resp = api.garth.connectapi(path, method="POST", json=payload)
    if hasattr(resp, "json"):
        try:
            return resp.json()
        except Exception:
            return {"status": getattr(resp, "status_code", "ok")}
    return resp


_SPORT_ID = {"Run": "running", "Bike": "cycling", "running": "running", "cycling": "cycling"}


def push_workout(name: str, sport: str, prescription: str, date: str | None = None) -> dict:
    """Create one structured workout, optionally schedule it on `date`."""
    sport_key = _SPORT_ID.get(sport, "running")
    steps = parse_prescription(prescription, sport_key)
    payload = build_workout_payload(name, sport_key, steps)
    if os.environ.get("GARMIN_FIXTURE_MODE"):
        return {"ok": True, "workout_id": "fixture", "name": name, "steps": len(steps),
                "scheduled": date, "note": "fixture mode — parsed OK, not sent to Garmin",
                "payload_preview": payload}
    try:
        created = _api_post("/workout-service/workout", payload) or {}
        wid = created.get("workoutId") or created.get("workoutid") or created.get("id")
        if not wid:
            return {"ok": False, "error": "Garmin did not return a workoutId", "raw": str(created)[:400]}
        result = {"ok": True, "workout_id": wid, "name": name, "steps": len(steps)}
        if date:
            try:
                sched = _api_post(f"/workout-service/schedule/{wid}", {"date": date}) or {}
                result["scheduled"] = date
                result["schedule_raw"] = str(sched)[:200]
            except Exception as e:
                result["schedule_error"] = str(e)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "hint": "Garmin workout-service call failed — endpoint or auth issue."}


def push_week(week_start: str, plan: dict) -> dict:
    """Push every cardio session in a generated week plan."""
    sessions = (plan or {}).get("sessions") or []
    day_to_date = {}
    try:
        monday = dt.date.fromisoformat(week_start)
        for i, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
            day_to_date[name] = (monday + dt.timedelta(days=i)).isoformat()
    except Exception:
        pass

    out = []
    for s in sessions:
        typ = s.get("type")
        if typ not in ("Run", "Bike", "running", "cycling"):
            continue
        name = f"{s.get('day', '')} — {s.get('title', 'Session')}".strip(" —")
        out.append({
            "day": s.get("day"),
            "result": push_workout(name, typ, s.get("prescription") or s.get("title") or "",
                                   day_to_date.get(s.get("day"))),
        })
    pushed = sum(1 for o in out if o["result"].get("ok"))
    return {"week_start": week_start, "pushed": pushed, "total": len(out), "items": out}
