"""
Fixture mode — serve canned numbers instead of calling Garmin.

Turn on with env var GARMIN_FIXTURE_MODE=1. The backend then reads
fixtures.json (copy fixtures.example.json and fill in your numbers) and
FixtureClient synthesises 12 weeks of plausible trends from those anchors,
so every tab (Fitness, Sleep, adaptive plan, Garmin push) works end-to-end
without a single Garmin request.

Only a handful of leaf methods are overridden; the aggregators in
GarminClient (snapshot, monthly_volume, *_trend built on them) inherit and
keep working against the fixture data.
"""
from __future__ import annotations

import json
import math
import datetime as dt
from pathlib import Path

from garmin_client import GarminClient

_FILE = Path(__file__).parent / "fixtures.json"

_DEFAULTS = {
    "vo2_running": 40.0, "vo2_cycling": 43.0,
    "resting_hr": 52, "weight_kg": 74.0,
    "hrv_last_night": 62, "hrv_baseline_low": 55, "hrv_baseline_high": 85,
    "readiness": 68, "sleep_score": 74,
    "training_status": "Productive", "acwr": 1.10, "ftp": 240,
    "endurance_score": 480, "hill_score": 55,
    "heat_accl_pct": 60, "altitude_accl_m": 300,
    "pr_5k_sec": 1230, "pr_10k_sec": 2600, "pr_half_sec": 5700, "pr_marathon_sec": None,
    "weekly_bike_km": 130.0, "weekly_run_km": 28.0,
    "recent_5k_run": {"distance_m": 5000, "moving_sec": 1250, "avg_hr": 172},
    "sleep_avg_min": 430, "sleep_deep_min": 65, "sleep_rem_min": 95, "sleep_light_min": 245,
    "sleep_awake_min": 25, "bedtime": "23:20", "waketime": "06:40",
    # trend directions per 12 weeks (small): +improves
    "vo2_gain_12wk": 1.2, "rhr_drop_12wk": 2, "weight_drop_12wk": 1.0,
}


def _load() -> dict:
    data = dict(_DEFAULTS)
    try:
        data.update(json.loads(_FILE.read_text()))
    except Exception:
        pass
    return data


