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

    # ---- VO2 max / fitness metrics (Fitness tab + adaptive planner) ----

    @staticmethod
    def _dig_vo2(obj) -> tuple:
        """Pull (running, cycling) VO2 max out of any of Garmin's shapes."""
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        if not isinstance(obj, dict):
            return None, None
        # get_max_metrics: {generic:{vo2MaxValue}, cycling:{vo2MaxValue}}
        # training_status.mostRecentVO2Max: same shape nested one deeper
        gen = obj.get("generic") or obj.get("running") or {}
        cyc = obj.get("cycling") or {}
        run_v = gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue") or obj.get("vo2MaxValue")
        cyc_v = cyc.get("vo2MaxPreciseValue") or cyc.get("vo2MaxValue")
        return run_v, cyc_v

    def vo2max_current(self, date: str | None = None) -> dict:
        """Current VO2 max. Prefers training_status.mostRecentVO2Max (what the
        Garmin app shows as 'today'), falls back to get_max_metrics."""
        date = date or self.today()
        run_v = cyc_v = None
        try:
            ts = self.training_status(date) or {}
            run_v, cyc_v = self._dig_vo2(ts.get("mostRecentVO2Max"))
        except Exception:
            pass
        if not run_v and not cyc_v:
            try:
                run_v, cyc_v = self._dig_vo2(self.api.get_max_metrics(date))
            except Exception as e:
                return {"error": str(e)}
        def r1(v):
            return round(float(v), 1) if isinstance(v, (int, float)) else None
        return {"date": date, "running": r1(run_v), "cycling": r1(cyc_v)}

    def vo2max_trend(self, weeks: int = 10) -> list[dict]:
        """One VO2 max reading per week (sampled on each week's Monday). VO2 max
        genuinely moves slowly, so repeated values across weeks are expected."""
        out = []
        for i in range(weeks - 1, -1, -1):
            monday, _ = self.week_bounds(offset_weeks=-i)
            run_v = cyc_v = None
            try:
                run_v, cyc_v = self._dig_vo2(self.api.get_max_metrics(monday))
            except Exception:
                pass
            def r1(v):
                return round(float(v), 1) if isinstance(v, (int, float)) else None
            out.append({"week_start": monday, "running": r1(run_v), "cycling": r1(cyc_v)})
        return out

    def personal_records(self) -> dict:
        """Personal records keyed by a readable label, seconds for time PRs."""
        try:
            raw = self.api.get_personal_record() or []
        except Exception as e:
            return {"error": str(e)}
        # typeId map (Garmin running/cycling PR codes)
        labels = {1: "run_1mi", 2: "run_1km", 3: "run_5k", 4: "run_10k",
                  5: "run_half", 6: "run_marathon", 7: "longest_run_m",
                  8: "longest_ride_m", 12: "longest_ride_m"}
        out = {}
        for r in raw if isinstance(raw, list) else []:
            if not isinstance(r, dict):
                continue
            key = labels.get(r.get("typeId"))
            val = r.get("value")
            if key and isinstance(val, (int, float)):
                out[key] = val
        return out

    def endurance_score(self) -> dict:
        try:
            end = (self.week_bounds()[1])
            start = self.week_bounds(offset_weeks=-11)[0]
            return self.api.get_endurance_score(start, end) or {}
        except Exception as e:
            return {"error": str(e)}

    def hill_score(self) -> dict:
        try:
            end = self.week_bounds()[1]
            start = self.week_bounds(offset_weeks=-11)[0]
            return self.api.get_hill_score(start, end) or {}
        except Exception as e:
            return {"error": str(e)}

    def weight_trend(self, weeks: int = 12) -> list[dict]:
        """Weekly latest weight (kg). Best-effort from weigh-ins / body comp."""
        out = []
        for i in range(weeks - 1, -1, -1):
            mon, sun = self.week_bounds(offset_weeks=-i)
            kg = None
            try:
                wi = self.api.get_weigh_ins(mon, sun) or {}
                allw = wi.get("dailyWeightSummaries") or wi.get("dateWeightList") or []
                if isinstance(allw, list) and allw:
                    last = allw[-1]
                    grams = last.get("weight") or (last.get("latestWeight") or {}).get("weight")
                    if grams:
                        kg = round(grams / 1000.0, 1)
            except Exception:
                pass
            out.append({"week_start": mon, "kg": kg})
        return out

    def resting_hr_trend(self, weeks: int = 12) -> list[dict]:
        out = []
        for i in range(weeks - 1, -1, -1):
            mon, _ = self.week_bounds(offset_weeks=-i)
            rhr = None
            try:
                d = self.api.get_rhr_day(mon) or {}
                metrics = d.get("allMetrics", {}).get("metricsMap", {}) if isinstance(d, dict) else {}
                arr = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
                if arr and isinstance(arr, list):
                    rhr = arr[0].get("value")
            except Exception:
                pass
            out.append({"week_start": mon, "bpm": rhr})
        return out

    def acclimatization(self) -> dict:
        """Heat / altitude acclimation — lives inside the training status blob."""
        try:
            ts = self.training_status() or {}
            hac = ts.get("heatAltitudeAcclimation") or {}
            if not hac:
                # sometimes under the most-recent training status sub-object
                mr = ts.get("mostRecentTrainingStatus", {}) or {}
                latest = (mr.get("latestTrainingStatusData") or {})
                for v in latest.values():
                    if isinstance(v, dict) and ("heatAcclimationPercentage" in v or "heatTrend" in v):
                        hac = v
                        break
            return {
                "heat_pct": hac.get("heatAcclimationPercentage"),
                "altitude_m": hac.get("altitudeAcclimation"),
                "heat_trend": hac.get("heatTrend"),
                "altitude_trend": hac.get("altitudeTrend"),
                "current_altitude_m": hac.get("currentAltitude"),
            }
        except Exception as e:
            return {"error": str(e)}

    def activities_last_weeks(self, weeks: int = 12) -> list[dict]:
        """Flat list of activities across the last N ISO weeks (best-effort)."""
        start, _ = self.week_bounds(offset_weeks=-(weeks - 1))
        _, end = self.week_bounds(offset_weeks=0)
        acts = self.activities_in_range(start, end)
        return [a for a in acts if isinstance(a, dict) and a.get("activityId")]

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
