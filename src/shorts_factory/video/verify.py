"""싱크 검증 — 만든 산출물이 씬 계약과 ±200ms 안에서 맞는가.

specs/00 성공 기준 4: "자막 타이밍과 TTS 오디오 싱크 오차 ±200ms 이내".
specs/05 `[9. assemble]`: "싱크 오차 ±200ms 이내 검증."

나레이션 오디오의 시각축은 `scenes.timed.json`의 `start`/`end`다 — `[3]`이 문자 정렬
실측에서 뽑아 배속 보정까지 끝낸 값이고, 씬의 시각을 읽는 곳은 이 파일 하나다
(ADR-0020). 그래서 오디오 파일을 열지 않고도 검증이 성립한다. 여기서 재는 것은
**우리가 만든 두 산출물이 그 계약에서 얼마나 벗어났는가**다.

    1. 자막 큐 — 되읽은 ASS의 시각 vs 씬의 `start`/`end` (반올림·포맷 버그)
    2. 클립 배치 — 타임라인에 놓은 시각 vs 씬의 `start` (오프셋 누적 버그)
    3. 총 길이 — 조립 결과 길이 vs `total_duration` (꼬리 처리 버그)

FFmpeg를 부르지 않는다. 프레임 격자(30fps)에서 오는 ±33ms는 이 검사 뒤에 붙는
양자화 오차이고, 허용치 200ms는 그것까지 삼킬 만큼 넓다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .subtitles import Cue
from .timeline import Timeline

#: specs/00 성공 기준 4
SYNC_TOLERANCE = 0.2


@dataclass(frozen=True)
class SyncReport:
    errors: tuple[str, ...]
    #: 관측된 가장 큰 오차(초). 허용치와 비교해 여유를 본다
    max_drift: float
    tolerance: float = SYNC_TOLERANCE

    @property
    def passed(self) -> bool:
        return not self.errors


def check_sync(
    scenes: Sequence[dict[str, Any]],
    *,
    cues: Sequence[Cue],
    timeline: Timeline,
    total_duration: float,
    tolerance: float = SYNC_TOLERANCE,
) -> SyncReport:
    errors: list[str] = []
    drifts: list[float] = [0.0]

    if len(cues) != len(scenes):
        errors.append(f"자막 큐 {len(cues)}개 / 씬 {len(scenes)}개 — 개수가 다르다")
    if len(timeline.segments) != len(scenes):
        errors.append(
            f"클립 {len(timeline.segments)}개 / 씬 {len(scenes)}개 — 개수가 다르다"
        )
    if errors:
        return SyncReport(errors=tuple(errors), max_drift=0.0, tolerance=tolerance)

    for scene, cue, segment in zip(scenes, cues, timeline.segments):
        scene_id = scene["scene_id"]
        start = float(scene["start"])
        end = float(scene["end"])

        for label, measured, expected in (
            ("자막 시작", cue.start, start),
            ("자막 끝", cue.end, end),
            ("클립 배치", segment.start, start),
        ):
            drift = abs(measured - expected)
            drifts.append(drift)
            if drift > tolerance:
                errors.append(
                    f"scenes/{scene_id}: {label} {measured:.3f}초가 계약 "
                    f"{expected:.3f}초에서 {drift * 1000:.0f}ms 벗어났다 "
                    f"(허용 ±{tolerance * 1000:.0f}ms)"
                )

        if segment.scene_id != scene_id:
            errors.append(
                f"클립 순서가 씬과 다르다: {segment.scene_id} != {scene_id}"
            )

    drift = abs(timeline.total_duration - total_duration)
    drifts.append(drift)
    if drift > tolerance:
        errors.append(
            f"타임라인 길이 {timeline.total_duration:.3f}초가 total_duration "
            f"{total_duration:.3f}초에서 {drift * 1000:.0f}ms 벗어났다 "
            f"(허용 ±{tolerance * 1000:.0f}ms)"
        )

    return SyncReport(
        errors=tuple(errors), max_drift=max(drifts), tolerance=tolerance
    )
