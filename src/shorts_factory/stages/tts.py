"""[3. tts+sync] — 대본 → 나레이션 오디오 + 실측 타임스탬프. 2부의 첫 단계다.

specs/05-pipeline.md:
    [3. tts+sync] → narration.wav + timing.json + scenes.timed.json
                    (ElevenLabs with-timestamps 문자 정렬 → 문장 경계 실측, ADR-0004)

    "대본 전체 단일 호출 + with-timestamps. 문자 정렬에서 씬(자막 줄) 경계 start/end
     추출해 runs/{run_id}/scenes.timed.json을 새로 쓴다(ADR-0017. 06-script.json은
     건드리지 않는다) — 문장 경계를 뽑은 뒤 각 줄의 마지막 문장 끝만 취한다 (ADR-0013).
     실측-추정 오차 씬당 ±1.5초 초과 시 경고. 총 길이 102초 초과 시 대본 축약이
     필요하다고 리포트하고 멈춘다 — 2부가 1부를 직접 다시 돌리지 않는다. ... atempo 1.1
     적용 후 타임스탬프도 1/1.1 스케일 보정."

## 경계 (ADR-0017)

입력은 `topics/{slug}/06-script.json` **하나뿐이고 읽기 전용**이다. `run_id`도 그 안에서
읽는다 — `runs/{run_id}/topic.json`(1부 계약)에 기대지 않으므로 2부는 1부 산출물 중
경계면 파일 하나만 보고 돈다. 산출물은 전부 `runs/{run_id}/` 아래다.

## 102초 초과 시 왜 멈추는가

여기서 할 수 있는 일이 없다. 배속을 더 올리는 것은 ADR-0004가 정한 1.1~1.2배 밖으로
나가는 결정이고, 대본을 줄이는 것은 1부 소관이다(ADR-0017 단방향 경계). 그래서
`narration.wav`와 `timing.json`은 남기고(다시 사지 않아도 되게) `scenes.timed.json`은
쓰지 않는다 — 하류 단계가 계약 파일을 보고 진행해 버리는 것을 막는다.

## 이 단계가 하지 않는 것

- **게이트 판정.** `judgment/human.json`의 `decision: go` 확인은 2부 진입점의 몫이다
  (ADR-0017). 아직 그 진입점이 없다
- 자막 파일(ASS) 생성. `[9. assemble]`이 `scenes.timed.json`으로 만든다 (ADR-0020)
- 대본 수정. 오차가 크든 길이가 넘치든 대본은 1부 것이다
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..runstate import RunState
from ..schemas.scenes import validate_scenes
from ..schemas.script_rules import TOTAL_SECONDS
from ..schemas.timed_scenes import build_timed_scenes, validate_timed_scenes
from ..tts.audio import DEFAULT_FFMPEG, DEFAULT_TEMPO, AudioError, write_narration
from ..tts.base import TTSClient
from ..tts.sync import (
    DRIFT_TOLERANCE,
    LINE_JOINER,
    SyncError,
    drift_warnings,
    narration_text,
    scale,
    scene_boundaries,
)

log = logging.getLogger(__name__)

STAGE = "3-tts-sync"

#: 1부↔2부 경계면 파일 (ADR-0017). 이 단계의 유일한 입력이다.
SCRIPT_FILE = "06-script.json"

NARRATION_FILE = "narration.wav"
TIMING_FILE = "timing.json"
TIMED_SCENES_FILE = "scenes.timed.json"

#: specs/01 총 길이 상한. 초과분은 대본 축약 대상이고 그건 1부 몫이다.
MAX_TOTAL_SECONDS = TOTAL_SECONDS[1]

#: 실측 오디오 길이와 씬 마지막 end의 허용 차이(초). 넘으면 [9]의 싱크가 흔들린다.
AUDIO_TOLERANCE = 0.5

#: TTS 단일 호출 타임아웃(초). 570자 한 편 분량 기준.
TIMEOUT = 300


class TTSStageError(Exception):
    pass


@dataclass
class TTSResult:
    topic: str
    slug: str
    run_id: str
    run_dir: Path
    tempo: float = DEFAULT_TEMPO
    scene_count: int = 0
    #: atempo 적용 전 길이(초). 정렬 기준.
    raw_duration: float = 0.0
    #: 배속 보정 후 마지막 씬의 end. scenes.timed.json의 total_duration이다.
    total_duration: float = 0.0
    #: 실제로 쓰인 narration.wav의 길이(초).
    audio_duration: float = 0.0
    narration_path: Path | None = None
    timing_path: Path | None = None
    scenes_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    over_length: bool = False
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.scenes_path is not None

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        head = (
            f"[3] {self.topic} — {self.scene_count}씬 / "
            f"{self.raw_duration:.1f}초 원속 → atempo {self.tempo} → "
            f"{self.total_duration:.1f}초"
        )
        if self.over_length:
            return (
                f"{head} → 상한 {MAX_TOTAL_SECONDS:.0f}초 초과. "
                f"중단 — 대본 축약이 필요하다 (1부 소관){tail}"
            )
        return f"{head} → {TIMED_SCENES_FILE}{tail}"


def _load_script(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise TTSStageError(
            f"대본이 없다: {path}. 1부 [2. validate]가 끝난 토픽만 2부에 들어온다 (ADR-0017)."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TTSStageError(f"{SCRIPT_FILE}을(를) 읽을 수 없다: {path} — {exc}") from exc

    errors, _warnings = validate_scenes(data)
    if errors:
        # 입력이 씬 계약을 어겼다. 고치는 자리는 1부다 (ADR-0017).
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise TTSStageError(f"{path}이(가) 씬 계약을 위반한다:\n{listed}")
    return data


def _outputs(run_dir: Path) -> tuple[Path, Path, Path]:
    return (
        run_dir / NARRATION_FILE,
        run_dir / TIMING_FILE,
        run_dir / TIMED_SCENES_FILE,
    )


def build_timing(
    *,
    source: dict[str, Any],
    boundaries: list[tuple[float, float]],
    narration_meta: dict[str, Any],
    tempo: float,
    raw_duration: float,
    audio_duration: float,
    warnings: list[str],
) -> dict[str, Any]:
    """timing.json 문서 — **이 단계의 실행 기록이다** (ADR-0020).

    하류 단계가 판단 근거로 읽는 계약이 아니다. 씬의 시각을 읽는 곳은
    `scenes.timed.json` 하나뿐이고(ADR-0020 "값 하나의 출처는 하나"), 여기 남는 것은
    (1) 엔진 메타, (2) 배속과 원속 길이 — 계산을 재현할 근거, (3) 경고,
    (4) 총 길이 — **길이 초과로 멈춰 `scenes.timed.json`을 쓰지 않는 경우에도**
    무엇이 얼마나 넘쳤는지 남기기 위한 것이다. 호출은 편당 과금이다.

    `total_duration`은 atempo 적용 후(최종 영상 기준)다. 원속으로 되돌리려면 `tempo`를
    곱하면 된다.

    자막 큐(`cues`)는 담지 않는다. `[9. assemble]`은 전환 규칙이 비트에 걸려 있어
    (스펙 03) 어차피 `scenes.timed.json`을 읽으며, 거기에 `text`·`start`·`end`가
    전부 있다.
    """
    return {
        "run_id": source["run_id"],
        "topic": source["topic"],
        "engine": narration_meta,
        "tempo": tempo,
        "raw_duration": round(raw_duration, 3),
        "total_duration": boundaries[-1][1] if boundaries else 0.0,
        "audio": {
            "path": NARRATION_FILE,
            "duration": round(audio_duration, 3),
        },
        "warnings": warnings,
    }


def run_tts_stage(
    slug: str,
    *,
    tts: TTSClient,
    paths: Paths | None = None,
    tempo: float = DEFAULT_TEMPO,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner=subprocess.run,
) -> TTSResult:
    paths = paths or Paths.from_env()

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    source = _load_script(script_path)

    # 계보는 run_id로 잇는다 (ADR-0017). 대본이 자기 run_id를 들고 있으므로
    # 1부의 topic.json을 찾아갈 이유가 없다.
    run_id = source["run_id"]
    topic = source["topic"]
    run_dir = paths.run_dir(run_id)
    narration_path, timing_path, scenes_path = _outputs(run_dir)

    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and all(
        p.exists() for p in (narration_path, timing_path, scenes_path)
    ):
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        return TTSResult(
            topic=topic, slug=slug, run_id=run_id, run_dir=run_dir,
            tempo=timing.get("tempo", tempo),
            scene_count=len(source["scenes"]),
            raw_duration=timing.get("raw_duration", 0.0),
            total_duration=timing.get("total_duration", 0.0),
            audio_duration=timing.get("audio", {}).get("duration", 0.0),
            narration_path=narration_path, timing_path=timing_path,
            scenes_path=scenes_path, warnings=timing.get("warnings", []),
            skipped=True,
        )

    state.mark_running(STAGE)

    # 지난 실행이 남긴 계약 파일을 먼저 치운다. 이번 실행이 실패하거나 길이 초과로
    # 멈추면 새 narration.wav와 짝이 맞지 않는 옛 타임스탬프가 남고, 하류 단계는
    # 파일이 있다는 이유로 그대로 진행해 버린다.
    scenes_path.unlink(missing_ok=True)

    texts = [scene["text"] for scene in source["scenes"]]
    text = narration_text(texts, joiner=LINE_JOINER)
    log.info("[%s] 단일 호출 %d자 / %d씬 (ADR-0004)", STAGE, len(text), len(texts))

    narration = tts.synthesize(text, timeout=TIMEOUT, label=STAGE)

    try:
        raw_boundaries, warnings = scene_boundaries(
            narration.alignment, texts, joiner=LINE_JOINER
        )
    except SyncError as exc:
        state.mark_failed(STAGE, str(exc))
        raise TTSStageError(str(exc)) from exc

    # specs/05 — atempo 적용 후 타임스탬프도 1/tempo 스케일 보정
    boundaries = scale(raw_boundaries, 1.0 / tempo)
    raw_duration = narration.raw_duration
    total_duration = boundaries[-1][1]

    try:
        audio_duration = write_narration(
            narration, narration_path, tempo=tempo, executable=ffmpeg, runner=runner
        )
    except AudioError as exc:
        state.mark_failed(STAGE, str(exc))
        raise TTSStageError(str(exc)) from exc

    if abs(audio_duration - total_duration) > AUDIO_TOLERANCE:
        warnings.append(
            f"{NARRATION_FILE} 길이 {audio_duration:.2f}초와 씬 총 길이 "
            f"{total_duration:.2f}초의 차이가 {AUDIO_TOLERANCE}초를 넘는다"
        )

    warnings.extend(
        drift_warnings(boundaries, source["scenes"], tolerance=DRIFT_TOLERANCE)
    )

    timed = build_timed_scenes(source, boundaries)
    errors, timed_warnings = validate_timed_scenes(timed)
    warnings.extend(timed_warnings)

    # timing.json은 길이 초과로 멈추는 경우에도 남긴다 — 호출은 편당 과금이고,
    # 무엇이 얼마나 넘쳤는지는 이 파일에서 읽는다.
    timing = build_timing(
        source=source, boundaries=boundaries, narration_meta=narration.meta,
        tempo=tempo, raw_duration=raw_duration, audio_duration=audio_duration,
        warnings=warnings,
    )
    write_text(timing_path, dump_json(timing))

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result = TTSResult(
        topic=topic, slug=slug, run_id=run_id, run_dir=run_dir, tempo=tempo,
        scene_count=len(texts), raw_duration=raw_duration,
        total_duration=total_duration, audio_duration=audio_duration,
        narration_path=narration_path, timing_path=timing_path, warnings=warnings,
    )

    info: dict[str, Any] = {
        "scene_count": len(texts),
        "tempo": tempo,
        "raw_duration": round(raw_duration, 3),
        "total_duration": total_duration,
        "audio_duration": round(audio_duration, 3),
        "warnings": warnings,
        "outputs": [
            p.relative_to(paths.root).as_posix() for p in (narration_path, timing_path)
        ],
    }

    if errors:
        # 우리가 만든 문서가 계약을 어겼다는 뜻이라 하류로 넘기지 않는다.
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        message = f"{TIMED_SCENES_FILE}이 씬 계약을 위반한다:\n{listed}"
        state.mark_failed(STAGE, message, **info)
        raise TTSStageError(message)

    if total_duration > MAX_TOTAL_SECONDS:
        # specs/05 — 리포트하고 멈춘다. 재생성은 사람이 1부를 다시 실행해 판단한다.
        message = (
            f"총 길이 {total_duration:.2f}초가 상한 {MAX_TOTAL_SECONDS:.0f}초를 넘는다. "
            f"대본 축약이 필요하다 — 1부에서 {SCRIPT_FILE}을 다시 만들어야 한다 (ADR-0017). "
            f"{TIMED_SCENES_FILE}은 쓰지 않았다"
        )
        state.mark_failed(STAGE, message, **info)
        log.warning("[%s] %s", STAGE, message)
        result.over_length = True
        return result

    write_text(scenes_path, dump_json(timed))
    info["outputs"].append(scenes_path.relative_to(paths.root).as_posix())
    state.mark_done(STAGE, **info)

    result.scenes_path = scenes_path
    return result
