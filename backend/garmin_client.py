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
    """Unpack GARMIN_TOKENS_B64 (from generate_garmin_tokens.py, run on a
    machine Garmin doesn't block) to disk so login() resumes that session
    instead of a fresh password login — which Cloudflare 403s from datacenter
    IPs like Render's. Re-unpacks whenever the token files are missing, not
    just when the dir is absent."""
    b64 = os.environ.get("GARMIN_TOKENS_B64")
    if not b64:
        print("GARMIN_TOKENS_B64 not set — will attempt a fresh login (blocked on Render).")
        return
    if (TOKEN_DIR / "oauth1_token.json").exists():
        return
    try:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode("".join(b64.split()))  # tolerate wrapped/space-padded env values
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(TOKEN_DIR)
        got = sorted(p.name for p in TOKEN_DIR.iterdir())
        print(f"GARMIN_TOKENS_B64 unpacked to {TOKEN_DIR}: {got}")
    except Exception as e:
        print(f"Could not unpack GARMIN_TOKENS_B64: {e}")


class GarminClient:
    def __init__(self):
        _ensure_tokens_on_disk()
        email = os.environ.get("GARMIN_EMAIL")
        password = os.environ.get("GARMIN_PASSWORD")
        self.api = garminconnect.Garmin(email, password)
        have_tokens = (TOKEN_DIR / "oauth1_token.json").exists()
        try:
            # login(tokenstore) resumes the saved session in this garminconnect
            # version; it does not fall back to a password login.
            self.api.login(str(TOKEN_DIR))
        except Exception as e:
            if have_tokens:
                # Last-ditch: load the session straight through garth.
                print(f"login(tokenstore) failed ({e}) — trying garth.load()")
                self.api.garth.load(str(TOKEN_DIR))
            else:
                raise

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
        """Nightly HRV average. Built day-by-day from get_hrv_data — this
        library version has no bulk get_hrv_trend endpoint."""
        start, end = self._range_dates(span)
        out = []
        d = dt.date.fromisoformat(start)
        last = dt.date.fromisoformat(end)
        while d <= last:
            val = None
            try:
                h = self.api.get_hrv_data(d.isoformat()) or {}
                summ = h.get("hrvSummary") or {}
                val = summ.get("lastNightAvg") or summ.get("weeklyAvg")
            except Exception:
                pass
            out.append({"date": d.isoformat(), "value": val})
            d += dt.timedelta(days=1)
        return out

    @staticmethod
    def _activity_load(a: dict) -> float:
        """TRIMP-ish daily load: minutes scaled by how hard the average HR was.
        Used because this library version has no get_training_load_trend."""
        mins = (a.get("duration") or a.get("movingDuration") or 0) / 60.0
        hr = a.get("averageHR") or 0
        if mins <= 0:
            return 0.0
        # ~1.0x at easy aerobic (130), ~2.5x at threshold+ (170)
        factor = 1.0 if not hr else max(0.5, min(3.0, ((hr - 100) / 30.0)))
        return round(mins * factor, 1)

    def training_load_trend(self, span: str = "week") -> list[dict]:
        """Daily training load derived from activities."""
        start, end = self._range_dates(span)
        acts = [a for a in self.activities_in_range(start, end) if isinstance(a, dict)]
        by_day: dict = {}
        for a in acts:
            day = (a.get("startTimeLocal") or "")[:10]
            if day:
                by_day[day] = by_day.get(day, 0.0) + self._activity_load(a)
        out = []
        d = dt.date.fromisoformat(start)
        last = dt.date.fromisoformat(end)
        while d <= last:
            out.append({"date": d.isoformat(), "value": round(by_day.get(d.isoformat(), 0.0), 1)})
            d += dt.timedelta(days=1)
        return out

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
        """One VO2 max reading per week, sampled at the FRESHEST day of each
        week (its end, or today for the current week) and walking back a few
        days if Garmin didn't stamp a value that day. Sampling Mondays made the
        newest point stale by up to a week."""
        today = dt.date.today()

        def r1(v):
            return round(float(v), 1) if isinstance(v, (int, float)) else None

        out = []
        for i in range(weeks - 1, -1, -1):
            monday, sunday = self.week_bounds(offset_weeks=-i)
            probe = min(dt.date.fromisoformat(sunday), today)
            floor = dt.date.fromisoformat(monday)
            run_v = cyc_v = None
            hit_date = None
            while probe >= floor:
                try:
                    run_v, cyc_v = self._dig_vo2(self.api.get_max_metrics(probe.isoformat()))
                except Exception:
                    run_v = cyc_v = None
                if run_v or cyc_v:
                    hit_date = probe.isoformat()
                    break
                probe -= dt.timedelta(days=1)
            out.append({"week_start": monday, "date": hit_date or monday,
                        "running": r1(run_v), "cycling": r1(cyc_v)})
        return out

    def vo2max_daily(self, days: int = 30) -> list[dict]:
        """Every day Garmin actually stamped a running VO2 max in the window
        (Garmin only writes one every 1-3 days, so gaps are normal)."""
        today = dt.date.today()
        out = []
        for i in range(days - 1, -1, -1):
            d = (today - dt.timedelta(days=i)).isoformat()
            try:
                run_v, cyc_v = self._dig_vo2(self.api.get_max_metrics(d))
            except Exception:
                run_v = cyc_v = None
            if isinstance(run_v, (int, float)) or isinstance(cyc_v, (int, float)):
                out.append({
                    "date": d,
                    "running": round(float(run_v), 1) if isinstance(run_v, (int, float)) else None,
                    "cycling": round(float(cyc_v), 1) if isinstance(cyc_v, (int, float)) else None,
                })
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
            kg, when = None, mon
            try:
                wi = self.api.get_weigh_ins(mon, sun) or {}
                allw = wi.get("dailyWeightSummaries") or wi.get("dateWeightList") or []
                if isinstance(allw, list) and allw:
                    last = allw[-1]
                    grams = last.get("weight") or (last.get("latestWeight") or {}).get("weight")
                    if grams:
                        kg = round(grams / 1000.0, 1)
                        when = (last.get("summaryDate") or last.get("calendarDate") or mon)[:10]
            except Exception:
                pass
            out.append({"week_start": mon, "date": when, "kg": kg})
        return out

    def resting_hr_trend(self, weeks: int = 12) -> list[dict]:
        """Weekly resting HR, sampled at the freshest day of each week."""
        today = dt.date.today()
        out = []
        for i in range(weeks - 1, -1, -1):
            mon, sun = self.week_bounds(offset_weeks=-i)
            probe = min(dt.date.fromisoformat(sun), today)
            floor = dt.date.fromisoformat(mon)
            rhr, when = None, mon
            while probe >= floor:
                try:
                    d = self.api.get_rhr_day(probe.isoformat()) or {}
                    metrics = (d.get("allMetrics") or {}).get("metricsMap") or {}
                    arr = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
                    if arr and isinstance(arr, list) and arr[0].get("value"):
                        rhr = arr[0]["value"]
                        when = probe.isoformat()
                        break
                except Exception:
                    pass
                probe -= dt.timedelta(days=1)
            out.append({"week_start": mon, "date": when, "bpm": rhr})
        return out

    def sleep_history(self, nights: int = 14) -> list[dict]:
        """Per-night sleep: score, stage minutes, bed/wake clock times, RHR."""
        out = []
        for i in range(nights - 1, -1, -1):
            day = (dt.date.today() - dt.timedelta(days=i)).isoformat()
            rec = {"date": day, "score": None, "total_min": None, "deep_min": None,
                   "rem_min": None, "light_min": None, "awake_min": None,
                   "bedtime": None, "waketime": None, "resting_hr": None}
            try:
                s = self.api.get_sleep_data(day) or {}
                d = s.get("dailySleepDTO", {}) or {}
                def m(sec):
                    return round(sec / 60) if isinstance(sec, (int, float)) else None
                rec["total_min"] = m(d.get("sleepTimeSeconds"))
                rec["deep_min"] = m(d.get("deepSleepSeconds"))
                rec["rem_min"] = m(d.get("remSleepSeconds"))
                rec["light_min"] = m(d.get("lightSleepSeconds"))
                rec["awake_min"] = m(d.get("awakeSleepSeconds"))
                rec["score"] = (d.get("sleepScores", {}) or {}).get("overall", {}).get("value")
                rec["resting_hr"] = s.get("restingHeartRate") or d.get("restingHeartRate")
                for k_src, k_dst in (("sleepStartTimestampLocal", "bedtime"), ("sleepEndTimestampLocal", "waketime")):
                    ts = d.get(k_src)
                    if isinstance(ts, (int, float)):
                        rec[k_dst] = dt.datetime.fromtimestamp(ts / 1000).strftime("%H:%M")
                    elif isinstance(ts, str) and "T" in ts:
                        rec[k_dst] = ts.split("T")[1][:5]
            except Exception:
                pass
            out.append(rec)
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
    if os.environ.get("GARMIN_FIXTURE_MODE"):
        # Serve canned numbers from fixtures.json — no Garmin requests at all.
        from fixtures import FixtureClient
        return FixtureClient()
    return GarminClient()
