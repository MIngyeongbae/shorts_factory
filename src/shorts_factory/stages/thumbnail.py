"""[9t. thumbnail] — 씬 첫 프레임 + 막 + 제목 → `thumbnail.{lang}.png`. ADR-0091·0092.

specs/05-pipeline.md:

    [9t. thumbnail] → thumbnail.{lang}.png (채널 그리드용. 씬 클립의 첫 프레임에
                      어두운 막을 덮고 제목 훅을 그대로 얹는다. 언어당 1장이고
                      판이 되는 씬도 언어마다 다르다)

## 입력 (ADR-0020)

| 파일 | 무엇을 읽는가 |
|---|---|
| `runs/{run_id}/clips/{scene_id}.mp4` | **판이다.** 첫 프레임 한 장 |
| `runs/{run_id}/scenes.json` | `info` 여부 하나 — 판 후보를 고른다 (ADR-0092) |
| `runs/{run_id}/scenes.timed.{lang}.json` | 선택 필드 `title` — 그 언어의 제목 (ADR-0065) |

**`timeline.{lang}.mp4`를 읽지 않는다** — 거기엔 제목 훅과 자막이 이미 구워져 있어 제목이
두 겹이 되고 나레이션 자막까지 딸려온다 (ADR-0091 맥락 3).

## 판은 언어마다 다르다 (ADR-0092)

세 채널에 같은 화면이 올라가는 것을 줄이려는 결정이다. 후보는 **`info`가 없는 씬** —
계측 표시(ADR-0075)가 제목 글자와 겹치기 때문이다. 후보를 언어 수로 나눠 고르게 퍼뜨리고
**ko는 항상 가장 이른 후보**를 받는다 (ADR-0091이 실측한 씬 1에 제일 가깝다).

**다른 씬에는 「위쪽 1/3이 빈다」는 실측이 없다** — 고른 씬 id를 `state.json`에 남겨
사람이 되짚을 수 있게 한다 (ADR-0092 되돌릴 조건 3).

## 기존 단계를 수정하지 않는다 (specs/05 D-4)

입력이 `[7]`의 클립과 `[3]`의 실측뿐이라 `[8]`·`[9]`와 선후가 없다. `[9]`가 실패한 편에도
썸네일은 나온다. 바뀌는 기존 단계는 `[10] upload` 하나이고, 그것도 **있으면 같이 올린다**일
뿐이다 (D-3).

## 제목이 없으면 굽지 않는다

자막(`굽기 → 없음`)·엔딩(`파일 없음이 정상`)과 다른 판정이다 — **이 산출물이 하는 일
전부가 제목**이라 제목 없는 썸네일은 만들 이유가 없다. 그 언어를 건너뛰고 경고한다.
편 전체를 실패로 만들지는 않는다 (D-5 — 썸네일은 마감이지 본편이 아니다).

## 왜 새 렌더 값이 없는가

제목은 `[9]`의 제목 훅과 **같은 스타일 줄·같은 줄바꿈 함수**를 탄다 (사람 결정 —
*"지금 제목 크기랑 똑같이해"*). 이 단계가 새로 읽는 계약값은 `thumbnail.scrim` 셋뿐이다.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..judgment import JudgmentError, languages_for
from ..config import Paths
from ..runstate import RunState
from .contract import SCENES_FILE
from ..schemas.timed_scenes import (
    LANGUAGES,
    PRIMARY_LANGUAGE,
    TIMED_SCENES_PATTERN,
    present_languages,
)
from ..video.ffmpeg import (
    DEFAULT_FFMPEG,
    FFmpegError,
    escape_filter_path,
    run_ffmpeg,
)
from ..video.subtitles import FONT_SUFFIXES, FONTS_DIR
from ..video.thumbnail import (
    PLATE_SCENE_ID,
    plate_candidates,
    plate_scene_id,
    THUMBNAIL_PATTERN,
    TITLE_MAX_LINES,
    ThumbnailError,
    build_ass,
    build_command,
    build_filter,
    scrim_bottom,
    thumbnail_path,
    title_lines,
)
from ..video.pools import clip_source_dir
from ..video.timeline import CLIPS_DIR

log = logging.getLogger(__name__)

STAGE = "9t-thumbnail"

#: 판을 만든 ASS는 산출물 옆에 남긴다 — 렌더를 사람이 그대로 재현할 수 있게 한다
#: (`[9]`가 `subtitles.{lang}.ass`를 남기는 것과 같은 태도).
ASS_PATTERN = "thumbnail.{lang}.ass"


class ThumbnailStageError(Exception):
    pass


@dataclass
class LanguageThumbnail:
    lang: str
    title: str = ""
    line_count: int = 0
    scrim_bottom: int = 0
    #: 판이 된 씬. **언어마다 다르다** (ADR-0092) — 되돌릴 조건 3을 보는 값이라 기록한다.
    scene_id: int = PLATE_SCENE_ID
    path: Path | None = None
    skipped: bool = False
    warning: str = ""

    @property
    def burned(self) -> bool:
        return self.path is not None


@dataclass
class ThumbnailResult:
    run_id: str
    run_dir: Path
    topic: str = ""
    languages: dict[str, LanguageThumbnail] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def burned(self) -> list[str]:
        return [l for l, r in self.languages.items() if r.burned]

    @property
    def passed(self) -> bool:
        return bool(self.burned)

    @property
    def summary(self) -> str:
        if not self.languages:
            return f"[{STAGE}] 굽지 않았다 — 제목이 있는 언어가 없다"
        parts = []
        for lang, item in self.languages.items():
            if not item.burned:
                parts.append(f"{lang} 건너뜀")
            else:
                mark = " (스킵)" if item.skipped else ""
                parts.append(f"{lang} 씬{item.scene_id} {item.line_count}줄{mark}")
        return f"[{STAGE}] 썸네일 {len(self.burned)}장 — {' · '.join(parts)}"


def _fonts_dir(paths: Paths) -> tuple[str | None, str | None]:
    """`[9]`와 같은 판정 — 없으면 libass가 시스템 폰트로 떨어진다 (ADR-0002)."""
    fonts = paths.root / FONTS_DIR
    if fonts.is_dir() and any(
        p.suffix.lower() in FONT_SUFFIXES for p in fonts.iterdir()
    ):
        return str(fonts), None
    return None, (
        f"{FONTS_DIR}/에 자막 폰트가 없다. 썸네일 제목이 시스템 폰트로 떨어진다 "
        "(ADR-0002 — 레이어 B 폰트는 리포지토리 에셋이다)"
    )


def title_of(run_dir: Path, lang: str) -> str:
    """그 언어의 제목. 파일이나 필드가 없으면 빈 문자열이다 (D-3)."""
    path = run_dir / TIMED_SCENES_PATTERN.format(lang=lang)
    if not path.exists():
        return ""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ThumbnailStageError(f"{path.name}을 읽을 수 없다: {exc}") from exc
    return str(document.get("title") or "").strip()


def plate_path(run_dir: Path, scene_id: int = PLATE_SCENE_ID, lang: str = "") -> Path:
    """그 언어의 판 — 겹풀(`clips.{lang}/`)에 있으면 그것, 없으면 기본 풀 (ADR-0095)."""
    return run_dir / clip_source_dir(run_dir, lang, scene_id) / f"{scene_id}.mp4"


def scene_contract(run_dir: Path) -> list[dict[str, Any]]:
    """판 후보를 고르려고 읽는 씬 계약. **없거나 깨지면 빈 목록이다** (D-3).

    `info` 여부만 본다 — 이 단계가 씬 계약에서 읽는 전부다.
    """
    path = run_dir / SCENES_FILE
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    scenes = document.get("scenes") if isinstance(document, dict) else document
    return scenes if isinstance(scenes, list) else []


def plate_for(
    run_dir: Path, lang: str, candidates: Sequence[int]
) -> tuple[Path, int, str]:
    """이 언어의 판 `(경로, 씬 id, 경고)` — 클립이 없으면 씬 1로 떨어진다 (ADR-0092).

    후보가 가리키는 클립이 없는 경우는 `[7]`이 그 씬에서 실패한 편이다. 썸네일 하나
    때문에 편을 세우지 않는다 (D-5).
    """
    position = LANGUAGES.index(lang) if lang in LANGUAGES else 0
    scene_id = plate_scene_id(candidates, position=position, total=len(LANGUAGES))
    path = plate_path(run_dir, scene_id, lang)
    if path.exists():
        return path, scene_id, ""
    fallback = plate_path(run_dir, lang=lang)
    warning = (
        f"{lang}: 판으로 고른 씬 {scene_id}의 클립이 없어 씬 {PLATE_SCENE_ID}로 떨어졌다 "
        f"— 이 언어의 썸네일이 다른 언어와 겹칠 수 있다 (ADR-0092)"
    )
    return fallback, PLATE_SCENE_ID, warning


def _language_state(state: RunState, lang: str) -> dict[str, Any]:
    return state.stage(STAGE).setdefault("languages", {}).setdefault(
        lang, {"status": "pending"}
    )


def _burn(
    *, lang: str, title: str, run_dir: Path, plate: Path, scene_id: int,
    fonts_dir: str | None, ffmpeg: str, runner,
) -> LanguageThumbnail:
    lines = title_lines(title, lang)
    if len(lines) > TITLE_MAX_LINES:
        # 제목 훅과 같은 상한이다. `[9]`는 제목만 빼고 영상을 내지만 여기서는 뺄 것이
        # 제목밖에 없으므로 그 언어를 건너뛴다.
        return LanguageThumbnail(
            lang=lang, title=title, line_count=len(lines), scene_id=scene_id,
            warning=(
                f"{lang}: 제목이 {TITLE_MAX_LINES}줄에 안 들어가({len(lines)}줄) 썸네일을 "
                f"굽지 않는다 (ADR-0091 결정 4). 제목: {title!r}"
            ),
        )

    bottom = scrim_bottom(len(lines))
    ass_path = run_dir / ASS_PATTERN.format(lang=lang)
    ass_path.write_text(build_ass(title, lang), encoding="utf-8")

    out = thumbnail_path(run_dir, lang)
    out.unlink(missing_ok=True)
    cmd = build_command(
        clip=plate.relative_to(run_dir).as_posix(),
        filter_graph=build_filter(
            bottom,
            escape_filter_path(ass_path, run_dir),
            escape_filter_path(fonts_dir, run_dir) if fonts_dir else None,
        ),
        output=out.name,
        executable=ffmpeg,
    )
    run_ffmpeg(cmd, cwd=run_dir, produces=out, runner=runner)
    return LanguageThumbnail(
        lang=lang, title=title, line_count=len(lines), scrim_bottom=bottom,
        scene_id=scene_id, path=out,
    )


def run_thumbnail_stage(
    run_id: str,
    *,
    paths: Paths | None = None,
    langs: Sequence[str] | None = None,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner=subprocess.run,
) -> ThumbnailResult:
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    present = present_languages(run_dir)
    if PRIMARY_LANGUAGE not in present:
        raise ThumbnailStageError(
            f"{TIMED_SCENES_PATTERN.format(lang=PRIMARY_LANGUAGE)}이 없다: {run_dir}. "
            "[3. tts+sync]를 먼저 실행하라"
        )
    # 판 후보는 **계측 표시가 없는 씬**이고 그중 어느 것을 쓰는지는 언어가 정한다
    # (ADR-0092). 씬 계약이 없으면 후보가 비고 전 언어가 씬 1로 떨어진다 (D-3).
    candidates = plate_candidates(scene_contract(run_dir))
    if not any(
        plate_path(run_dir, scene_id).exists()
        for scene_id in (*candidates, PLATE_SCENE_ID)
    ):
        raise ThumbnailStageError(
            f"판이 될 클립이 없다: {plate_path(run_dir)}. [7. videogen]을 먼저 실행하라 — "
            f"썸네일의 판은 씬 클립의 첫 프레임이다 (ADR-0091·0092)"
        )

    # 돌리는 언어는 **명시 > 판정 > 있는 것 전부**다 (ADR-0094 결정 4).
    try:
        wanted = languages_for(paths, run_id, langs=langs, present=present)
    except JudgmentError as exc:
        raise ThumbnailStageError(str(exc)) from exc
    unknown = [l for l in wanted if l not in LANGUAGES]
    if unknown:
        raise ThumbnailStageError(
            f"모르는 언어다: {unknown} (가능: {', '.join(LANGUAGES)})"
        )
    selected = [l for l in LANGUAGES if l in wanted]

    primary = json.loads(
        (run_dir / TIMED_SCENES_PATTERN.format(lang=PRIMARY_LANGUAGE)).read_text(
            encoding="utf-8"
        )
    )
    state = RunState.load_or_create(run_dir, run_id, topic=primary.get("topic", ""))
    result = ThumbnailResult(
        run_id=run_id, run_dir=run_dir, topic=primary.get("topic", "")
    )

    fonts_dir, font_warning = _fonts_dir(paths)
    if font_warning:
        result.warnings.append(font_warning)

    state.mark_running(STAGE)
    try:
        for lang in selected:
            out = thumbnail_path(run_dir, lang)
            lang_state = _language_state(state, lang)
            if lang_state.get("status") == "done" and not force and out.exists():
                log.info("[%s] %s는 이미 있다 — 스킵 (run_id=%s)", STAGE, lang, run_id)
                result.languages[lang] = LanguageThumbnail(
                    lang=lang, title=lang_state.get("title", ""),
                    line_count=lang_state.get("line_count", 0),
                    scrim_bottom=lang_state.get("scrim_bottom", 0),
                    scene_id=lang_state.get("scene_id", PLATE_SCENE_ID),
                    path=out, skipped=True,
                )
                continue

            title = title_of(run_dir, lang)
            if not title:
                warning = (
                    f"{lang}: {TIMED_SCENES_PATTERN.format(lang=lang)}에 title이 없어 "
                    "썸네일을 굽지 않는다 (ADR-0065·0091)"
                )
                log.warning("[%s] %s", STAGE, warning)
                result.warnings.append(warning)
                result.languages[lang] = LanguageThumbnail(lang=lang, warning=warning)
                lang_state.update({"status": "skipped", "warning": warning})
                continue

            plate, plate_scene, plate_warning = plate_for(run_dir, lang, candidates)
            if plate_warning:
                log.warning("[%s] %s", STAGE, plate_warning)
                result.warnings.append(plate_warning)

            try:
                item = _burn(
                    lang=lang, title=title, run_dir=run_dir, plate=plate,
                    scene_id=plate_scene, fonts_dir=fonts_dir, ffmpeg=ffmpeg,
                    runner=runner,
                )
            except (FFmpegError, ThumbnailError) as exc:
                raise ThumbnailStageError(f"{lang}: {exc}") from exc

            result.languages[lang] = item
            if item.warning:
                log.warning("[%s] %s", STAGE, item.warning)
                result.warnings.append(item.warning)
                lang_state.update({"status": "skipped", "warning": item.warning})
            else:
                lang_state.update({
                    "status": "done",
                    "title": item.title,
                    "line_count": item.line_count,
                    "scrim_bottom": item.scrim_bottom,
                    #: 되돌릴 조건 3(제목이 안 읽히는 편이 잦다)을 보는 자리다 — ADR-0092
                    "scene_id": item.scene_id,
                    "outputs": [
                        p.relative_to(paths.root).as_posix()
                        for p in (item.path, run_dir / ASS_PATTERN.format(lang=lang))
                    ],
                })
            state.save()
    except ThumbnailStageError as exc:
        state.mark_failed(
            STAGE, str(exc), languages=state.stage(STAGE).get("languages", {})
        )
        raise

    langs_info = state.stage(STAGE).get("languages", {})
    state.mark_done(STAGE, **{
        "languages": langs_info,
        "burned": result.burned,
        "warnings": result.warnings,
        "outputs": sorted(
            p for info in langs_info.values() for p in info.get("outputs", [])
        ),
    })
    state.save()
    return result
