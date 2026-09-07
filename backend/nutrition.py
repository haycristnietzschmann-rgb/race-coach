"""
Per-day energy and macro targets derived from the training plan.

The planner already knows what Monday looks like before Monday happens — this
turns that into "how much to eat, and how to portion the meal prep." It's a
forecast, not a diary: the numbers exist on Sunday when you're cooking.

Same shape as planner.py: pure functions over the plan dict, no new
dependencies, heuristics transparent enough to argue with.

Two numbers per day:
  baseline — BMR (Mifflin-St Jeor) scaled for non-training daily movement
  session  — modelled cost of that day's prescribed work plus its lift

Everything else (target, macros, meal-prep grams) comes off those.
"""

import re
import datetime as dt

# Energy cost of work, kcal per kg bodyweight per hour. MET-derived and
# deliberately conservative — the point is a number you can cook to, not lab
# accuracy. calibration_factor() corrects these against what Garmin actually
# recorded, so they converge on your physiology instead of staying textbook.
COST_PER_KG_HR = {
    ("Bike", "easy"): 6.8,
    ("Bike", "moderate"): 8.6,
    ("Bike", "hard"): 11.0,
    ("Run", "easy"): 9.5,
    ("Run", "moderate"): 11.5,
    ("Run", "hard"): 14.0,
    ("Swim", "easy"): 8.0,
    ("Swim", "moderate"): 9.8,
    ("Swim", "hard"): 12.0,
    ("Choice", "easy"): 7.0,
    ("Choice", "moderate"): 8.6,
    ("Choice", "hard"): 10.5,
}
DEFAULT_COST = 8.0

# A lift block on top of the cardio. Roughly 45 min of the named split.
LIFT_KCAL = {"Push": 190, "Pull": 190, "Legs": 260, "Full": 240}

# Typical durations when the prescription doesn't state one, by type+intensity.
FALLBACK_MIN = {
    ("Bike", "easy"): 90, ("Bike", "moderate"): 105, ("Bike", "hard"): 70,
    ("Run", "easy"): 50, ("Run", "moderate"): 55, ("Run", "hard"): 45,
    ("Swim", "easy"): 40, ("Swim", "moderate"): 45, ("Swim", "hard"): 40,
    ("Choice", "easy"): 40, ("Choice", "moderate"): 50, ("Choice", "hard"): 45,
}

# Rough moving speeds (km/h) for turning a stated distance into a duration.
SPEED_KMH = {
    ("Bike", "easy"): 27.0, ("Bike", "moderate"): 30.0, ("Bike", "hard"): 33.0,
    ("Run", "easy"): 10.5, ("Run", "moderate"): 12.0, ("Run", "hard"): 14.0,
}

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# The deficit is never generated here — it arrives as prescribed_deficit_kcal,
# a number set by whoever is managing the athlete's care. This module's job is
# to implement it faithfully and to protect the training underneath it, not to
# decide how large a cut anyone should run.
#
# What it does enforce is the session-fuel floor: whatever the deficit asks
# for, the target never drops below resting metabolism plus the full cost of
# that day's work. A cut comes out of the discretionary margin, never out of
# the workout. On a rest day almost all of it lands; on a four-hour ride day
# almost none does, which is the correct shape.
SESSION_FUEL_FLOOR_MULT = 1.2


def _num_range(text: str, pattern: str):
    """First number (or midpoint of a range) matching pattern, else None."""
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    lo = float(m.group(1).replace(",", "."))
    hi = m.group(2)
    if hi:
        return (lo + float(hi.replace(",", "."))) / 2
    return lo


def parse_duration_min(prescription: str, type_: str, intensity: str):
    """
    Minutes of work implied by a prescription string.

    Handles the three shapes the planner actually emits:
      "2-2.5 h Z2 with 2x20 min SS"   -> hours, take the midpoint
      "45-55 min Z2 + 5 strides"      -> minutes, midpoint
      "100-120 km Z2"                 -> distance, divide by assumed speed
    Falls back to a typical duration for the type when none of those parse.
    """
    text = (prescription or "").replace("–", "-").replace("—", "-")
    key = (type_, intensity)

    hours = _num_range(text, r"(\d+(?:[.,]\d+)?)\s*(?:-\s*(\d+(?:[.,]\d+)?)\s*)?h\b")
    if hours:
        return hours * 60

    mins = _num_range(text, r"(\d+(?:[.,]\d+)?)\s*(?:-\s*(\d+(?:[.,]\d+)?)\s*)?min\b")
    # Guard against "6x3 min hard" — an interval length, not session duration.
    if mins and not re.search(r"[x×]\s*\d+(?:[.,]\d+)?\s*min", text, re.I):
        return mins

    km = _num_range(text, r"(\d+(?:[.,]\d+)?)\s*(?:-\s*(\d+(?:[.,]\d+)?)\s*)?km\b")
    if km:
        speed = SPEED_KMH.get(key) or SPEED_KMH.get((type_, "easy")) or 25.0
        return km / speed * 60

    return FALLBACK_MIN.get(key, 60)


