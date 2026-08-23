"""[9. assemble] — 클립 + 씬 계약 → 자막이 박힌 타임라인 영상, **언어당 1회**. ADR-0056.

specs/05-pipeline.md:
    [9. assemble] → timeline.{lang}.mp4 (언어당 1회 — 같은 clips/, 언어별 실측으로 트림,
                    디졸브 + 언어별 자막 번인 + 엔딩 하드컷)

    "입력은 같은 `clips/`와 그 언어의 `scenes.timed.{lang}.json`·`narration.{lang}.wav`,
     출력은 `subtitles.{lang}.ass`·`timeline.{lang}.mp4`. 씬마다 클립을 그 언어의 씬 길이 +
     0.6초로 트림한다 — 클립은 세 언어의 최장으로 만들어졌으므로 트림은 항상 앞에서
     자르고 꼬리를 버리는 방향이다. 클립이 씬보다 짧으면(10초 클램프) 마지막 프레임을
     정지로 늘리고 경고한다. … 폰트는 언어별이다. … 싱크 오차 ±200ms 이내 검증."

## 입력 (ADR-0020)

| 파일 | 무엇을 읽는가 |
|---|---|
| `runs/{run_id}/scenes.timed.{lang}.json` | `scene_id`·`text`·`start`·`end` (+ `scenes.json`의 `beat`·`transition`을 메모리 병합) |
| `runs/{run_id}/narration.{lang}.wav` | 그 언어의 나레이션. 있으면 싣는다 |
| `runs/{run_id}/clips/{scene_id}.mp4` | `[7. videogen]`의 클립. 존재 여부만 본다 |
| `runs/{run_id}/clips.json` | **선택.** `[7]`의 기록 — 클램프된 씬(클립 < 씬 길이)을 알아 정지로 늘린다 |
| `runs/{run_id}/ending.json` | **선택.** `[8. ending]`의 엔딩 실사 컷 (ADR-0055) |

`timing.{lang}.json`·`prompts.json`은 열지 않는다. 씬 계약은 `--slug`로 run을 찾을 때
`run_id` 한 줄만 읽는다.

## 언어 루프

실측 파일이 있는 언어마다 돈다 (ko 필수, ja·en은 있으면 — D-3). 클립 풀은 하나이고
자막·트림·나레이션만 언어별이다. 한 언어의 실패는 그 언어에서 끝난다 — 나머지는 돈다.

## 엔딩은 붙이되 검증하지 않는다 (ADR-0055)

`ending.json`이 있으면 마지막 씬 뒤에 **하드컷**으로 잇고 컷 사이는 디졸브다. 세 언어가
같은 엔딩 클립을 쓴다. **자막과 싱크 검증(±200ms)은 씬 구간만 본다.**

## 산출물

- `runs/{run_id}/subtitles.{lang}.ass` — 번인에 쓴 자막 원본
- `runs/{run_id}/timeline.{lang}.mp4` — 그 언어의 나레이션이 실린 영상. SFX·BGM은 `[10. mix]`
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..config import Paths, write_text
from ..runstate import RunState
from .contract import (
    SceneContractNotFound,
    TimedScenesNotFound,
    load_merged_scenes,
    load_scene_contract,
)
from ..schemas import ending as ending_schema
from ..schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE, present_languages
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
from ..video.subtitles import (
    FONT_SUFFIXES,
    FONTS_DIR,
    build_ass,
    font_name_for,
    parse_ass,
)
from ..video.timeline import (
    CLIPS_DIR,
    DISSOLVE,
    HARD_CUT,
    Timeline,
    TimelineError,
    build_timeline,
    extend_with_ending,
)
from ..video.verify import SYNC_TOLERANCE, check_sync

log = logging.getLogger(__name__)

STAGE = "9-assemble"

SUBTITLES_PATTERN = "subtitles.{lang}.ass"
TIMELINE_PATTERN = "timeline.{lang}.mp4"
NARRATION_PATTERN = "narration.{lang}.wav"

#: 선택적 입력 — `[7]`의 실행 기록. 클램프된 씬을 알기 위해서만 연다 (D-3).
CLIPS_RECORD_FILE = "clips.json"

#: 선택적 입력 — `[8. ending]`의 엔딩 실사 컷 계약 (ADR-0055). 부재는 경고가 아니다.
ENDING_FILE = ending_schema.RECORD_FILE
ENDING_DIR = ending_schema.ENDING_DIR


class AssembleStageError(Exception):
    pass


@dataclass
class LanguageAssembly:
    lang: str
    scene_count: int = 0
    total_duration: float = 0.0
    narration_duration: float = 0.0
    dissolves: int = 0
    cuts: int = 0
    #: 하드컷 진입 씬. `[10. mix]`의 thump 동기화 지점이다 (스펙 04)
    cut_scene_ids: tuple[int, ...] = ()
    max_drift: float = 0.0
    ending_cuts: int = 0
    padded_scene_ids: tuple[int, ...] = ()
    font_name: str = ""
    narration_muxed: bool = False
    subtitles_path: Path | None = None
    timeline_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.timeline_path is not None

    @property
    def line(self) -> str:
        ending = f" / 엔딩 {self.ending_cuts}컷" if self.ending_cuts else ""
        return (
            f"{self.lang} {self.scene_count}씬 {self.total_duration:.1f}초 "
            f"(디졸브 {self.dissolves} · 하드컷 {self.cuts}{ending}, "
            f"싱크 오차 최대 {self.max_drift * 1000:.0f}ms){' (스킵)' if self.skipped else ''}"
        )


@dataclass
class AssembleResult:
    run_id: str
    run_dir: Path
    topic: str
    languages: dict[str, LanguageAssembly] = field(default_factory=dict)

    @property
    def primary(self) -> LanguageAssembly:
        return self.languages[PRIMARY_LANGUAGE]

    # --- ko 편의 속성 -------------------------------------------------------
    @property
    def scene_count(self) -> int:
        return self.primary.scene_count

    @property
    def total_duration(self) -> float:
        return self.primary.total_duration

    @property
    def dissolves(self) -> int:
        return self.primary.dissolves

    @property
    def cuts(self) -> int:
        return self.primary.cuts

    @property
    def cut_scene_ids(self) -> tuple[int, ...]:
        return self.primary.cut_scene_ids

    @property
    def max_drift(self) -> float:
        return self.primary.max_drift

    @property
    def ending_cuts(self) -> int:
        return self.primary.ending_cuts

    @property
    def subtitles_path(self) -> Path | None:
        return self.primary.subtitles_path

    @property
    def timeline_path(self) -> Path | None:
        return self.primary.timeline_path

    @property
    def warnings(self) -> list[str]:
        return [
            w if len(self.languages) == 1 else f"[{lang}] {w}"
            for lang, result in self.languages.items() for w in result.warnings
        ]

    @property
    def skipped(self) -> bool:
        return bool(self.languages) and all(r.skipped for r in self.languages.values())

    @property
    def passed(self) -> bool:
        return bool(self.languages) and all(r.passed for r in self.languages.values())

    @property
    def summary(self) -> str:
        lines = " · ".join(r.line for r in self.languages.values())
        return (
            f"[9] {self.topic} — {lines} (허용 ±{SYNC_TOLERANCE * 1000:.0f}ms) "
            f"→ timeline.{{{','.join(self.languages)}}}.mp4"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssembleStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 씬 계약에서 `run_id`를 읽는다 (ADR-0017)."""
    if run_id:
        return run_id
    if not slug:
        raise AssembleStageError("run_id 또는 slug 중 하나는 있어야 한다")

    try:
        contract, _path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise AssembleStageError(str(exc)) from exc
    resolved = contract.get("run_id")
    if not resolved:
        raise AssembleStageError(f"씬 계약에 run_id가 없다 (slug={slug})")
    return str(resolved)


