from __future__ import annotations

import os
import json
import datetime as dt
from pathlib import Path
from functools import lru_cache

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

from garmin_client import get_client
from coach import generate_brief, answer_chat
from morning_report import generate_morning_report
from push import add_subscription, send_notification_to_all
from planner import assemble_context, generate_week_plan, block_meta, project_vo2

# Training goal fed to the Claude coaching prompts (morning brief + Ask Coach).
# Falls back to the legacy RACE_GOAL env var so existing Render configs keep
# working until they're renamed to TRAINING_GOAL.
TRAINING_GOAL = os.environ.get(
    "TRAINING_GOAL",
    os.environ.get(
        "RACE_GOAL",
        "Get genuinely faster at running and cycling while holding solid weekly distance.",
    ),
)

# Optional — only set these when training for a specific race. Consumed by
# /api/dashboard's "race" block; left unset the app runs in general-training mode.
RACE_NAME = os.environ.get("RACE_NAME")
RACE_DATE = os.environ.get("RACE_DATE")  # YYYY-MM-DD

app = FastAPI(title="Training Coach API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your frontend's real origin once deployed
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---- Persisted daily cache ----
# Render's free tier stops the process after ~15 minutes of no traffic and
# restarts it on the next request. An in-memory cache alone gets wiped by
# that restart, meaning a genuinely same-day request could trigger a fresh
# (paid) Claude call for no reason. Persisting to disk means a cold restart
# on the same calendar day still finds today's already-generated text.
_CACHE_FILE = Path(__file__).parent / "daily_cache.json"

def _load_persisted_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_persisted_cache(data: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(data, default=str))
    except Exception:
        pass

_persisted = _load_persisted_cache()
_cache = {
    "date": _persisted.get("brief_date"),
    "snapshot": _persisted.get("snapshot"),
    "brief": _persisted.get("brief"),
}

# ---- Adaptive planner state ----
# Separate file from the daily cache: this one is the planner's memory —
# every generated week plus the actuals + recovery/VO2 deltas that followed,
# so the weekly recalculation can learn what ramp / deload has worked.
_PLAN_FILE = Path(__file__).parent / "plan_state.json"

def _load_plan_state() -> dict:
    if _PLAN_FILE.exists():
        try:
            return json.loads(_PLAN_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_plan_state(data: dict) -> None:
    try:
        _PLAN_FILE.write_text(json.dumps(data, default=str))
    except Exception:
        pass

_plan_state = _load_plan_state()
_plan_state.setdefault("weeks", {})       # monday_iso -> generated plan
_plan_state.setdefault("outcomes", [])    # rolling log of week -> what happened


def _days_to_race() -> int | None:
    if not RACE_DATE:
        return None
    target = dt.date.fromisoformat(RACE_DATE)
    return (target - dt.date.today()).days


@app.get("/api/dashboard")
def dashboard(refresh: bool = False):
    today = dt.date.today().isoformat()

    # Only actually bypass the cache if it's genuinely stale (new day) or the
    # cache is empty. A manual "refresh" no longer forces a fresh (paid)
    # Claude call every single click — it just re-pulls Garmin data, which is
    # free, and reuses today's brief unless it doesn't exist yet.
    need_new_brief = _cache["date"] != today or _cache["brief"] is None

    if _cache["date"] != today or refresh:
        client = get_client()
        snapshot = client.snapshot()
        brief = generate_brief(snapshot, TRAINING_GOAL) if need_new_brief else _cache["brief"]
        _cache.update(date=today, snapshot=snapshot, brief=brief)
        _persisted.update(brief_date=today, brief=brief, snapshot=snapshot)
        _save_persisted_cache(_persisted)

    return {
        "training_goal": TRAINING_GOAL,
        "race": {
            "name": RACE_NAME,
            "date": RACE_DATE,
            "days_to_race": _days_to_race(),
        },
        "snapshot": _cache["snapshot"],
        "coach_brief": _cache["brief"],
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---- Morning report: broader daily briefing, top-of-screen + push ----

_morning_cache: dict = {"date": _persisted.get("morning_date"), "report": _persisted.get("morning_report")}


def _run_morning_job():
    """Generates today's report and pushes it. Called by the scheduler,
    and also callable directly for testing."""
    snapshot = _get_cached_snapshot()  # reuses today's snapshot if dashboard already pulled it
    report = generate_morning_report(snapshot)
    today = dt.date.today().isoformat()
    _morning_cache.update(date=today, report=report)
    _persisted.update(morning_date=today, morning_report=report)
    _save_persisted_cache(_persisted)
    send_notification_to_all("Your morning report is ready", report[:120])
    return report


@app.get("/api/morning-report")
def morning_report():
    today = dt.date.today().isoformat()
    if _morning_cache["date"] != today:
        _run_morning_job()
    return {"date": _morning_cache["date"], "report": _morning_cache["report"]}


@app.post("/api/morning-report/generate-now")
def morning_report_generate_now():
    """Manual trigger — handy for testing without waiting for the scheduled hour."""
    return {"date": dt.date.today().isoformat(), "report": _run_morning_job()}


# ---- Push notification subscription ----

@app.get("/api/vapid-public-key")
def vapid_public_key():
    return {"key": os.environ.get("VAPID_PUBLIC_KEY", "")}


@app.post("/api/subscribe")
def subscribe(subscription: dict):
    add_subscription(subscription)
    return {"status": "subscribed"}


# ---- Ask Coach: live chat, grounded in today's real Garmin snapshot ----

def _get_cached_snapshot():
    """Garmin data genuinely doesn't change meaningfully within a day, so
    every endpoint that needs a snapshot (dashboard, chat, morning report)
    shares one pull per day instead of each hitting Garmin separately —
    and reuses the dashboard's persisted snapshot if that already ran today."""
    today = dt.date.today().isoformat()
    if _cache["date"] == today and _cache["snapshot"] is not None:
        return _cache["snapshot"]
    client = get_client()
    snapshot = client.snapshot()
    _cache.update(date=today, snapshot=snapshot)
    _persisted.update(brief_date=today, snapshot=snapshot)
    _save_persisted_cache(_persisted)
    return snapshot

# Persisted like the brief/report caches above — keyed by "date::question" as
# a plain string since JSON can't use tuples as dict keys. Old-date entries
# are pruned on save so this file doesn't grow forever.
_chat_cache: dict = _persisted.get("chat_cache", {})

def _chat_cache_key(today: str, message: str) -> str:
    return today + "::" + message.strip().lower()

@app.post("/api/chat")
def chat(body: dict):
    message = body.get("message", "")
    history = body.get("history", [])
    if not message:
        return {"reply": "Ask me something first."}
    today = dt.date.today().isoformat()
    cache_key = _chat_cache_key(today, message)
    # Only cache standalone questions (no conversation history) — a repeated
    # follow-up mid-conversation depends on context and shouldn't reuse an
    # old answer, but re-asking the same suggestion-chip question later the
    # same day (even after a server restart) should just return what was
    # already said, free.
    if not history and cache_key in _chat_cache:
        return {"reply": _chat_cache[cache_key]}
    snapshot = _get_cached_snapshot()
    reply = answer_chat(message, snapshot, TRAINING_GOAL, history)
    if not history:
        _chat_cache[cache_key] = reply
        # keep only today's entries so this doesn't grow unbounded over time
        pruned = {k: v for k, v in _chat_cache.items() if k.startswith(today + "::")}
        _chat_cache.clear(); _chat_cache.update(pruned)
        _persisted["chat_cache"] = _chat_cache
        _save_persisted_cache(_persisted)
    return {"reply": reply}


# ---- Daily scheduler: generates + pushes the morning report automatically ----
# Runs only while the backend process is alive — once you deploy this to
# Render/Railway (always-on), it fires every morning without you doing anything.

scheduler = BackgroundScheduler()
scheduler.add_job(_run_morning_job, "cron", hour=7, minute=0)
scheduler.start()


# ---- Overview tab: recovery / sleep / strain rings + trend charts ----

_overview_cache: dict = {}


@app.get("/api/overview")
def overview(span: str = "week", refresh: bool = False):
    """span: week | month | 3month"""
    if span not in ("week", "month", "3month"):
        span = "week"

    key = f"{span}:{dt.date.today().isoformat()}"
    if key in _overview_cache and not refresh:
        return _overview_cache[key]

    client = get_client()
    today = dt.date.today().isoformat()

    result = {
        "span": span,
        "today": {
            "readiness": client.readiness(today),
            "sleep": client.sleep(today),
            "hrv": client.hrv(today),
            "training_status": client.training_status(today),
            "body_battery": client.body_battery(today),
            "stats": client.stats(today),
        },
        "trends": {
            "readiness": client.readiness_trend(span),
            "sleep": client.sleep_trend(span),
            "hrv": client.hrv_trend(span),
            "training_load": client.training_load_trend(span),
        },
    }
    _overview_cache.clear()  # only keep the latest span cached
    _overview_cache[key] = result
    return result


# ---- Training tab: weekly calendar, summary, HR zones, monthly volume ----

_training_cache: dict = {}


@app.get("/api/training")
def training(week_offset: int = 0, refresh: bool = False):
    key = f"{week_offset}:{dt.date.today().isoformat()}"
    if key in _training_cache and not refresh:
        return _training_cache[key]

    client = get_client()
    start, end = client.week_bounds(offset_weeks=week_offset)
    activities = client.activities_in_range(start, end)
    valid_activities = [a for a in activities if isinstance(a, dict) and "activityId" in a]

    total_distance = sum(a.get("distance") or 0 for a in valid_activities)
    total_duration = sum(a.get("duration") or 0 for a in valid_activities)
    total_calories = sum(a.get("calories") or 0 for a in valid_activities)

    result = {
        "week_start": start,
        "week_end": end,
        "activities": valid_activities,
        "summary": {
            "distance_km": round(total_distance / 1000, 1),
            "duration_min": round(total_duration / 60),
            "calories": round(total_calories),
            "sessions": len(valid_activities),
        },
        "hr_zones": client.weekly_hr_zones(valid_activities),
        "monthly_volume": client.monthly_volume(weeks=10),
    }
    _training_cache.clear()
    _training_cache[key] = result
    return result


# ---- Adaptive planner + Fitness tab ----

_fitness_cache: dict = {}


def _plan_monday(week_start: str = None) -> str:
    base = week_start or dt.date.today().isoformat()
    try:
        return block_meta(base)["week_start"]
    except Exception:
        return block_meta(dt.date.today().isoformat())["week_start"]


def _completion_pct(plan: dict, bike_actual: float, run_actual: float) -> int:
    planned = (plan.get("bike_km") or 0) + (plan.get("run_km") or 0)
    if planned <= 0:
        return 0
    return round((bike_actual + run_actual) / planned * 100)


def _record_outcome(current_monday: str) -> None:
    """Learning write: snapshot how the PREVIOUS week actually went vs its
    plan, so future recalculations can see what ramp/deload landed well."""
    prev = (dt.date.fromisoformat(current_monday) - dt.timedelta(days=7)).isoformat()
    prev_plan = _plan_state["weeks"].get(prev)
    if not prev_plan:
        return
    try:
        client = get_client()
        end = (dt.date.fromisoformat(prev) + dt.timedelta(days=6)).isoformat()
        acts = [a for a in client.activities_in_range(prev, end) if isinstance(a, dict)]

        def km(kind: str) -> float:
            return round(sum((a.get("distance") or 0) for a in acts
                             if kind in ((a.get("activityType") or {}).get("typeKey") or "")) / 1000, 1)

        bike_actual, run_actual = km("cycling"), km("running")
        vo2 = client.vo2max_current()
        outcome = {
            "week_start": prev,
            "planned": {k: prev_plan.get(k) for k in ("bike_km", "run_km", "role", "ramp_pct", "deload_pct")},
            "actual": {"bike_km": bike_actual, "run_km": run_actual},
            "completion_pct": _completion_pct(prev_plan, bike_actual, run_actual),
            "vo2_after": {"running": vo2.get("running"), "cycling": vo2.get("cycling")} if isinstance(vo2, dict) else None,
            "vo2_projected_for_this_week": (prev_plan.get("vo2_projection") or {}).get("next_week"),
            "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        _plan_state["outcomes"] = [o for o in _plan_state["outcomes"] if o.get("week_start") != prev]
        _plan_state["outcomes"].append(outcome)
        _plan_state["outcomes"] = _plan_state["outcomes"][-12:]
        _save_plan_state(_plan_state)
    except Exception:
        pass


@app.get("/api/plan/week")
def plan_week(week_start: str = None, refresh: bool = False):
    monday = _plan_monday(week_start)
    existing = _plan_state["weeks"].get(monday)
    if existing and not refresh:
        return existing

    client = get_client()
    context = assemble_context(client, _plan_state, monday)
    plan = generate_week_plan(context)

    _plan_state["weeks"][monday] = plan
    if len(_plan_state["weeks"]) > 16:                     # keep ~4 months
        for stale in sorted(_plan_state["weeks"])[:-16]:
            _plan_state["weeks"].pop(stale, None)
    _save_plan_state(_plan_state)
    return plan


@app.post("/api/plan/recalculate")
def plan_recalculate(body: dict = None):
    body = body or {}
    monday = _plan_monday(body.get("week_start"))
    _record_outcome(monday)                                 # log how last week went first
    return plan_week(week_start=monday, refresh=True)


@app.get("/api/fitness")
def fitness(refresh: bool = False):
    key = dt.date.today().isoformat()
    if _fitness_cache.get("key") == key and not refresh:
        return _fitness_cache["data"]

    client = get_client()
    this_monday = _plan_monday(None)
    next_monday = (dt.date.fromisoformat(this_monday) + dt.timedelta(days=7)).isoformat()
    vo2_series = client.vo2max_trend(weeks=10)

    data = {
        "vo2_series": vo2_series,
        "vo2_current": client.vo2max_current(),
        "vo2_projection": project_vo2(vo2_series, block_meta(next_monday)["role"]),
        "volume_12wk": client.monthly_volume(weeks=12),
        "readiness_trend": client.readiness_trend("3month"),
        "hrv_trend": client.hrv_trend("month"),
        "training_load_trend": client.training_load_trend("month"),
        "stats_today": client.stats(),
        "block": block_meta(dt.date.today().isoformat()),
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    _fitness_cache.update(key=key, data=data)
    return data
