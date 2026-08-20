from __future__ import annotations

import os
import datetime as dt
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

RACE_NAME = os.environ.get("RACE_NAME", "Race Day")
RACE_DATE = os.environ.get("RACE_DATE")  # YYYY-MM-DD
RACE_GOAL = os.environ.get("RACE_GOAL", "Finish strong and healthy.")

app = FastAPI(title="Race Coach API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your frontend's real origin once deployed
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Cache the daily snapshot + brief in memory for a few minutes so repeated
# dashboard refreshes / phone wake-ups don't hammer Garmin or the Claude API.
_cache = {"date": None, "snapshot": None, "brief": None}


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
        brief = generate_brief(snapshot, RACE_GOAL) if need_new_brief else _cache["brief"]
        _cache.update(date=today, snapshot=snapshot, brief=brief)

    return {
        "race": {
            "name": RACE_NAME,
            "date": RACE_DATE,
            "days_to_race": _days_to_race(),
            "goal": RACE_GOAL,
        },
        "snapshot": _cache["snapshot"],
        "coach_brief": _cache["brief"],
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---- Morning report: broader daily briefing, top-of-screen + push ----

_morning_cache: dict = {"date": None, "report": None}


def _run_morning_job():
    """Generates today's report and pushes it. Called by the scheduler,
    and also callable directly for testing."""
    client = get_client()
    snapshot = client.snapshot()
    report = generate_morning_report(snapshot)
    _morning_cache.update(date=dt.date.today().isoformat(), report=report)
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

@app.post("/api/chat")
def chat(body: dict):
    message = body.get("message", "")
    history = body.get("history", [])
    if not message:
        return {"reply": "Ask me something first."}
    client = get_client()
    snapshot = client.snapshot()
    reply = answer_chat(message, snapshot, RACE_GOAL, history)
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
