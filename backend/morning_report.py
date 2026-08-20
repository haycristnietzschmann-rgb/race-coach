"""
Generates the daily "morning report" — a broader daily health/training
summary shown at the top of the Overview tab and pushed as a notification.
Distinct from coach.py's race-focused brief: this one covers recovery,
sleep quality, and a general recommendation for the day.
"""
import os
import json
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You write a short "good morning" briefing for one athlete, \
based on their overnight Garmin data. This is broader than race-day coaching \
— it's a general daily readiness note, similar to what Whoop's AI coach sends \
each morning.

Structure, in this order, under 90 words total:
1. One-sentence headline verdict (e.g. "Well recovered, ready to train")
2. The single biggest factor behind that verdict, grounded in the actual numbers
3. One concrete suggestion for today's training intensity

Be direct and specific. If data is missing, say so rather than inventing it. \
No greetings, no sign-off, no filler."""


def generate_morning_report(snapshot: dict) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Overnight/today Garmin summary:\n{json.dumps(snapshot, indent=2, default=str)}",
        }],
    )
    return "".join(b.text for b in response.content if b.type == "text")
