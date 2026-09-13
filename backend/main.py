from __future__ import annotations

import os
import json
import hmac
import datetime as dt
from pathlib import Path
from functools import lru_cache

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

from garmin_client import get_client, token_status
from snapshot import serve, save as save_snapshot, load as load_snapshot, age_hours
from coach import generate_brief, answer_chat, summarize_snapshot
from morning_report import generate_morning_report
from push import add_subscription, send_notification_to_all
from planner import (
    assemble_context, generate_week_plan, adjust_week,
    block_meta, project_vo2, build_fitness, build_sleep,
)
from nutrition import (
    week_targets, deficit_for_goal, training_adherence, diet_adherence,
    batch_totals, portion_batch, scale_batch_to_days, WEEKDAYS,
)
from fatsecret import (
    FatSecretError, create_profile, day_totals, search_foods,
    start_link, finish_link, saved_foods,
)
from garmin_writer import push_workout, push_week as gc_push_week, reconcile_week

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


def _morning_extra_context() -> str:
    """Plan-vs-actual for the current week + sleep debt, for the morning brief."""
    bits = []
    try:
        monday = _plan_monday(None)
        plan = _plan_state["weeks"].get(monday) or {}
        if plan:
            client = get_client()
            end = (dt.date.fromisoformat(monday) + dt.timedelta(days=6)).isoformat()
            acts = [a for a in client.activities_in_range(monday, end) if isinstance(a, dict)]

            def km(kind):
                return round(sum((a.get("distance") or 0) for a in acts
                                 if kind in ((a.get("activityType") or {}).get("typeKey") or "")) / 1000, 1)

            bits.append(
                f"Week ({plan.get('role')}, {plan.get('focus')}-focus): "
                f"bike {km('cycling')}/{plan.get('bike_km')} km, run {km('running')}/{plan.get('run_km')} km so far."
            )
    except Exception:
        pass
    try:
        analysis = build_sleep(get_client()).get("analysis", {})
        debt = analysis.get("debt_hours")
        if isinstance(debt, (int, float)):
            bits.append(f"Sleep balance last 7 nights: {debt:+.1f} h "
                        f"(need ~{analysis.get('sleep_need_hours')} h).")
    except Exception:
        pass
    return " ".join(bits)


def _run_morning_job():
    """Generates today's report and pushes it. Called by the scheduler,
    and also callable directly for testing."""
    snapshot = _get_cached_snapshot()  # reuses today's snapshot if dashboard already pulled it
    report = generate_morning_report(snapshot, _morning_extra_context())
    today = dt.date.today().isoformat()
    _morning_cache.update(date=today, report=report)
    _persisted.update(morning_date=today, morning_report=report)
    _save_persisted_cache(_persisted)
    send_notification_to_all("Your morning report is ready", report[:120])
    return report


def _run_bedtime_job():
    """Evening wind-down nudge with tonight's recommended bedtime."""
    try:
        analysis = build_sleep(get_client()).get("analysis", {})
        bt = analysis.get("recommended_bedtime")
        need = analysis.get("sleep_need_hours")
        if bt:
            send_notification_to_all(
                "Wind-down time",
                f"Target bedtime tonight is {bt} (need ~{need} h). Screens down soon.",
            )
    except Exception:
        pass


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
scheduler.add_job(_run_bedtime_job, "cron", hour=21, minute=0)
scheduler.start()


# ---- Overview tab: recovery / sleep / strain rings + trend charts ----

_overview_cache: dict = {}


@app.get("/api/overview")
def overview(span: str = "week", refresh: bool = False):
    return serve(f"overview:{span}", lambda: _overview_live(span, refresh))


def _overview_live(span: str = "week", refresh: bool = False):
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
    return serve(f"training:{week_offset}", lambda: _training_live(week_offset, refresh))


def _training_live(week_offset: int = 0, refresh: bool = False):
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
    fb = body.get("feedback")
    if isinstance(fb, list) and fb:
        _plan_state["feedback"] = fb[-24:]                  # RPE / feel, newest kept
        _save_plan_state(_plan_state)
    _record_outcome(monday)                                 # log how last week went first
    _fitness_cache.clear()
    return plan_week(week_start=monday, refresh=True)


