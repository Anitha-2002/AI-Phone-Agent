"""CLI testing utility for the AI Phone Agent.

Usage examples:
    python test_agent.py --diagnostics
    python test_agent.py --call
    python test_agent.py --call --to +919176114370
    python test_agent.py --chat "Hello, who are you and how can you help me?"
"""
import argparse
import json
import os
import sys
import urllib.request
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def test_diagnostics():
    print("=" * 60)
    print(" 🔍 RUNNING SYSTEM DIAGNOSTICS")
    print("=" * 60)

    # 1. Twilio
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        print("❌ Twilio: Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in .env")
    else:
        try:
            from twilio.rest import Client

            client = Client(sid, token)
            account = client.api.v2010.account.fetch()
            print(f"✅ Twilio: Authenticated successfully ({account.friendly_name}, status: {account.status})")
        except Exception as e:
            print(f"❌ Twilio: Authentication failed - {e}")

    # 2. Ollama
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    target_model = os.getenv("LLM_MODEL", "llama3.1:8b")
    try:
        url = f"{ollama_base.rstrip('/')}/models"
        req = urllib.request.Request(url, headers={"User-Agent": "test-agent"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", [])]
            if any(target_model in m for m in models):
                print(f"✅ Ollama: Online & model '{target_model}' is ready! ({ollama_base})")
            else:
                print(f"⚠️ Ollama: Connected, but model '{target_model}' not found in: {models}")
                print(f"   Run: ollama pull {target_model}")
    except Exception as e:
        print(f"❌ Ollama: Cannot connect to {ollama_base} ({e})")
        print("   Make sure Ollama is running (`ollama serve`).")

    # 3. Faster-Whisper
    whisper_model = os.getenv("WHISPER_MODEL", "base")
    whisper_device = os.getenv("WHISPER_DEVICE", "auto")
    print(f"✅ STT: faster-whisper configured (model: {whisper_model}, device: {whisper_device})")

    # 4. Kokoro TTS
    kokoro_voice = os.getenv("KOKORO_VOICE", "af_heart")
    print(f"✅ TTS: Kokoro configured (voice: {kokoro_voice})")

    # 5. Public Tunnel URL
    public_base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if public_base.startswith("https://"):
        print(f"✅ Tunnel: PUBLIC_BASE_URL is set ({public_base})")
    else:
        print(f"⚠️ Tunnel: PUBLIC_BASE_URL is '{public_base}'. Ensure ngrok is running and starts with https://")

    print("=" * 60)


def test_call(to_number: str | None = None, from_number: str | None = None):
    to_num = to_number or os.getenv("DEFAULT_TO_NUMBER")
    from_num = from_number or os.getenv("TWILIO_PHONE_NUMBER")
    public_base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")

    print("=" * 60)
    print(" 📞 TRIGGERING OUTBOUND CALL VIA TWILIO")
    print("=" * 60)

    if not to_num:
        print("❌ Error: Target phone number is missing. Provide via --to or set DEFAULT_TO_NUMBER in .env.")
        return
    if not from_num:
        print("❌ Error: Twilio phone number is missing. Provide via --from or set TWILIO_PHONE_NUMBER in .env.")
        return
    if not public_base:
        print("❌ Error: PUBLIC_BASE_URL is missing in .env.")
        return
    if not sid or not token:
        print("❌ Error: Twilio credentials missing in .env.")
        return

    try:
        from twilio.rest import Client

        client = Client(sid, token)
        webhook_url = f"{public_base}/voice"
        print(f"Calling: {to_num}")
        print(f"From:    {from_num}")
        print(f"Webhook: {webhook_url}")

        call = client.calls.create(to=to_num, from_=from_num, url=webhook_url)
        print(f"✅ Call dispatched! Call SID: {call.sid}")
        print(f"Status: {call.status}")
        print("Your phone should ring in a few seconds!")
    except Exception as e:
        print(f"❌ Failed to trigger call: {e}")
    print("=" * 60)


def test_chat(message: str):
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = os.getenv("LLM_MODEL", "llama3.1:8b")

    from bot import SYSTEM_PROMPT

    print("=" * 60)
    print(f" 💬 TESTING AI CHAT WITH OLLAMA ({model})")
    print("=" * 60)
    print(f"User: \"{message}\"")
    print("Thinking...")

    try:
        url = f"{ollama_base.rstrip('/')}/chat/completions"
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ],
                "temperature": 0.7,
            }
        ).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"]
            print(f"🤖 AI Spoken Reply:\n\"{reply}\"")
    except Exception as e:
        print(f"❌ Chat failed: {e}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="AI Phone Agent Test Utility")
    parser.add_argument("--diagnostics", action="store_true", help="Run pre-flight system diagnostics")
    parser.add_argument("--call", action="store_true", help="Trigger an outbound call to your phone")
    parser.add_argument("--to", type=str, default=None, help="Destination phone number (e.g. +919176114370)")
    parser.add_argument("--from-number", type=str, default=None, help="Twilio phone number (e.g. +17372508034)")
    parser.add_argument("--chat", type=str, default=None, help="Send a test message to Ollama using phone prompt")

    args = parser.parse_args()

    if not (args.diagnostics or args.call or args.chat):
        parser.print_help()
        sys.exit(1)

    if args.diagnostics:
        test_diagnostics()
    if args.chat:
        test_chat(args.chat)
    if args.call:
        test_call(args.to, args.from_number)


if __name__ == "__main__":
    main()

