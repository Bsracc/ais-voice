"""Raw-PCM frame serializer for the WebSocket voice transport (/ws/voice).

Wire protocol (agreed with the browser client, do not change unilaterally):
  client -> server: binary frames of raw PCM16 mono at 16 kHz.
  server -> client: binary frames of raw PCM16 mono at 24 kHz.
  Text messages (e.g. JSON ``{"type": "end"}``) are ignored, never crash.

Only audio frames are serialized: every other frame serializes to None,
which FastAPIWebsocketOutputTransport treats as "taken, nothing to send".
"""

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

AUDIO_IN_SAMPLE_RATE = 16000
AUDIO_OUT_SAMPLE_RATE = 24000
NUM_CHANNELS = 1


class RawPCMSerializer(FrameSerializer):
    """Minimal FrameSerializer: raw PCM bytes <-> audio frames, nothing else."""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, str):
            # Text messages (e.g. {"type": "end"}) are ignored.
            return None
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(
                audio=bytes(data),
                sample_rate=AUDIO_IN_SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
            )
        return None
