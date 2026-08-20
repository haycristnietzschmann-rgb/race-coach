"""
Minimal web push helper. Uses the standard Web Push protocol (works for
Chrome/Edge on Mac and, once installed as a PWA, Safari on iOS 16.4+).

Setup (one-time):
    pip install pywebpush py-vapid
    vapid --gen  # generates private_key.pem / public_key.pem in this folder
Then put the generated public key (as a URL-safe base64 string) into your
.env as VAPID_PUBLIC_KEY, and the private key PEM path as VAPID_PRIVATE_KEY_PATH.
"""
import os
import json
from pathlib import Path
from pywebpush import webpush, WebPushException

SUBSCRIPTIONS_FILE = Path(__file__).parent / "subscriptions.json"
VAPID_PRIVATE_KEY_PATH = os.environ.get("VAPID_PRIVATE_KEY_PATH", "private_key.pem")
VAPID_CLAIMS = {"sub": f"mailto:{os.environ.get('NOTIFY_EMAIL', 'you@example.com')}"}


def _load_subscriptions() -> list[dict]:
    if not SUBSCRIPTIONS_FILE.exists():
        return []
    return json.loads(SUBSCRIPTIONS_FILE.read_text())


def _save_subscriptions(subs: list[dict]) -> None:
    SUBSCRIPTIONS_FILE.write_text(json.dumps(subs, indent=2))


def add_subscription(subscription: dict) -> None:
    subs = _load_subscriptions()
    if subscription not in subs:
        subs.append(subscription)
        _save_subscriptions(subs)


def send_notification_to_all(title: str, body: str) -> None:
    """Best-effort push to every stored subscription. Silently drops dead ones."""
    if not Path(VAPID_PRIVATE_KEY_PATH).exists():
        print("VAPID private key not found — skipping push (set up VAPID first, see push.py docstring)")
        return

    subs = _load_subscriptions()
    still_valid = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims=VAPID_CLAIMS.copy(),
            )
            still_valid.append(sub)
        except WebPushException as e:
            print(f"Push failed for one subscription (likely expired): {e}")
    _save_subscriptions(still_valid)
