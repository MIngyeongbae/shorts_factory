"""TTS 어댑터와 싱크 로직 (ADR-0004, ADR-0013).

실물은 `elevenlabs.ElevenLabsClient`, 테스트 대역은 `fake.FakeTTSClient`다. 단계 코드는
`base.TTSClient`만 보므로 IVC→PVC 교체는 `ELEVEN_VOICE_ID` 값만 바꾸면 된다.
"""

from .base import (
    Alignment,
    Narration,
    TTSClient,
    TTSError,
    TTSNotConfigured,
    TTSRateLimited,
    TTSTimeout,
)
from .elevenlabs import DEFAULT_OUTPUT_FORMAT, MODEL_ID, ElevenLabsClient
from .audio import DEFAULT_TEMPO, AudioError, FFmpegError, write_narration
from .sync import (
    DRIFT_TOLERANCE,
    LINE_JOINER,
    SyncError,
    drift_warnings,
    narration_text,
    scale,
    scene_boundaries,
)

__all__ = [
    "Alignment",
    "Narration",
    "TTSClient",
    "TTSError",
    "TTSNotConfigured",
    "TTSRateLimited",
    "TTSTimeout",
    "DEFAULT_OUTPUT_FORMAT",
    "MODEL_ID",
    "ElevenLabsClient",
    "DEFAULT_TEMPO",
    "AudioError",
    "FFmpegError",
    "write_narration",
    "DRIFT_TOLERANCE",
    "LINE_JOINER",
    "SyncError",
    "drift_warnings",
    "narration_text",
    "scale",
    "scene_boundaries",
]