def _load_timed_scenes(run_dir: Path, lang: str) -> dict[str, Any]:
    """그 언어의 실측(시각·text) + 씬 계약(전환 등)의 메모리 병합본 (ADR-0052)."""
    try:
        data = load_merged_scenes(run_dir, lang)
    except (TimedScenesNotFound, SceneContractNotFound) as exc:
        raise AssembleStageError(str(exc)) from exc
    errors, warnings = validate_timed_scenes(data)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise AssembleStageError(
            f"scenes.timed.{lang}.json이(가) 씬 계약을 위반한다:\n{listed}"
        )
    return {"data": data, "warnings": warnings}


def _clip_paths(run_dir: Path, timeline: Timeline) -> list[Path]:
    """타임라인 순서대로 클립 경로. 하나라도 없으면 멈춘다.

    씬 단위 폴백(specs/05 실패 정책)은 `[7]`의 몫이다. 여기까지 클립이 안 왔다는
    것은 그 폴백도 실패했다는 뜻이라, 구멍 난 타임라인을 만드는 대신 멈춘다.

    엔딩 컷은 `[8]`이 `ending/`에 놓으므로 구간마다 `source_dir`을 따라간다 (ADR-0055).
    """
    paths = [
        run_dir / segment.source_dir / segment.clip_name
        for segment in timeline.segments
    ]
    missing = [
        f"{segment.source_dir}/{segment.clip_name}"
        for segment, path in zip(timeline.segments, paths)
        if not path.exists()
    ]
    if missing:
        producer = (
            f"[7. videogen]이 {CLIPS_DIR}/에 씬마다 하나씩 놓는다"
            if all(m.startswith(f"{CLIPS_DIR}/") for m in missing)
            else "[7. videogen]과 [8. ending]이 각자의 디렉터리에 놓는다"
        )
        raise AssembleStageError(
            f"클립 {len(missing)}개가 없다 ({', '.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}). {producer} (specs/05)."
        )
    return paths


