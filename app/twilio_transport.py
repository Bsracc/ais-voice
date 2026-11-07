"""Twilio Media Streams transport for the AIS voice service (Phase 2).

There is NO ``pipecat.transports.network.twilio`` in the installed
pipecat 1.15 — the official Twilio piece is
``pipecat.serializers.twilio.TwilioFrameSerializer``, used with
``FastAPIWebsocketTransport`` (same pattern as the browser WebSocket
transport in app/ws_transport.py, different wire format). This module
is the thin layer on top of it:

  * ``build_twiml`` — the TwiML Twilio fetches when a call connects:
    ``<Connect><Stream url="wss://.../twilio/stream"/></Connect>``.
  * ``parse_twilio_message`` — Twilio's WebSocket events. The stream
    starts with a ``connected`` event, then a ``start`` event carrying
    ``streamSid`` / ``callSid``; audio arrives as ``media`` events
    (base64 mu-law 8 kHz); the call ends with a ``stop`` event.
    The ``start`` event must be read BEFORE the transport is built,
    because TwilioFrameSerializer needs stream_sid at construction.
  * ``build_twilio_pipeline`` — the same voice pipeline as the browser
    (app/pipeline.py), but for a phone call: 8 kHz in/out (Twilio is
    mu-law 8 kHz) and STREAMING Deepgram STT + Cartesia TTS. The REST
    services are NOT used here: on Bsr's laptop the internet is direct,
    so streaming works — and a phone call needs interim transcripts
    and mid-sentence barge-in, which the REST path cannot give.

Sample-rate note: the serializer resamples Twilio's 8 kHz mu-law to
the pipeline's input rate and back. The pipeline runs at 8 kHz both
ways, so the resamplers are near no-ops and Deepgram/Cartesia both
accept 8 kHz linear PCM.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse
from xml.sax.saxutils import escape as _xml_escape

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions

from app.config import Settings
from app.pipeline import (
    VoicePipeline,
    _live_llm,
    _require_key,
    start_greeting_text,
)
from app.prompt import build_voice_prompt

# Twilio Media Streams are always mu-law 8 kHz.
TWILIO_SAMPLE_RATE = 8000
TWILIO_STREAM_PATH = "/twilio/stream"
TWILIO_VOICE_PATH = "/twilio/voice"


def build_twiml(stream_url: str) -> str:
    """TwiML that connects the call's audio to our media-stream socket.

    Twilio fetches this from POST /twilio/voice when the call connects.
    The stream URL must be public wss:// (ngrok on the laptop, see
    DIAL_TEST.md). Raises ValueError for anything else — a bad URL
    would silently drop the call's audio.
    """
    url = (stream_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "wss" or not parsed.netloc:
        raise ValueError(
            "TWILIO_STREAM_URL must be a public wss:// URL "
            f"(got {stream_url!r}). On the laptop this is the ngrok URL "
            "plus /twilio/stream, e.g. wss://<id>.ngrok-free.app/twilio/stream."
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Connect><Stream url="{_xml_escape(url, {"\"": "&quot;"})}" /></Connect>\n'
        "</Response>\n"
    )


def twiml_url_from_stream_url(stream_url: str) -> str:
    """Derive the public TwiML URL from the stream URL.

    wss://<host>/twilio/stream -> https://<host>/twilio/voice.
    The stream URL must end with /twilio/stream, otherwise the
    derivation is a guess and we refuse.
    """
    url = (stream_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "wss" or not parsed.netloc:
        raise ValueError(f"TWILIO_STREAM_URL must start with wss:// (got {url!r}).")
    rest = url[len("wss://"):]
    if not rest.endswith(TWILIO_STREAM_PATH):
        raise ValueError(
            "TWILIO_STREAM_URL must end with "
            f"{TWILIO_STREAM_PATH} (got {url!r})."
        )
    host = rest[: -len(TWILIO_STREAM_PATH)]
    return f"https://{host}{TWILIO_VOICE_PATH}"


def parse_twilio_message(data: str | bytes) -> dict | None:
    """Parse one Twilio Media Streams WebSocket message.

    Returns {"event": "start", "stream_sid": ..., "call_sid": ...} for
    the start event, {"event": "stop"} for the stop event, and None for
    everything else (connected, media, dtmf, mark, garbage) — those are
    left to the transport's serializer, which handles media/dtmf.
    """
    try:
        message = json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    event = message.get("event")
    if event == "start":
        start = message.get("start") or {}
        stream_sid = start.get("streamSid")
        call_sid = start.get("callSid")
        if not stream_sid:
            return None
        return {"event": "start", "stream_sid": stream_sid, "call_sid": call_sid}
    if event == "stop":
        return {"event": "stop"}
    return None


def _live_streaming_stt(settings: Settings) -> DeepgramSTTService:
    """Streaming Deepgram STT (Nova-3, 8 kHz linear PCM).

    Unlike the browser path (REST, app/rest_services.py) this opens a
    live WebSocket to Deepgram — only possible where the network allows
    outbound WebSockets (the laptop, not this VM's egress proxy).
    """
    api_key = _require_key(settings, "DEEPGRAM_API_KEY")
    return DeepgramSTTService(
        api_key=api_key,
        sample_rate=TWILIO_SAMPLE_RATE,
        live_options=LiveOptions(
            language="en",
            model=settings.DEEPGRAM_MODEL,
            smart_format=True,
            interim_results=True,
        ),
    )


def _live_streaming_tts(settings: Settings) -> CartesiaTTSService:
    """Streaming Cartesia TTS (Sonic 3, raw PCM 8 kHz out)."""
    api_key = _require_key(settings, "CARTESIA_API_KEY")
    if not settings.CARTESIA_VOICE_ID:
        raise ValueError(
            "missing Cartesia voice: CARTESIA_VOICE_ID is empty. "
            "Pick a voice in the Cartesia dashboard and set its ID in "
            "ais-voice/.env — no default voice is invented."
        )
    return CartesiaTTSService(
        api_key=api_key,
        voice_id=settings.CARTESIA_VOICE_ID,
        model="sonic-3",
        sample_rate=TWILIO_SAMPLE_RATE,
    )


def build_twilio_pipeline(
    settings: Settings,
    stt: FrameProcessor | None = None,
    tts: FrameProcessor | None = None,
    llm: FrameProcessor | None = None,
    transport=None,
) -> VoicePipeline:
    """Build the voice pipeline for one Twilio phone call.

    Same shape as app.pipeline.build_pipeline (same system prompt,
    same Silero VAD + turn tracking, same greeting), but at 8 kHz with
    streaming STT/TTS. Pass fake services to build offline without
    keys (tests); pass a FastAPIWebsocketTransport built with
    TwilioFrameSerializer as ``transport`` for a live call. In
    muse-operator mode no LLM is created (same rule as the browser
    pipeline) and no Anthropic key is required.
    """
    system_prompt = build_voice_prompt(settings)
    context = LLMContext(messages=[{"role": "system", "content": system_prompt}])

    mode = str(getattr(settings, "BRAIN_MODE", "anthropic") or "anthropic").strip().lower()
    if mode not in ("anthropic", "muse-operator"):
        raise ValueError(
            f"invalid BRAIN_MODE: {settings.BRAIN_MODE!r}. "
            "Allowed values: 'anthropic', 'muse-operator'."
        )
    if mode == "anthropic" and llm is None:
        llm = _live_llm(settings, system_prompt)
    if stt is None:
        stt = _live_streaming_stt(settings)
    if tts is None:
        tts = _live_streaming_tts(settings)

    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.4))
        ),
    )

    processors: list[FrameProcessor] = []
    if transport is not None:
        processors.append(transport.input())
    processors.append(stt)
    processors.append(aggregators.user())
    if llm is not None:
        processors.append(llm)
    processors.append(tts)
    if transport is not None:
        processors.append(transport.output())
    processors.append(aggregators.assistant())

    pipeline = Pipeline(processors)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
        ),
    )

    return VoicePipeline(
        pipeline=pipeline,
        worker=worker,
        stt=stt,
        tts=tts,
        llm=llm,
        context=context,
        transport=transport,
    )


def greeting_text(settings: Settings) -> str:
    """Opening line for a phone call — same disclosure-first greeting."""
    return start_greeting_text(settings)


__all__ = [
    "TWILIO_SAMPLE_RATE",
    "TWILIO_STREAM_PATH",
    "TWILIO_VOICE_PATH",
    "build_twilio_pipeline",
    "build_twiml",
    "greeting_text",
    "parse_twilio_message",
    "twiml_url_from_stream_url",
]
