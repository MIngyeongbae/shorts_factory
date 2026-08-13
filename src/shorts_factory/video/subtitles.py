"""ASS 자막 생성 — 레이어 B. specs/schema/subtitle-style.json, ADR-0002, ADR-0013.

**스타일 값은 이 파일에 없다.** 계약 파일에서 로드한다 (ADR-0034 §3) — 값이 코드와
스펙 문서 두 곳에 있으면 갈라지고, 갈라진 쪽을 읽은 사람만 기준이 달라진다.

자막은 **후처리 합성**이다 (ADR-0002). 정확해야 하는 한국어를 이미지 생성에 맡기지
않는다. 큐 1개 = 씬 1개다 (ADR-0013) — 씬을 다시 묶거나 쪼개지 않는다.

시각의 출처는 `scenes.timed.json` 하나다 (ADR-0020). 이 모듈은 씬의 `start`/`end`를
그대로 쓰고, ASS의 시간 해상도(1/100초)만큼만 반올림한다 — 최대 5ms이므로 specs/00의
±200ms 안이다.

## 세 값은 함께 정해졌다

`40px` · `1줄 22자` · `2줄`은 서로 맞물린 한 벌이다. 40px에서 22자는 880px이라 가로
안전폭 960px 안에 들어가고, specs/01이 허용하는 **가장 긴 큐(43자)도 22+21로 두 줄에
들어간다.** 그래서 정상 범위의 큐는 폰트를 줄일 일이 없다 — 큐별 폰트 축소 경로를
두지 않는다.

22자 × 2줄에 안 들어가는 큐는 specs/01의 43자를 넘겼다는 뜻이므로 **실패로 올린다**
(`check_overflow`). 자막 단계가 글자를 작게 만들어 삼키면 상류 위반이 화면에서만
티가 나고 기록에는 남지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from ..schemas import vocab

# --- 스타일 값 (specs/schema/subtitle-style.json) -----------------------------
#
# **선언하지 않고 로드한다** (ADR-0034 §3). 이 값들이 코드와 스펙 03 두 곳에 적혀
# 있던 것이 `mj_video` 사고와 같은 모양이라 계약 파일로 옮겼다.

_STYLE = vocab.SUBTITLE_STYLE

PLAY_RES_X, PLAY_RES_Y = _STYLE["play_res"]
FONT_NAME = _STYLE["font_name"]
FONTS_DIR = _STYLE["fonts_dir"]
PRIMARY_COLOUR = _STYLE["primary_colour"]
OUTLINE_COLOUR = _STYLE["outline_colour"]
OUTLINE = _STYLE["outline"]
SHADOW = _STYLE["shadow"]
ALIGNMENT = _STYLE["alignment"]
MARGIN_LR = _STYLE["margin_lr"]

#: 폰트 크기·줄당 글자 수·줄 수는 **함께 정해진 한 벌이다.** 하나를 고치면 셋을 다시
#: 계산해야 하므로 계약 파일의 `_geometry`가 그 산수를 들고 있다.
MAX_LINE_CHARS = _STYLE["max_line_chars"]
MAX_LINES = _STYLE["max_lines"]
FONT_SIZE = _STYLE["font_size"]

#: 자막이 쓸 수 있는 가로 폭(px)
TEXT_WIDTH = PLAY_RES_X - 2 * MARGIN_LR

#: 한글 한 글자의 가로 advance ÷ 폰트 크기. 한글은 전각이라 1.0이다 (숫자·라틴은 더 좁다).
GLYPH_WIDTH_RATIO = 1.0

#: 자막 블록이 놓이는 세로 밴드 (화면 비율)
BAND = tuple(_STYLE["band"])

#: 자막 블록의 **아래끝**을 밴드의 아래끝에 맞추는 하단 여백(px). 위끝은 줄 수에 따라
#: 움직이며(1줄 78.7% / 2줄 75.4%) 둘 다 밴드 안이다 — `subtitle_band()`가 계산한다.
MARGIN_V = round(PLAY_RES_Y * (1 - BAND[1]))

#: libass가 한 줄에 쓰는 세로 크기 ÷ 폰트 크기의 근사치. 밴드 계산에만 쓴다.
LINE_HEIGHT_RATIO = 1.2

#: ASS의 줄바꿈 표시 (WrapStyle 2 = 자동 줄바꿈 없음, 이 표시만 줄을 나눈다)
LINE_BREAK = r"\N"


class SubtitleError(Exception):
    """자막 문서를 만들 수 없음."""


# --- 줄바꿈 -----------------------------------------------------------------


def _split_at(text: str, index: int) -> tuple[str, str]:
    return text[:index].rstrip(), text[index:].lstrip()


def wrap_text(
    text: str, *, limit: int = MAX_LINE_CHARS, max_lines: int = MAX_LINES
) -> list[str]:
    """자막 한 줄을 화면 줄로 나눈다. 결과는 항상 `max_lines`개 이하다.

    - `limit`자 이하면 나누지 않는다
    - 나눌 때는 **공백에서만** 자른다. 어절 중간을 자르지 않는다
    - 후보 중 긴 쪽이 가장 짧아지는 지점을 고른다 (균형)
    - 공백이 하나도 없으면 어쩔 수 없이 가운데에서 자른다

    `limit`을 넘는 결과가 나올 수 있다 (모듈 독스트링의 충돌). 넘었는지는
    `overflow_warning()`이 판정한다 — 나누는 일과 판정을 섞지 않는다.
    """
    if max_lines < 1:
        raise SubtitleError(f"max_lines는 1 이상이어야 한다: {max_lines}")

    collapsed = " ".join(text.split())
    if not collapsed:
        raise SubtitleError("빈 자막 줄이다")
    if len(collapsed) <= limit or max_lines == 1:
        return [collapsed]

    candidates = [m.start() for m in re.finditer(r"\s+", collapsed)]
    if not candidates:
        candidates = [len(collapsed) // 2]

    def cost(index: int) -> tuple[int, int]:
        head, tail = _split_at(collapsed, index)
        return max(len(head), len(tail)), abs(len(head) - len(tail))

    head, tail = _split_at(collapsed, min(candidates, key=cost))
    if not head or not tail:  # 맨 앞/뒤 공백뿐이라 나뉘지 않았다
        return [collapsed]
    return [head] + wrap_text(tail, limit=limit, max_lines=max_lines - 1)


def check_overflow(scene_id: int, lines: Sequence[str], *, limit: int = MAX_LINE_CHARS) -> None:
    """`limit`자를 넘긴 줄이 있으면 실패시킨다.

    **폰트를 줄여 삼키지 않는다.** 22자 × 2줄에 안 들어가는 큐는 스펙 01의
    "줄당 최대 43자"를 넘겼다는 뜻이고, 그건 1부에서 고칠 문제다. 자막 단계가
    글자를 작게 만들어 넘기면 상류 위반이 화면에서만 티가 나고 기록에는 안 남는다.
    """
    longest = max(len(line) for line in lines)
    if longest <= limit:
        return
    raise SubtitleError(
        f"scenes/{scene_id}: 자막을 {len(lines)}줄로 나눠도 가장 긴 줄이 {longest}자다 "
        f"(스펙 03 상한 {limit}자 × {MAX_LINES}줄). 스펙 01의 '줄당 최대 43자'를 "
        f"넘긴 대본이라는 뜻이다 — 1부에서 고쳐야 한다"
    )


def subtitle_band(line_count: int) -> tuple[float, float]:
    """자막 블록이 차지하는 세로 구간 비율 `(위, 아래)`. specs/03 "72~82%" 검증용."""
    bottom = PLAY_RES_Y - MARGIN_V
    top = bottom - line_count * FONT_SIZE * LINE_HEIGHT_RATIO
    return top / PLAY_RES_Y, bottom / PLAY_RES_Y


# --- ASS 문서 ---------------------------------------------------------------


def escape_text(text: str) -> str:
    """ASS 이벤트 텍스트로 안전하게 만든다.

    `{`/`}`는 오버라이드 태그 블록을 여는 문자라 그대로 두면 자막이 통째로 사라진다.
    줄바꿈은 우리가 넣은 `\\N`만 남는다 — 원문의 개행은 `wrap_text`가 이미 지웠다.
    """
    return text.replace("{", r"\{").replace("}", r"\}")


def ass_timestamp(seconds: float) -> str:
    """ASS 시간 표기 `H:MM:SS.CC`. 해상도가 1/100초라 최대 5ms 반올림 오차가 난다."""
    if seconds < 0:
        raise SubtitleError(f"음수 시각이다: {seconds}")
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def parse_timestamp(stamp: str) -> float:
    """`ass_timestamp`의 역함수. 만든 자막을 되읽어 검증할 때 쓴다."""
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})\.(\d{2})", stamp.strip())
    if not match:
        raise SubtitleError(f"ASS 시간 표기가 아니다: {stamp!r}")
    hours, minutes, secs, centis = (int(g) for g in match.groups())
    return hours * 3600 + minutes * 60 + secs + centis / 100


@dataclass(frozen=True)
class Cue:
    """ASS 이벤트 한 줄 (= 씬 하나)."""

    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def style_line() -> str:
    """specs/03 자막 스타일 한 줄. 필드 순서는 ASS V4+ 규격 그대로다."""
    return (
        "Style: Default,"
        f"{FONT_NAME},{FONT_SIZE},"
        f"{PRIMARY_COLOUR},&H000000FF,{OUTLINE_COLOUR},&H00000000,"
        "-1,0,0,0,"  # Bold(-1=true), Italic, Underline, StrikeOut
        "100,100,0,0,"  # ScaleX, ScaleY, Spacing, Angle
        f"1,{OUTLINE},{SHADOW},"  # BorderStyle(1=외곽선+그림자)
        f"{ALIGNMENT},{MARGIN_LR},{MARGIN_LR},{MARGIN_V},1"
    )


HEADER = "\n".join(
    (
        "[Script Info]",
        "; specs/03-visual-rules.md 자막 스타일 / ADR-0002 레이어 B",
        "ScriptType: v4.00+",
        f"PlayResX: {PLAY_RES_X}",
        f"PlayResY: {PLAY_RES_Y}",
        "WrapStyle: 2",  # 자동 줄바꿈 금지 — 줄 나누기는 wrap_text가 한다
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
    )
)

EVENTS_HEADER = "\n".join(
    (
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    )
)


def build_ass(scenes: Sequence[dict[str, Any]]) -> tuple[str, list[str]]:
    """`scenes.timed.json`의 씬 배열 → (ASS 문서, 경고).

    씬의 `text`·`start`·`end`를 그대로 옮긴다. 대본을 고치지 않는다 (ADR-0017).
    """
    if not scenes:
        raise SubtitleError("씬이 없다")

    warnings: list[str] = []
    events: list[str] = []
    previous_end: float | None = None

    for scene in scenes:
        scene_id = scene["scene_id"]
        start = float(scene["start"])
        end = float(scene["end"])
        if end <= start:
            raise SubtitleError(f"scenes/{scene_id}: start({start}) >= end({end})")
        if previous_end is not None and start < previous_end - 1e-9:
            raise SubtitleError(
                f"scenes/{scene_id}: start({start})가 앞 씬의 end({previous_end})보다 이르다"
            )
        previous_end = end

        lines = wrap_text(scene["text"])
        check_overflow(scene_id, lines)

        text = LINE_BREAK.join(escape_text(line) for line in lines)
        events.append(
            f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},"
            f"Default,,0,0,0,,{text}"
        )

    document = "\n".join((HEADER, style_line(), "", EVENTS_HEADER, *events)) + "\n"
    return document, warnings


#: 이벤트 텍스트 맨 앞의 오버라이드 블록. 태그는 `\`로 시작하므로 escape된 `\{`와
#: 헷갈리지 않는다.
_OVERRIDE_RE = re.compile(r"^\{(\\[^}]*)\}")


def parse_ass(document: str) -> list[Cue]:
    """ASS 문서에서 큐를 되읽는다. 만든 자막을 검증할 때만 쓴다 (verify.py)."""
    cues: list[Cue] = []
    for line in document.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:") :].split(",", 9)
        if len(fields) < 10:
            raise SubtitleError(f"필드가 모자란 Dialogue 줄이다: {line!r}")

        #: 큐별 오버라이드는 더 이상 만들지 않지만(폰트 축소 경로 없음), 손으로 고친
        #: 자막을 되읽어도 태그가 본문으로 새지 않도록 걷어내고 읽는다.
        body = _OVERRIDE_RE.sub("", fields[9])

        cues.append(
            Cue(
                start=parse_timestamp(fields[1]),
                end=parse_timestamp(fields[2]),
                lines=tuple(
                    part.replace(r"\{", "{").replace(r"\}", "}")
                    for part in body.split(LINE_BREAK)
                ),
            )
        )
    return cues
