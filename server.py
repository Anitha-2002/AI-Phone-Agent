"""FastAPI entry point: signed Twilio webhook -> TwiML <Stream> -> signed WebSocket -> bot."""
import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Response, WebSocket  # noqa: E402
from loguru import logger  # noqa: E402
from twilio.twiml.voice_response import Connect, VoiceResponse  # noqa: E402

from pipecat.runner.utils import parse_telephony_websocket  # noqa: E402

from bot import run_bot  # noqa: E402
from security import is_valid_webhook, is_valid_websocket  # noqa: E402

logger.remove()
logger.configure(extra={"call_sid": "-"})
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="{time:HH:mm:ss.SSS} | {level: <7} | {extra[call_sid]} | {message}",
)

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
_host = urlparse(PUBLIC_BASE_URL).netloc
WS_URL = f"wss://{_host}/ws"
WS_SIGNATURE_URLS = [WS_URL, f"https://{_host}/ws"]
MAX_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "1"))

active_calls = 0  # single event loop, so check-then-increment below is race-free

app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True, "active_calls": active_calls, "max_calls": MAX_CALLS}


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