@app.get("/api/plan/block-review")
def plan_block_review():
    """Claude summary of the most recently completed 4-week cycle, from the
    outcomes log. Cached until a new week's outcome is recorded."""
    from planner import block_review
    return block_review(_plan_state)


@app.post("/api/plan/adjust")
def plan_adjust(body: dict = None):
    """Rework the rest of the current week around missed sessions."""
    body = body or {}
    monday = _plan_monday(body.get("week_start"))
    plan = _plan_state["weeks"].get(monday)
    if not plan:
        client = get_client()
        plan = generate_week_plan(assemble_context(client, _plan_state, monday))
        _plan_state["weeks"][monday] = plan

    missed = body.get("missed") or []
    completed = body.get("completed") or []
    days_remaining = body.get("days_remaining") or []
    try:
        snap = summarize_snapshot(get_client().snapshot())
    except Exception:
        snap = {}

    context = {
        "block": block_meta(monday),
        "week_plan": plan,
        "missed": missed,
        "completed": completed,
        "days_remaining": days_remaining,
        "recovery_snapshot": snap,
    }
    result = adjust_week(context)

    plan.setdefault("adjustments", []).append({
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "missed": missed, "result": result,
    })
    plan["adjustments"] = plan["adjustments"][-5:]
    _plan_state["weeks"][monday] = plan
    _save_plan_state(_plan_state)
    return result


@app.get("/api/fitness")
def fitness(refresh: bool = False):
    return serve("fitness", lambda: _fitness_live(refresh))


def _fitness_live(refresh: bool = False):
    key = dt.date.today().isoformat()
    if _fitness_cache.get("key") == key and not refresh:
        return _fitness_cache["data"]
    data = build_fitness(get_client())
    _fitness_cache.update(key=key, data=data)
    return data


# ---- Push structured workouts to Garmin ----

@app.post("/api/garmin/push-workout")
def garmin_push_workout(body: dict = None):
    """Create one structured workout on Garmin (and schedule it if a date is
    given). Body: {name, sport ('Run'|'Bike'), prescription, date?}."""
    body = body or {}
    return push_workout(
        body.get("name") or "Training Coach session",
        body.get("sport") or "Run",
        body.get("prescription") or body.get("title") or "",
        body.get("date"),
    )


_sleep_cache: dict = {}


@app.get("/api/sleep")
def sleep(refresh: bool = False):
    return serve("sleep", lambda: _sleep_live(refresh))


def _sleep_live(refresh: bool = False):
    key = dt.date.today().isoformat()
    if _sleep_cache.get("key") == key and not refresh:
        return _sleep_cache["data"]
    data = build_sleep(get_client())
    _sleep_cache.update(key=key, data=data)
    return data


@app.post("/api/garmin/push-week")
def garmin_push_week(body: dict = None):
    """Push every cardio session in a week's plan to Garmin. Body: {week_start}."""
    body = body or {}
    monday = _plan_monday(body.get("week_start"))
    plan = _plan_state["weeks"].get(monday)
    if not plan:
        plan = generate_week_plan(assemble_context(get_client(), _plan_state, monday))
        _plan_state["weeks"][monday] = plan
        _save_plan_state(_plan_state)
    return gc_push_week(monday, plan)


@app.get("/api/garmin/scheduled")
def garmin_scheduled(week_start: str = None):
    """What's on the Garmin calendar this week vs the app plan (app plan wins)."""
    monday = _plan_monday(week_start)
    plan = _plan_state["weeks"].get(monday) or {}
    return reconcile_week(monday, plan)


# ---- Nutrition: macro estimator / planner ----
# Forecast, not a diary. The planner already knows the week's work, so this
# turns it into a per-day energy + macro target you can portion meal prep to.
# Intake logging lives in a real tracker; this side only produces the numbers.

_NUTRITION_FILE = Path(__file__).parent / "nutrition_state.json"


