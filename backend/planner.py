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

    # VO2 max often jumps when Garmin's estimate catches up after a training
    # ramp, then plateaus. A plain 6-point slope keeps projecting that early
    # spike forward. Weight the RECENT weeks: blend the last-3 slope (what the
    # trend is doing now) with the last-6 slope (longer context), 70/30.
    recent = vals[-6:]
    slope_long = _slope_per_week(recent)
    slope_short = _slope_per_week(vals[-3:]) if len(vals) >= 3 else slope_long
    eff_slope = 0.7 * slope_short + 0.3 * slope_long

    delta = eff_slope * 0.6                       # damp
    delta += {"build": 0.05, "build+": 0.05, "peak": 0.05, "deload": -0.05}.get(role, 0.0)
    delta = max(-0.3, min(0.4, delta))           # realistic weekly bound
    nxt = round(current + delta, 1)

    # Confidence drops when the short and long trends disagree (a decelerating
    # or noisy series) — high confidence should mean a steady trend, not just
    # "we have six numbers".
    disagree = abs(slope_short - slope_long)
    if len(recent) < 3:
        conf = "low"
    elif disagree > 0.6:
        conf = "low"
    elif disagree > 0.25 or len(recent) < 5:
        conf = "medium"
    else:
        conf = "high"

    direction = "still climbing" if eff_slope > 0.15 else (
        "flattening out" if eff_slope > 0.02 else
        "holding steady" if eff_slope > -0.05 else "easing back")
    return {
        "current": current,
        "next_week": nxt,
        "delta": round(delta, 1),
        "confidence": conf,
        "rationale": (
            f"Recent VO2 max is {direction}: last 3 weeks ~{slope_short:+.2f}/wk, "
            f"last {len(recent)} weeks ~{slope_long:+.2f}/wk. Weighting the recent "
            f"trend, next week projects {delta:+.1f} to about {nxt}."
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
        "recent_session_feel": plan_state.get("feedback", [])[-16:],   # athlete RPE 1-5
        "fixed_days": FIXED_DAYS,
    }


BLOCK_REVIEW_PROMPT = """You review a completed 4-week training cycle for one
athlete. You get each week's plan vs actual volume, completion %, and the VO2
max that followed, plus their session RPE ratings if any.

Say plainly: did the cycle work (fitness/consistency), what specifically to keep,
what to watch, and one concrete adjustment for the cycle just starting.

Reply with ONLY JSON:
{"cycle": int, "verdict": "2-3 sentences", "what_worked": "one line",
 "watch_out": "one line", "adjust_next": "one imperative line"}"""


def block_review(plan_state: dict) -> dict:
    outcomes = plan_state.get("outcomes", [])
    if len(outcomes) < 3:
        return {}
    last4 = outcomes[-4:]
    payload = {"weeks": last4, "session_feel": plan_state.get("feedback", [])[-16:]}
    try:
        out = _claude_json(BLOCK_REVIEW_PROMPT, payload, max_tokens=600)
        out.setdefault("cycle", block_meta(last4[-1]["week_start"])["cycle"])
        return out
    except Exception:
        done = sum(1 for o in last4 if (o.get("completion_pct") or 0) >= 85)
        v0 = (last4[0].get("vo2_after") or {}).get("running")
        v1 = (last4[-1].get("vo2_after") or {}).get("running")
        trend = (f"VO2 max {v0}→{v1}. " if v0 and v1 else "")
        return {
            "cycle": block_meta(last4[-1]["week_start"])["cycle"],
            "verdict": f"{trend}Hit target volume on {done}/4 weeks.",
            "what_worked": "Weeks you completed cleanly." if done >= 3 else "",
            "watch_out": "Completion slipped — targets may be too high." if done < 2 else "",
            "adjust_next": "Hold ramp rate." if done >= 3 else "Trim next week's target ~10%.",
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


def _claude_json(system: str, payload: dict, max_tokens: int = 1400):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload, indent=2, default=str)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip()
    return json.loads(text)


def generate_week_plan(context: dict) -> dict:
    """Claude-generated plan; falls back to the heuristic on any failure."""
    try:
        plan = _claude_json(SYSTEM_PROMPT, context)
        plan.setdefault("week_start", context["block"]["week_start"])
        plan.setdefault("vo2_projection", context["vo2_projection_model"])
        plan["source"] = "claude"
        return plan
    except Exception as e:
        fb = heuristic_week_plan(context)
        fb["planner_error"] = str(e)
        return fb


# -------------------------------------------------- missed-session rescheduling

ADJUST_PROMPT = """The athlete missed one or more sessions this week. Rework ONLY \
the days that are still ahead, inside the same fixed structure (Mon/Tue/Thu are \
<=90 min evening slots after a lift; Wed is the 2-3 h window; Fri is rest; Sat is \
an optional easy bike/run/rest after Legs; Sun is the long ride).

Decide per missed session: fold its key stimulus into a remaining day, move it to \
the Sat optional slot, or drop it — whichever costs least. Never create a second \
hard day back-to-back, never exceed a slot's time cap, keep Sat easy, and don't \
push total week volume above the original target. If recovery is low, prefer \
dropping over cramming.

Reply with ONLY JSON:
{"adjusted_sessions":[{"day","type","title","prescription","intensity","change_note"}],
 "dropped":[{"day","title","reason"}],
 "rationale":"2-3 sentences"}
adjusted_sessions must cover every remaining trainable day (include unchanged ones \
too, with change_note "unchanged")."""


def heuristic_adjust(context: dict) -> dict:
    """No-Claude fallback: move the first missed quality session to Saturday if
    it's free, otherwise drop everything missed with a note."""
    remaining = context.get("days_remaining", [])
    missed = context.get("missed", [])
    adjusted, dropped = [], []
    sat_free = "Sat" in remaining
    for i, ms in enumerate(missed):
        if i == 0 and sat_free:
            adjusted.append({
                "day": "Sat", "type": ms.get("type", "Run"),
                "title": ms.get("title", "Made-up session") + " (moved)",
                "prescription": ms.get("prescription", "Shortened version of the missed session"),
                "intensity": ms.get("intensity", "moderate"),
                "change_note": "Moved here from " + ms.get("day", "?") + " — keep it a touch shorter.",
            })
            sat_free = False
        else:
            dropped.append({"day": ms.get("day", "?"), "title": ms.get("title", "session"),
                            "reason": "No spare slot this week without stacking load — let it go."})
    return {
        "adjusted_sessions": adjusted,
        "dropped": dropped,
        "rationale": "Heuristic reshuffle (Claude unavailable): one missed session moved to the "
                     "Saturday optional slot if it was free, the rest dropped rather than crammed.",
        "source": "heuristic",
    }


def adjust_week(context: dict) -> dict:
    try:
        out = _claude_json(ADJUST_PROMPT, context, max_tokens=1200)
        out["source"] = "claude"
        return out
    except Exception as e:
        fb = heuristic_adjust(context)
        fb["adjust_error"] = str(e)
        return fb


# ----------------------------------------------------- fitness analytics (tab)

def _sec_to_clock(s) -> str | None:
    if not isinstance(s, (int, float)) or s <= 0:
        return None
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _parse_predictions(raw) -> dict:
    """Normalise get_race_predictions() (list-of-days or single dict) to
    {'5K','10K','half','marathon'} in seconds, newest entry."""
    if isinstance(raw, list):
        raw = sorted([r for r in raw if isinstance(r, dict)],
                     key=lambda r: r.get("calendarDate", ""))
        raw = raw[-1] if raw else {}
    if not isinstance(raw, dict):
        return {}
    return {
        "5K": raw.get("raceTime5K") or raw.get("time5K"),
        "10K": raw.get("raceTime10K") or raw.get("time10K"),
        "half": raw.get("raceTimeHalfMarathon") or raw.get("timeHalfMarathon"),
        "marathon": raw.get("raceTimeMarathon") or raw.get("timeMarathon"),
    }


def _best_recent_5k_equiv(activities: list[dict]) -> float | None:
    """Riegel-scale the fastest recent run (>=3 km) to a 5 km time."""
    best = None
    for a in activities or []:
        tk = ((a.get("activityType") or {}).get("typeKey") or "")
        if "running" not in tk:
            continue
        dist = a.get("distance") or 0
        dur = a.get("movingDuration") or a.get("duration") or 0
        if dist < 3000 or dur <= 0:
            continue
        pred = dur * (5000.0 / dist) ** 1.06
        if best is None or pred < best:
            best = pred
    return best


def _fitness_slope_per_week(vo2_series, pace_series, endurance_series) -> tuple:
    """Blend the VO2 trend, easy-pace-at-HR trend and endurance-score trend into
    one 'am I getting fitter' slope, expressed as fractional pace gain per week
    (positive = getting faster). Returns (weekly_pct, note)."""
    parts = []
    vo2 = [x.get("running") for x in (vo2_series or []) if isinstance(x.get("running"), (int, float))]
    if len(vo2) >= 3:
        s = _slope_per_week(vo2[-6:])
        parts.append(("VO2", s * 0.010))                       # ~1 pt ≈ 1% at 5K
    pace = [x.get("sec_per_km") for x in (pace_series or []) if isinstance(x.get("sec_per_km"), (int, float))]
    if len(pace) >= 3:
        s = _slope_per_week(pace[-6:])                          # sec/km per week, lower=better
        base = sum(pace[-3:]) / 3
        parts.append(("efficiency", -s / base if base else 0))
    endu = [x.get("overallScore") or x.get("score") for x in (endurance_series or [])
            if isinstance(x, dict) and isinstance(x.get("overallScore") or x.get("score"), (int, float))]
    if len(endu) >= 3:
        s = _slope_per_week(endu[-6:])
        base = sum(endu[-3:]) / 3
        parts.append(("endurance", (s / base) * 0.5 if base else 0))
    if not parts:
        return 0.0, "no fitness trend signal yet"
    weekly = sum(p[1] for p in parts) / len(parts)
    weekly = max(-0.006, min(0.012, weekly))                    # keep it realistic
    return weekly, "blend of " + ", ".join(p[0] for p in parts)


def estimate_pr_5k(personal_records: dict, activities: list[dict],
                   vo2_series, pace_series, endurance_series, role: str) -> dict:
    """Athlete's real 5K PR (Garmin personal record, else best recent 5K-equiv
    effort) and what it could realistically drop to in 4 weeks on the current
    fitness trajectory. This is a training-trajectory estimate, NOT a race
    predictor."""
    pr = (personal_records or {}).get("run_5k")
    current = pr or _best_recent_5k_equiv(activities)
    if not current:
        return {"pr_sec": None, "pr_str": None, "source": "no data", "projection_4wk_str": None,
                "delta_sec": None, "rationale": "No 5K PR on Garmin and no recent run ≥3 km to estimate one from."}
    source = "Garmin personal record" if pr else "best recent run, Riegel-scaled (no PR on file)"

    weekly_pct, note = _fitness_slope_per_week(vo2_series, pace_series, endurance_series)
    if role == "deload":
        weekly_pct = min(weekly_pct, 0.0)
    proj = current * (1 - weekly_pct) ** 4
    proj = min(proj, current)                 # "potential" can't be slower than the PR
    delta = round(current - proj)             # >= 0, seconds you could take off
    if delta <= 1:
        rationale = (
            f"Your 5K PR is {_sec_to_clock(current)} ({source}). Current fitness trend "
            f"({note}) is flat-to-down, so 4 weeks of this training points at matching "
            f"the PR rather than beating it — build a bit more before expecting a new one."
        )
    else:
        rationale = (
            f"Your 5K PR is {_sec_to_clock(current)} ({source}). Current fitness trend "
            f"({note}) works out to roughly {weekly_pct*100:+.1f}%/wk at 5K pace, so about "
            f"{_sec_to_clock(proj)} looks reachable in ~4 weeks if training holds "
            f"(−{delta}s). Not a race-day prediction — assumes a good day and a real effort."
        )
    return {
        "pr_sec": round(current),
        "pr_str": _sec_to_clock(current),
        "source": source,
        "projection_4wk_sec": round(proj),
        "projection_4wk_str": _sec_to_clock(proj),
        "delta_sec": delta,
        "trend_note": note,
        "rationale": rationale,
    }


def estimate_heat_acclimation(activities: list[dict], today: dt.date = None) -> dict:
    """The watch doesn't report acclimatization, so estimate heat readiness from
    training consistency over the last 3 weeks (a well-trained aerobic system
    carries real heat tolerance) plus the season. NOT a measured value."""
    today = today or dt.date.today()
    cutoff = (today - dt.timedelta(days=21)).isoformat()
    days = set()
    for a in activities or []:
        d = (a.get("startTimeLocal") or "")[:10]
        tk = (a.get("activityType") or {}).get("typeKey") or ""
        if d >= cutoff and ("running" in tk or "cycling" in tk):
            days.add(d)
    consistency = min(1.0, len(days) / 14.0)
    month_factor = {6: 1.0, 7: 1.0, 8: 1.0, 9: 0.85, 5: 0.7, 10: 0.6}.get(today.month, 0.4)
    heat_pct = round(min(95, consistency * 100 * month_factor * 1.05))
    return {
        "heat_pct": heat_pct,
        "altitude_m": None,
        "heat_trend": "building" if consistency > 0.6 else "holding" if consistency > 0.3 else "low",
        "estimated": True,
        "note": (f"Estimated from {len(days)} training days in the last 3 weeks and the season "
                 f"(month {today.month}) — your watch doesn't record acclimatization."),
    }


def biological_age(vo2_running, rhr, chrono_age=None) -> dict:
    """Fitness (biological) age from VO2 max, lightly adjusted by resting HR.
    ~0.4 ml/kg/min of VO2 max ≈ one year (male reference norms)."""
    if not isinstance(vo2_running, (int, float)):
        return {"fitness_age": None, "note": "Needs a current running VO2 max."}
    fa = 20 + (48.0 - vo2_running) / 0.4
    if isinstance(rhr, (int, float)):
        fa += (rhr - 55) * 0.15                                 # lower RHR trims a little
    fa = max(18, min(80, round(fa, 1)))
    out = {"fitness_age": fa, "basis": "VO2 max" + (" + resting HR" if isinstance(rhr, (int, float)) else "")}
    if isinstance(chrono_age, (int, float)):
        out["chronological_age"] = chrono_age
        out["delta_years"] = round(fa - chrono_age, 1)
    return out


def composite_fitness_index(vo2_series, pace_series, rhr_series, endurance_series) -> list[dict]:
    """Weekly 0–100 fitness index blending VO2 max, aerobic efficiency (pace at
    HR, inverted), resting HR (inverted) and endurance score — each normalised
    against its own recent range. This is the 'fitness improving on multiple
    factors' graph."""
    weeks = {}
    def add(series, key, invert):
        vals = [(x.get("week_start"), x.get(key)) for x in (series or [])
                if isinstance(x, dict) and isinstance(x.get(key), (int, float))]
        nums = [v for _, v in vals]
        if len(nums) < 2:
            return
        lo, hi = min(nums), max(nums)
        rng = (hi - lo) or 1
        for wk, v in vals:
            norm = (v - lo) / rng
            if invert:
                norm = 1 - norm
            weeks.setdefault(wk, []).append(norm)
    add(vo2_series, "running", False)
    add(pace_series, "sec_per_km", True)
    add(rhr_series, "bpm", True)
    add([{"week_start": x.get("week_start") or x.get("calendarDate"),
          "score": x.get("overallScore") or x.get("score")} for x in (endurance_series or []) if isinstance(x, dict)],
        "score", False)
    today = dt.date.today()
    out = []
    for wk, v in sorted(weeks.items()):
        if not v:
            continue
        try:
            shown = min(dt.date.fromisoformat(wk) + dt.timedelta(days=6), today).isoformat()
        except Exception:
            shown = wk
        out.append({"week_start": wk, "date": shown, "index": round(sum(v) / len(v) * 100)})
    return out


def pace_at_hr_series(activities: list[dict], weeks: int = 10) -> list[dict]:
    """Weekly mean easy-run pace normalised to HR 140 (sec/km). Lower = fitter.
    Only uses runs with an average HR in an easy-aerobic band."""
    today = dt.date.today()
    this_monday = _monday(today)
    buckets: dict[str, list[float]] = {}
    for a in activities or []:
        tk = ((a.get("activityType") or {}).get("typeKey") or "")
        if "running" not in tk:
            continue
        dist = a.get("distance") or 0
        dur = a.get("movingDuration") or a.get("duration") or 0
        hr = a.get("averageHR") or a.get("avgHr")
        # Wide enough to catch real easy/steady runs; still excludes intervals.
        if dist < 2000 or dur <= 0 or not hr or hr < 110 or hr > 172:
            continue
        try:
            d = dt.date.fromisoformat((a.get("startTimeLocal") or "")[:10])
        except Exception:
            continue
        wk = _monday(d).isoformat()
        pace = (dur / (dist / 1000.0))
        buckets.setdefault(wk, []).append((pace * (140.0 / hr), d.isoformat()))
    out = []
    for i in range(weeks - 1, -1, -1):
        wk = (this_monday - dt.timedelta(days=7 * i)).isoformat()
        vals = buckets.get(wk)
        out.append({
            "week_start": wk,
            "date": max(v[1] for v in vals) if vals else wk,
            "sec_per_km": round(sum(v[0] for v in vals) / len(vals)) if vals else None,
        })
    return out


# ----------------------------------------------------------------- sleep (tab)

SLEEP_PROMPT = """You are a sleep coach for one endurance athlete. You get their
last ~14 nights (score, total minutes, deep/REM/light/awake minutes, bedtime,
wake time, resting HR) plus their current training phase and load.

Work out:
- their individual sleep NEED in hours (not a generic 8 — infer it from the
  nights where score and next-day readiness were best, nudged up for higher
  training load / build & peak weeks),
- a recommended BEDTIME and WAKE TIME that hit that need with a consistent
  schedule, anchored to their recent actual wake times,
- current sleep DEBT in hours over the last 7 nights vs need,
- what to prioritise.

Reply with ONLY JSON:
{"sleep_need_hours": float, "recommended_bedtime": "HH:MM",
 "recommended_waketime": "HH:MM", "debt_hours": float,
 "consistency_note": "one sentence", "analysis": "3-4 sentences",
 "priorities": ["short imperative", "..."]}"""


def _clock_to_min(s):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _min_to_clock(x):
    x = int(round(x)) % (24 * 60)
    return f"{x // 60:02d}:{x % 60:02d}"


def heuristic_sleep(nights: list[dict], role: str) -> dict:
    got = [n for n in nights if isinstance(n.get("total_min"), (int, float))]
    if not got:
        return {"sleep_need_hours": 8.0, "recommended_bedtime": "22:45",
                "recommended_waketime": "06:45", "debt_hours": None,
                "consistency_note": "No sleep data yet.", "analysis": "Not enough sleep history.",
                "priorities": ["Wear the watch overnight for a few nights."], "source": "heuristic"}
    good = [n for n in got if isinstance(n.get("score"), (int, float)) and n["score"] >= 80] or got
    need_min = sum(n["total_min"] for n in good) / len(good)
    need_min = max(450, need_min)
    need_min += {"build": 10, "build+": 20, "peak": 25, "deload": 0}.get(role, 0)
    last7 = got[-7:]
    debt = sum((need_min - n["total_min"]) for n in last7) / 60.0
    wakes = [_clock_to_min(n["waketime"]) for n in got if n.get("waketime")]
    wake = sorted(wakes)[len(wakes) // 2] if wakes else 6 * 60 + 45
    return {
        "sleep_need_hours": round(need_min / 60, 1),
        "recommended_waketime": _min_to_clock(wake),
        "recommended_bedtime": _min_to_clock(wake - need_min - 20),
        "debt_hours": round(debt, 1),
        "consistency_note": "Wake times vary by "
            + (f"{(max(wakes) - min(wakes))} min" if len(wakes) > 1 else "n/a")
            + " over the window.",
        "analysis": (f"Your best-scoring nights average {need_min/60:.1f} h, so that's the working "
                     f"target ({role} week). Last 7 nights ran a "
                     f"{abs(debt):.1f} h {'deficit' if debt > 0 else 'surplus'} against it."),
        "priorities": (["Clear the sleep debt with 2–3 earlier nights."] if debt > 2 else
                       ["Hold the current schedule — it's working."]),
        "source": "heuristic",
    }


def sleep_analysis(nights: list[dict], role: str) -> dict:
    try:
        out = _claude_json(SLEEP_PROMPT, {"nights": nights, "training_phase": role}, max_tokens=900)
        out["source"] = "claude"
        # keep the heuristic's numbers if Claude omitted them
        for k, v in heuristic_sleep(nights, role).items():
            out.setdefault(k, v)
        return out
    except Exception as e:
        fb = heuristic_sleep(nights, role)
        fb["analysis_error"] = str(e)
        return fb


def build_sleep(garmin) -> dict:
    def safe(fn, d):
        try:
            return fn()
        except Exception:
            return d
    role = block_meta(dt.date.today().isoformat()).get("role", "build")
    nights = safe(lambda: garmin.sleep_history(nights=14), [])
    return {
        "nights": nights,
        "analysis": sleep_analysis(nights, role),
        "block": block_meta(dt.date.today().isoformat()),
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
    }


_TS_CODES = {
    0: "No status", 1: "Detraining", 2: "Unproductive", 3: "Recovery",
    4: "Maintaining", 5: "Productive", 6: "Peaking", 7: "Overreaching", 8: "Strained",
}


def _ts_summary(ts: dict) -> tuple:
    """Best-effort (status label, acute:chronic ratio) out of Garmin's deeply
    nested get_training_status blob."""
    if not isinstance(ts, dict):
        return None, None
    label = None
    acwr = None
    mr = ts.get("mostRecentTrainingStatus") or {}
    latest = mr.get("latestTrainingStatusData") or {}
    for dev in (latest.values() if isinstance(latest, dict) else []):
        if not isinstance(dev, dict):
            continue
        code = dev.get("trainingStatus")
        if isinstance(code, int):
            label = _TS_CODES.get(code, str(code))
        label = dev.get("trainingStatusFeedbackPhrase") and label or label
        acwr = acwr or dev.get("acuteChronicWorkloadRatio") or dev.get("acwr")
    label = label or ts.get("trainingStatus")
    if isinstance(label, int):
        label = _TS_CODES.get(label, str(label))
    return label, (round(acwr, 2) if isinstance(acwr, (int, float)) else None)


def build_fitness(garmin) -> dict:
    """Everything the Fitness tab shows, assembled defensively."""
    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    next_monday = (_monday(dt.date.today()) + dt.timedelta(days=7)).isoformat()
    role = block_meta(next_monday)["role"]

    vo2_series = safe(lambda: garmin.vo2max_trend(weeks=12), [])
    vo2_current = safe(lambda: garmin.vo2max_current(), {})
    activities = safe(lambda: garmin.activities_last_weeks(weeks=12), [])
    prs = safe(lambda: garmin.personal_records(), {})
    pace_series = pace_at_hr_series(activities, weeks=12)
    rhr_series = safe(lambda: garmin.resting_hr_trend(weeks=12), [])
    endurance = safe(lambda: garmin.endurance_score(), {})
    endurance_series = ((endurance or {}).get("enduranceScoreDTO") or {}).get("groupList", []) if isinstance(endurance, dict) else []
    ts = safe(lambda: garmin.training_status(), {})
    stats_today = safe(lambda: garmin.stats(), {})

    ts_label, acwr = _ts_summary(ts)
    load_trend = safe(lambda: garmin.training_load_trend("3month"), [])
    if acwr is None:
        # Garmin doesn't expose ACWR in this API version — derive it from the
        # daily load series (7-day sum vs the 28-day weekly average).
        vals = [p.get("value") for p in load_trend if isinstance(p, dict) and isinstance(p.get("value"), (int, float))]
        if len(vals) >= 28:
            acute = sum(vals[-7:])
            chronic = sum(vals[-28:]) / 4.0
            acwr = round(acute / chronic, 2) if chronic else None

    vo2_run = vo2_current.get("running") if isinstance(vo2_current, dict) else None
    rhr_now = (stats_today or {}).get("restingHeartRate") or (rhr_series[-1]["bpm"] if rhr_series and rhr_series[-1].get("bpm") else None)

    return {
        "block": safe(lambda: block_meta(dt.date.today().isoformat()), {}),
        "vo2_series": vo2_series,
        "vo2_daily": safe(lambda: garmin.vo2max_daily(days=30), []),
        "vo2_current": vo2_current,
        "vo2_projection": project_vo2(vo2_series, role),
        "estimate_pr_5k": estimate_pr_5k(prs, activities, vo2_series, pace_series, endurance_series, role),
        "personal_records": prs,
        "biological_age": biological_age(vo2_run, rhr_now),
        "fitness_index_series": composite_fitness_index(vo2_series, pace_series, rhr_series, endurance_series),
        "pace_at_hr_series": pace_series,
        "rhr_series": rhr_series,
        "weight_series": safe(lambda: garmin.weight_trend(weeks=12), []),
        "endurance_score": endurance,
        "hill_score": safe(lambda: garmin.hill_score(), {}),
        "acclimatization": estimate_heat_acclimation(activities),
        "training_status": ts,
        "training_status_label": ts_label,
        "acwr": acwr,
        "training_load_trend": load_trend,
        "volume_12wk": safe(lambda: garmin.monthly_volume(weeks=12), []),
        "hrv_trend": safe(lambda: garmin.hrv_trend("month"), []),
        "stats_today": stats_today,
        "ftp": (ts or {}).get("functionalThresholdPower"),
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
    }
