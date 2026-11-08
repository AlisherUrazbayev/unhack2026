"""SMS sending through Twilio (functions only, no classes).

Setup (twilio.env in the project root, loaded with `uv run --env-file twilio.env ...`):
    TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxx
    TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxx
    TWILIO_FROM_NUMBER=+1xxxxxxxxxx

If those aren't set, this runs in DRY RUN mode: nothing is actually sent and
every message is reported as delivered, so the apps still work offline.

Phone numbers must be in E.164 format, e.g. +14165550123 (db.to_e164 converts them).

Usage:
    import send as sms
    future = sms.send_async("+14165550123", "Flood warning near you")
    ...later, once future.done():
    message_id, status, error = future.result()   # status: "delivered", "failed", ...
"""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
DRY_RUN = not (ACCOUNT_SID and AUTH_TOKEN and FROM_NUMBER)

FINAL_STATUSES = {"delivered", "undelivered", "failed"}
STATUS_TIMEOUT = 120      # seconds to wait for a delivery report
POLL_EVERY = 3            # seconds between status checks

ERROR_OPTED_OUT = 21610   # recipient replied STOP; Twilio blocks texts to them

_client = None
_executor = ThreadPoolExecutor(max_workers=8)   # sends run here so the UI never freezes


def get_client():
    global _client
    if _client is None:
        from twilio.rest import Client   # imported lazily so dry run works without twilio
        _client = Client(ACCOUNT_SID, AUTH_TOKEN)
    return _client


def format_error(e):
    """Short, readable error like '21610: Attempt to send to unsubscribed recipient'."""
    code = getattr(e, "code", None)
    msg = getattr(e, "msg", None) or str(e)
    return f"{code}: {msg}" if code else msg


def send_sms(phone, text):
    """Send one SMS (blocking). Returns (message_id, error)."""
    if DRY_RUN:
        return f"dry-{uuid.uuid4().hex[:8]}", None
    try:
        msg = get_client().messages.create(to=phone, from_=FROM_NUMBER, body=text)
        return msg.sid, None
    except Exception as e:   # TwilioRestException, bad number, network error...
        return None, format_error(e)


def get_status(message_id):
    """Current status: queued, sending, sent, delivered, undelivered, failed."""
    if message_id.startswith("dry-"):
        return "delivered"
    try:
        return get_client().messages(message_id).fetch().status
    except Exception:
        return "unknown"


def send_and_track(phone, text):
    """Send, then poll until the carrier reports a final status (or we time out).
    Returns (message_id, status, error)."""
    message_id, error = send_sms(phone, text)
    if error:
        return message_id, "failed", error

    status = "sent"
    deadline = time.time() + STATUS_TIMEOUT
    while time.time() < deadline:
        status = get_status(message_id)
        if status in FINAL_STATUSES:
            break
        time.sleep(POLL_EVERY)
    return message_id, status, None


def send_async(phone, text):
    """Run send_and_track in the background. Returns a Future."""
    return _executor.submit(send_and_track, phone, text)


def sms_segments(text):
    """How many SMS parts a message will be split into (affects cost)."""
    gsm = all(ord(ch) < 128 for ch in text)      # rough check: emoji/accents force UCS-2
    single, multi = (160, 153) if gsm else (70, 67)
    return 1 if len(text) <= single else -(-len(text) // multi)


if __name__ == "__main__":
    # Quick test: uv run --env-file twilio.env python src/send.py +14165550123
    import sys
    number = sys.argv[1] if len(sys.argv) > 1 else "+15555550100"
    print("DRY RUN" if DRY_RUN else "LIVE", "→", number)
    print(send_and_track(number, "Test alert from the flood showcase"))
