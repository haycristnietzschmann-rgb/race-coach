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
_TARGET_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"}

# Percentage-of-FTP bands per step. Without a power target Garmin marks a bike
# workout as having "non power-based steps", and a smart trainer (Tacx Flow)
# drops out of ERG and asks the rider to change gear by hand — which defeats
# the point of prescribing intervals at all. Running keeps pace targets; only
# cycling gets these.
_FTP_BAND = {
    "warmup": (0.50, 0.65),
    "cooldown": (0.45, 0.55),
    "recovery": (0.40, 0.55),
    "vo2": (1.06, 1.20),
    "threshold": (0.95, 1.05),
    "sweetspot": (0.84, 0.97),
    "steady": (0.56, 0.75),
}


def _explicit_ftp_pct(text: str):
    """Read a stated intensity — "95-105% FTP", "@ 90% ftp" — if present."""
    t = (text or "").replace("–", "-").replace("—", "-")
    m = re.search(r"(\d{2,3})\s*-\s*(\d{2,3})\s*%\s*(?:of\s*)?ftp", t, re.I)
    if m:
        return int(m.group(1)) / 100, int(m.group(2)) / 100
    m = re.search(r"(\d{2,3})\s*%\s*(?:of\s*)?ftp", t, re.I)
    if m:
        p = int(m.group(1)) / 100
        return round(p - 0.03, 3), round(p + 0.03, 3)
    return None


def _work_band(text: str):
    """Which band a work interval belongs to, from how it is described."""
    low = (text or "").lower()
    if any(w in low for w in ("vo2", "vo₂", "max repeats", "anaerobic", "hard")):
        return _FTP_BAND["vo2"]
    if any(w in low for w in ("threshold", "ftp", "lt2")):
        return _FTP_BAND["threshold"]
    if any(w in low for w in ("sweet", "ss", "tempo")):
        return _FTP_BAND["sweetspot"]
    return _FTP_BAND["steady"]


def _power_target(ftp, band):
    """(target, low_w, high_w) for a band, or a no-target triple without FTP."""
    if not ftp or not band:
        return _TARGET_NONE, None, None
    lo, hi = band
    return _TARGET_POWER, int(round(ftp * lo)), int(round(ftp * hi))


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


