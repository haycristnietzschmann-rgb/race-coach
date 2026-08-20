"""
Thin wrapper around the unofficial `garminconnect` library.

Logs in once, caches the session (garminconnect handles token caching to
disk via garth under the hood), and exposes the handful of endpoints the
dashboard needs.

Security note: your Garmin username/password are read from environment
variables and never sent anywhere except Garmin's own servers via this
library. Do not commit a .env file with real credentials.
"""
from __future__ import annotations

import os
import io
import base64
import zipfile
import datetime as dt
from pathlib import Path
from functools import lru_cache

import garminconnect

# Where a resumable session (from generate_garmin_tokens.py) gets unpacked to.
TOKEN_DIR = Path(os.environ.get("GARMIN_TOKENSTORE_DIR", "/tmp/garmin_tokens"))


def _ensure_tokens_on_disk() -> None:
    """If a saved session was provided via GARMIN_TOKENS_B64 (created by
    running generate_garmin_tokens.py on a machine Garmin doesn't block),
    unpack it to disk once. login() then resumes that session instead of
    attempting a fresh password login — which Cloudflare blocks from most
    cloud/datacenter server IPs, including Render's."""
    b64 = os.environ.get("GARMIN_TOKENS_B64")
    if not b64 or TOKEN_DIR.exists():
        return
    try:
        raw = base64.b64decode(b64)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(TOKEN_DIR)
    except Exception as e:
        print(f"Could not unpack GARMIN_TOKENS_B64: {e}")


