"""
Run this ONCE, on your own Mac — not on Render — to log into Garmin from a
normal home IP address (which Garmin's Cloudflare protection doesn't block,
unlike most cloud/datacenter servers).

This saves a resumable session and prints it as a single base64 string.
Paste that string into Render as a new environment variable called
GARMIN_TOKENS_B64, and the deployed backend will resume your session
instead of ever attempting a fresh password login from Render's IP.

Usage:
    cd backend
    source venv/bin/activate
    python3 generate_garmin_tokens.py
"""
import os
import io
import base64
import zipfile
import tempfile
from pathlib import Path
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

email = os.environ["GARMIN_EMAIL"]
password = os.environ["GARMIN_PASSWORD"]

DEFAULT_TOKEN_LOCATIONS = [
    Path.home() / ".garminconnect",
    Path.home() / ".garth",
]


def find_populated_dir(candidates):
    for d in candidates:
        if d.exists() and any(d.iterdir()):
            return d
    return None


with tempfile.TemporaryDirectory() as tmp:
    token_dir = Path(tmp) / "garmin_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    print(f"Logging in as {email} ...")
    garmin = Garmin(email, password)
    garmin.login(str(token_dir))
    print("Login call completed.")

    # Try every known way this library (or its dependencies) might expose
    # a "save session to disk" method, in case login(path) doesn't do it
    # automatically in this version.
    for attr_path in ["garth.dump", "client.dump", "dump"]:
        obj = garmin
        try:
            for part in attr_path.split(".")[:-1]:
                obj = getattr(obj, part)
            method = getattr(obj, attr_path.split(".")[-1])
            method(str(token_dir))
            print(f"Saved via garmin.{attr_path}()")
            break
        except AttributeError:
            continue
        except Exception as e:
            print(f"garmin.{attr_path}() existed but failed: {e}")

    source_dir = token_dir if any(token_dir.iterdir()) else find_populated_dir(DEFAULT_TOKEN_LOCATIONS)

    if source_dir is None:
        print("\nCouldn't find any saved session files. Checked:")
        print(f"  {token_dir}")
        for d in DEFAULT_TOKEN_LOCATIONS:
            print(f"  {d}")
        print("\nAvailable methods on the garmin object, for debugging:")
        print([m for m in dir(garmin) if not m.startswith("_")])
        raise SystemExit(1)

    print(f"Using session files from: {source_dir}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in source_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(source_dir))
    encoded = base64.b64encode(buf.getvalue()).decode()

    print("\nCopy everything between the lines below and paste it into Render")
    print("as a new environment variable named GARMIN_TOKENS_B64:\n")
    print("-" * 60)
    print(encoded)
    print("-" * 60)
