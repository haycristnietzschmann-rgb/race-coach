"""
Shared Claude client.

Deliberately lazy: the key is looked up on first use, not at import. An eager
`anthropic.Anthropic(api_key=os.environ[...])` at module scope means a missing
or unset key raises KeyError while main.py is still importing, which takes down
the entire service — Garmin routes, nutrition, and the mounted frontend
included. Claude is one feature here, not a hard dependency, so a missing key
should degrade the coaching text and leave the rest of the app standing.
"""

import os
import anthropic


class _LazyAnthropic:
    """Proxies to a real client, constructed on first attribute access."""

    _client = None

    def __getattr__(self, name):
        if _LazyAnthropic._client is None:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set — Claude-generated coaching is "
                    "unavailable. Add it to backend/.env (or the Render "
                    "environment) and restart."
                )
            _LazyAnthropic._client = anthropic.Anthropic(api_key=key)
        return getattr(_LazyAnthropic._client, name)


client = _LazyAnthropic()


def available() -> bool:
    """True when a key is present, for callers that want to skip Claude work."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
