"""
Adaptive weekly training planner.

Turns recent Garmin history + recovery trend + performance markers + past-block
outcomes into next week's volume / intensity, a deload-or-ramp decision, a
VO2 max projection, and a short written rationale — via Claude, with a pure
heuristic fallback so the endpoint always returns something usable.

The learning loop lives in plan_state.json (written by main.py): each week's
generated plan plus the actuals + recovery/VO2 deltas that followed, so
"what's worked best for this athlete" accumulates instead of being re-guessed.
"""
from __future__ import annotations

import os
import json
import datetime as dt

import anthropic

from coach import summarize_snapshot

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Same anchor + cycle rules as the frontend (fpWeekMeta in index.html).
BLOCK_ANCHOR_MONDAY = dt.date(2026, 8, 31)   # Monday of the week containing 2026-09-01
ROLES = ["build", "build+", "peak", "deload"]
BASE_VOLUME = {
    "build":  {"bike": 150, "run": 26},
    "build+": {"bike": 175, "run": 30},
    "peak":   {"bike": 200, "run": 34},
    "deload": {"bike": 110, "run": 18},
}


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def block_meta(week_start_iso: str) -> dict:
    monday = _monday(dt.date.fromisoformat(week_start_iso))
    w = (monday - BLOCK_ANCHOR_MONDAY).days // 7
    idx = w % 4
    cycle = max(1, w // 4 + 1)
    focus = "bike" if (w // 4) % 2 == 0 else "run"
    return {
        "week_start": monday.isoformat(),
        "weeks_since_anchor": w,
        "cycle": cycle,
        "week_in_cycle": idx + 1,
        "role": ROLES[idx],
        "focus": focus,
    }


def base_volume_target(meta: dict) -> dict:
    base = BASE_VOLUME[meta["role"]]
    t = {"bike": base["bike"], "run": base["run"]}
    if meta["focus"] == "run":
        t["run"] = round(t["run"] * 1.3)
        t["bike"] = round(t["bike"] * 0.82)
    return t


# ---------------------------------------------------------------- VO2 projection

def _slope_per_week(points: list[float]) -> float:
    """Least-squares slope of evenly-spaced points (x = 0,1,2,...)."""
    n = len(points)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(points) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((xs[i] - mx) * (points[i] - my) for i in range(n)) / denom


def project_vo2(series: list[dict], role: str, key: str = "running") -> dict:
    """Dampened linear projection of next week's VO2 max from recent weekly
    readings. Pure math — used as the offline fallback and as a computed
    anchor handed to Claude."""
    vals = [p[key] for p in series if isinstance(p.get(key), (int, float))]
    if not vals:
        return {"current": None, "next_week": None, "delta": None,
                "confidence": "none", "rationale": "No recent VO2 max readings available."}
    current = round(float(vals[-1]), 1)
    recent = vals[-6:]
    raw_slope = _slope_per_week(recent)
    delta = raw_slope * 0.6                       # damp the trend
    delta += {"build": 0.05, "build+": 0.05, "peak": 0.05, "deload": -0.05}.get(role, 0.0)
    delta = max(-0.3, min(0.4, delta))           # realistic weekly bound
    nxt = round(current + delta, 1)
    conf = "low" if len(recent) < 3 else ("medium" if len(recent) < 5 else "high")
    direction = "improving" if delta > 0.05 else ("easing back" if delta < -0.05 else "holding steady")
    return {
        "current": current,
        "next_week": nxt,
        "delta": round(delta, 1),
        "confidence": conf,
        "rationale": (
            f"Recent weekly VO2 max is {direction} (trend ~{raw_slope:+.2f}/wk over "
            f"{len(recent)} weeks). On a {role} week the modelled change is "
            f"{delta:+.1f}, so next week is projected around {nxt}."
        ),
    }


# ---------------------------------------------------------------- context + plan

FIXED_DAYS = [
    {"day": "Mon", "slot": "run-quality",  "lift": "Push"},
    {"day": "Tue", "slot": "bike-quality", "lift": "Pull"},
    {"day": "Wed", "slot": "midweek-long", "lift": None},
    {"day": "Thu", "slot": "easy-run",     "lift": "Push"},
    {"day": "Fri", "slot": "cardio-rest",  "lift": "Pull"},
    {"day": "Sat", "slot": "sat-choice",   "lift": "Legs"},
    {"day": "Sun", "slot": "long-ride",    "lift": None},
]


def assemble_context(garmin, plan_state: dict, week_start_iso: str) -> dict:
    meta = block_meta(week_start_iso)
    base = base_volume_target(meta)

    try:
        volume_history = [w for w in garmin.monthly_volume(weeks=12) if isinstance(w, dict)]
    except Exception:
        volume_history = []
    try:
        vo2_series = garmin.vo2max_trend(weeks=8)
    except Exception:
        vo2_series = []
    try:
        snap = summarize_snapshot(garmin.snapshot())
    except Exception:
        snap = {}
    try:
        readiness_trend = [p for p in garmin.readiness_trend("month") if isinstance(p, dict)]
    except Exception:
        readiness_trend = []

    prev_monday = (_monday(dt.date.fromisoformat(week_start_iso)) - dt.timedelta(days=7)).isoformat()
    history_log = plan_state.get("weeks", {})

    return {
        "block": meta,
        "base_volume_target": base,
        "recent_weekly_volume": volume_history[-12:],
        "vo2_series": vo2_series,
        "vo2_projection_model": project_vo2(vo2_series, meta["role"]),
        "today_snapshot": snap,
        "readiness_trend": readiness_trend[-21:],
        "previous_week_plan": history_log.get(prev_monday),
        "recent_block_outcomes": plan_state.get("outcomes", [])[-6:],
        "fixed_days": FIXED_DAYS,
    }


HEURISTIC_SESSIONS = {
    "build": {
        "run-quality": ("Run", "VO2 intervals", "6×3 min hard / 2 min jog", "hard"),
        "bike-quality": ("Bike", "Bike VO2", "5×4 min hard / 4 min easy", "hard"),
        "midweek-long": ("Bike", "Endurance + sweet-spot", "2–2.5 h Z2 with 2×20 min SS", "moderate"),
        "easy-run": ("Run", "Easy run + strides", "45–55 min Z2 + 5 strides", "easy"),
        "sat-choice": ("Choice", "Optional easy bike or run", "30–45 min easy, or rest", "easy"),
        "long-ride": ("Bike", "Long ride", "100–120 km Z2", "easy"),
    },
}


def heuristic_week_plan(context: dict) -> dict:
    """Pure-python plan: ramp from last week's actual toward the role's base
    target, clamped. No Claude call. Always safe to return."""
    meta = context["block"]
    base = context["base_volume_target"]
    role = meta["role"]

    hist = context.get("recent_weekly_volume") or []
    last = hist[-1] if hist else {}
    last_bike = float(last.get("bike_km") or 0) or base["bike"]
    last_run = float(last.get("run_km") or 0) or base["run"]

    if role == "deload":
        bike = round(min(base["bike"], last_bike * 0.55))
        run = round(min(base["run"], last_run * 0.55))
        ramp_pct, deload_pct = None, 45
    else:
        step = {"build": 1.06, "build+": 1.10, "peak": 1.08}.get(role, 1.05)
        bike = round(max(base["bike"] * 0.85, min(base["bike"] * 1.15, last_bike * step)))
        run = round(max(base["run"] * 0.85, min(base["run"] * 1.15, last_run * step)))
        ramp_pct, deload_pct = round((step - 1) * 100), None

    tmpl = HEURISTIC_SESSIONS["build"]
    sessions = []
    for fd in context["fixed_days"]:
        if fd["slot"] == "cardio-rest":
            continue
        typ, title, presc, inten = tmpl[fd["slot"]]
        if role == "deload" and inten == "hard":
            title, presc, inten = title + " (short)", "4×2 min, full recovery", "moderate"
        sessions.append({"day": fd["day"], "type": typ, "title": title,
                         "prescription": presc, "intensity": inten, "after_lift": fd["lift"]})

    return {
        "week_start": meta["week_start"],
        "role": role,
        "focus": meta["focus"],
        "bike_km": bike,
        "run_km": run,
        "ramp_pct": ramp_pct,
        "deload_pct": deload_pct,
        "sessions": sessions,
        "vo2_projection": context["vo2_projection_model"],
        "rationale": (
            f"{role.capitalize()} week (cycle {meta['cycle']}, {meta['focus']}-focus). "
            f"Targets ramped from last week's actual ({round(last_bike)} km bike / "
            f"{round(last_run)} km run) toward the block base. Heuristic fallback — "
            f"Claude planner unavailable."
        ),
        "source": "heuristic",
    }


SYSTEM_PROMPT = """You are an endurance coach setting one athlete's next training \
week inside a fixed weekly structure you cannot change:
- Mon 21:00 (after Push lift), <=90 min: run quality
- Tue 21:00 (after Pull lift), <=90 min: bike quality
- Wed 19:00 (no lift), 2-3 h: the big aerobic/quality session
- Thu 21:00 (after Push lift), <=90 min: easy run
- Fri: cardio rest (social)
- Sat morning (after Legs lift): OPTIONAL easy bike OR run OR rest
- Sun: long ride 100 km+

The block is a rolling 4-week cycle: build -> build+ -> peak -> deload. Cycles \
alternate bike-focus and run-focus. Goal: get genuinely faster at running and \
cycling while holding solid weekly volume. No race date.

Rules:
- Ramp weekly volume 3-12% within a build cycle; never above ~12% over last \
  week's ACTUAL volume.
- Deload week: cut volume 35-55% vs the peak week, keep ONE short sharpener.
- Respect recovery: if the readiness/HRV trend is falling, hold or trim volume \
  and soften intensity regardless of block position.
- Legs lift is Saturday and the long ride is Sunday — keep Saturday easy or rest.
- VO2 max projection: realistic weekly change is about -0.3 to +0.4. Use the \
  provided computed model value as an anchor; adjust only with a stated reason.

Reply with ONLY a JSON object, no prose around it:
{"week_start","role","focus","bike_km","run_km","ramp_pct"(int|null),
 "deload_pct"(int|null),
 "sessions":[{"day","type","title","prescription","intensity","after_lift"}],
 "vo2_projection":{"current","next_week","delta","confidence","rationale"},
 "rationale":"2-4 sentences on the decisions"}"""


def generate_week_plan(context: dict) -> dict:
    """Claude-generated plan; falls back to the heuristic on any failure."""
    try:
        user_content = json.dumps(context, indent=2, default=str)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        plan = json.loads(text)
        plan.setdefault("week_start", context["block"]["week_start"])
        plan.setdefault("vo2_projection", context["vo2_projection_model"])
        plan["source"] = "claude"
        return plan
    except Exception as e:
        fb = heuristic_week_plan(context)
        fb["planner_error"] = str(e)
        return fb
