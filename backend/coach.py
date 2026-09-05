"""
Turns a raw Garmin data snapshot into a short, opinionated coaching note
using the Claude API.
"""
from __future__ import annotations

import os
import json
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are a calm, experienced endurance coach speaking directly \
to one athlete you know well. You get a JSON snapshot of their Garmin data \
(readiness, sleep, HRV, training status, body battery, recent activities) \
plus their training goal. Write a short daily brief:

1. One line verdict: go hard / easy day / rest / normal training
2. 2-3 sentences of reasoning grounded in the actual numbers you were given
3. One concrete instruction for today

Be direct and specific, not generic motivational filler. If data is missing \
or errored, say so plainly rather than inventing numbers. Keep it under 120 words."""


def summarize_snapshot(snapshot: dict) -> dict:
    """Strip a raw Garmin snapshot down to the handful of fields the coach
    actually needs. The full snapshot includes minute-by-minute time series
    (sleep movement, stress readings, etc.) that cost real money to send to
    Claude on every call for no benefit — this keeps only aggregates."""
    sleep = snapshot.get("sleep") or {}
    scores = sleep.get("sleepScores", {}) if isinstance(sleep, dict) else {}
    readiness = snapshot.get("readiness") or {}
    hrv = snapshot.get("hrv") or {}
    training_status = snapshot.get("training_status") or {}
    body_battery = snapshot.get("body_battery") or {}
    activities = snapshot.get("recent_activities") or []

    return {
        "date": snapshot.get("date"),
        "readiness_score": readiness.get("score") if isinstance(readiness, dict) else None,
        "sleep_score": scores.get("overall", {}).get("value") if isinstance(scores, dict) else None,
        "sleep_duration_sec": sleep.get("sleepTimeSeconds"),
        "deep_sleep_sec": sleep.get("deepSleepSeconds"),
        "rem_sleep_sec": sleep.get("remSleepSeconds"),
        "resting_hr": sleep.get("restingHeartRate"),
        "hrv_last_night": hrv.get("lastNightAvg") if isinstance(hrv, dict) else None,
        "training_load": training_status.get("trainingLoad") if isinstance(training_status, dict) else None,
        "vo2_max": training_status.get("vo2Max") if isinstance(training_status, dict) else None,
        "body_battery": body_battery.get("charged") if isinstance(body_battery, dict) else None,
        "recent_activities": [
            {"name": a.get("activityName"), "distance_km": round((a.get("distance") or 0) / 1000, 1)}
            for a in activities[:5] if isinstance(a, dict)
        ],
    }


CHAT_SYSTEM_PROMPT = """You are a calm, experienced endurance coach in an ongoing \
chat with one athlete you know well. You're given their current Garmin snapshot \
(recovery, sleep, HRV, training load) and their training goal as background. Answer \
their question directly and conversationally, grounded in the actual numbers \
you were given. Keep answers to 2-4 sentences unless the question genuinely \
needs more. If data needed to answer is missing, say so rather than inventing it."""


def generate_brief(snapshot: dict, training_goal: str) -> str:
    lean_snapshot = summarize_snapshot(snapshot)
    user_content = (
        f"Training goal: {training_goal}\n\n"
        f"Today's Garmin summary:\n{json.dumps(lean_snapshot, indent=2, default=str)}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # cheapest current model, per your cost priority
        max_tokens=250,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def answer_chat(message: str, snapshot: dict, training_goal: str, history: list[dict] | None = None) -> str:
    lean_snapshot = summarize_snapshot(snapshot)
    context = (
        f"Training goal: {training_goal}\n"
        f"Current Garmin summary:\n{json.dumps(lean_snapshot, indent=2, default=str)}"
    )
    messages = [{"role": "user", "content": f"[Background context]\n{context}"}]
    messages.append({"role": "assistant", "content": "Got it — I have your current data. What's up?"})
    for turn in (history or [])[-6:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # interactive chat — cheap model is the right fit here
        max_tokens=200,
        system=CHAT_SYSTEM_PROMPT,
        messages=messages,
    )
    return "".join(block.text for block in response.content if block.type == "text")
