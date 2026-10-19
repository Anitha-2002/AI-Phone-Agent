"""FastAPI entry point: signed Twilio webhook -> TwiML <Stream> -> signed WebSocket -> bot."""
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime

from fastapi import FastAPI, Request, Response, WebSocket  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from twilio.twiml.voice_response import Connect, VoiceResponse  # noqa: E402

from pipecat.runner.utils import parse_telephony_websocket  # noqa: E402

from bot import SYSTEM_PROMPT, run_bot  # noqa: E402
from security import is_valid_webhook, is_valid_websocket  # noqa: E402

logger.remove()
logger.configure(extra={"call_sid": "-"})
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="{time:HH:mm:ss.SSS} | {level: <7} | {extra[call_sid]} | {message}",
)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
_host = urlparse(PUBLIC_BASE_URL).netloc if PUBLIC_BASE_URL else "localhost:8000"
WS_URL = f"wss://{_host}/ws"
WS_SIGNATURE_URLS = [WS_URL, f"https://{_host}/ws"]
MAX_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "1"))

active_calls = 0  # single event loop, so check-then-increment below is race-free

app = FastAPI(title="AI Phone Agent API", version="1.0.0")


class TriggerCallRequest(BaseModel):
    to: str | None = None
    from_number: str | None = None


class ChatTestRequest(BaseModel):
    message: str


@app.get("/health")
async def health():
    return {
        "ok": True,
        "active_calls": active_calls,
        "max_calls": MAX_CALLS,
        "public_base_url": PUBLIC_BASE_URL or "not_configured",
    }


@app.post("/voice")
async def voice_webhook(request: Request):
    form = dict(await request.form())
    signature = request.headers.get("x-twilio-signature", "")
    if not is_valid_webhook(f"{PUBLIC_BASE_URL}/voice", form, signature):
        return Response(status_code=403)

    response = VoiceResponse()
    if active_calls >= MAX_CALLS:
        logger.warning("At capacity; turning caller away")
        response.say("Sorry, all of our agents are busy. Please call back in a few minutes.")
        response.hangup()
    else:
        connect = Connect()
        connect.stream(url=WS_URL)
        response.append(connect)
    return Response(content=str(response), media_type="application/xml")


@app.websocket("/ws")
async def twilio_ws(websocket: WebSocket):
    global active_calls

    # Validate BEFORE accepting the connection
    signature = websocket.headers.get("x-twilio-signature", "")
    if not is_valid_websocket(WS_SIGNATURE_URLS, signature):
        await websocket.close(code=1008)
        return

    if active_calls >= MAX_CALLS:
        await websocket.close(code=1013)
        return

    await websocket.accept()
    active_calls += 1
    try:
        transport_type, call_data = await parse_telephony_websocket(websocket)
        if transport_type != "twilio":
            logger.error(f"Unexpected transport type: {transport_type}")
            return

        stream_sid = call_data["stream_id"]
        call_sid = call_data["call_id"]
        with logger.contextualize(call_sid=call_sid):
            logger.info(f"Call started (stream {stream_sid})")
            try:
                await run_bot(websocket, stream_sid, call_sid)
            except Exception:
                logger.exception("Call pipeline failed")
            finally:
                logger.info("Call ended")
    finally:
        active_calls -= 1


# ==============================================================================
# Testing & Management APIs
# ==============================================================================