def _load_ending(run_dir: Path, warnings: list[str]) -> list[float]:
    """`ending.json` → 컷마다의 표시 초. 파일이 없으면 빈 목록이다 (D-3).

    **계약을 어긴 문서는 붙이지 않는다.** 깨진 엔딩 때문에 완성 영상 전체를 잃는 것보다
    엔딩 없이 나가는 쪽이 낫다 — 엔딩은 마감이지 본편이 아니다 (specs/05 D-5).
    """
    path = run_dir / ENDING_FILE
    if not path.exists():
        return []

    document = _load_json(path, ENDING_FILE)
    errors = ending_schema.validate_ending(document)
    if errors:
        warnings.append(
            f"{ENDING_FILE}이 계약을 어겨 엔딩을 붙이지 않았다 ({'; '.join(errors[:3])}). "
            "[8. ending]을 --force로 다시 돌린다"
        )
        return []

    photos = document.get("photos", [])
    missing = [p["file"] for p in photos if not (run_dir / p["file"]).exists()]
    if missing:
        warnings.append(
            f"엔딩 클립 {len(missing)}개가 없어 엔딩을 붙이지 않았다 "
            f"({', '.join(missing[:3])}). [8. ending]을 --force로 다시 돌린다"
        )
        return []

    return [float(photo["seconds"]) for photo in photos]


