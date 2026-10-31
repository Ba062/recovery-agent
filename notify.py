"""Email notifications for live mode, sent with Resend (https://resend.com). Suggested in a mentor session.

send(subject, text) never raises and never waits long: a notification is a convenience, the incident run must not
depend on it. Without RESEND_API_KEY and NOTIFY_TO in .env nothing is sent ("email not configured"); the dashboard
still shows the notification and live mode logs it to live_runs/.

Resend's free tier: without a verified domain, emails come from onboarding@resend.dev and can only go to the address
of the Resend account owner, so NOTIFY_TO must be that address.
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RESEND_URL = "https://api.resend.com/emails"
SENDER = "Nabd <onboarding@resend.dev>"
TIMEOUT_S = 5

SENT, NOT_CONFIGURED, FAILED = "sent", "email not configured", "send failed"


def configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("NOTIFY_TO"))


def send(subject: str, text: str) -> dict:
    """Email one notification. Returns {"status": SENT | NOT_CONFIGURED | FAILED, "detail": "..."}."""
    if not configured():
        return {"status": NOT_CONFIGURED, "detail": "set RESEND_API_KEY and NOTIFY_TO in .env to send emails"}
    try:
        r = requests.post(RESEND_URL, timeout=TIMEOUT_S,
                          headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}"},
                          json={"from": SENDER, "to": [os.getenv("NOTIFY_TO")], "subject": subject, "text": text})
    except Exception as e:  # no network, timeout, DNS, a bad header...: never let it reach the incident run
        return {"status": FAILED, "detail": type(e).__name__}
    if r.status_code >= 300:
        return {"status": FAILED, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    return {"status": SENT, "detail": f"to {os.getenv('NOTIFY_TO')}"}