@app.post("/api/trigger-call")
async def api_trigger_call(req: TriggerCallRequest | None = None):
    """Trigger an outbound phone call connecting the caller to the AI agent."""
    to_num = (req.to if req and req.to else None) or os.getenv("DEFAULT_TO_NUMBER")
    from_num = (req.from_number if req and req.from_number else None) or os.getenv("TWILIO_PHONE_NUMBER")

    if not to_num:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "Destination number 'to' is required. Provide it in JSON body or set DEFAULT_TO_NUMBER in .env.",
            },
        )
    if not from_num:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "Twilio number 'from_number' is required. Provide it in JSON body or set TWILIO_PHONE_NUMBER in .env.",
            },
        )
    if not PUBLIC_BASE_URL:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "PUBLIC_BASE_URL is not configured in .env."},
        )

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is missing in .env."},
        )

    try:
        from twilio.rest import Client

        client = Client(sid, token)
        call = await asyncio.to_thread(
            client.calls.create,
            to=to_num,
            from_=from_num,
            url=f"{PUBLIC_BASE_URL}/voice",
        )
        logger.info(f"Triggered call to {to_num} (Call SID: {call.sid})")
        return {
            "ok": True,
            "call_sid": call.sid,
            "status": call.status,
            "to": to_num,
            "from": from_num,
            "webhook_url": f"{PUBLIC_BASE_URL}/voice",
        }
    except Exception as e:
        logger.exception("Failed to trigger outbound call via Twilio")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/api/diagnostics")
async def api_diagnostics():
    """Run pre-flight diagnostics across Twilio, Ollama, Whisper, and Kokoro."""
    results = {}

    # 1. Twilio Check
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        results["twilio"] = {"ok": False, "error": "Credentials missing in .env"}
    else:
        try:
            from twilio.rest import Client

            client = Client(sid, token)
            account = await asyncio.to_thread(client.api.v2010.account.fetch)
            results["twilio"] = {
                "ok": True,
                "account_name": account.friendly_name,
                "status": account.status,
                "type": account.type,
            }
        except Exception as e:
            results["twilio"] = {"ok": False, "error": str(e)}

    # 2. Ollama Check
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    target_model = os.getenv("LLM_MODEL", "llama3.1:8b")

    def _check_ollama():
        url = f"{ollama_base.rstrip('/')}/models"
        req = urllib.request.Request(url, headers={"User-Agent": "pipecat-agent"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", [])]
            return models

    try:
        found_models = await asyncio.to_thread(_check_ollama)
        has_model = any(target_model in m for m in found_models)
        results["ollama"] = {
            "ok": has_model,
            "endpoint": ollama_base,
            "target_model": target_model,
            "model_ready": has_model,
            "available_models": found_models,
            "message": "Ready" if has_model else f"Model '{target_model}' not found in Ollama. Run: ollama pull {target_model}",
        }
    except Exception as e:
        results["ollama"] = {
            "ok": False,
            "endpoint": ollama_base,
            "error": f"Cannot connect to Ollama ({e}). Is 'ollama serve' running?",
        }

    # 3. Whisper STT Config Check
    whisper_model = os.getenv("WHISPER_MODEL", "base")
    whisper_device = os.getenv("WHISPER_DEVICE", "auto")
    results["stt_whisper"] = {
        "ok": True,
        "model": whisper_model,
        "device": whisper_device,
        "note": "faster-whisper with automatic first-call caching",
    }

    # 4. Kokoro TTS Config Check
    kokoro_voice = os.getenv("KOKORO_VOICE", "af_heart")
    results["tts_kokoro"] = {
        "ok": True,
        "voice": kokoro_voice,
        "note": "kokoro-onnx with automatic first-call caching",
    }

    # 5. Public URL Check
    results["tunnel"] = {
        "ok": bool(PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")),
        "public_base_url": PUBLIC_BASE_URL or "Missing (set in .env)",
    }

    # 6. Call Recording Check
    call_rec = os.getenv("CALL_RECORDING", "false").lower() in ("true", "1", "yes")
    results["call_recording"] = {
        "ok": True,
        "enabled": call_rec,
        "storage_dir": "recordings/",
        "note": "Enabled - audio saved to recordings/*.wav" if call_rec else "Disabled (set CALL_RECORDING=true in .env to record calls)",
    }

    all_ok = all(v.get("ok", False) for v in results.values())
    return {"status": "healthy" if all_ok else "attention_needed", "services": results}


@app.get("/api/recordings")
async def api_list_recordings():
    """List all audio recordings stored on disk."""
    if not os.path.exists("recordings"):
        return {"recordings": []}
    files = []
    for fname in sorted(os.listdir("recordings"), reverse=True):
        if fname.endswith(".wav"):
            fpath = os.path.join("recordings", fname)
            stat = os.stat(fpath)
            files.append(
                {
                    "filename": fname,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "created_at": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
                    "download_url": f"/api/recordings/{fname}",
                }
            )
    return {"recordings": files}


@app.get("/api/recordings/{filename}")
async def api_get_recording(filename: str):
    """Stream or download a specific call recording WAV file."""
    clean_name = os.path.basename(filename)
    fpath = os.path.join("recordings", clean_name)
    if not os.path.isfile(fpath):
        return JSONResponse(status_code=404, content={"error": "Recording file not found"})
    return FileResponse(fpath, media_type="audio/wav", filename=clean_name)


@app.post("/api/test/chat")
async def api_test_chat(req: ChatTestRequest):
    """Test AI response text locally using the exact phone prompt without dialing."""
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = os.getenv("LLM_MODEL", "llama3.1:8b")

    def _query():
        url = f"{ollama_base.rstrip('/')}/chat/completions"
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": req.message},
                ],
                "temperature": 0.7,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]

    try:
        reply = await asyncio.to_thread(_query)
        return {
            "ok": True,
            "user_message": req.message,
            "assistant_response": reply,
            "model": model,
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"Ollama chat error: {e}"},
        )


