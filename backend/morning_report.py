"""
Generates the daily "morning report" — a broader daily health/training
summary shown at the top of the Overview tab and pushed as a notification.
Distinct from coach.py's goal-focused brief: this one covers recovery,
sleep quality, and a general recommendation for the day.
"""
import os
import json
import anthropic
from coach import summarize_snapshot

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You write a short "good morning" briefing for one athlete, \
based on their overnight Garmin data. This is broader than goal-specific coaching \
— it's a general daily readiness note, similar to what Whoop's AI coach sends \
each morning.

Structure, in this order, under 90 words total:
1. One-sentence headline verdict (e.g. "Well recovered, ready to train")
2. The single biggest factor behind that verdict, grounded in the actual numbers
3. One concrete suggestion for today's training intensity

Be direct and specific. If data is missing, say so rather than inventing it. \
No greetings, no sign-off, no filler."""


def generate_morning_report(snapshot: dict) -> str:
    # Trim the raw Garmin snapshot before sending — the raw version includes
    # minute-by-minute sleep movement, SpO2, HRV, and respiration data
    # (hundreds of entries), which was silently inflating every call to
    # 30,000+ tokens instead of a few hundred.
    lean_snapshot = summarize_snapshot(snapshot)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Overnight/today Garmin summary:\n{json.dumps(lean_snapshot, indent=2, default=str)}",
        }],
    )
    return "".join(b.text for b in response.content if b.type == "text")
