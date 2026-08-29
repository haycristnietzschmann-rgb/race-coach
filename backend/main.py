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
        _persisted.update(brief_date=today, brief=brief, snapshot=snapshot)
        _save_persisted_cache(_persisted)

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
    reply = answer_chat(message, snapshot, RACE_GOAL, history)
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
