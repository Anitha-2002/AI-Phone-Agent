"""One Pipecat pipeline per call: Twilio audio -> STT -> LLM -> TTS -> Twilio audio."""
import os

from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

SYSTEM_PROMPT = (
    "You are a friendly phone assistant. You are speaking on a live phone call, "
    "so reply in one or two short spoken sentences. Never use lists, markdown, "
    "emoji or special symbols. Say numbers and times the way a person would say them aloud. "
    "If you did not understand, ask the caller to repeat."
)

GREETING_INSTRUCTION = "Greet the caller briefly and ask how you can help."


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

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )

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
        await runner.cancel()

    await runner.add_workers(worker)
    await runner.run()
