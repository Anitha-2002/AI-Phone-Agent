# AI phone agent: Phase 0 and Phase 1

Hosted STT (Deepgram) + LLM (Together) + TTS (Cartesia), orchestrated by Pipecat, over Twilio Media Streams.
Written against the Pipecat 1.x docs (`PipelineWorker`, `WorkerRunner`, `Settings` objects, VAD on the user aggregator).
Pipecat changes quickly: if an import fails, check the current docs at https://docs.pipecat.ai.

## Phase 0: setup (1-2 days)

1. Write targets: fill in `docs/acceptance_targets.md`.
2. Plan your recordings: `docs/test_set.md`.
3. Accounts and keys:
   - Twilio: account SID, auth token, one voice-capable number. Trial accounts can only call verified numbers and play a trial message first.
   - Deepgram, Together AI, Cartesia: one API key each. Pick a Cartesia voice from their catalog and put its ID in `CARTESIA_VOICE_ID`.
   - Check each provider's current model names (`STT_MODEL`, `LLM_MODEL`); the defaults in `.env.example` may be outdated.
4. Environment (Python 3.11+):

   ```bash
   cd ai_caller
   python3.11 -m venv call_env && source call_env/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   cp .env.example .env     # then fill in the values
   ```

   If `pip` rejects an extra name (for example `cartesia`), check the service's page at docs.pipecat.ai for its install extra.
5. Public tunnel (Twilio must reach your laptop over HTTPS):

   ```bash
   ngrok http 8000
   ```

   Put the HTTPS URL it prints into `PUBLIC_BASE_URL` in `.env` (no trailing slash). If the URL changes, update `.env` and the Twilio webhook together.

## Phase 1: first real call

1. Start the server:

   ```bash
   python server.py
   ```

   Check `https://<your-ngrok-url>/health` returns `{"ok": true, ...}`.
2. In the Twilio Console: Phone Numbers > Active Numbers > your number > Voice Configuration.
   Set "A call comes in" to **Webhook**, method **HTTP POST**, URL `https://<your-ngrok-url>/voice`.
3. Call the number. You should hear a greeting, then be able to talk to the agent.
4. Read the logs: every line carries the call SID.

## How it works

- `/voice` (POST): verifies `X-Twilio-Signature` against `PUBLIC_BASE_URL + /voice`, then returns TwiML with `<Connect><Stream url="wss://.../ws">`. If at capacity it says the line is busy and hangs up.
- `/ws`: verifies the signature on the WebSocket handshake **before** accepting, reads Twilio's `connected`/`start` messages (`parse_telephony_websocket`), then starts one Pipecat pipeline for the call.
- `bot.py`: `transport.input() -> STT -> user aggregator (Silero VAD) -> LLM -> TTS -> transport.output() -> assistant aggregator`, at 8 kHz. When the caller hangs up, the pipeline is cancelled; when the pipeline ends, the serializer hangs up the Twilio call.
- `MAX_CONCURRENT_CALLS` is a simple in-process limit. It is replaced by Redis admission control in Phase 6.

## Troubleshooting

- 403 on `/voice`, or "bad signature" in logs: `PUBLIC_BASE_URL` does not exactly match the URL configured in Twilio (scheme, host, path, no stray trailing slash). To confirm it is only a URL mismatch, you can briefly set `SKIP_TWILIO_SIGNATURE=true` on your laptop. Turn it off again.
- WebSocket signature failures only: the code tries both the `wss://` and `https://` forms of the URL. If both fail, check Twilio's current Media Streams security docs for the signed URL form.
- Silence after the greeting: check logs for STT/LLM/TTS auth or model-name errors first.
- Hang-up errors at construction: `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are both required by the serializer.

## Known Phase 1 limits (by design, fixed in later phases)

- Turn-taking is VAD-only; expect clumsy pauses and cut-offs. Phase 2 adds a semantic turn model and tuned barge-in, plus playback-aware history trimming.
- No per-turn latency table or failure fallbacks yet. Phase 3.
- No tools, compliance flow, or Redis capacity control. Phases 4-6.
