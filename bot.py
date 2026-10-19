"""One Pipecat pipeline per call: Twilio audio -> STT -> LLM -> TTS -> Twilio audio."""
import os
import wave
from datetime import datetime

from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.ollama.llm import OLLamaLLMService
import sys
from unittest.mock import MagicMock

# Pipecat's whisper/stt.py on macOS arm64 checks for mlx_whisper at module load
# even when using faster-whisper. Stub it if not installed so faster-whisper works.
if "mlx_whisper" not in sys.modules:
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        sys.modules["mlx_whisper"] = MagicMock()

from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

SYSTEM_PROMPT = (
    "You are Maya, a warm voice receptionist for Bay Area Community Health (BACH). "
    "You help patients schedule medical appointments across our Bay Area clinics, including Fremont, Hayward, and San Jose. "
    "You are speaking on a live phone call, so always reply in one or two short, natural spoken sentences. "
    "Never use lists, bullet points, asterisks, markdown, or emojis. "
    "Say numbers, dates, and times naturally as spoken words. "
    "If the caller mentions a severe medical emergency, tell them to hang up and dial 911 immediately. "
    "Guide the patient one step at a time: "
    "1. Ask if they are a new or existing patient with Bay Area Community Health. "
    "2. Ask what reason or type of visit they need (like routine checkup, general doctor visit, or sick visit). "
    "3. Ask for their preferred clinic location (Fremont, Hayward, or San Jose). "
    "4. Ask for their full name and date of birth. "
    "5. Ask what day and time (morning or afternoon) works best for them. "
    "Once you have their details, confirm the appointment information and thank them warmly. "
    "If you did not understand or need clarification, politely ask them to repeat."
)

GREETING_INSTRUCTION = (
    "Say: 'Thank you for calling Bay Area Community Health. My name is Maya. "
    "Are you calling to book a medical appointment today?'"
)


async def run_bot(websocket: WebSocket, stream_sid: str, call_sid: str) -> None:
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        auth_token=os.environ["TWILIO_AUTH_TOKEN"],
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt = WhisperSTTService(
        device=os.getenv("WHISPER_DEVICE", "auto"),
        settings=WhisperSTTService.Settings(
            model=os.getenv("WHISPER_MODEL", "base"),
            language=Language.EN,
        ),
    )

    llm = OLLamaLLMService(
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        settings=OLLamaLLMService.Settings(
            model=os.getenv("LLM_MODEL", "llama3.1:8b"),
            system_instruction=SYSTEM_PROMPT,
        ),
    )

    tts = KokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=os.getenv("KOKORO_VOICE", "af_heart"),
        ),
    )

    context = LLMContext()
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    call_recording_enabled = os.getenv("CALL_RECORDING", "false").lower() in ("true", "1", "yes")
    audiobuffer = None
    if call_recording_enabled:
        os.makedirs("recordings", exist_ok=True)
        audiobuffer = AudioBufferProcessor(
            sample_rate=8000,
            num_channels=1,
            buffer_size=0,
            auto_start_recording=True,
        )

        @audiobuffer.event_handler("on_audio_data")
        async def on_audio_data(buffer, audio: bytes, sample_rate: int, num_channels: int):
            if not audio:
                return
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recordings/call_{call_sid}_{timestamp}.wav"
            try:
                with wave.open(filename, "wb") as wf:
                    wf.setnchannels(num_channels)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(audio)
                logger.info(f"Call recording saved: {filename} ({len(audio)} bytes)")
            except Exception as e:
                logger.error(f"Failed to write recording {filename}: {e}")

    pipeline_elements = [
        transport.input(),
        stt,
        aggregators.user(),
        llm,
        tts,
        transport.output(),
        aggregators.assistant(),
    ]
    if audiobuffer:
        pipeline_elements.append(audiobuffer)

    pipeline = Pipeline(pipeline_elements)

    worker = PipelineWorker(
        pipeline,
        name=f"call-{call_sid}",
        params=PipelineParams(
            audio_in_sample_rate=8000,   # Twilio is 8 kHz; avoids extra resampling
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # handle_sigint=False: uvicorn owns process signals, not the per-call runner
    runner = WorkerRunner(handle_sigint=False)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Caller connected; sending greeting")
        context.add_message({"role": "user", "content": GREETING_INSTRUCTION})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Caller disconnected; cancelling pipeline")
        if audiobuffer:
            try:
                await audiobuffer.stop_recording()
            except Exception as e:
                logger.warning(f"Error stopping audio recording: {e}")
        await runner.cancel()

    await runner.add_workers(worker)
    await runner.run()