class FixtureClient(GarminClient):
    def __init__(self):
        self.f = _load()
        self.api = None  # never touched

    # ---- daily leaves ----
    def readiness(self, date=None):
        return {"score": self.f["readiness"], "date": date or self.today()}

    def stats(self, date=None):
        return {"restingHeartRate": self.f["resting_hr"], "date": date or self.today()}

    def sleep(self, date=None):
        f = self.f
        return {"dailySleepDTO": {
            "sleepTimeSeconds": f["sleep_avg_min"] * 60,
            "deepSleepSeconds": f["sleep_deep_min"] * 60,
            "remSleepSeconds": f["sleep_rem_min"] * 60,
            "lightSleepSeconds": f["sleep_light_min"] * 60,
            "awakeSleepSeconds": f["sleep_awake_min"] * 60,
            "sleepScores": {"overall": {"value": f["sleep_score"]}},
        }, "restingHeartRate": f["resting_hr"]}

    def hrv(self, date=None):
        f = self.f
        return {"lastNightAvg": f["hrv_last_night"],
                "baseline": {"lowUpper": f["hrv_baseline_low"], "balancedHigh": f["hrv_baseline_high"]}}

    def training_status(self, date=None):
        f = self.f
        return {
            "functionalThresholdPower": f["ftp"],
            "mostRecentVO2Max": {
                "generic": {"vo2MaxValue": f["vo2_running"]},
                "cycling": {"vo2MaxValue": f["vo2_cycling"]},
            },
            "mostRecentTrainingStatus": {"latestTrainingStatusData": {"dev": {
                "trainingStatus": _status_code(f["training_status"]),
                "acuteChronicWorkloadRatio": f["acwr"],
            }}},
            "heatAltitudeAcclimation": {
                "heatAcclimationPercentage": f["heat_accl_pct"],
                "altitudeAcclimation": f["altitude_accl_m"],
                "heatTrend": "STABLE", "altitudeTrend": "STABLE",
            },
        }

    def body_battery(self, date=None):
        return {"charged": 70}

    def race_predictions(self):
        f = self.f
        return [{"calendarDate": self.today(),
                 "raceTime5K": f["pr_5k_sec"], "raceTime10K": f["pr_10k_sec"],
                 "raceTimeHalfMarathon": f["pr_half_sec"], "raceTimeMarathon": f["pr_marathon_sec"]}]

    def recent_activities(self, limit=5):
        return self._synth_activities(self.today(), 5)

    # ---- ranged leaves ----
    def hrv_trend(self, span="week"):
        start, end = self._range_dates(span)
        return _daily(start, end, lambda i, n: {
            "date": _iso(start, i),
            "weeklyAvg": round(self.f["hrv_last_night"] + 4 * math.sin(i / 4) + i * 0.05),
        })

    def training_load_trend(self, span="week"):
        start, end = self._range_dates(span)
        base = (self.f["weekly_bike_km"] + self.f["weekly_run_km"]) * 4
        return _daily(start, end, lambda i, n: {
            "date": _iso(start, i),
            "value": round(base + 60 * math.sin(i / 6) + i * 1.5),
        })

    def activities_in_range(self, start, end):
        d0 = dt.date.fromisoformat(start)
        d1 = dt.date.fromisoformat(end)
        days = (d1 - d0).days + 1
        out = []
        for i in range(days):
            out += self._synth_activities(_iso(start, i), None, day_index=(d0 + dt.timedelta(days=i)).weekday())
        return out

    def weekly_hr_zones(self, activities):
        return {f"zone{i}": [0, 900, 2400, 1500, 600, 200][i] for i in range(1, 6)}

    def weekly_intensity_minutes(self):
        return {"moderateMinutes": 180, "vigorousMinutes": 90}

    def vo2max_current(self, date=None):
        return {"date": date or self.today(),
                "running": self.f["vo2_running"], "cycling": self.f["vo2_cycling"]}

    def vo2max_trend(self, weeks=10):
        gain = self.f["vo2_gain_12wk"]
        out = []
        for i in range(weeks - 1, -1, -1):
            mon, _ = self.week_bounds(offset_weeks=-i)
            frac = (weeks - 1 - i) / max(1, weeks - 1)
            out.append({"week_start": mon,
                        "running": round(self.f["vo2_running"] - gain * (1 - frac), 1),
                        "cycling": round(self.f["vo2_cycling"] - gain * 0.7 * (1 - frac), 1)})
        return out

    def personal_records(self):
        f = self.f
        return {k: v for k, v in {
            "run_5k": f["pr_5k_sec"], "run_10k": f["pr_10k_sec"],
            "run_half": f["pr_half_sec"], "run_marathon": f["pr_marathon_sec"],
        }.items() if v}

    def endurance_score(self):
        base = self.f["endurance_score"]
        return {"enduranceScoreDTO": {"overallScore": base, "groupList": [
            {"week_start": self.week_bounds(offset_weeks=-i)[0], "overallScore": round(base - i * 6)}
            for i in range(11, -1, -1)]}}

    def hill_score(self):
        return {"hillScoreDTO": {"overallScore": self.f["hill_score"]}}

    def weight_trend(self, weeks=12):
        drop = self.f["weight_drop_12wk"]
        out = []
        for i in range(weeks - 1, -1, -1):
            mon, _ = self.week_bounds(offset_weeks=-i)
            frac = (weeks - 1 - i) / max(1, weeks - 1)
            out.append({"week_start": mon, "kg": round(self.f["weight_kg"] + drop * (1 - frac) + 0.2 * math.sin(i), 1)})
        return out

    def resting_hr_trend(self, weeks=12):
        drop = self.f["rhr_drop_12wk"]
        out = []
        for i in range(weeks - 1, -1, -1):
            mon, _ = self.week_bounds(offset_weeks=-i)
            frac = (weeks - 1 - i) / max(1, weeks - 1)
            out.append({"week_start": mon, "bpm": round(self.f["resting_hr"] + drop * (1 - frac))})
        return out

    def acclimatization(self):
        return {"heat_pct": self.f["heat_accl_pct"], "altitude_m": self.f["altitude_accl_m"],
                "heat_trend": "STABLE", "altitude_trend": "STABLE"}

    def sleep_history(self, nights=14):
        f = self.f
        out = []
        for i in range(nights - 1, -1, -1):
            day = (dt.date.today() - dt.timedelta(days=i)).isoformat()
            wob = math.sin(i / 2.3)
            total = round(f["sleep_avg_min"] + wob * 35)
            out.append({
                "date": day,
                "score": round(f["sleep_score"] + wob * 8),
                "total_min": total, "deep_min": round(f["sleep_deep_min"] + wob * 8),
                "rem_min": round(f["sleep_rem_min"] + wob * 10),
                "light_min": round(f["sleep_light_min"] + wob * 12),
                "awake_min": max(5, round(f["sleep_awake_min"] - wob * 6)),
                "bedtime": f["bedtime"], "waketime": f["waketime"],
                "resting_hr": f["resting_hr"],
            })
        return out

    # ---- helpers ----
    def _synth_activities(self, date_iso, limit, day_index=None):
        """A believable run + ride pattern for a given date."""
        d = dt.date.fromisoformat(date_iso)
        wd = day_index if day_index is not None else d.weekday()
        acts = []
        wk_run = self.f["weekly_run_km"] * 1000
        wk_bike = self.f["weekly_bike_km"] * 1000
        r = self.f["recent_5k_run"]
        if wd in (0, 3):  # Mon/Thu run
            dist = wk_run * (0.5 if wd == 0 else 0.35)
            acts.append(_act(f"{date_iso}r", "running", date_iso, dist,
                             int(dist / 1000 * (r["moving_sec"] / 5)), r["avg_hr"] - (0 if wd == 0 else 25)))
        if wd in (1, 2, 6):  # Tue/Wed/Sun ride
            dist = wk_bike * (0.18 if wd == 1 else 0.30 if wd == 2 else 0.52)
            acts.append(_act(f"{date_iso}b", "cycling", date_iso, dist, int(dist / 1000 * 110), 135))
        return acts[:limit] if limit else acts


def _status_code(label: str) -> int:
    codes = {"No status": 0, "Detraining": 1, "Unproductive": 2, "Recovery": 3,
             "Maintaining": 4, "Productive": 5, "Peaking": 6, "Overreaching": 7, "Strained": 8}
    return codes.get(label, 5)


def _iso(start: str, i: int) -> str:
    return (dt.date.fromisoformat(start) + dt.timedelta(days=i)).isoformat()


def _daily(start: str, end: str, fn):
    n = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    return [fn(i, n) for i in range(n)]


def _act(aid, type_key, date_iso, dist_m, dur_s, hr):
    return {
        "activityId": aid,
        "activityName": ("Run" if type_key == "running" else "Ride"),
        "activityType": {"typeKey": type_key},
        "startTimeLocal": f"{date_iso} 18:30:00",
        "distance": round(dist_m), "duration": dur_s, "movingDuration": dur_s,
        "calories": round(dist_m / 1000 * (60 if type_key == "running" else 25)),
        "averageHR": hr, "maxHR": hr + 15,
        "elevationGain": 40, "elevationLoss": 40,
    }