@app.get("/test", response_class=HTMLResponse)
async def test_dashboard():
    """Interactive web console to trigger calls, run diagnostics, and test AI chat."""
    default_to = os.getenv("DEFAULT_TO_NUMBER", "")
    default_from = os.getenv("TWILIO_PHONE_NUMBER", "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AI Phone Agent - Test Console</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    :root {{ --primary: #2563eb; --bg: #f8fafc; --card: #ffffff; --text: #0f172a; --border: #e2e8f0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 24px; max-width: 900px; margin: 0 auto; }}
    h1 {{ margin-bottom: 8px; }}
    .subtitle {{ color: #64748b; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
    @media(min-width: 768px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    .full {{ grid-column: 1 / -1; }}
    h2 {{ font-size: 1.15rem; margin-top: 0; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }}
    label {{ display: block; font-size: 0.85rem; font-weight: 600; color: #475569; margin-bottom: 4px; }}
    input[type="text"] {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 0.95rem; margin-bottom: 12px; }}
    button {{ background: var(--primary); color: white; border: none; border-radius: 8px; padding: 10px 16px; font-weight: 600; cursor: pointer; transition: opacity 0.2s; width: 100%; }}
    button:hover {{ opacity: 0.9; }}
    .status-box {{ margin-top: 12px; padding: 12px; border-radius: 8px; background: #f1f5f9; font-size: 0.85rem; font-family: monospace; white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow-y: auto; }}
  </style>
</head>
<body>
  <h1>🎙️ AI Phone Agent Console</h1>
  <div class="subtitle">FastAPI + Twilio + faster-whisper + Ollama (Llama 3.1) + Kokoro TTS</div>

  <div class="grid">
    <!-- Call Trigger Card -->
    <div class="card">
      <h2>📞 Trigger Outbound Call</h2>
      <label>To (Your Phone Number):</label>
      <input type="text" id="callTo" value="{default_to}" placeholder="+919176114370" />
      <label>From (Twilio Phone Number):</label>
      <input type="text" id="callFrom" value="{default_from}" placeholder="+17372508034" />
      <button onclick="triggerCall()">Call My Phone Now</button>
      <div id="callStatus" class="status-box">Ready to place call...</div>
    </div>

    <!-- Diagnostics Card -->
    <div class="card">
      <h2>🔍 System Diagnostics</h2>
      <p style="font-size: 0.85rem; color: #64748b; margin-top: 0;">Checks Twilio, Ollama, Whisper, Kokoro & Ngrok tunnel.</p>
      <button onclick="runDiagnostics()" style="background: #475569;">Run Health Checks</button>
      <div id="diagStatus" class="status-box">Click above to run diagnostics...</div>
    </div>

    <!-- AI Chat Simulator Card -->
    <div class="card full">
      <h2>💬 Test AI Spoken Response (Dry Run)</h2>
      <p style="font-size: 0.85rem; color: #64748b; margin-top: 0;">Sends your message directly to Ollama with the live phone system prompt.</p>
      <div style="display: flex; gap: 8px;">
        <input type="text" id="chatMsg" placeholder="e.g. Hi, who are you and how can you help me?" style="margin-bottom: 0;" />
        <button onclick="sendChat()" style="width: 140px;">Send</button>
      </div>
      <div id="chatStatus" class="status-box" style="margin-top: 12px;">Type a message above to test response tone...</div>
    </div>
    <!-- Call Recordings Card -->
    <div class="card full">
      <h2>🎧 Call Recordings (recordings/)</h2>
      <p style="font-size: 0.85rem; color: #64748b; margin-top: 0;">Controlled by <code>CALL_RECORDING=true</code> in <code>.env</code>. Audio is stored locally as WAV files.</p>
      <button onclick="loadRecordings()" style="background: #475569; width: auto; padding: 8px 16px; margin-bottom: 12px;">Refresh Recordings</button>
      <div id="recordingsList" style="font-size: 0.9rem;">Click above to view saved recordings...</div>
    </div>
  </div>

  <script>
    async function loadRecordings() {{
      const container = document.getElementById('recordingsList');
      container.innerHTML = 'Loading recordings...';
      try {{
        const res = await fetch('/api/recordings');
        const data = await res.json();
        if (!data.recordings || data.recordings.length === 0) {{
          container.innerHTML = '<p style="color:#64748b; margin:0;">No call recordings found yet. Complete a call with CALL_RECORDING=true to record audio.</p>';
          return;
        }}
        let html = '<ul style="list-style:none; padding:0; margin:0;">';
        for (const r of data.recordings) {{
          html += `<li style="padding:10px 0; border-bottom:1px solid #e2e8f0; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px;">
            <div>
              <b>${{r.filename}}</b> <span style="color:#64748b; font-size:0.8rem;">(${{r.size_kb}} KB &bull; ${{r.created_at}})</span>
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              <audio controls src="${{r.download_url}}" style="height:32px;"></audio>
              <a href="${{r.download_url}}" download style="font-size:0.85rem; color:#2563eb; text-decoration:none; font-weight:600;">Download</a>
            </div>
          </li>`;
        }}
        html += '</ul>';
        container.innerHTML = html;
      }} catch (err) {{
        container.innerHTML = 'Error: ' + err.message;
      }}
    }}

    async function triggerCall() {{
      const to = document.getElementById('callTo').value.trim();
      const from = document.getElementById('callFrom').value.trim();
      const status = document.getElementById('callStatus');
      status.textContent = 'Triggering call via Twilio...';
      try {{
        const res = await fetch('/api/trigger-call', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ to: to || null, from_number: from || null }})
        }});
        const data = await res.json();
        status.textContent = JSON.stringify(data, null, 2);
      }} catch (err) {{
        status.textContent = 'Error: ' + err.message;
      }}
    }}

    async function runDiagnostics() {{
      const status = document.getElementById('diagStatus');
      status.textContent = 'Running diagnostics across services...';
      try {{
        const res = await fetch('/api/diagnostics');
        const data = await res.json();
        status.textContent = JSON.stringify(data, null, 2);
      }} catch (err) {{
        status.textContent = 'Error: ' + err.message;
      }}
    }}

    async function sendChat() {{
      const msg = document.getElementById('chatMsg').value.trim();
      const status = document.getElementById('chatStatus');
      if (!msg) return;
      status.textContent = 'Waiting for Ollama response...';
      try {{
        const res = await fetch('/api/test/chat', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ message: msg }})
        }});
        const data = await res.json();
        if (data.ok) {{
          status.textContent = '🤖 AI Spoke:\\n"' + data.assistant_response + '"';
        }} else {{
          status.textContent = 'Error: ' + (data.error || 'Failed');
        }}
      }} catch (err) {{
        status.textContent = 'Error: ' + err.message;
      }}
    }}
  </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
