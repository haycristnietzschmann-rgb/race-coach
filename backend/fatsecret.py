"""
FatSecret Platform API client — OAuth 1.0, HMAC-SHA1.

Chosen because it is the one mainstream food tracker that still gives an
individual developer write access to a user's diary. Cronometer's API is
enterprise-only, MyFitnessPal has been partner-only for years, and Yazio has
no public API at all.

Two request styles:

  two-legged   consumer key + secret only. Enough for the food database
               (foods.search, food.get) and for profile.create.
  three-legged adds a profile's oauth_token/secret. Required for anything
               touching a diary — food_entries.get, food_entry.create.

The profile is created and owned by this backend, so there is no interactive
OAuth dance: profile.create returns a token pair we store and reuse.

Note FatSecret dates are "days since 1970-01-01", not ISO strings.
"""

from __future__ import annotations

import os
import time
import hmac
import base64
import hashlib
import secrets
import datetime as dt
from urllib.parse import quote, urlencode

import requests

BASE_URL = "https://platform.fatsecret.com/rest/server.api"
EPOCH = dt.date(1970, 1, 1)


class FatSecretError(RuntimeError):
    pass


def _quote(s) -> str:
    """RFC 5849 percent-encoding: unreserved set is ALPHA / DIGIT / - . _ ~"""
    return quote(str(s), safe="-._~")


def date_to_days(d) -> int:
    """FatSecret counts diary dates in whole days since the Unix epoch."""
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    return (d - EPOCH).days


def days_to_date(n: int) -> str:
    return (EPOCH + dt.timedelta(days=int(n))).isoformat()


def _sign(method: str, url: str, params: dict, secret: str, token_secret: str = "") -> str:
    """
    OAuth 1.0 signature base string, then HMAC-SHA1.

    The parameters are percent-encoded, sorted as encoded key/value pairs, and
    joined — then the whole joined string is encoded again as one component.
    Getting the double-encoding wrong is the usual reason a signature fails,
    so it is spelled out rather than folded into urlencode().
    """
    norm = "&".join(
        f"{_quote(k)}={_quote(v)}"
        for k, v in sorted((str(k), str(v)) for k, v in params.items())
    )
    base = f"{method.upper()}&{_quote(url)}&{_quote(norm)}"
    key = f"{_quote(secret)}&{_quote(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def call(api_method: str, token: str = None, token_secret: str = None,
         timeout: int = 20, **params) -> dict:
    """
    One signed call. Extra keyword arguments become API parameters.

    Raises FatSecretError on a transport failure or an API-level error object,
    which FatSecret returns with HTTP 200 — so the body has to be inspected
    rather than trusting the status code.
    """
    key = os.environ.get("FATSECRET_KEY")
    secret = os.environ.get("FATSECRET_SECRET")
    if not key or not secret:
        raise FatSecretError(
            "FATSECRET_KEY / FATSECRET_SECRET are not set — add them to "
            "backend/.env and to the Render environment."
        )

    oauth = {
        "oauth_consumer_key": key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        oauth["oauth_token"] = token

    full = {"method": api_method, "format": "json", **params, **oauth}
    full["oauth_signature"] = _sign("GET", BASE_URL, full, secret, token_secret or "")

    try:
        r = requests.get(BASE_URL, params=full, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise FatSecretError(f"FatSecret request failed: {e}") from e
    except ValueError as e:
        raise FatSecretError(f"FatSecret returned non-JSON: {r.text[:200]}") from e

    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        code = err.get("code")
        msg = err.get("message", "unknown error")
        if str(code) == "21":
            msg += (" — this usually means the calling IP is not on the app's "
                    "allowed list in the FatSecret dashboard.")
        raise FatSecretError(f"FatSecret error {code}: {msg}")
    return data


# ---- Database (two-legged) ----

def search_foods(query: str, page: int = 0, max_results: int = 20) -> dict:
    return call("foods.search", search_expression=query,
                page_number=page, max_results=max_results)


def get_food(food_id: str) -> dict:
    return call("food.get.v2", food_id=food_id)


# ---- Profile ownership ----

def create_profile(user_id: str = None) -> dict:
    """
    Mint a profile this backend owns. Returns {auth_token, auth_secret},
    which must be stored — FatSecret will not show them again.
    """
    params = {"user_id": user_id} if user_id else {}
    data = call("profile.create", **params)
    prof = data.get("profile") or {}
    if not prof.get("auth_token"):
        raise FatSecretError(f"profile.create returned no token: {data}")
    return {"token": prof["auth_token"], "secret": prof["auth_secret"]}


# ---- Diary (three-legged) ----

def get_entries(token: str, secret: str, date) -> list:
    """Every food entry logged on one date. [] when nothing was logged."""
    data = call("food_entries.get.v2", token=token, token_secret=secret,
                date=date_to_days(date))
    entries = (data.get("food_entries") or {}).get("food_entry") or []
    return entries if isinstance(entries, list) else [entries]


def day_totals(token: str, secret: str, date) -> dict:
    """Collapse a day's entries into the four numbers the dashboard scores."""
    total = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for e in get_entries(token, secret, date):
        total["kcal"] += float(e.get("calories") or 0)
        total["protein_g"] += float(e.get("protein") or 0)
        total["carbs_g"] += float(e.get("carbohydrate") or 0)
        total["fat_g"] += float(e.get("fat") or 0)
    return {k: round(v, 1) for k, v in total.items()}
