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
from urllib.parse import quote, parse_qsl

import requests

BASE_URL = "https://platform.fatsecret.com/rest/server.api"
REQUEST_TOKEN_URL = "https://authentication.fatsecret.com/oauth/request_token"
AUTHORIZE_URL = "https://authentication.fatsecret.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://authentication.fatsecret.com/oauth/access_token"
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


# ---- Three-legged OAuth: link the athlete's own fatsecret.com account ----
#
# profile.create mints a profile inside this app's namespace, which the
# Calorie Counter phone app cannot sign into. To score fuelling against a
# diary the athlete actually keeps on their phone, the backend has to be
# authorised against their real account instead — which is this flow.
#
# Verification is out-of-band by default: FatSecret shows a PIN in the browser
# and the athlete pastes it back. That avoids registering a callback URL and
# behaves the same on localhost as on Render.


def _oauth_get(url: str, params: dict, token_secret: str = "") -> dict:
    """Signed GET against an endpoint that answers form-encoded, not JSON."""
    key = os.environ.get("FATSECRET_KEY")
    secret = os.environ.get("FATSECRET_SECRET")
    if not key or not secret:
        raise FatSecretError("FATSECRET_KEY / FATSECRET_SECRET are not set.")

    full = {
        "oauth_consumer_key": key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
        **params,
    }
    full["oauth_signature"] = _sign("GET", url, full, secret, token_secret)

    try:
        r = requests.get(url, params=full, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        body = getattr(e.response, "text", "")[:200] if getattr(e, "response", None) else ""
        raise FatSecretError(f"FatSecret OAuth call failed: {e} {body}") from e

    parsed = dict(parse_qsl(r.text))
    if not parsed:
        raise FatSecretError(f"Unexpected OAuth response: {r.text[:200]}")
    return parsed


def start_link(callback: str = "oob") -> dict:
    """
    Step 1. Returns a temporary token plus the URL to approve it at.

    The temporary secret must survive until the verifier comes back, so the
    caller has to store it — it is half of the signing key for step 2.
    """
    d = _oauth_get(REQUEST_TOKEN_URL, {"oauth_callback": callback})
    token = d.get("oauth_token")
    if not token:
        raise FatSecretError(f"No request token returned: {d}")
    return {
        "request_token": token,
        "request_secret": d.get("oauth_token_secret", ""),
        "authorize_url": f"{AUTHORIZE_URL}?oauth_token={_quote(token)}",
    }


def finish_link(request_token: str, request_secret: str, verifier: str) -> dict:
    """
    Step 2. Trades the approved temporary token for a lasting access token.

    The returned pair is what every later diary call is signed with, and
    FatSecret will not show it again — store it before returning.
    """
    d = _oauth_get(
        ACCESS_TOKEN_URL,
        {"oauth_token": request_token, "oauth_verifier": verifier.strip()},
        token_secret=request_secret,
    )
    token = d.get("oauth_token")
    if not token:
        raise FatSecretError(f"No access token returned: {d}")
    return {"token": token, "secret": d.get("oauth_token_secret", "")}
