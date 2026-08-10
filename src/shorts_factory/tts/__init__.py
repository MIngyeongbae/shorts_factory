"""TTS 어댑터와 싱크 로직 (ADR-0004, ADR-0013).

실물 ElevenLabs 어댑터는 아직 없다. `TTSClient`를 구현해 이 패키지에 넣으면
`[3. tts+sync]`는 코드 변경 없이 그대로 돈다 — `base.py` 독스트링의 정렬 계약을 지킬 것.
"""

from .base import (
    Alignment,
    Narration,
    TTSClient,
    TTSError,
    TTSRateLimited,
    TTSTimeout,
)
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
    "TTSRateLimited",
    "TTSTimeout",
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