def bmr(weight_kg: float, height_cm: float, age: int, sex: str = "MALE") -> float:
    """Mifflin-St Jeor resting metabolic rate, kcal/day."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if str(sex).upper().startswith("M") else -161)


def session_kcal(session: dict, weight_kg: float, calibration: float = 1.0) -> dict:
    """Modelled cost of one planned session, split into cardio and lift."""
    type_ = session.get("type") or "Choice"
    intensity = session.get("intensity") or "easy"
    minutes = parse_duration_min(session.get("prescription"), type_, intensity)
    rate = COST_PER_KG_HR.get((type_, intensity), DEFAULT_COST)
    cardio = rate * weight_kg * (minutes / 60.0) * calibration
    lift = LIFT_KCAL.get(session.get("after_lift"), 0)
    return {
        "cardio_kcal": round(cardio),
        "lift_kcal": lift,
        "minutes": round(minutes),
        "type": type_,
        "intensity": intensity,
        "title": session.get("title"),
    }


# Non-training movement multiplier on BMR. Kept modest because the training
# itself is counted separately — folding a high activity factor over the top
# of an explicit session cost is the classic way to double-count and end up
# several hundred kcal high every single day.
NEAT_FACTOR = 1.30

# Protein is held near-flat; carbohydrate is what periodises with the work.
PROTEIN_G_PER_KG = {"fuel": 1.8, "recomp": 2.2, "deficit": 2.4}

# Extra protein on days carrying a lift block. In a deficit, protein is what
# decides whether the weight lost is fat or the muscle you went to the gym to
# build, so the resistance days get the most.
LIFT_PROTEIN_BONUS_G_PER_KG = 0.2
FAT_G_PER_KG_FLOOR = 0.8


def carb_g_per_kg(session_total: float, weight_kg: float) -> float:
    """
    Carbohydrate scaled to the day's work: rest days sit near 4 g/kg, a long
    ride pushes past 9. Driven by session cost per kg so it tracks the actual
    demand rather than a label on the day.
    """
    per_kg = session_total / weight_kg if weight_kg else 0
    if per_kg <= 2:
        return 4.0
    if per_kg >= 22:
        return 9.5
    return 4.0 + (per_kg - 2) * (5.5 / 20.0)


def day_target(session_list: list, weight_kg: float, height_cm: float, age: int,
               sex: str = "MALE", mode: str = "fuel",
               prescribed_deficit_kcal: int = 0,
               calibration: float = 1.0) -> dict:
    """
    Energy and macro target for a single day.

    mode:
      "fuel"    — match expenditure; the number rises with the training
      "recomp"  — matched overall, protein high, carbs periodised harder
      "deficit" — expenditure minus prescribed_deficit_kcal, floored so the
                  day's session always stays fully fuelled

    prescribed_deficit_kcal is supplied by the caller. Nothing here derives it.
    """
    resting = bmr(weight_kg, height_cm, age, sex)
    baseline = resting * NEAT_FACTOR
    sessions = [session_kcal(s, weight_kg, calibration) for s in session_list]
    session_total = sum(s["cardio_kcal"] + s["lift_kcal"] for s in sessions)
    tdee = baseline + session_total

    notes = []
    applied_deficit = 0
    if mode == "deficit" and prescribed_deficit_kcal > 0:
        floor = resting * SESSION_FUEL_FLOOR_MULT + session_total
        wanted = tdee - prescribed_deficit_kcal
        applied_deficit = round(tdee - max(wanted, floor))
        if applied_deficit < prescribed_deficit_kcal:
            notes.append(
                f"Cut held at {applied_deficit} kcal instead of "
                f"{prescribed_deficit_kcal} — the rest would have come out of "
                "today's session fuel."
            )

    target = tdee - applied_deficit

    has_lift = any(s.get("lift_kcal") for s in sessions)
    protein_per_kg = PROTEIN_G_PER_KG.get(mode, 1.8)
    if has_lift:
        protein_per_kg += LIFT_PROTEIN_BONUS_G_PER_KG
    protein_g = round(protein_per_kg * weight_kg)

    carb_g = round(carb_g_per_kg(session_total, weight_kg) * weight_kg)
    fat_floor = round(FAT_G_PER_KG_FLOOR * weight_kg)
    fat_g = round((target - protein_g * 4 - carb_g * 4) / 9)

    if fat_g < fat_floor:
        # Never balance the day by dropping fat below the hormonal floor —
        # take it out of carbohydrate instead, and say so.
        fat_g = fat_floor
        carb_g = max(0, round((target - protein_g * 4 - fat_g * 9) / 4))
        notes.append("Carbs reduced to hold fat at the minimum healthy intake.")

    return {
        "resting_kcal": round(resting),
        "baseline_kcal": round(baseline),
        "session_kcal": round(session_total),
        "tdee_kcal": round(tdee),
        "target_kcal": round(target),
        "deficit_applied": applied_deficit,
        "deficit_prescribed": prescribed_deficit_kcal,
        "protein_g": protein_g,
        "protein_g_per_kg": round(protein_per_kg, 2),
        "carbs_g": carb_g,
        "fat_g": fat_g,
        "has_lift": has_lift,
        "sessions": sessions,
        "notes": notes,
    }


def week_targets(plan: dict, profile: dict, mode: str = "fuel",
                 prescribed_deficit_kcal: int = 0,
                 calibration: float = 1.0) -> dict:
    """Seven days of targets from one plan_state week entry."""
    weight = profile.get("weight_kg") or 70.0
    height = profile.get("height_cm") or 178.0
    age = profile.get("age")
    sex = profile.get("sex", "MALE")
    if age is None:
        raise ValueError("age is required to compute a calorie target")

    by_day = {d: [] for d in DAYS}
    for s in plan.get("sessions", []):
        by_day.setdefault(s.get("day"), []).append(s)

    week_start = plan.get("week_start")
    monday = dt.date.fromisoformat(week_start) if week_start else None

    days = []
    for i, d in enumerate(DAYS):
        t = day_target(by_day.get(d, []), weight, height, age, sex,
                       mode, prescribed_deficit_kcal, calibration)
        t["day"] = d
        t["date"] = (monday + dt.timedelta(days=i)).isoformat() if monday else None
        days.append(t)

    return {
        "week_start": week_start,
        "mode": mode,
        "calibration": round(calibration, 3),
        "profile": {"weight_kg": weight, "height_cm": height, "age": age, "sex": sex},
        "days": days,
        "week_kcal": sum(d["target_kcal"] for d in days),
        "hardest_day": max(days, key=lambda d: d["target_kcal"])["day"],
    }


def calibration_factor(planned: list, actual: list) -> float:
    """
    Ratio of what Garmin actually recorded to what we modelled, so the cost
    table drifts toward this athlete. Clamped hard — a couple of mis-tagged
    activities shouldn't be able to swing every future target.

    planned/actual: matched lists of session kcal for the same days.
    """
    p = sum(x for x in planned if x)
    a = sum(x for x in actual if x)
    if p <= 0 or a <= 0:
        return 1.0
    return max(0.75, min(1.25, a / p))


# Energy density of body fat, kcal per kg. The classic 7700 figure — roughly
# 87% lipid at 9 kcal/g. Real loss is never pure fat, so treat any timeline
# built on it as the optimistic edge rather than a promise.
KCAL_PER_KG_FAT = 7700

# Weekly loss beyond this fraction of bodyweight starts costing lean mass, and
# costs it faster the leaner and younger the athlete. Used to flag a goal as
# too aggressive for its timeframe rather than to silently clamp it.
MAX_SAFE_LOSS_FRACTION = 0.0075


def deficit_for_goal(current_kg: float, target_kg: float, weeks: float) -> dict:
    """
    The daily deficit a weight goal actually requires.

    Deliberately inverted from how these tools usually work: rather than
    picking a deficit and predicting an outcome, this takes the outcome and
    derives the deficit. It's the honest direction — most goals need a far
    smaller cut than people assume, and seeing the real number up front
    prevents choosing an aggressive deficit that the goal never called for.
    """
    to_lose = current_kg - target_kg
    days = max(1.0, weeks * 7)
    daily = to_lose * KCAL_PER_KG_FAT / days
    weekly_rate = (to_lose / weeks) if weeks else 0
    rate_fraction = (weekly_rate / current_kg) if current_kg else 0

    verdict = "conservative"
    if rate_fraction > MAX_SAFE_LOSS_FRACTION:
        verdict = "aggressive"
    elif rate_fraction > MAX_SAFE_LOSS_FRACTION * 0.6:
        verdict = "moderate"

    return {
        "to_lose_kg": round(to_lose, 2),
        "weeks": weeks,
        "daily_deficit_kcal": round(daily),
        "weekly_loss_kg": round(weekly_rate, 3),
        "weekly_loss_pct_bw": round(rate_fraction * 100, 2),
        "verdict": verdict,
        "muscle_gain_compatible": daily <= 400,
    }


def observed_weekly_loss(weigh_ins: list) -> dict:
    """
    Actual kg/week from Garmin weigh-ins, by least squares over the series.

    A trend line rather than first-minus-last: day-to-day scale readings swing
    a kilo on hydration alone, and picking two endpoints lets one bad morning
    dictate the whole answer. weigh_ins: [{"date": iso, "kg": float}, ...]
    """
    pts = [(dt.date.fromisoformat(w["date"]).toordinal(), w["kg"])
           for w in weigh_ins if w.get("kg") and w.get("date")]
    if len(pts) < 3:
        return {"weekly_kg": None, "n": len(pts), "reason": "need 3+ weigh-ins"}

    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((p[0] - mx) ** 2 for p in pts)
    if denom == 0:
        return {"weekly_kg": None, "n": n, "reason": "all on one date"}
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom
    return {"weekly_kg": round(slope * 7, 3), "n": n,
            "span_days": pts[-1][0] - pts[0][0]}


def adapt_deficit(prescribed: int, intended_weekly_kg: float,
                  observed: dict, max_step: int = 150) -> dict:
    """
    Correct the deficit against what the scale actually did.

    The modelled cost of a session and the true one differ by more than most
    calculators admit, so a deficit chosen on paper drifts. Comparing intended
    against observed loss closes that loop. Movement is capped per adjustment
    because a fortnight of water-weight noise should nudge the target, not
    redesign it.
    """
    obs = observed.get("weekly_kg")
    if obs is None:
        return {"deficit_kcal": prescribed, "changed": 0,
                "reason": observed.get("reason", "no trend yet")}

    gap_kg = (-intended_weekly_kg) - (-obs)   # positive => losing too slowly
    correction = gap_kg * KCAL_PER_KG_FAT / 7
    step = max(-max_step, min(max_step, round(correction)))
    return {
        "deficit_kcal": max(0, prescribed + step),
        "changed": step,
        "observed_weekly_kg": obs,
        "intended_weekly_kg": intended_weekly_kg,
        "reason": ("losing slower than planned" if step > 0 else
                   "losing faster than planned" if step < 0 else "on track"),
    }


# How close an intake has to land to count as hitting the target. ±7% on
# calories is about the resolution home food logging actually has — tighter
# than that measures your kitchen scale, not your discipline.
KCAL_TOLERANCE = 0.07
MACRO_TOLERANCE = 0.15

# Planned type -> Garmin activityType substrings that satisfy it.
_TYPE_MATCH = {
    "Run": ("running", "treadmill"),
    "Bike": ("cycling", "biking", "virtual_ride"),
    "Swim": ("swimming",),
    "Choice": ("running", "cycling", "biking", "swimming", "walking"),
}


def training_adherence(plan: dict, activities: list,
                       through_day: int = None) -> dict:
    """
    Planned sessions vs what Garmin actually recorded that week.

    Matching is by type within the week rather than by day: moving Sunday's
    long ride to Saturday is a rescheduled session, not a missed one, and a
    plan the athlete assigns to days themselves should not punish that.
    """
    planned = plan.get("sessions", []) or []

    # Only score what has actually come due. A week that started this morning
    # has six sessions still ahead of it, and counting those as missed reports
    # 0% every Monday — a number that says nothing about the athlete and
    # trains them to ignore the metric.
    if through_day is not None:
        allowed = set(DAYS[:through_day + 1])
        planned = [s for s in planned if s.get("day") in allowed]
    pool = []
    for a in activities or []:
        raw = a.get("activityType")
        key = raw.get("typeKey") if isinstance(raw, dict) else (raw or "")
        pool.append(str(key or "").lower())

    completed, missing = [], []
    for s in planned:
        wanted = _TYPE_MATCH.get(s.get("type"), ())
        hit = next((i for i, k in enumerate(pool)
                    if any(w in k for w in wanted)), None)
        if hit is None:
            missing.append(s.get("title") or s.get("type"))
        else:
            completed.append(s.get("title") or s.get("type"))
            pool.pop(hit)          # one activity satisfies one session

    # Lift blocks are prescribed as an attribute of a cardio session
    # (after_lift: "Push"), not as sessions of their own, but they are real
    # planned work and the athlete is training for hypertrophy — so they are
    # scored separately against Garmin's strength_training activities rather
    # than being written off as extra.
    lifts_planned = [s.get("after_lift") for s in planned if s.get("after_lift")]
    lift_hits = 0
    for _ in lifts_planned:
        hit = next((i for i, k in enumerate(pool) if "strength" in k), None)
        if hit is not None:
            pool.pop(hit)
            lift_hits += 1

    total = len(planned)
    return {
        "planned": total,
        "scored_through": DAYS[through_day] if through_day is not None else None,
        "completed": len(completed),
        "missed": len(missing),
        "rate": round(len(completed) / total, 3) if total else None,
        "completed_titles": completed,
        "missed_titles": missing,
        "lifts_planned": len(lifts_planned),
        "lifts_completed": lift_hits,
        "lift_rate": round(lift_hits / len(lifts_planned), 3) if lifts_planned else None,
        "extra_activities": len(pool),
    }


def diet_adherence(days: list, intake_by_date: dict) -> dict:
    """
    Logged intake vs each day's target.

    Days with no logged intake are reported separately rather than scored as
    zero — an unlogged day means the tracker was not used, which is a
    different fact from a day the athlete badly overshot, and averaging the
    two together produces a number that means nothing.
    """
    scored, unlogged = [], []
    for d in days:
        actual = intake_by_date.get(d.get("date"))
        if not actual or not actual.get("kcal"):
            unlogged.append(d.get("date"))
            continue
        tgt = d["target_kcal"] or 1
        err = (actual["kcal"] - tgt) / tgt
        scored.append({
            "date": d.get("date"),
            "target_kcal": tgt,
            "actual_kcal": actual["kcal"],
            "error_pct": round(err * 100, 1),
            "on_target": abs(err) <= KCAL_TOLERANCE,
            "protein_hit": (actual.get("protein_g") or 0) >=
                           d["protein_g"] * (1 - MACRO_TOLERANCE),
        })

    n = len(scored)
    return {
        "logged_days": n,
        "unlogged_days": len(unlogged),
        "unlogged_dates": unlogged,
        "rate": round(sum(1 for s in scored if s["on_target"]) / n, 3) if n else None,
        "protein_rate": round(sum(1 for s in scored if s["protein_hit"]) / n, 3) if n else None,
        "mean_error_pct": round(sum(s["error_pct"] for s in scored) / n, 1) if n else None,
        "days": scored,
    }


# ---- Meal prep: one batch, portioned across the week ----
#
# The workflow this models is the real one: cook a single batch on Sunday,
# put it on a scale, then divide it into containers. Crucially the split is
# by cooked weight, not by raw ingredient weight — rice roughly triples and
# chicken loses about a quarter, and those shifts differ every time depending
# on heat and lid. Measuring the batch after cooking sidesteps the whole
# problem: whatever the pot weighs is the truth, and macros per 100 g cooked
# follow from it.

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def batch_totals(ingredients: list) -> dict:
    """
    Macros for a whole batch from its raw ingredients.

    ingredients: [{"name", "grams", "kcal_100g", "protein_100g",
                   "carbs_100g", "fat_100g"}, ...]
    """
    total = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0,
             "raw_grams": 0.0}
    lines = []
    for ing in ingredients:
        g = float(ing.get("grams") or 0)
        if g <= 0:
            continue
        f = g / 100.0
        line = {
            "name": ing.get("name"),
            "grams": round(g, 1),
            "kcal": round(float(ing.get("kcal_100g") or 0) * f, 1),
            "protein_g": round(float(ing.get("protein_100g") or 0) * f, 1),
            "carbs_g": round(float(ing.get("carbs_100g") or 0) * f, 1),
            "fat_g": round(float(ing.get("fat_100g") or 0) * f, 1),
        }
        lines.append(line)
        total["kcal"] += line["kcal"]
        total["protein_g"] += line["protein_g"]
        total["carbs_g"] += line["carbs_g"]
        total["fat_g"] += line["fat_g"]
        total["raw_grams"] += g

    return {"lines": lines, **{k: round(v, 1) for k, v in total.items()}}


def portion_batch(batch: dict, cooked_grams: float, days: list,
                  share_of_day: float = 1.0) -> dict:
    """
    Split one cooked batch across days in proportion to their targets.

    share_of_day is the fraction of each day's energy this batch is meant to
    cover — a prep that is lunch only should be told so, otherwise it sizes
    itself as though it were the entire day's food.

    Portions are proportional to each day's target rather than equal, which is
    the whole reason for doing this: an equal split underfeeds the long ride
    and overfeeds the rest day by the same amount.
    """
    cooked_grams = float(cooked_grams or 0)
    if cooked_grams <= 0:
        return {"error": "cooked_grams must be greater than zero"}
    if not days:
        return {"error": "no days to portion across"}

    per_100 = {
        "kcal": round(batch["kcal"] / cooked_grams * 100, 1),
        "protein_g": round(batch["protein_g"] / cooked_grams * 100, 1),
        "carbs_g": round(batch["carbs_g"] / cooked_grams * 100, 1),
        "fat_g": round(batch["fat_g"] / cooked_grams * 100, 1),
    }

    wanted = [max(0.0, d["target_kcal"] * share_of_day) for d in days]
    demand = sum(wanted)
    if demand <= 0:
        return {"error": "days have no energy target"}

    # The batch is whatever it is. Scale the requested shares to fit it, and
    # report the ratio rather than quietly pretending the batch was the right
    # size — an athlete who cooked 30% short needs to know that, not to
    # receive confident portions that leave them hungry by Thursday.
    coverage = min(1.0, cooked_grams * per_100["kcal"] / 100 / demand)

    portions = []
    for d, w in zip(days, wanted):
        grams = cooked_grams * (w / demand)
        portions.append({
            "day": d["day"],
            "date": d.get("date"),
            "grams": round(grams),
            "kcal": round(grams / 100 * per_100["kcal"]),
            "protein_g": round(grams / 100 * per_100["protein_g"]),
            "carbs_g": round(grams / 100 * per_100["carbs_g"]),
            "fat_g": round(grams / 100 * per_100["fat_g"]),
            "day_target_kcal": d["target_kcal"],
            "covers_pct": round(grams / 100 * per_100["kcal"] / d["target_kcal"] * 100),
        })

    return {
        "cooked_grams": round(cooked_grams),
        "per_100g_cooked": per_100,
        "batch": {k: batch[k] for k in ("kcal", "protein_g", "carbs_g", "fat_g")},
        "share_of_day": share_of_day,
        "coverage": round(coverage, 3),
        "shortfall_kcal": round(max(0.0, demand - batch["kcal"])),
        "portions": portions,
    }


def scale_batch_to_days(ingredients: list, days: list,
                        share_of_day: float = 1.0) -> dict:
    """
    How much raw ingredient to buy and cook for a given set of days.

    Runs the other direction from portion_batch: rather than dividing a batch
    that already exists, it scales a recipe's proportions up until the batch
    covers the week. Answers the question actually asked at the shop.
    """
    base = batch_totals(ingredients)
    if base["kcal"] <= 0:
        return {"error": "ingredients carry no energy — check the per-100g values"}

    needed = sum(d["target_kcal"] * share_of_day for d in days)
    factor = needed / base["kcal"]

    return {
        "scale_factor": round(factor, 3),
        "needed_kcal": round(needed),
        "shopping_list": [
            {"name": l["name"], "grams": round(l["grams"] * factor)}
            for l in base["lines"]
        ],
        "projected": {
            "kcal": round(base["kcal"] * factor),
            "protein_g": round(base["protein_g"] * factor),
            "carbs_g": round(base["carbs_g"] * factor),
            "fat_g": round(base["fat_g"] * factor),
        },
        "note": ("Weigh the batch after cooking and send that number back — "
                 "portions are computed from cooked weight, not this estimate."),
    }
