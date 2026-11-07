"""Minimal fake Pipecat processors for offline tests.

These never call any real API and never touch the network. They forward
frames downstream, and the fake LLM answers any transcription with one
fixed sentence so a pipeline can be exercised end to end without keys.
"""

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601


class _PassThroughProcessor(FrameProcessor):
    """Forward every frame downstream unchanged."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await FrameProcessor.process_frame(self, frame, direction)
        await self.push_frame(frame, direction)


class FakeSTT(_PassThroughProcessor):
    """Fake speech-to-text: turns audio into one fixed transcription."""

    FIXED_TEXT = "Hello, this is a test caller."

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await FrameProcessor.process_frame(self, frame, direction)
        if isinstance(frame, AudioRawFrame):
            await self.push_frame(
                # timestamp is declared str: an int breaks the worker
                # observer's telemetry model (UserInputMessageData).
                TranscriptionFrame(
                    text=self.FIXED_TEXT, user_id="", timestamp=time_now_iso8601()
                ),
                direction,
            )
        else:
            await self.push_frame(frame, direction)


class FakeTTS(_PassThroughProcessor):
    """Fake text-to-speech: turns text into a short silence audio frame."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await FrameProcessor.process_frame(self, frame, direction)
        if isinstance(frame, TextFrame):
            # Like a real TTS service: started frame, audio, stopped frame.
            # The stopped frame matters: the output transport buffers audio
            # into 40 ms chunks and only flushes a short trailing buffer
            # when the TTS turn ends — without it the audio never leaves.
            silence = b"\x00\x00" * 160  # 10 ms of silence at 16 kHz, mono
            await self.push_frame(TTSStartedFrame(), direction)
            await self.push_frame(
                # TTSAudioRawFrame (not bare AudioRawFrame): real TTS services
                # emit this subclass, and only Frame subclasses carry the
                # `id` the pipeline worker observer reads on every push.
                TTSAudioRawFrame(audio=silence, sample_rate=16000, num_channels=1),
                direction,
            )
            await self.push_frame(TTSStoppedFrame(), direction)
        else:
            await self.push_frame(frame, direction)


class FakeLLM(_PassThroughProcessor):
    """Fake LLM: answers any transcription with one fixed sentence."""

    FIXED_REPLY = "This is a test reply from the AISIMA voice agent."

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await FrameProcessor.process_frame(self, frame, direction)
        if isinstance(frame, TranscriptionFrame):
            await self.push_frame(TextFrame(text=self.FIXED_REPLY), direction)
        else:
            await self.push_frame(frame, direction)


class FakeTransport:
    """Stand-in for a WebRTC transport in tests.

    Real runs pass a SmallWebRTCTransport; tests pass this so the
    transport.input()/output() wiring can be exercised without a browser.
    """

    def __init__(self):
        self._input = _PassThroughProcessor()
        self._output = _PassThroughProcessor()

    def input(self) -> FrameProcessor:
        return self._input

    def output(self) -> FrameProcessor:
        return self._output