def parse_prescription(text: str, sport: str, ftp: int = None) -> list[dict]:
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

    # Cycling steps carry watts; running keeps pace. Anything the trainer can
    # hold in ERG has to be expressed as power, so the band is resolved here
    # once and reused for every step below.
    use_power = sport == "cycling" and bool(ftp)
    work_band = (_explicit_ftp_pct(text) or _work_band(text)) if use_power else None

    def _t(kind):
        """(target, t1, t2) for a non-work step."""
        if use_power:
            return _power_target(ftp, _FTP_BAND[kind])
        return _TARGET_NONE, None, None

    def _tw():
        """(target, t1, t2) for the working effort."""
        if use_power:
            return _power_target(ftp, work_band)
        # `pace` is assigned below; this closure only runs after that.
        if pace:
            return _TARGET_PACE, pace[0], pace[1]
        return _TARGET_NONE, None, None

    # repeat: "6×3 min ... / 2 min jog"  or  "3x10min @ threshold, 5 min easy"
    rep = re.search(
        r"(\d+)\s*(?:[–-]\s*\d+\s*)?[×x]\s*"
        r"(\d+(?:[–-]\d+)?)\s*(min|mins|minutes|sec|secs|s|h)\b"
        # Greedy, not lazy: text between the interval and its recovery ("3x5
        # min at FTP, 5 min recovery") has to be consumed for the recovery
        # clause to be reached at all. Lazy matching preferred the empty string
        # and silently fell back to a default 90 s float.
        r"(?:[^,/(]*(?:@\s*[\d:]+\s*/\s*km)?)?"
        # The recovery word is required, not optional. Greedy matching above
        # otherwise runs past "(3 min easy recovery)" and reads "10 min
        # cool-down" as the float. Parentheses count as a separator, since
        # plans write the recovery inside them as often as after a comma.
        r"(?:\s*[,/(]\s*(\d+(?:[–-]\d+)?)\s*(min|mins|sec|secs|s)\s*"
        r"(?:easy\s+|steady\s+)?(?:jog|easy|float|recovery|rest|spin|walk))?",
        low,
    )
    total = re.search(r"(\d+(?:[–-]\d+)?(?:\.\d+)?)\s*\+?\s*(h|hour|hours|min|mins|minutes)\b", low)
    dist = re.search(r"(\d+(?:[–-]\d+)?)\s*\+?\s*km\b", low)
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
            wt, w1, w2 = _t("warmup")
            steps.append(_exec_step(order, "warmup", warm, target=wt, t1=w1, t2=w2,
                                    desc="Warm-up easy")); order += 1
            steady = max(300, total_s - rep_total - warm - cool)
            st, s1, s2 = _t("steady")
            steps.append(_exec_step(order, "interval", steady, target=st, t1=s1, t2=s2,
                                    desc="Steady aerobic base")); order += 1
        else:
            wt, w1, w2 = _t("warmup")
            steps.append(_exec_step(order, "warmup", warm, target=wt, t1=w1, t2=w2,
                                    desc="Warm-up easy")); order += 1

        it, i1, i2 = _tw()
        rt, r1, r2 = _t("recovery")
        inner = [
            _exec_step(order + 1, "interval", work_s, target=it, t1=i1, t2=i2, desc=text),
            _exec_step(order + 2, "recovery", rec_s, target=rt, t1=r1, t2=r2,
                       desc="Easy recovery"),
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
        ct, c1, c2 = _t("cooldown")
        steps.append(_exec_step(order, "cooldown", cool, target=ct, t1=c1, t2=c2,
                                desc="Cool-down easy"))
        return steps

    # steady session with a stated duration or distance
    if total:
        secs = _to_seconds(total.group(1), total.group(2))
        if not is_long:
            wt, w1, w2 = _t("warmup")
            steps.append(_exec_step(order, "warmup", warm, target=wt, t1=w1, t2=w2,
                                    desc="Warm-up")); order += 1
            secs = max(300, secs - warm - cool)
        mt, m1, m2 = _tw()
        steps.append(_exec_step(order, "interval", secs, target=mt, t1=m1, t2=m2,
                                desc=text)); order += 1
        if not is_long:
            ct, c1, c2 = _t("cooldown")
            steps.append(_exec_step(order, "cooldown", cool, target=ct, t1=c1, t2=c2,
                                    desc="Cool-down"))
        return steps

    if dist:
        km = _mid(dist.group(1))
        dt_, d1, d2 = _tw()
        steps.append(_exec_step(order, "interval", int(km * 1000), end_cond=_END_DIST,
                                target=dt_, t1=d1, t2=d2, desc=text))
        return steps

    # fallback: one lap-button step carrying the whole instruction. Even here
    # a bike step gets a power band — an untargeted step is precisely what
    # drops a smart trainer out of ERG.
    ft, f1, f2 = _tw()
    return [_exec_step(1, "other", 0, end_cond=_END_LAP, target=ft, t1=f1, t2=f2,
                       desc=text or "See plan")]


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


def _api_get(path: str):
    api = get_client().api
    try:
        return api.connectapi(path)
    except Exception:
        resp = api.garth.connectapi(path)
        return resp.json() if hasattr(resp, "json") else resp


def list_scheduled(start_iso: str, end_iso: str) -> list[dict]:
    """Workouts already on the Garmin calendar between two dates. Reads
    calendar-service month by month; best-effort."""
    if os.environ.get("GARMIN_FIXTURE_MODE"):
        return []
    start = dt.date.fromisoformat(start_iso)
    end = dt.date.fromisoformat(end_iso)
    seen, out = set(), []
    d = start.replace(day=1)
    while d <= end:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            try:
                # calendar-service months are 0-indexed
                data = _api_get(f"/calendar-service/year/{d.year}/month/{d.month - 1}") or {}
                for item in (data.get("calendarItems") or []):
                    if not isinstance(item, dict):
                        continue
                    if (item.get("itemType") or "").lower() not in ("workout", "scheduledworkout"):
                        continue
                    day = (item.get("date") or "")[:10]
                    if start_iso <= day <= end_iso:
                        out.append({
                            "date": day,
                            "title": item.get("title") or item.get("workoutName"),
                            "workout_id": item.get("workoutId") or item.get("id"),
                            "sport": (item.get("sportTypeKey") or item.get("sportType") or "").lower(),
                        })
            except Exception:
                pass
        d = (d.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
    return out


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def reconcile_week(week_start: str, plan: dict) -> dict:
    """Compare what's on Garmin's calendar this week against the app plan.
    App plan wins: mismatches and Garmin-only items are reported, not merged
    over the plan."""
    try:
        monday = dt.date.fromisoformat(week_start)
    except Exception:
        return {"error": "bad week_start"}
    end = (monday + dt.timedelta(days=6)).isoformat()
    garmin_items = list_scheduled(week_start, end)
    by_date = {}
    for g in garmin_items:
        by_date.setdefault(g["date"], []).append(g)

    planned_dates = {}
    for s in (plan or {}).get("sessions", []):
        try:
            i = _DAY_NAMES.index(s.get("day"))
        except ValueError:
            continue
        planned_dates[(monday + dt.timedelta(days=i)).isoformat()] = s

    in_sync, mismatched, garmin_only, missing_on_garmin = [], [], [], []
    for date, s in planned_dates.items():
        gs = by_date.get(date, [])
        want = f"{s.get('day')} — {s.get('title', '')}".strip(" —").lower()
        if not gs:
            missing_on_garmin.append({"date": date, "title": s.get("title")})
        elif any((g.get("title") or "").lower().strip() == want for g in gs):
            in_sync.append({"date": date, "title": s.get("title")})
        else:
            mismatched.append({"date": date, "plan": s.get("title"),
                               "on_garmin": [g.get("title") for g in gs]})
    for date, gs in by_date.items():
        if date not in planned_dates:
            garmin_only.append({"date": date, "titles": [g.get("title") for g in gs]})

    return {
        "week_start": week_start,
        "garmin_items": garmin_items,
        "in_sync": in_sync,
        "mismatched": mismatched,          # app plan wins — re-push to fix
        "garmin_only": garmin_only,        # you added these on Garmin
        "missing_on_garmin": missing_on_garmin,
        "verdict": ("in sync" if not mismatched and not missing_on_garmin
                    else "plan and Garmin differ — re-push the week to match the plan"),
    }


_SPORT_ID = {"Run": "running", "Bike": "cycling", "running": "running", "cycling": "cycling"}


def current_ftp() -> int | None:
    """
    Cycling FTP, or None. Watt targets are meaningless without it.

    CYCLING_FTP wins when set. Garmin does expose functionalThresholdPower on
    the biometric profile, but returns null for a manually-entered FTP — which
    is exactly the case here — so the profile lookup is a fallback rather than
    the source of truth. training_status does not carry the field at all,
    despite planner.py having read it from there since the beginning.
    """
    env = os.environ.get("CYCLING_FTP")
    if env:
        try:
            return int(env)
        except ValueError:
            print(f"CYCLING_FTP is not a number: {env!r} — ignoring")
    try:
        r = get_client().api.garth.connectapi(
            "/userprofile-service/userprofile/personal-information") or {}
        ftp = (r.get("biometricProfile") or {}).get("functionalThresholdPower")
        if ftp:
            return int(ftp)
    except Exception as e:
        print(f"current_ftp lookup failed: {e}")
    return None


def sport_of(session: dict) -> str | None:
    """
    "cycling" / "running" / None, from however the planner labelled it.

    The heuristic planner emits "Bike"; Claude emits "bike-quality",
    "long-ride", "midweek-long". Matching on the exact strings meant every
    Claude-generated session was silently skipped and never reached Garmin.
    """
    blob = " ".join(str(session.get(k) or "") for k in
                    ("type", "title", "prescription")).lower()
    if any(w in blob for w in ("swim",)):
        return None
    if any(w in blob for w in ("bike", "ride", "cycl", "spin", "ftp", "watt")):
        return "cycling"
    if any(w in blob for w in ("run", "jog", "tempo run", "strides")):
        return "running"
    return None


def push_workout(name: str, sport: str, prescription: str, date: str | None = None,
                 ftp: int = None) -> dict:
    """Create one structured workout, optionally schedule it on `date`."""
    sport_key = _SPORT_ID.get(sport, sport if sport in ("cycling", "running") else "running")
    if sport_key == "cycling" and ftp is None:
        ftp = current_ftp()
    steps = parse_prescription(prescription, sport_key, ftp=ftp)
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

    ftp = current_ftp()
    out = []
    for s in sessions:
        sport = sport_of(s)
        if not sport:
            continue
        name = f"{s.get('day', '')} — {s.get('title', 'Session')}".strip(" —")
        out.append({
            "day": s.get("day"),
            "sport": sport,
            "result": push_workout(name, sport, s.get("prescription") or s.get("title") or "",
                                   day_to_date.get(s.get("day")), ftp=ftp),
        })
    pushed = sum(1 for o in out if o["result"].get("ok"))
    return {"week_start": week_start, "pushed": pushed, "total": len(out), "items": out}