def _clip_seconds(run_dir: Path) -> dict[int, float]:
    """`clips.json`의 씬별 클립 길이 — **선택적 입력이다** (D-3). 없거나 깨지면 빈 dict."""
    path = run_dir / CLIPS_RECORD_FILE
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[int, float] = {}
    for entry in record.get("scenes", []):
        try:
            out[int(entry["scene_id"])] = float(entry["seconds"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def padded_indices(
    timeline: Timeline, clip_seconds: dict[int, float], *, tolerance: float = 0.001
) -> list[int]:
    """클립이 그 언어의 씬 길이 + 꼬리보다 짧은 입력 번호 — 마지막 프레임을 정지로 늘린다 (스펙 05 `[9]`)."""
    out: list[int] = []
    for index, segment in enumerate(timeline.segments):
        if not segment.is_scene:
            continue
        known = clip_seconds.get(segment.scene_id)
        if known is not None and known + tolerance < segment.clip_length:
            out.append(index)
    return out


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


def _language_state(state: RunState, lang: str) -> dict[str, Any]:
    return state.stage(STAGE).setdefault("languages", {}).setdefault(lang, {"status": "pending"})


def run_assemble_stage(
    run_id: str,
    *,
    paths: Paths | None = None,
    langs: Sequence[str] | None = None,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner=subprocess.run,
) -> AssembleResult:
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    present = present_languages(run_dir)
    if PRIMARY_LANGUAGE not in present:
        raise AssembleStageError(
            f"scenes.timed.{PRIMARY_LANGUAGE}.json이 없다: {run_dir}. [3. tts+sync]를 먼저 실행하라 — "
            "총 길이가 상한을 넘어 멈춘 run에는 이 파일이 일부러 없다 (ADR-0017)."
        )
    wanted = list(langs) if langs else present
    unknown = [l for l in wanted if l not in LANGUAGES]
    if unknown:
        raise AssembleStageError(f"모르는 언어다: {unknown} (가능: {', '.join(LANGUAGES)})")
    absent = [l for l in wanted if l not in present]
    if absent:
        raise AssembleStageError(
            f"실측 파일이 없는 언어다: {', '.join(f'scenes.timed.{l}.json' for l in absent)} — "
            "[3. tts]가 그 언어를 돌지 않았다"
        )
    selected = [l for l in LANGUAGES if l in wanted]

    # 소재명은 ko 실측에서 — 언어와 무관하다.
    primary_doc = _load_timed_scenes(run_dir, PRIMARY_LANGUAGE)["data"]
    if primary_doc["run_id"] != run_id:
        # 계보는 run_id로 잇는다 (ADR-0017). 다른 대본의 타임스탬프로 조립하면
        # 자막과 클립이 통째로 어긋난 영상이 조용히 나온다.
        raise AssembleStageError(
            f"scenes.timed.{PRIMARY_LANGUAGE}.json의 run_id({primary_doc['run_id']})가 "
            f"조립 대상 run({run_id})과 다르다"
        )
    topic = primary_doc["topic"]
    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    result = AssembleResult(run_id=run_id, run_dir=run_dir, topic=topic)

    fonts_dir, font_warning = _fonts_dir(paths)
    clip_seconds = _clip_seconds(run_dir)

    to_run: list[str] = []
    for lang in selected:
        subtitles_path = run_dir / SUBTITLES_PATTERN.format(lang=lang)
        timeline_path = run_dir / TIMELINE_PATTERN.format(lang=lang)
        lang_state = _language_state(state, lang)
        if (
            lang_state.get("status") == "done" and not force
            and subtitles_path.exists() and timeline_path.exists()
        ):
            log.info("[%s] %s는 이미 완료돼 스킵한다 (run_id=%s)", STAGE, lang, run_id)
            result.languages[lang] = LanguageAssembly(
                lang=lang, scene_count=lang_state.get("scene_count", 0),
                total_duration=lang_state.get("total_duration", 0.0),
                narration_duration=lang_state.get("narration_duration", 0.0),
                dissolves=lang_state.get("dissolves", 0), cuts=lang_state.get("cuts", 0),
                cut_scene_ids=tuple(lang_state.get("cut_scene_ids", ())),
                max_drift=lang_state.get("max_drift", 0.0),
                ending_cuts=lang_state.get("ending_cuts", 0),
                padded_scene_ids=tuple(lang_state.get("padded_scene_ids", ())),
                font_name=lang_state.get("font_name", ""),
                narration_muxed=bool(lang_state.get("narration_muxed")),
                subtitles_path=subtitles_path, timeline_path=timeline_path,
                warnings=lang_state.get("warnings", []), skipped=True,
            )
            continue
        to_run.append(lang)

    if not to_run:
        return result

    state.mark_running(STAGE)
    try:
        for lang in to_run:
            result.languages[lang] = _assemble_language(
                lang=lang, run_id=run_id, run_dir=run_dir, paths=paths, state=state,
                fonts_dir=fonts_dir, font_warning=font_warning, clip_seconds=clip_seconds,
                ffmpeg=ffmpeg, runner=runner,
            )
    except AssembleStageError as exc:
        state.mark_failed(STAGE, str(exc), languages=state.stage(STAGE).get("languages", {}))
        raise

    langs_info = state.stage(STAGE).get("languages", {})
    info = {
        "languages": langs_info,
        "scene_count": result.scene_count if PRIMARY_LANGUAGE in result.languages else 0,
        "warnings": result.warnings,
        "outputs": sorted(
            p for lang_info in langs_info.values() for p in lang_info.get("outputs", [])
        ),
    }
    state.mark_done(STAGE, **info)
    return result


def _assemble_language(
    *, lang: str, run_id: str, run_dir: Path, paths: Paths, state: RunState,
    fonts_dir: str | None, font_warning: str | None, clip_seconds: dict[int, float],
    ffmpeg: str, runner,
) -> LanguageAssembly:
    subtitles_path = run_dir / SUBTITLES_PATTERN.format(lang=lang)
    timeline_path = run_dir / TIMELINE_PATTERN.format(lang=lang)
    narration_path = run_dir / NARRATION_PATTERN.format(lang=lang)
    lang_state = _language_state(state, lang)
    lang_state["status"] = "running"
    lang_state.pop("error", None)
    state.save()

    def fail(message: str) -> AssembleStageError:
        lang_state["status"] = "failed"
        lang_state["error"] = message
        state.save()
        return AssembleStageError(f"[{lang}] {message}")

    try:
        loaded = _load_timed_scenes(run_dir, lang)
    except AssembleStageError as exc:
        raise fail(str(exc)) from exc
    document: dict[str, Any] = loaded["data"]
    if document["run_id"] != run_id:
        raise fail(
            f"scenes.timed.{lang}.json의 run_id({document['run_id']})가 조립 대상 run({run_id})과 다르다"
        )
    scenes: list[dict[str, Any]] = document["scenes"]
    total_duration = float(document["total_duration"])

    # 지난 실행이 남긴 영상을 먼저 치운다. 이번 실행이 실패하면 새 자막과 짝이 맞지
    # 않는 옛 영상이 남고, [10. mix]는 파일이 있다는 이유로 그대로 진행해 버린다.
    timeline_path.unlink(missing_ok=True)

    warnings: list[str] = list(loaded["warnings"])

    try:
        timeline = build_timeline(scenes)
    except TimelineError as exc:
        raise fail(str(exc)) from exc

    font_name = font_name_for(lang)
    ass_document, ass_warnings = build_ass(scenes, font_name=font_name, lang=lang)
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
        lang_state["max_drift"] = report.max_drift
        raise fail(f"싱크가 계약에서 벗어났다 (specs/00 성공 기준 4):\n{listed}")

    if font_warning:
        warnings.append(font_warning)

    # 검증이 끝난 뒤에 엔딩을 붙인다 — 싱크 검증의 대조 대상은 씬 구간이고, 엔딩에는
    # 자막 큐도 실측 시각도 없다 (ADR-0055).
    ending_lengths = _load_ending(run_dir, warnings)
    full = extend_with_ending(timeline, ending_lengths, source_dir=ENDING_DIR)
    try:
        clips = _clip_paths(run_dir, full)
    except AssembleStageError as exc:
        raise fail(str(exc)) from exc

    pads = padded_indices(full, clip_seconds)
    padded_ids = tuple(full.segments[i].scene_id for i in pads)
    for index in pads:
        segment = full.segments[index]
        warnings.append(
            f"씬 {segment.scene_id}: 클립 {clip_seconds[segment.scene_id]:.1f}초가 씬 길이 + 꼬리 "
            f"{segment.clip_length:.1f}초보다 짧아 마지막 프레임을 정지로 늘린다 ([7]의 10초 클램프)"
        )

    narration_muxed = narration_path.exists()
    if not narration_muxed:
        warnings.append(
            f"{narration_path.name}이 없어 소리 없는 영상으로 낸다 — [3]이 이 언어를 돌았는지 확인하라"
        )

    graph = build_filter_graph(
        full,
        subtitles=escape_filter_path(subtitles_path, run_dir),
        fontsdir=escape_filter_path(fonts_dir, run_dir) if fonts_dir else None,
        pad_indices=pads,
    )
    cmd = build_command(
        full,
        inputs=[relative_path(p, run_dir) for p in clips],
        filter_graph=graph,
        output=timeline_path.name,
        executable=ffmpeg,
        audio=relative_path(narration_path, run_dir) if narration_muxed else None,
    )

    for warning in warnings:
        log.warning("[%s] %s: %s", STAGE, lang, warning)
    log.info(
        "[%s] %s 클립 %d개 (엔딩 %d) / 디졸브 %d · 하드컷 %d / %.1f초",
        STAGE, lang, len(clips), len(ending_lengths),
        timeline.counts[DISSOLVE], timeline.counts[HARD_CUT],
        full.total_duration,
    )

    try:
        run_ffmpeg(cmd, cwd=run_dir, produces=timeline_path, runner=runner)
    except FFmpegError as exc:
        raise fail(str(exc)) from exc

    lang_state.update({
        "status": "done",
        "scene_count": len(scenes),
        "total_duration": full.total_duration,
        "narration_duration": timeline.total_duration,
        "dissolves": timeline.counts[DISSOLVE],
        "cuts": timeline.counts[HARD_CUT],
        "cut_scene_ids": list(timeline.cut_scene_ids),
        "max_drift": report.max_drift,
        "ending_cuts": len(ending_lengths),
        "padded_scene_ids": list(padded_ids),
        "font_name": font_name,
        "narration_muxed": narration_muxed,
        "warnings": warnings,
        "outputs": [
            p.relative_to(paths.root).as_posix()
            for p in (subtitles_path, timeline_path)
        ],
    })
    state.save()

    return LanguageAssembly(
        lang=lang, scene_count=len(scenes), total_duration=full.total_duration,
        narration_duration=timeline.total_duration,
        dissolves=timeline.counts[DISSOLVE], cuts=timeline.counts[HARD_CUT],
        cut_scene_ids=timeline.cut_scene_ids, max_drift=report.max_drift,
        ending_cuts=len(ending_lengths), padded_scene_ids=padded_ids,
        font_name=font_name, narration_muxed=narration_muxed,
        subtitles_path=subtitles_path, timeline_path=timeline_path,
        warnings=warnings,
    )
