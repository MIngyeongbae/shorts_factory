"""TTS 어댑터와 싱크 로직 (ADR-0004, ADR-0013, ADR-0081).

실물은 둘이다 — `typecast.TypecastClient`(**기본**)와 `elevenlabs.ElevenLabsClient`.
테스트 대역은 `fake.FakeTTSClient`다. 단계 코드는 `base.TTSClient`만 보므로 제공자
교체는 `run.py tts --provider`가, 목소리 교체는 그 제공자의 voice_id 환경변수가 한다.
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
from .typecast import GRANULARITY, MAX_TEXT_CHARS
from .typecast import MODEL_ID as TYPECAST_MODEL_ID
from .typecast import TypecastClient
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
    "GRANULARITY",
    "MAX_TEXT_CHARS",
    "TYPECAST_MODEL_ID",
    "TypecastClient",
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
