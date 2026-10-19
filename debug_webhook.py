#!/usr/bin/env python3
"""
Debug script — run this WHILE your server is running in another terminal.
It will:
1. Test that /voice returns valid TwiML
2. Trigger a call and show what Twilio is doing
3. Check if the webhook URL is reachable

Run: python debug_webhook.py
"""
import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

print("=" * 60)
print("AI Phone Agent — Webhook Debug")
print("=" * 60)
print(f"PUBLIC_BASE_URL: {PUBLIC_BASE_URL}")
print(f"TWILIO_ACCOUNT_SID: {TWILIO_ACCOUNT_SID[:8]}...")
print()

# ── 1. Test /voice endpoint directly ─────────────────────────────
voice_url = f"{PUBLIC_BASE_URL}/voice"
print(f"[1] Testing /voice webhook: {voice_url}")
try:
    req = urllib.request.Request(voice_url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
        ct = resp.headers.get("content-type", "")
        print(f"    ✅ Status: {resp.status}")
        print(f"    Content-Type: {ct}")
        print(f"    Body (first 400 chars):\n{body[:400]}")
        if "<Stream" in body:
            print("    ✅ TwiML contains <Stream> — webhook looks correct!")
        elif "Connect" in body:
            print("    ✅ TwiML contains <Connect> — webhook looks correct!")
        else:
            print("    ⚠️  TwiML does NOT contain <Stream> or <Connect>!")
            print("       Twilio won't open a WebSocket. Check server output.")
except urllib.error.HTTPError as e:
    print(f"    ❌ HTTP Error {e.code}: {e.reason}")
    try:
        print(f"    Body: {e.read().decode()[:300]}")
    except Exception:
        pass
except Exception as e:
    print(f"    ❌ Connection error: {e}")
    print(f"    Is your server running? Is ngrok still active?")

print()

# ── 2. Fetch last call from Twilio to see its status ────────────
print("[2] Fetching last 3 Twilio calls to check status...")
try:
    import base64
    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    api_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json?PageSize=3"
    req = urllib.request.Request(
        api_url,
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        calls = data.get("calls", [])
        if not calls:
            print("    No calls found in Twilio account.")
        for c in calls:
            sid = c.get("sid", "?")
            status = c.get("status", "?")
            direction = c.get("direction", "?")
            to = c.get("to", "?")
            duration = c.get("duration", "?")
            uri = c.get("uri", "")
            print(f"    Call SID: {sid}")
            print(f"    Direction: {direction} | Status: {status} | To: {to} | Duration: {duration}s")

            # Get call recordings for this call
            rec_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{sid}/Recordings.json"
            req2 = urllib.request.Request(rec_url, headers={"Authorization": f"Basic {credentials}"})
            try:
                with urllib.request.urlopen(req2, timeout=10) as r2:
                    recs = json.loads(r2.read().decode())
                    rec_count = recs.get("end", 0)
                    print(f"    Twilio-side recordings: {rec_count}")
            except Exception:
                pass
            print()
except Exception as e:
    print(f"    ❌ Twilio API error: {e}")

print()

# ── 3. Check WebSocket URL ────────────────────────────────────────
from urllib.parse import urlparse
_host = urlparse(PUBLIC_BASE_URL).netloc if PUBLIC_BASE_URL else "localhost:8000"
ws_url = f"wss://{_host}/ws"
print(f"[3] WebSocket URL your TwiML advertises: {ws_url}")
print("    Twilio will connect here after /voice returns TwiML.")
print("    Make sure this matches your ngrok URL.")
print()

print("=" * 60)
print("NEXT STEPS:")
print("  • If step [1] fails → server not running or ngrok URL wrong.")
print("  • If [1] succeeds but no <Stream> → check voice_webhook() code.")
print("  • If [1] succeeds → trigger a call, watch server terminal for:")
print("    'Incoming voice webhook' and 'Caller connected; sending greeting'")
print("=" * 60)

