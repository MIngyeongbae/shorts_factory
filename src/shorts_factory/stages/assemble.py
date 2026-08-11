"""[9. assemble] — 클립 + 씬 계약 → 자막이 박힌 타임라인 영상.

specs/05-pipeline.md:
    [9. assemble] → timeline.mp4 (FFmpeg: 디졸브 + 자막 번인)

    "자막은 **scenes.timed.json** 기준 ASS 생성 후 번인 (ADR-0020 — 씬의 `text`·
     `start`·`end`를 읽는 곳은 이 파일 하나다). 전환 규칙(스펙 03)이 비트에 걸려 있어
     같은 파일의 `beat`을 함께 본다. 싱크 오차 ±200ms 이내 검증."

## 입력 (ADR-0020)

| 파일 | 무엇을 읽는가 |
|---|---|
| `runs/{run_id}/scenes.timed.json` | `scene_id`·`beat`·`text`·`start`·`end` |
| `runs/{run_id}/clips/{scene_id}.mp4` | `[7. motion]`의 클립. 존재 여부만 본다 |

`timing.json`은 열지 않는다 — `[3]`의 실행 기록이지 계약이 아니다. `prompts.json`도
이 단계의 입력이 아니다. `06-script.json`은 `--slug`로 run을 찾을 때 `run_id` 한 줄만
읽고(ADR-0017 "계보는 run_id로 잇는다"), `--run-id`를 직접 주면 그마저도 열지 않는다.

## 산출물

- `runs/{run_id}/subtitles.ass` — 번인에 쓴 자막 원본. 사람이 열어 고칠 수 있게 남긴다
- `runs/{run_id}/timeline.mp4` — 소리 없는 영상. TTS·SFX·BGM은 `[10. mix]`가 붙인다

## 이 단계가 하지 않는 것

- **대형 숫자·파티클 합성.** 그건 `[8. overlay]`의 레이어 B다 (스펙 03 오버레이 표)
- **차임·whoosh 동기화.** 하드컷 지점을 상태에 남기기만 한다. 오디오는 스펙 04와
  `[10. mix]` 소관이다
- **클립 길이 검증.** "클립 길이 = 씬 길이 + 디졸브 겹침 0.6초"는 `[7]`의 계약이고,
  실측하려면 ffprobe가 필요하다. 이 단계는 계약을 믿고 `trim`으로 잘라 쓴다
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..runstate import RunState
from ..schemas.timed_scenes import validate_timed_scenes
from ..video.ffmpeg import (
    DEFAULT_FFMPEG,
    FFmpegError,
    build_command,
    build_filter_graph,
    escape_filter_path,
    relative_path,
    run_ffmpeg,
)
from ..video.subtitles import FONTS_DIR, build_ass, parse_ass
from ..video.timeline import (
    DISSOLVE,
    HARD_CUT,
    Timeline,
    TimelineError,
    build_timeline,
)
from ..video.verify import SYNC_TOLERANCE, check_sync

log = logging.getLogger(__name__)

STAGE = "9-assemble"

#: 1부↔2부 경계면 파일 (ADR-0017). `--slug`로 run을 찾을 때만 연다.
SCRIPT_FILE = "06-script.json"

TIMED_SCENES_FILE = "scenes.timed.json"
CLIPS_DIR = "clips"
SUBTITLES_FILE = "subtitles.ass"
TIMELINE_FILE = "timeline.mp4"

#: libass가 읽는 폰트 파일 확장자
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")


class AssembleStageError(Exception):
    pass


@dataclass
class AssembleResult:
    run_id: str
    run_dir: Path
    topic: str
    scene_count: int = 0
    total_duration: float = 0.0
    dissolves: int = 0
    cuts: int = 0
    #: 하드컷 진입 씬. `[10. mix]`의 차임 동기화 지점이다 (스펙 04)
    cut_scene_ids: tuple[int, ...] = ()
    max_drift: float = 0.0
    subtitles_path: Path | None = None
    timeline_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.timeline_path is not None

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[9] {self.topic} — {self.scene_count}씬 / {self.total_duration:.1f}초 / "
            f"디졸브 {self.dissolves} · 하드컷 {self.cuts} / "
            f"싱크 오차 최대 {self.max_drift * 1000:.0f}ms "
            f"(허용 ±{SYNC_TOLERANCE * 1000:.0f}ms) → {TIMELINE_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssembleStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 경계면 파일에서 `run_id`를 읽는다.

    1부의 `topic.json`을 뒤지지 않는다 — 2부는 `06-script.json`과 `judgment/human.json`
    외의 1부 산출물에 의존하지 않는다 (ADR-0017).
    """
    if run_id:
        return run_id
    if not slug:
        raise AssembleStageError("run_id 또는 slug 중 하나는 있어야 한다")

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise AssembleStageError(
            f"대본이 없다: {script_path}. --run-id로 run을 직접 지정할 수도 있다."
        )
    script = _load_json(script_path, f"씬 계약({SCRIPT_FILE})")
    resolved = script.get("run_id")
    if not resolved:
        raise AssembleStageError(f"{script_path}에 run_id가 없다")
    return resolved