class GarminClient:
    def __init__(self):
        email = os.environ["GARMIN_EMAIL"]
        password = os.environ["GARMIN_PASSWORD"]
        _ensure_tokens_on_disk()
        self.api = garminconnect.Garmin(email, password)
        # Passing a tokenstore path makes login() try the saved session
        # first (works from any IP, including Render's) and only falls
        # back to a fresh password login if there's no valid saved session.
        self.api.login(str(TOKEN_DIR))

    def today(self) -> str:
        return dt.date.today().isoformat()

    def readiness(self, date: str | None = None) -> dict:
        date = date or self.today()
        try:
            return self.api.get_training_readiness(date) or {}
        except Exception as e:
            return {"error": str(e)}

    def stats(self, date: str | None = None) -> dict:
        """Daily stats — resting HR, average stress, steps. Garmin exposes
        these separately from sleep/readiness/HRV, so they need their own call."""
        date = date or self.today()
        try:
            return self.api.get_stats(date) or {}
        except Exception as e:
            return {"error": str(e)}

    def sleep(self, date: str | None = None) -> dict:
        date = date or self.today()
        try:
            return self.api.get_sleep_data(date) or {}
        except Exception as e:
            return {"error": str(e)}

    def hrv(self, date: str | None = None) -> dict:
        date = date or self.today()
        try:
            return self.api.get_hrv_data(date) or {}
        except Exception as e:
            return {"error": str(e)}

    def training_status(self, date: str | None = None) -> dict:
        date = date or self.today()
        try:
            return self.api.get_training_status(date) or {}
        except Exception as e:
            return {"error": str(e)}

    def body_battery(self, date: str | None = None) -> dict:
        date = date or self.today()
        try:
            return self.api.get_body_battery(date, date) or {}
        except Exception as e:
            return {"error": str(e)}

    def race_predictions(self) -> dict:
        try:
            return self.api.get_race_predictions() or {}
        except Exception as e:
            return {"error": str(e)}

    def recent_activities(self, limit: int = 5) -> list:
        try:
            return self.api.get_activities(0, limit) or []
        except Exception as e:
            return [{"error": str(e)}]

    # ---- Range helpers for Overview tab (week / month / 3month trends) ----

    def _range_dates(self, span: str) -> tuple[str, str]:
        end = dt.date.today()
        days = {"week": 7, "month": 30, "3month": 90}.get(span, 7)
        start = end - dt.timedelta(days=days - 1)
        return start.isoformat(), end.isoformat()

    def readiness_trend(self, span: str = "week") -> list[dict]:
        start, end = self._range_dates(span)
        out = []
        d = dt.date.fromisoformat(start)
        while d.isoformat() <= end:
            r = self.readiness(d.isoformat())
            out.append({"date": d.isoformat(), "score": r.get("score") if isinstance(r, dict) else None})
            d += dt.timedelta(days=1)
        return out

    def sleep_trend(self, span: str = "week") -> list[dict]:
        start, end = self._range_dates(span)
        out = []
        d = dt.date.fromisoformat(start)
        while d.isoformat() <= end:
            s = self.sleep(d.isoformat())
            score = None
            hours = None
            if isinstance(s, dict):
                daily = s.get("dailySleepDTO", {}) or {}
                score = (daily.get("sleepScores", {}) or {}).get("overall", {}).get("value")
                secs = daily.get("sleepTimeSeconds")
                hours = round(secs / 3600, 2) if secs else None
            out.append({"date": d.isoformat(), "score": score, "hours": hours})
            d += dt.timedelta(days=1)
        return out

    def hrv_trend(self, span: str = "week") -> list[dict]:
        try:
            start, end = self._range_dates(span)
            return self.api.get_hrv_trend(start, end) or []
        except Exception as e:
            return [{"error": str(e)}]

    def training_load_trend(self, span: str = "week") -> list[dict]:
        try:
            start, end = self._range_dates(span)
            return self.api.get_training_load_trend(start, end) or []
        except Exception as e:
            return [{"error": str(e)}]

    # ---- Training tab: weekly calendar, summary, HR zones, volume ----

    def activities_in_range(self, start: str, end: str) -> list[dict]:
        try:
            return self.api.get_activities_by_date(start, end) or []
        except Exception as e:
            return [{"error": str(e)}]

    def week_bounds(self, offset_weeks: int = 0) -> tuple[str, str]:
        today = dt.date.today()
        monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=offset_weeks)
        sunday = monday + dt.timedelta(days=6)
        return monday.isoformat(), sunday.isoformat()

    def weekly_hr_zones(self, activities: list[dict]) -> dict:
        """Aggregate seconds-in-zone across a list of activities, best-effort."""
        totals = {f"zone{i}": 0 for i in range(1, 6)}
        for act in activities:
            act_id = act.get("activityId")
            if not act_id:
                continue
            try:
                zones = self.api.get_activity_hr_in_timezones(act_id) or []
                for z in zones:
                    idx = z.get("zoneNumber")
                    secs = z.get("secsInZone", 0)
                    if idx and 1 <= idx <= 5:
                        totals[f"zone{idx}"] += secs
            except Exception:
                continue
        return totals

    def weekly_intensity_minutes(self) -> dict:
        try:
            return self.api.get_weekly_intensity_minutes() or {}
        except Exception as e:
            return {"error": str(e)}

    def monthly_volume(self, weeks: int = 10) -> list[dict]:
        """Distance per week, broken down by discipline, for the last N weeks."""
        out = []
        for i in range(weeks - 1, -1, -1):
            start, end = self.week_bounds(offset_weeks=-i)
            acts = self.activities_in_range(start, end)
            valid = [a for a in acts if isinstance(a, dict)]
            dist = sum(a.get("distance") or 0 for a in valid)
            dur = sum(a.get("duration") or 0 for a in valid)

            def dist_for(keyword: str) -> float:
                return sum(
                    a.get("distance") or 0
                    for a in valid
                    if keyword in ((a.get("activityType") or {}).get("typeKey") or "")
                )

            bike_km = round(dist_for("cycling") / 1000, 1)
            run_km = round(dist_for("running") / 1000, 1)
            swim_km = round(dist_for("swimming") / 1000, 1)

            out.append({
                "week_start": start,
                "distance_km": round(dist / 1000, 1),
                "duration_min": round(dur / 60),
                "sessions": len(valid),
                "bike_km": bike_km,
                "run_km": run_km,
                "swim_km": swim_km,
            })
        return out

    def snapshot(self) -> dict:
        """Everything the dashboard needs in one call."""
        return {
            "date": self.today(),
            "readiness": self.readiness(),
            "sleep": self.sleep(),
            "hrv": self.hrv(),
            "training_status": self.training_status(),
            "body_battery": self.body_battery(),
            "race_predictions": self.race_predictions(),
            "recent_activities": self.recent_activities(),
        }


@lru_cache
def get_client() -> GarminClient:
    # Cached so we only log in once per process, not once per request.
    return GarminClient()
