"""썸네일 렌더 — 씬 첫 프레임 + 그라데이션 막 + 제목 훅. ADR-0091.

**판이 되는 씬은 언어마다 다르다** (ADR-0092) — `plate_candidates`가 계측 표시(`info`)가
없는 씬을 고르고 `plate_scene_id`가 언어 자리로 하나를 집는다. 세 채널에 같은 화면이
올라가는 것을 줄이려는 결정이다.

## 왜 새 렌더 값이 없는가

사람이 *"지금 제목 크기랑 똑같이해"*로 정했다. 그래서 제목은 `[9]`가 굽는 제목 훅과
**같은 스타일 줄·같은 줄바꿈 함수**를 탄다 — 크기(99px)·줄당 상한·줄 수·채워쓰기·일본어
금칙·폰트·외곽선이 전부 `subtitle-style.json`의 `title`에서 온다. 이 모듈이 새로 읽는 값은
`thumbnail.scrim` 셋뿐이다 (ADR-0034 §3 — 없어도 되는 계약값은 안 만든다).

## 막은 왜 그라데이션인가 — 실측이 정했다

처음 설계는 딱딱한 반투명 띠였다. 실편 3편에 찍어 보니 **진하기보다 아래끝이 문제였다**:
반투명이라 레터박스로 안 읽히고 **그림을 가로로 자른 자국**으로 보인다(금화 판에서 동전
더미가 중간에 끊겼다). 아래끝을 0으로 빼니 자국이 사라지고 조명처럼 읽힌다. 근거는
ADR-0091 맥락 5와 `subtitle-style.json`의 `thumbnail._scrim`.

## FFmpeg 하나로 합성한다

Pillow은 이 리포지토리의 의존성이 아니다. 막은 `geq`의 알파 식으로 만들어 `overlay`하고
글자는 `ass` 필터가 얹는다 — 클립에서 프레임을 뽑는 것까지 **한 번의 호출**이다.
`video/ending.py`가 `drawtext`로 크레딧을 굽는 것과 같은 태도다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ..schemas import vocab
from .ffmpeg import HEIGHT, WIDTH
from .subtitles import (
    EVENTS_HEADER,
    HEADER,
    LINE_HEIGHT_RATIO,
    TITLE_FONT_SIZE,
    TITLE_KINSOKU,
    TITLE_MARGIN_V,
    TITLE_MAX_LINES,
    SubtitleError,
    fill_text,
    font_name_for,
    title_event,
    title_max_line_chars_for,
    title_style_line,
)

_SCRIM: dict[str, Any] = vocab.SUBTITLE_STYLE["thumbnail"]["scrim"]

#: 막의 위쪽 알파. 밝고 복잡한 판(금화)이 바닥을 정했다 — 계약의 `_scrim` 참조.
SCRIM_ALPHA = float(_SCRIM["alpha"])

#: 알파를 그대로 유지하는 구간 (막 높이에 대한 비율). 나머지에서 0으로 뺀다.
#: **막이 피사체를 가리면 알파가 아니라 이 값을 먼저 줄인다** (ADR-0091 되돌릴 조건 4).
SCRIM_HOLD = float(_SCRIM["hold"])

#: 제목 블록 아래끝에서 막을 더 내리는 여백(px). 글자가 막 가장자리에 붙으면 잘려 보인다.
SCRIM_PAD = int(_SCRIM["pad"])

#: 산출물 이름 — 언어당 한 장. `[10] upload`가 이 이름으로 찾는다.
THUMBNAIL_PATTERN = "thumbnail.{lang}.png"

#: 판을 뽑는 클립의 **되떨어질 자리**. **`timeline.{lang}.mp4`가 아니다** — 거기엔 제목
#: 훅과 자막이 이미 구워져 있어 제목이 두 겹이 되고 나레이션 자막까지 딸려온다
#: (ADR-0091 맥락 3). **고정값이 아니다** — 판이 되는 씬은 언어마다 다르고(ADR-0092)
#: 이 값은 후보를 못 구했을 때만 쓴다.
PLATE_SCENE_ID = 1


def plate_candidates(scenes: Sequence[dict[str, Any]]) -> list[int]:
    """판이 될 수 있는 씬 id — **`info`가 없는 씬**이다 (ADR-0092).

    `info` 씬은 빨간 계측 표시가 화면에 구워져 있어(ADR-0075) 제목 글자와 겹친다.
    깨끗한 씬이 하나도 없으면 **전 씬으로 넓힌다** — 판이 없는 것보다 겹치는 편이 낫고,
    막이 그라데이션이라 완전히 무너지지는 않는다.
    """
    ids = [int(scene["scene_id"]) for scene in scenes if "scene_id" in scene]
    clean = [
        int(scene["scene_id"])
        for scene in scenes
        if "scene_id" in scene and not scene.get("info")
    ]
    return clean or ids


def plate_scene_id(candidates: Sequence[int], *, position: int, total: int) -> int:
    """이 언어의 판이 될 씬 id — 후보를 언어 수로 나눠 고르게 퍼뜨린다 (ADR-0092).

    `position`은 언어 자리(ko=0), `total`은 언어 수다. **자리 0은 항상 가장 이른 후보**라
    ko가 ADR-0091이 실측한 씬 1에 제일 가깝게 남는다.

    후보가 비어 있으면 `PLATE_SCENE_ID`로 떨어진다 — 씬 계약을 못 읽은 편에서도 지금까지와
    같이 돈다 (D-3).
    """
    if not candidates:
        return PLATE_SCENE_ID
    if total <= 0:
        return candidates[0]
    index = position * len(candidates) // total
    return candidates[min(max(index, 0), len(candidates) - 1)]


class ThumbnailError(Exception):
    pass


def title_lines(title: str, lang: str) -> list[str]:
    """제목 훅과 **같은 줄바꿈**을 탄다 — 채워쓰기 + 일본어 행두 금칙 (ADR-0074·0080)."""
    return fill_text(
        title.strip(), limit=title_max_line_chars_for(lang), kinsoku=TITLE_KINSOKU
    )


def scrim_bottom(line_count: int) -> int:
    """막의 아래끝(px). **제목 블록을 따라 움직인다** — 2줄 제목에 절반을 덮지 않는다.

    6줄(제목 훅의 상한)이면 화면의 54%까지 간다 — ADR-0091 되돌릴 조건 1의 관측 자리다.
    """
    if line_count < 1:
        raise ThumbnailError(f"제목 줄 수는 1 이상이어야 한다: {line_count}")
    block = line_count * TITLE_FONT_SIZE * LINE_HEIGHT_RATIO
    return min(HEIGHT, round(TITLE_MARGIN_V + block + SCRIM_PAD))


def scrim_expression(bottom: int) -> str:
    """`geq`의 알파 식 — 위에서 `SCRIM_ALPHA`, `hold`까지 유지, `bottom`에서 0.

    필터 인자 안에 들어가므로 쉼표를 쓰지 않는다 — `if(lt(...))`의 인자 구분자가
    쉼표라 `escape_filter_arg`가 이스케이프한다.
    """
    top = round(SCRIM_ALPHA * 255)
    hold = round(bottom * SCRIM_HOLD)
    if hold >= bottom:  # hold가 1.0이면 딱딱한 띠가 된다 — 그 경우를 막는다
        raise ThumbnailError(
            f"hold({SCRIM_HOLD})가 막 전체를 덮는다 — 아래끝이 자른 자국으로 남는다"
        )
    fade = f"{top}*(1-(Y-{hold})/({bottom}-{hold}))"
    return f"if(lt(Y,{hold}),{top},if(lt(Y,{bottom}),{fade},0))"


def build_ass(title: str, lang: str) -> str:
    """제목 하나만 든 ASS. `[9]`의 자막 문서와 달리 씬 큐가 없다.

    구간은 넉넉히 잡는다 — 정지 프레임 한 장이라 끝 시각이 화면에 안 나타난다.
    """
    event, warning = title_event(
        title.strip(), end=99.0, limit=title_max_line_chars_for(lang)
    )
    if warning:
        raise ThumbnailError(warning)
    return "\n".join(
        (HEADER, title_style_line(font_name_for(lang)), "", EVENTS_HEADER, event)
    ) + "\n"


def build_filter(bottom: int, subtitles: str, fontsdir: str | None) -> str:
    """클립 → [첫 프레임 → 막 덮기 → 제목 얹기] 한 줄짜리 filtergraph.

    막은 `color` 소스에 `geq`로 알파를 그려 `overlay`한다. 루마만 낮추는 방법
    (`geq=lum=…`)은 색이 안 빠져 **물 빠진 그림**이 되므로 쓰지 않는다.

    `subtitles`·`fontsdir`는 **이미 이스케이프된 값**을 받는다 (`escape_filter_path`) —
    `[9]`의 `build_filter_graph`와 같은 계약이다. 경로 이스케이프를 두 곳에서 따로
    구현하면 한쪽만 Windows 드라이브 문자를 놓친다 (실제로 그렇게 한 번 깨졌다).
    """
    ass = f"ass=filename={subtitles}"
    if fontsdir:
        ass += f":fontsdir={fontsdir}"
    #: `geq` 식의 쉼표는 filtergraph의 인자 구분자라 그대로 두면 필터가 쪼개진다.
    alpha = scrim_expression(bottom).replace(",", "\\,")
    return (
        f"[0:v]select=eq(n\\,0),setpts=N/TB[plate];"
        f"color=c=black:s={WIDTH}x{HEIGHT}:r=1:d=1,format=rgba,"
        f"geq=r=0:g=0:b=0:a={alpha}[scrim];"
        f"[plate][scrim]overlay=0:0,{ass}[out]"
    )


def build_command(
    *, clip: str, filter_graph: str, output: str, executable: str = "ffmpeg"
) -> list[str]:
    """정지 이미지 한 장이라 코덱 인자가 없다 — 확장자가 PNG를 고른다."""
    return [
        executable, "-y", "-loglevel", "error", "-i", clip,
        "-filter_complex", filter_graph, "-map", "[out]", "-frames:v", "1", output,
    ]


def thumbnail_path(run_dir: Path, lang: str) -> Path:
    return run_dir / THUMBNAIL_PATTERN.format(lang=lang)


__all__ = [
    "PLATE_SCENE_ID",
    "plate_candidates",
    "plate_scene_id",
    "SCRIM_ALPHA",
    "SCRIM_HOLD",
    "SCRIM_PAD",
    "THUMBNAIL_PATTERN",
    "TITLE_MAX_LINES",
    "SubtitleError",
    "ThumbnailError",
    "build_ass",
    "build_command",
    "build_filter",
    "scrim_bottom",
    "scrim_expression",
    "thumbnail_path",
    "title_lines",
]
