"""ASS 자막 생성 — 레이어 B. specs/03 "자막 스타일", ADR-0002, ADR-0013.

    - 위치: 하단 중앙, 세로 기준 화면 72~82% 지점
    - 폰트: 굵은 고딕 (Pretendard Bold 계열), 흰색 + 검정 외곽선 3px
    - 1줄 최대 18자, 2줄 초과 금지

자막은 **후처리 합성**이다 (ADR-0002). 정확해야 하는 한국어를 이미지 생성에 맡기지
않는다. 큐 1개 = 씬 1개다 (ADR-0013) — 씬을 다시 묶거나 쪼개지 않는다.

시각의 출처는 `scenes.timed.json` 하나다 (ADR-0020). 이 모듈은 씬의 `start`/`end`를
그대로 쓰고, ASS의 시간 해상도(1/100초)만큼만 반올림한다 — 최대 5ms이므로 specs/00의
±200ms 안이다.

## 스펙 두 줄이 충돌하는 지점

"1줄 최대 18자"와 "2줄 초과 금지"는 34자를 넘는 줄에서 동시에 만족될 수 없다. 그런데
specs/01은 자막 줄당 **최대 43자**를 허용하고(실측 43·41자), 실물 대본 2편에도 40자
줄이 있다. 둘 중 하나는 반드시 깨진다.

여기서는 **줄 수(2줄)를 지키고, 넘친 줄은 그 큐의 폰트를 줄여 화면 안에 넣는다.**
3줄이 되면 자막 블록이 72~82% 밴드를 넘어 피사체 영역을 침범하고, 폰트를 그대로 두면
글자가 화면 밖으로 나가 잘린다 — 둘 다 화면이 깨지는데 후자는 소리 없이 깨진다.
어느 쪽을 깰지는 룰 테이블이 정할 일이므로 씬마다 경고를 남긴다 (실물 대본 기준
피사 8/25씬, 후버댐 1/27씬).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

# --- 스타일 상수 (specs/03 "자막 스타일", specs/00 산출물 정의) ---------------

#: specs/00 "9:16 세로, 1080×1920". ASS의 PlayRes를 실제 해상도와 같게 두면
#: ScaledBorderAndShadow 계산이 1:1이라 외곽선 3px이 그대로 3px이다.
PLAY_RES_X = 1080
PLAY_RES_Y = 1920

#: "굵은 고딕 (Pretendard Bold 계열)". 굵기는 Bold 플래그로 준다 — 폰트 파일 이름이
#: 아니라 패밀리 이름을 적어야 fontconfig가 없는 환경에서도 매칭이 된다.
FONT_NAME = "Pretendard"

#: 리포지토리가 폰트를 들고 있어야 렌더가 재현된다 (ADR-0002 "레이어 B용 폰트/스타일
#: 에셋을 코드 리포지토리에 포함"). 없으면 libass가 아무 폰트로 떨어져 룩이 바뀐다.
FONTS_DIR = "assets/fonts"

#: 흰색 + 검정 외곽선 3px. ASS 색은 &HAABBGGRR (알파 00 = 불투명).
PRIMARY_COLOUR = "&H00FFFFFF"
OUTLINE_COLOUR = "&H00000000"
OUTLINE = 3
SHADOW = 0

#: 하단 중앙 (ASS Alignment 2 = bottom center)
ALIGNMENT = 2
MARGIN_LR = 60

#: specs/03 자막 규칙
MAX_LINE_CHARS = 18
MAX_LINES = 2

#: 자막이 쓸 수 있는 가로 폭(px)
TEXT_WIDTH = PLAY_RES_X - 2 * MARGIN_LR

#: 한글 한 글자의 가로 advance ÷ 폰트 크기. 한글은 전각이라 1.0이다 (숫자·라틴은 더 좁다).
GLYPH_WIDTH_RATIO = 1.0

#: 폰트 크기는 **specs/03의 "1줄 최대 18자"에서 역산한다.** 18자가 가로 폭에 딱 들어가는
#: 크기가 곧 그 규칙이 뜻하는 크기다. 규칙 쪽 숫자(18자·좌우 여백)를 고치면 여기가
#: 따라온다 — 폰트 크기를 따로 고르지 않는다.
FONT_SIZE = int(TEXT_WIDTH / MAX_LINE_CHARS / GLYPH_WIDTH_RATIO)

#: specs/03 "위치: 하단 중앙, 세로 기준 화면 72~82% 지점"
BAND = (0.72, 0.82)

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


def fit_font_size(lines: Sequence[str], *, size: int = FONT_SIZE) -> int:
    """이 큐가 가로 폭 안에 들어가는 폰트 크기.

    `MAX_LINE_CHARS`자 이하면 기본 크기 그대로다. 넘으면 넘은 만큼 줄인다 — 줄이지
    않으면 글자가 화면 밖으로 나가 잘린다(WrapStyle 2라 libass가 대신 접어 주지도
    않고, 접어 준다면 이번엔 3줄이 되어 `MAX_LINES`가 깨진다).

    **스펙이 정한 처리가 아니다.** 스펙 03의 두 줄("1줄 최대 18자" / "2줄 초과 금지")과
    스펙 01의 "줄당 최대 43자"가 동시에 성립할 수 없어서 생긴 자리이고, 셋 중 무엇을
    깰지는 룰 테이블이 정할 일이다. 여기서는 **화면 밖으로 나가지 않는 것**을 최우선으로
    두고 그 사실을 씬별 경고로 올린다.
    """
    longest = max(len(line) for line in lines)
    if longest <= MAX_LINE_CHARS:
        return size
    return int(TEXT_WIDTH / longest / GLYPH_WIDTH_RATIO)


def overflow_warning(
    scene_id: int, lines: Sequence[str], *, limit: int = MAX_LINE_CHARS
) -> str | None:
    """18자를 넘긴 줄에 대한 경고. 넘지 않으면 `None`."""
    longest = max(len(line) for line in lines)
    if longest <= limit:
        return None
    return (
        f"scenes/{scene_id}: 자막을 {len(lines)}줄로 나눠도 가장 긴 줄이 {longest}자다 "
        f"(스펙 03 상한 {limit}자). 스펙 03의 '1줄 최대 {limit}자'와 "
        f"'{MAX_LINES}줄 초과 금지'가 스펙 01의 '줄당 최대 43자'와 충돌한다 — "
        f"줄 수를 지키고 폰트를 {FONT_SIZE}px → {fit_font_size(lines)}px로 줄였다"
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
    #: 이 큐에만 걸린 폰트 크기 오버라이드. 기본 크기면 `None`
    font_size: int | None = None

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
        warning = overflow_warning(scene_id, lines)
        if warning:
            warnings.append(warning)

        text = LINE_BREAK.join(escape_text(line) for line in lines)
        size = fit_font_size(lines)
        if size != FONT_SIZE:
            text = f"{{\\fs{size}}}{text}"
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

        body = fields[9]
        font_size: int | None = None
        override = _OVERRIDE_RE.match(body)
        if override:
            body = body[override.end() :]
            size = re.search(r"\\fs(\d+)", override.group(1))
            font_size = int(size.group(1)) if size else None

        cues.append(
            Cue(
                start=parse_timestamp(fields[1]),
                end=parse_timestamp(fields[2]),
                lines=tuple(
                    part.replace(r"\{", "{").replace(r"\}", "}")
                    for part in body.split(LINE_BREAK)
                ),
                font_size=font_size,
            )
        )
    return cues