def _load_timed_scenes(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AssembleStageError(
            f"{TIMED_SCENES_FILE}이 없다: {path}. [3. tts+sync]를 먼저 실행하라 — "
            "총 길이가 102초를 넘어 멈춘 run에는 이 파일이 일부러 없다 (ADR-0017)."
        )
    data = _load_json(path, TIMED_SCENES_FILE)
    errors, warnings = validate_timed_scenes(data)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise AssembleStageError(f"{path}이(가) 씬 계약을 위반한다:\n{listed}")
    return {"data": data, "warnings": warnings}


def _clip_paths(run_dir: Path, timeline: Timeline) -> list[Path]:
    """씬 순서대로 클립 경로. 하나라도 없으면 멈춘다.

    씬 단위 폴백(specs/05 실패 정책)은 `[6]`·`[7]`의 몫이다. 여기까지 클립이 안 왔다는
    것은 그 폴백도 실패했다는 뜻이라, 구멍 난 타임라인을 만드는 대신 멈춘다.
    """
    clips_dir = run_dir / CLIPS_DIR
    paths = [clips_dir / segment.clip_name for segment in timeline.segments]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise AssembleStageError(
            f"클립 {len(missing)}개가 없다 ({', '.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}). "
            f"[7. motion]이 {clips_dir}에 씬마다 하나씩 놓는다 (specs/05)."
        )
    return paths


def _fonts_dir(paths: Paths) -> tuple[str | None, str | None]:
    """`(libass에 줄 폰트 디렉터리, 경고)`.

    ADR-0002가 레이어 B 폰트를 리포지토리 에셋으로 두기로 했다. 없으면 libass가 시스템
    폰트로 떨어져 같은 대본이 기계마다 다른 룩으로 렌더된다 — 막지는 않고 경고한다.
    """
    fonts = paths.root / FONTS_DIR
    if fonts.is_dir() and any(
        p.suffix.lower() in FONT_SUFFIXES for p in fonts.iterdir()
    ):
        return str(fonts), None
    return None, (
        f"{FONTS_DIR}/에 자막 폰트가 없다. libass가 시스템 폰트로 떨어져 "
        "스펙 03의 '굵은 고딕'이 보장되지 않는다 (ADR-0002 — 레이어 B 폰트는 "
        "리포지토리 에셋이다)"
    )


def run_assemble_stage(
    run_id: str,
    *,
    paths: Paths | None = None,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner=subprocess.run,
) -> AssembleResult:
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    loaded = _load_timed_scenes(run_dir / TIMED_SCENES_FILE)
    document: dict[str, Any] = loaded["data"]
    if document["run_id"] != run_id:
        # 계보는 run_id로 잇는다 (ADR-0017). 다른 대본의 타임스탬프로 조립하면
        # 자막과 클립이 통째로 어긋난 영상이 조용히 나온다.
        raise AssembleStageError(
            f"{TIMED_SCENES_FILE}의 run_id({document['run_id']})가 "
            f"조립 대상 run({run_id})과 다르다"
        )
    scenes: list[dict[str, Any]] = document["scenes"]
    topic = document["topic"]
    total_duration = float(document["total_duration"])

    subtitles_path = run_dir / SUBTITLES_FILE
    timeline_path = run_dir / TIMELINE_FILE

    state = RunState.load_or_create(run_dir, run_id, topic=topic)

    if (
        state.is_done(STAGE)
        and not force
        and subtitles_path.exists()
        and timeline_path.exists()
    ):
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        info = state.stage(STAGE)
        return AssembleResult(
            run_id=run_id, run_dir=run_dir, topic=topic,
            scene_count=len(scenes), total_duration=total_duration,
            dissolves=info.get("dissolves", 0), cuts=info.get("cuts", 0),
            cut_scene_ids=tuple(info.get("cut_scene_ids", ())),
            max_drift=info.get("max_drift", 0.0),
            subtitles_path=subtitles_path, timeline_path=timeline_path,
            warnings=info.get("warnings", []), skipped=True,
        )

    state.mark_running(STAGE)

    # 지난 실행이 남긴 영상을 먼저 치운다. 이번 실행이 실패하면 새 자막과 짝이 맞지
    # 않는 옛 영상이 남고, [10. mix]는 파일이 있다는 이유로 그대로 진행해 버린다.
    timeline_path.unlink(missing_ok=True)

    warnings: list[str] = list(loaded["warnings"])

    try:
        timeline = build_timeline(scenes)
    except TimelineError as exc:
        state.mark_failed(STAGE, str(exc))
        raise AssembleStageError(str(exc)) from exc

    clips = _clip_paths(run_dir, timeline)

    ass_document, ass_warnings = build_ass(scenes)
    warnings.extend(ass_warnings)
    write_text(subtitles_path, ass_document)

    # 만든 자막을 되읽어 검증한다. 만들 때 쓴 숫자가 아니라 파일에 남은 숫자를 본다.
    report = check_sync(
        scenes,
        cues=parse_ass(subtitles_path.read_text(encoding="utf-8")),
        timeline=timeline,
        total_duration=total_duration,
    )
    if not report.passed:
        listed = "\n".join(f"  - {e}" for e in report.errors[:5])
        message = f"싱크가 계약에서 벗어났다 (specs/00 성공 기준 4):\n{listed}"
        state.mark_failed(STAGE, message, max_drift=report.max_drift)
        raise AssembleStageError(message)

    fonts_dir, font_warning = _fonts_dir(paths)
    if font_warning:
        warnings.append(font_warning)

    graph = build_filter_graph(
        timeline,
        subtitles=escape_filter_path(subtitles_path, run_dir),
        fontsdir=escape_filter_path(fonts_dir, run_dir) if fonts_dir else None,
    )
    cmd = build_command(
        timeline,
        inputs=[relative_path(p, run_dir) for p in clips],
        filter_graph=graph,
        output=TIMELINE_FILE,
        executable=ffmpeg,
    )

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)
    log.info(
        "[%s] 클립 %d개 / 디졸브 %d · 하드컷 %d / %.1f초",
        STAGE, len(clips), timeline.counts[DISSOLVE], timeline.counts[HARD_CUT],
        timeline.total_duration,
    )

    try:
        run_ffmpeg(cmd, cwd=run_dir, produces=timeline_path, runner=runner)
    except FFmpegError as exc:
        state.mark_failed(STAGE, str(exc))
        raise AssembleStageError(str(exc)) from exc

    info = {
        "scene_count": len(scenes),
        "total_duration": timeline.total_duration,
        "dissolves": timeline.counts[DISSOLVE],
        "cuts": timeline.counts[HARD_CUT],
        "cut_scene_ids": list(timeline.cut_scene_ids),
        "max_drift": report.max_drift,
        "warnings": warnings,
        "outputs": [
            p.relative_to(paths.root).as_posix()
            for p in (subtitles_path, timeline_path)
        ],
    }
    state.mark_done(STAGE, **info)

    return AssembleResult(
        run_id=run_id, run_dir=run_dir, topic=topic,
        scene_count=len(scenes), total_duration=timeline.total_duration,
        dissolves=timeline.counts[DISSOLVE], cuts=timeline.counts[HARD_CUT],
        cut_scene_ids=timeline.cut_scene_ids, max_drift=report.max_drift,
        subtitles_path=subtitles_path, timeline_path=timeline_path,
        warnings=warnings,
    )