def _load_nutrition_state() -> dict:
    if _NUTRITION_FILE.exists():
        try:
            return json.loads(_NUTRITION_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_nutrition_state(data: dict) -> None:
    try:
        _NUTRITION_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        print(f"nutrition state save failed: {e}")


_nutrition_state = _load_nutrition_state()
_nutrition_state.setdefault("mode", "fuel")
_nutrition_state.setdefault("prescribed_deficit_kcal", 0)
_nutrition_state.setdefault("intake", {})
_nutrition_state.setdefault("fatsecret", {})       # iso date -> {kcal, protein_g, ...}


def _athlete_profile() -> dict:
    """Weight / height / age / sex straight off the Garmin profile."""
    ud = (get_client().get_user_profile() or {}).get("userData") or {}
    age = None
    if ud.get("birthDate"):
        b = dt.date.fromisoformat(ud["birthDate"])
        today = dt.date.today()
        age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    return {
        "weight_kg": (ud.get("weight") or 0) / 1000.0 or None,
        "height_cm": ud.get("height"),
        "age": age,
        "sex": ud.get("gender", "MALE"),
    }


def _plan_for(monday: str) -> dict:
    plan = _plan_state["weeks"].get(monday)
    if not plan:
        plan = generate_week_plan(assemble_context(get_client(), _plan_state, monday))
        _plan_state["weeks"][monday] = plan
        _save_plan_state(_plan_state)
    return plan


def _nutrition_mode() -> tuple:
    """
    (mode, prescribed_deficit) — environment first, then local state.

    Same reason as _fatsecret_creds: nutrition_state.json is gitignored, so a
    deficit configured locally silently reverts to plain fuelling on the
    deployed host. A target that quietly differs between two copies of the
    same app is worse than one that is wrong in an obvious way.
    """
    mode = os.environ.get("NUTRITION_MODE") or _nutrition_state["mode"]
    raw = os.environ.get("NUTRITION_DEFICIT_KCAL")
    if raw:
        try:
            deficit = int(raw)
        except ValueError:
            print(f"NUTRITION_DEFICIT_KCAL is not a number: {raw!r} — ignoring")
            deficit = _nutrition_state["prescribed_deficit_kcal"]
    else:
        deficit = _nutrition_state["prescribed_deficit_kcal"]
    return mode, deficit


def _week_targets_now(week_start: str = None) -> dict:
    """This week's targets, or an {"error"} dict the routes can return as-is."""
    profile = _athlete_profile()
    if not profile.get("age"):
        return {"error": "No birth date on the Garmin profile."}
    mode, deficit = _nutrition_mode()
    return week_targets(
        _plan_for(_plan_monday(week_start)), profile,
        mode=mode, prescribed_deficit_kcal=deficit,
    )


@app.get("/api/nutrition/week")
def nutrition_week(week_start: str = None, mode: str = None, deficit: int = None):
    """Per-day kcal + macro targets for a planned week."""
    monday = _plan_monday(week_start)
    profile = _athlete_profile()
    if not profile.get("age"):
        return {"error": "No birth date on the Garmin profile."}
    return week_targets(
        _plan_for(monday), profile,
        mode=mode or _nutrition_mode()[0],
        prescribed_deficit_kcal=(deficit if deficit is not None
                                 else _nutrition_mode()[1]),
    )


@app.get("/api/nutrition/goal")
def nutrition_goal(current_kg: float, target_kg: float, weeks: float):
    """The daily deficit a weight goal actually requires."""
    return deficit_for_goal(current_kg, target_kg, weeks)


@app.get("/api/nutrition/adherence")
def nutrition_adherence(week_start: str = None):
    """Did you do the work, and did you fuel it — for one week."""
    monday = _plan_monday(week_start)
    plan = _plan_for(monday)
    sunday = (dt.date.fromisoformat(monday) + dt.timedelta(days=6)).isoformat()

    try:
        activities = get_client().activities_in_range(monday, sunday)
    except Exception as e:
        activities = []
        print(f"adherence: activity fetch failed: {e}")

    # For the current week, score only the days that have already happened.
    today = dt.date.today()
    monday_date = dt.date.fromisoformat(monday)
    through = (today - monday_date).days
    through = through if 0 <= through <= 6 else None
    training = training_adherence(plan, activities, through_day=through)

    profile = _athlete_profile()
    diet = {"logged_days": 0, "unlogged_days": 7, "rate": None,
            "note": "No intake source connected yet."}
    if profile.get("age"):
        cfg_mode, cfg_deficit = _nutrition_mode()
        targets = week_targets(
            plan, profile,
            mode=cfg_mode, prescribed_deficit_kcal=cfg_deficit,
        )
        diet = diet_adherence(targets["days"], _nutrition_state["intake"])

    return {"week_start": monday, "training": training, "diet": diet}


@app.post("/api/nutrition/link/start")
def nutrition_link_start():
    """
    Begin linking the athlete's own fatsecret.com account.

    Returns a URL to approve in a browser. The temporary secret is stashed
    server-side because it forms half of the signing key for the exchange —
    it is not something the caller should have to carry back.
    """
    try:
        d = start_link()
    except FatSecretError as e:
        return {"error": str(e)}
    _nutrition_state["fatsecret_pending"] = {
        "request_token": d["request_token"],
        "request_secret": d["request_secret"],
    }
    _save_nutrition_state(_nutrition_state)
    return {"authorize_url": d["authorize_url"],
            "next": "Approve in a browser, then POST the PIN to /api/nutrition/link/finish"}


@app.post("/api/nutrition/link/finish")
def nutrition_link_finish(body: dict):
    """Exchange the PIN for a lasting access token. Body: {verifier}."""
    pending = _nutrition_state.get("fatsecret_pending") or {}
    if not pending.get("request_token"):
        return {"error": "No link in progress — call /api/nutrition/link/start first."}
    verifier = (body or {}).get("verifier", "")
    if not verifier:
        return {"error": "Missing verifier (the PIN shown after approving)."}
    try:
        creds = finish_link(pending["request_token"], pending["request_secret"], verifier)
    except FatSecretError as e:
        return {"error": str(e)}
    _nutrition_state["fatsecret"] = creds
    _nutrition_state.pop("fatsecret_pending", None)
    _save_nutrition_state(_nutrition_state)
    return {"status": "linked"}


@app.post("/api/nutrition/connect")
def nutrition_connect():
    """
    Mint the FatSecret profile this backend logs against.

    Creates a durable object on FatSecret and returns a token pair that is
    shown once, so it is stored immediately. Idempotent by refusal: if a
    profile is already stored it is kept rather than silently replaced, which
    would orphan every diary entry logged against the old one.
    """
    if _nutrition_state.get("fatsecret", {}).get("token"):
        return {"status": "already-connected"}
    try:
        creds = create_profile(user_id=f"race-coach-{dt.date.today().isoformat()}")
    except FatSecretError as e:
        return {"error": str(e)}
    _nutrition_state["fatsecret"] = creds
    _save_nutrition_state(_nutrition_state)
    return {"status": "connected"}


@app.post("/api/nutrition/sync")
def nutrition_sync(week_start: str = None):
    """Pull each day's logged intake from FatSecret into local state."""
    creds = _fatsecret_creds()
    if not creds.get("token"):
        return {"error": "Not connected — POST /api/nutrition/connect first."}

    monday = dt.date.fromisoformat(_plan_monday(week_start))
    pulled, failed = 0, []
    for i in range(7):
        day = (monday + dt.timedelta(days=i)).isoformat()
        try:
            totals = day_totals(creds["token"], creds["secret"], day)
        except FatSecretError as e:
            failed.append({"date": day, "error": str(e)})
            continue
        if totals["kcal"]:
            _nutrition_state["intake"][day] = totals
            pulled += 1
    _save_nutrition_state(_nutrition_state)
    return {"days_with_intake": pulled, "failures": failed}


@app.get("/api/nutrition/foods")
def nutrition_foods(q: str, limit: int = 20):
    """Food database search, for building meal-prep components."""
    try:
        return search_foods(q, max_results=limit)
    except FatSecretError as e:
        return {"error": str(e)}


def _fatsecret_creds() -> dict:
    """
    The linked account's access token, environment first.

    nutrition_state.json is gitignored — correctly, it holds a credential —
    which means a link made locally does not survive a deploy. Reading the
    environment first lets the same token be handed to Render as config,
    so the link is made once rather than re-run through the PIN flow on
    every host.
    """
    token = os.environ.get("FATSECRET_ACCESS_TOKEN")
    secret = os.environ.get("FATSECRET_ACCESS_SECRET")
    if token and secret:
        return {"token": token, "secret": secret}
    return _nutrition_state.get("fatsecret") or {}


@app.get("/api/nutrition/prep/pantry")
def nutrition_pantry():
    """
    The athlete's own saved foods on FatSecret, as meal-prep components.

    Saved foods rather than the whole database: these are the things actually
    cooked with, already carrying the right brand and preparation.
    """
    creds = _fatsecret_creds()
    if not creds.get("token"):
        return {"error": "Not linked — run /api/nutrition/link/start first."}
    try:
        return {"foods": saved_foods(creds["token"], creds["secret"])}
    except FatSecretError as e:
        return {"error": str(e)}


@app.post("/api/nutrition/prep/plan")
def nutrition_prep_plan(body: dict):
    """
    How much to buy and cook. Body: {ingredients, days?, share_of_day?}.

    days defaults to weekdays — the prep covers Monday to Friday, with the
    weekend cooked fresh.
    """
    body = body or {}
    week = _week_targets_now(body.get("week_start"))
    if "error" in week:
        return week
    wanted = set(body.get("days") or WEEKDAYS)
    days = [d for d in week["days"] if d["day"] in wanted]
    return scale_batch_to_days(body.get("ingredients") or [], days,
                               float(body.get("share_of_day", 1.0)))


@app.post("/api/nutrition/prep/portion")
def nutrition_prep_portion(body: dict):
    """
    Split the cooked batch. Body: {ingredients, cooked_grams, days?, share_of_day?}.
    """
    body = body or {}
    week = _week_targets_now(body.get("week_start"))
    if "error" in week:
        return week
    wanted = set(body.get("days") or WEEKDAYS)
    days = [d for d in week["days"] if d["day"] in wanted]
    batch = batch_totals(body.get("ingredients") or [])
    return portion_batch(batch, body.get("cooked_grams"), days,
                         float(body.get("share_of_day", 1.0)))


@app.get("/api/garmin/status")
def garmin_status():
    """Diagnose the Garmin session: env var, token expiry, live call."""
    return token_status()


@app.post("/api/sync/push")
def sync_push(body: dict, x_sync_secret: str = Header(None)):
    """
    Accept a bundle of dashboard payloads from the athlete's own machine.

    Shared-secret authenticated: this replaces what every reader sees, so it
    must not be open. Refuses outright when SYNC_SECRET is unset rather than
    defaulting to open — an unset secret on a public host is the failure that
    matters here.
    """
    expected = os.environ.get("SYNC_SECRET")
    if not expected:
        raise HTTPException(status_code=503, detail="SYNC_SECRET is not configured on this server.")
    if not x_sync_secret or not hmac.compare_digest(x_sync_secret, expected):
        raise HTTPException(status_code=401, detail="Bad or missing X-Sync-Secret.")
    payloads = (body or {}).get("payloads")
    if not isinstance(payloads, dict) or not payloads:
        raise HTTPException(status_code=400, detail="Body needs a non-empty 'payloads' object.")
    return save_snapshot(payloads)


@app.get("/api/sync/status")
def sync_status():
    """When the snapshot was last refreshed, and what it holds."""
    data = load_snapshot()
    age = age_hours(data.get("pushed_at"))
    return {
        "pushed_at": data.get("pushed_at"),
        "age_hours": round(age, 1) if age is not None else None,
        "keys": sorted((data.get("payloads") or {}).keys()),
        "secret_configured": bool(os.environ.get("SYNC_SECRET")),
    }


# ---- Serve the frontend from this same service ----
# Single-service deploy: no separate static host, so the app and its API share
# one origin (no CORS, no "paste your backend URL" step). Mounted LAST so every
# /api/* route declared above still wins — StaticFiles only sees what's left.
# Path is derived from this file, not the working directory, so it resolves
# whether the process starts from the repo root or from backend/.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
    print(f"Serving frontend from {_FRONTEND_DIR}")
else:
    print(f"No frontend directory at {_FRONTEND_DIR} — API only.")
