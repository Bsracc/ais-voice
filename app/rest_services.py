"""REST (plain HTTPS) Deepgram STT and Cartesia TTS services for Pipecat.

Why REST instead of Pipecat's streaming services: on this VM the egress
proxy refuses every WebSocket upgrade — wss://api.deepgram.com and
wss://api.cartesia.ai both answer ``InvalidUpgrade: invalid Connection
header: close`` — so DeepgramSTTService and CartesiaTTSService can never
connect here and the pipeline dies at setup ("timeout setting the
pipeline up"). Plain HTTPS POSTs to the same providers go through fine
(verified live 7 Nov 2026: Deepgram /v1/listen transcribed
"How much does it cost?" at confidence 0.999; Cartesia /tts/bytes
returned raw PCM).

Consequences of the REST shape, stated plainly:
- STT is segmented, not streaming: a transcript arrives only after the
  user stops speaking (VAD segment end), one HTTP round-trip per segment.
  No interim transcripts.
- TTS is one HTTP round-trip per aggregated sentence: higher latency to
  first audio than streaming, and no barge-in mid-sentence — the whole
  sentence's audio must arrive before playback starts.
- Each request carries the full segment/sentence, so very long user turns
  make proportionally larger requests; Deepgram pre-recorded caps a
  request well above any single VAD segment in practice.

Proxy note: httpx's default (trust_env=True) reads the VM's no_proxy,
which contains bracketed IPv6 entries ("[::1]") that httpx cannot parse
as URL patterns — client construction itself raises InvalidURL. So the
client is built with trust_env=False and the proxy taken explicitly from
HTTPS_PROXY/https_proxy. When no proxy env var is set (e.g. a laptop
without the egress proxy) the client connects directly. trust_env=False
also skips SSL_CERT_FILE, but this VM's egress proxy MITMs TLS with a
custom CA, so the bundle path is passed explicitly as `verify` instead
— without it every request fails CERTIFICATE_VERIFY_FAILED.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator

import httpx
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

logger = logging.getLogger(__name__)

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
CARTESIA_TTS_BYTES_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2025-04-16"

# One HTTP round-trip per segment/sentence; 30 s is generous for short
# utterances and keeps a hung request from stalling the pipeline forever.
HTTP_TIMEOUT_S = 30.0

# Cartesia raw PCM output: signed 16-bit little-endian, mono, 24 kHz —
# the same rate the pipeline worker expects for output audio.
TTS_SAMPLE_RATE = 24000


def _proxy_from_env() -> str | None:
    """Egress proxy for the API calls, or None for a direct connection."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def _new_http_client() -> httpx.AsyncClient:
    # trust_env=False: see the module docstring — the VM's no_proxy breaks
    # httpx client construction. Proxy and CA bundle are passed explicitly.
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S,
        proxy=_proxy_from_env(),
        trust_env=False,
        verify=os.environ.get("SSL_CERT_FILE") or True,
    )


