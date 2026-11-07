"""Pipecat voice pipeline for the AISIMA dental-clinic outreach agent.

Stack (Pipecat 1.15.0, verified against the installed package):
- Transport: SmallWebRTC (browser, no Daily API key)
- VAD / turn-taking: Silero VAD wired into the user context aggregator;
  PipelineWorker turn tracking is on, so user speech interrupts TTS
  (barge-in).
- STT: Deepgram Nova-3, REST (pre-recorded /v1/listen, app/rest_services.py)
- LLM: Claude Sonnet 5.5 via the Anthropic 1.x SDK
- TTS: Cartesia Sonic 3, REST (/tts/bytes, app/rest_services.py)

REST, not streaming: on this VM the egress proxy refuses every WebSocket
upgrade, so Pipecat's streaming DeepgramSTTService / CartesiaTTSService
can never connect here. Plain HTTPS POSTs to the same providers work
(verified live 7 Nov 2026). Cost: transcripts arrive per VAD segment,
TTS audio per sentence — no interim transcripts, no mid-sentence barge-in.

Live factories refuse to start without their API key — a missing key
raises ValueError instead of ever calling an API unauthenticated.
Injected fake services (see app/fakes.py) build the same pipeline
offline, with no keys and no network.
"""

from dataclasses import dataclass

from anthropic import AsyncAnthropic
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
from pipecat.services.anthropic.llm import AnthropicLLMService

from app.config import Settings
from app.prompt import build_voice_prompt
from app.rest_services import CartesiaRestTTSService, DeepgramRestSTTService


@dataclass
class VoicePipeline:
    """Everything a runner needs to start one voice session."""

    pipeline: Pipeline
    worker: PipelineWorker
    stt: FrameProcessor
    tts: FrameProcessor
    llm: FrameProcessor | None  # None in muse-operator mode: no LLM processor.
    context: LLMContext
    transport: object | None


def start_greeting_text(settings: Settings) -> str:
    """Opening line, spoken first: AI disclosure, then the reason to call."""
    return (
        f"Hi, {settings.ai_disclosure} "
        "I'm calling about a website redesign for your dental clinic. "
        "Do you have two minutes?"
    )


def _require_key(settings: Settings, field: str) -> str:
    value = getattr(settings, field)
    if not value:
        raise ValueError(
            f"missing API key: {field} is empty. "
            "Set it in ais-voice/.env — the voice service never calls "
            "an API without a key."
        )
    return value


def _live_llm(settings: Settings, system_prompt: str) -> AnthropicLLMService:
    api_key = _require_key(settings, "ANTHROPIC_API_KEY")
    # Gateway support: when ANTHROPIC_BASE_URL is set (e.g. Nagoya
    # Router), build the Anthropic client against that endpoint and
    # hand it to Pipecat via `client=` — Pipecat then uses it as-is.
    # Empty base URL = official api.anthropic.com (unchanged behaviour).
    base_url = str(getattr(settings, "ANTHROPIC_BASE_URL", "") or "").strip()
    client = (
        AsyncAnthropic(api_key=api_key, base_url=base_url) if base_url else None
    )
    return AnthropicLLMService(
        api_key=api_key,
        client=client,
        settings=AnthropicLLMService.Settings(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1024,
        ),
    )


def _live_stt(settings: Settings) -> DeepgramRestSTTService:
    # REST, not streaming: the VM's egress proxy blocks WebSocket upgrades
    # to api.deepgram.com, so the streaming DeepgramSTTService can never
    # connect here. Pre-recorded /v1/listen over HTTPS works (verified
    # live 7 Nov 2026). One request per VAD segment; no interim results.
    api_key = _require_key(settings, "DEEPGRAM_API_KEY")
    return DeepgramRestSTTService(
        api_key=api_key,
        model=settings.DEEPGRAM_MODEL,
        language="en",
    )


def _live_tts(settings: Settings) -> CartesiaRestTTSService:
    # REST, not streaming: same egress-proxy WebSocket block as STT.
    # /tts/bytes over HTTPS works (verified live 7 Nov 2026, raw PCM
    # s16le 24 kHz). One request per aggregated sentence.
    api_key = _require_key(settings, "CARTESIA_API_KEY")
    if not settings.CARTESIA_VOICE_ID:
        raise ValueError(
            "missing Cartesia voice: CARTESIA_VOICE_ID is empty. "
            "Pick a voice in the Cartesia dashboard and set its ID in "
            "ais-voice/.env — no default voice is invented."
        )
    return CartesiaRestTTSService(api_key=api_key, voice_id=settings.CARTESIA_VOICE_ID)


def build_pipeline(
    settings: Settings,
    stt: FrameProcessor | None = None,
    tts: FrameProcessor | None = None,
    llm: FrameProcessor | None = None,
    transport=None,
) -> VoicePipeline:
    """Build the voice pipeline.

    Pass fake services (app.fakes) to build offline without keys.
    Pass a SmallWebRTCTransport as ``transport`` for a live browser
    session; without one the pipeline chains STT → LLM → TTS directly,
    which is what the tests exercise.

    In muse-operator mode (``settings.BRAIN_MODE == "muse-operator"``) no
    live LLM is created: the pipeline is built WITHOUT an LLM processor
    (STT → user aggregator → TTS) unless an ``llm`` is explicitly
    injected, and no Anthropic key is required. The server watcher drives
    operator turns from ``context.messages`` instead. In anthropic mode
    behaviour is unchanged: a missing ``llm`` is built live and a missing
    Anthropic key raises ValueError.
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
        stt = _live_stt(settings)
    if tts is None:
        tts = _live_tts(settings)

    # Silero VAD + turn tracking: user speech ends the bot's turn and
    # interrupts TTS (barge-in). No Daily, no extra VAD service.
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
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
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