class _RestServiceMixin:
    """Shared HTTP client handling for the REST services below."""

    _api_key: str
    _http_client: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = _new_http_client()
        return self._http_client

    async def _close_client(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


class DeepgramRestSTTService(_RestServiceMixin, SegmentedSTTService):
    """Deepgram pre-recorded STT over HTTPS, one request per VAD segment.

    Subclasses :class:`SegmentedSTTService`, so the base class buffers
    audio between VAD start/stop events and hands each padded segment to
    :meth:`run_stt` as WAV bytes (``wants_wav_segments`` stays True).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "nova-3",
        language: str = "en",
        http_client: httpx.AsyncClient | None = None,
        **kwargs,
    ):
        if not api_key:
            raise ValueError(
                "missing API key: DEEPGRAM_API_KEY is empty. "
                "Set it in ais-voice/.env — the voice service never calls "
                "an API without a key."
            )
        super().__init__(**kwargs)
        self._api_key = api_key
        self._model = model
        self._language = language
        self._http_client = http_client

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Transcribe one WAV segment via Deepgram /v1/listen."""
        try:
            response = await self._client().post(
                DEEPGRAM_LISTEN_URL,
                params={
                    "model": self._model,
                    "language": self._language,
                    "smart_format": "true",
                },
                content=audio,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": "audio/wav",
                },
            )
        except httpx.HTTPError as e:
            # Network/proxy failure: report it, don't crash the pipeline.
            logger.warning(f"{self}: Deepgram request failed: {e}")
            yield ErrorFrame(error=f"Deepgram request failed: {e}")
            return

        if response.status_code != 200:
            logger.warning(
                f"{self}: Deepgram error (status {response.status_code})"
            )
            yield ErrorFrame(
                error=f"Deepgram API error (status {response.status_code})"
            )
            return

        try:
            payload = response.json()
            alternative = payload["results"]["channels"][0]["alternatives"][0]
            transcript = (alternative.get("transcript") or "").strip()
        except (KeyError, IndexError, ValueError, AttributeError) as e:
            logger.warning(f"{self}: unexpected Deepgram response shape: {e}")
            yield ErrorFrame(error=f"Deepgram response parse error: {e}")
            return

        if not transcript:
            # Silence / unintelligible segment: nothing to transcribe.
            return

        await self.emit_stt_usage_metrics()
        yield TranscriptionFrame(
            transcript,
            self._user_id,
            time_now_iso8601(),
            Language(self._language),
            result=payload,
        )

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._close_client()


class CartesiaRestTTSService(_RestServiceMixin, TTSService):
    """Cartesia TTS over HTTPS, one request per aggregated sentence.

    The base :class:`TTSService` is configured with push_start_frame and
    push_stop_frames, so it emits the TTSStartedFrame / TTSStoppedFrame
    pair around the audio; :meth:`run_tts` only has to yield the
    TTSAudioRawFrame(s) — the same split Pipecat's own
    CartesiaHttpTTSService uses.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model: str = "sonic-3",
        language: str = "en",
        http_client: httpx.AsyncClient | None = None,
        **kwargs,
    ):
        if not api_key:
            raise ValueError(
                "missing API key: CARTESIA_API_KEY is empty. "
                "Set it in ais-voice/.env — the voice service never calls "
                "an API without a key."
            )
        if not voice_id:
            raise ValueError(
                "missing Cartesia voice: CARTESIA_VOICE_ID is empty. "
                "Pick a voice in the Cartesia dashboard and set its ID in "
                "ais-voice/.env — no default voice is invented."
            )
        super().__init__(
            sample_rate=TTS_SAMPLE_RATE,
            push_start_frame=True,
            push_stop_frames=True,
            **kwargs,
        )
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._language = language
        self._http_client = http_client

    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        """Synthesize one sentence via Cartesia /tts/bytes (raw PCM)."""
        try:
            response = await self._client().post(
                CARTESIA_TTS_BYTES_URL,
                json={
                    "model_id": self._model,
                    "transcript": text,
                    "voice": {"mode": "id", "id": self._voice_id},
                    "language": self._language,
                    "output_format": {
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": TTS_SAMPLE_RATE,
                    },
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Cartesia-Version": CARTESIA_VERSION,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as e:
            logger.warning(f"{self}: Cartesia request failed: {e}")
            yield ErrorFrame(error=f"Cartesia request failed: {e}")
            return

        if response.status_code != 200:
            logger.warning(
                f"{self}: Cartesia error (status {response.status_code})"
            )
            yield ErrorFrame(
                error=f"Cartesia API error (status {response.status_code})"
            )
            return

        audio = response.read()
        if not audio:
            yield ErrorFrame(error="Cartesia returned empty audio")
            return

        await self.start_tts_usage_metrics(text)
        try:
            yield TTSAudioRawFrame(
                audio=audio,
                sample_rate=TTS_SAMPLE_RATE,
                num_channels=1,
                context_id=context_id,
            )
        finally:
            await self.stop_ttfb_metrics()

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._close_client()
