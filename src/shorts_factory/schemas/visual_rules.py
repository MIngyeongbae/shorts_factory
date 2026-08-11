"""비트 → 시각 연출 룰 테이블과 이미지 프롬프트 조립. specs/03-visual-rules.md.

`[5. prompt]`가 쓰는 유일한 판단 근거다. ADR-0001에 따라 여기 없는 연출은 만들어 내지
않는다.

## 이 모듈이 스펙 03에서 그대로 옮겨온 것

- 베이스 스타일 3줄 (`BASE_STYLE`, `COMPOSITION`, 우하단 파티클 = `GLOBAL_OVERLAYS`)
- 구도 표 `(beat × subject_scale) → 토큰` 36칸 (`FRAMING_TABLE`, ADR-0018)
- 구도 토큰 → 샷 21행 (`FRAMINGS`)
- 비트별 오버레이·카메라 기본값 12행 (`BEAT_RULES`)
- 오버레이 타입 enum 2행 (`OVERLAYS`, ADR-0019)

## 이 모듈이 다루지 않는 것

- 전환 규칙·자막 스타일: 스펙 03에 있지만 `[9. assemble]` 소관이다
- 카메라 값 결정: `camera`는 이미 씬 계약에 들어 있다 (ADR-0014에서 `[1]`이 채운다).
  여기서는 비트 기본값과 어긋나는지 **검사만** 한다
- 레이어 A 어노테이션: 폐기됐다 (ADR-0019). 베이스 이미지는 전 씬 클린이다
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

# --- 베이스 스타일 (specs/03 "베이스 스타일 (전 씬 공통)") -------------------

#: specs/03 "베이스 스타일" — 펜선·수채 위의 극사실 건축 렌더링 (ADR-0023).
#: 실호출 6회로 검증한 문구다. 스펙 03이 유일한 출처이므로 여기서 고쳐 쓰지 않는다.
#: "realism dominant"가 핵심이다 — 수채로 뭉개면 재질이 사라지고, 이 채널에서
#: 재질은 곧 설명이다 (콘크리트 골재·거푸집 나뭇결·암반 층리, ADR-0022).
BASE_STYLE = (
    "photorealistic architectural rendering on white paper, mixed media: "
    "the focal subject rendered with true-to-life detail, materials and lighting; "
    "colour laid in as controlled watercolour washes over fine graphite and "
    "technical-pen underdrawing, with construction and guide lines left visible; "
    "realism dominant over painterly looseness; "
    "detail and colour fall away toward the edges into bare paper"
)

#: "9:16 구도, 피사체는 중앙~상단 1/3에 배치 (하단 1/3은 자막 영역으로 비움)"
COMPOSITION = (
    "vertical 9:16; subject placed in the centre to upper third; "
    "bottom third left empty for subtitles"
)

ASPECT_RATIO = "9:16"

#: ADR-0005 "해상도: 2K (… 4K 금지)"
RESOLUTION = "2K"

#: ADR-0005 "스타일 앵커 이미지 3장을 … 모든 생성 호출에 레퍼런스로 첨부"
#: (3~5장 → 3장 고정. gemini-3.1-flash-image의 스타일 레퍼런스 상한이다 — ADR-0021)
STYLE_ANCHOR_DIR = "assets/style_anchors"

#: "우하단 반짝이 파티클(✦) 오버레이 — 후처리 공통 레이어". 씬별 룰이 아니라 전 씬 공통이라
#: 씬 항목이 아닌 style 블록에 싣는다. 소비자는 [8. overlay]다.
GLOBAL_OVERLAYS: tuple[dict[str, Any], ...] = (
    {
        "type": "sparkle_particles",
        "layer": "B",
        "placement": "bottom_right",
        "scope": "all_scenes",
    },
)

#: 어느 씬에서든 베이스 이미지에 들어오면 안 되는 것. 둘 다 레이어 B가 소유한다
#: (자막은 스펙 03 "자막 스타일" + ADR-0002, 파티클은 위 공통 레이어).
GLOBAL_NEGATIVES: tuple[str, ...] = (
    "burned-in subtitles or caption bars",
    "sparkle particle overlay",
    # 레퍼런스 앵커 일부에 필기체 텍스처가 있어 모델이 따라 그린다. 그대로 두면
    # 한국어를 흉내 내다 깨진 글자를 만든다 — 정확해야 하는 글자는 전부 레이어 B다
    # (ADR-0002·0023). 실호출 6회에서 이 문구로 글자가 한 자도 나오지 않았다.
    "any legible or handwritten text, letters, numbers or annotation scribbles",
)

# --- 피사체 스케일 (specs/02, ADR-0018) --------------------------------------

#: 씬 계약의 `subject_scale`. 구도 표의 열이다.
SUBJECT_SCALES: tuple[str, ...] = ("wide", "close", "diagram")

# --- 구도 토큰 → 샷 (specs/03 "구도 토큰 → 샷" 21행) -------------------------


@dataclass(frozen=True)
class Framing:
    """구도 토큰 하나. `shot`이 프롬프트에 그대로 들어간다."""

    token: str
    shot: str


FRAMINGS: dict[str, Framing] = {
    f.token: f
    for f in (
        # wide 계열
        Framing("drone_wide", "aerial drone establishing shot, wide view of the site"),
        Framing("aerial_diorama", "bird's-eye diorama view of the scene"),
        Framing("attempt_medium", "medium shot of the attempted solution"),
        Framing("solution_medium", "medium shot of the working solution"),
        Framing("failure_result_wide", "wide shot that shows the failure outcome itself"),
        Framing("problem_wide", "wide view of the whole problem situation"),
        Framing(
            "frontal_symmetric",
            "frontal view of the key subject, symmetrical composition",
        ),
        Framing("present_wide", "present-day photoreal wide shot"),
        # close 계열
        Framing("subject_closeup", "close-up of the main subject"),
        Framing("detail_closeup", "tight detail close-up of the solution"),
        Framing("failure_closeup", "tight close-up of the point where it failed"),
        Framing("problem_closeup", "tight close-up of the problem spot"),
        Framing(
            "frontal_closeup",
            "frontal close-up of the key subject, symmetrical composition",
        ),
        Framing("present_closeup", "present-day photoreal close-up"),
        # diagram 계열
        Framing(
            "section_diagram",
            "cutaway sectional diagram, cut plane facing the camera",
        ),
        Framing("solution_diagram", "explanatory diagram of the attempted solution"),
        Framing("failure_diagram", "explanatory diagram of why it failed"),
        Framing("problem_diagram", "explanatory diagram of the problem"),
        Framing(
            "frontal_diagram",
            "frontal explanatory diagram of the key subject, symmetrical layout",
        ),
        Framing("cross_section", "cross-section cutaway, cut plane facing the camera"),
        Framing("present_section", "present-day photoreal shot of a cut/exposed face"),
    )
}

#: 구도가 앞 씬에서 이어진다는 표시 (hook_twist "전경 유지").
INHERIT_PREV = "@prev"
#: 구도가 훅 씬에서 온다는 표시 (ending_echo "훅과 동일/유사 구도 재사용").
ECHO_HOOK = "@hook"

#: 참조(`@prev`/`@hook`)가 성립하지 않을 때 쓰는 값. specs/03 "참조가 성립하지 않는 경우".
#: 가리킬 씬이 없거나, 있어도 subject_scale이 달라 구도를 이을 수 없을 때다.
REFERENCE_FALLBACK: dict[str, str] = {
    INHERIT_PREV: "drone_wide",
    ECHO_HOOK: "present_wide",
}

# --- 구도 표 (specs/03 "비트 × 스케일 → 구도 토큰" 12행 × 3열) ---------------

FRAMING_TABLE: dict[str, dict[str, str]] = {
    "hook_fact": {
        "wide": "drone_wide",
        "close": "subject_closeup",
        "diagram": "section_diagram",
    },
    "hook_twist": {
        "wide": INHERIT_PREV,
        "close": "subject_closeup",
        "diagram": "section_diagram",
    },
    "context": {
        "wide": "aerial_diorama",
        "close": "subject_closeup",
        "diagram": "section_diagram",
    },
    "context_number": {
        "wide": "aerial_diorama",
        "close": "subject_closeup",
        "diagram": "section_diagram",
    },
    "failed_solution": {
        "wide": "attempt_medium",
        "close": "detail_closeup",
        "diagram": "solution_diagram",
    },
    "failure_reason": {
        "wide": "failure_result_wide",
        "close": "failure_closeup",
        "diagram": "failure_diagram",
    },
    "dilemma_peak": {
        "wide": "problem_wide",
        "close": "problem_closeup",
        "diagram": "problem_diagram",
    },
    "turning_point": {
        "wide": "frontal_symmetric",
        "close": "frontal_closeup",
        "diagram": "frontal_diagram",
    },
    "solution_step": {
        "wide": "solution_medium",
        "close": "detail_closeup",
        "diagram": "cross_section",
    },
    "solution_number": {
        "wide": "solution_medium",
        "close": "detail_closeup",
        "diagram": "cross_section",
    },
    "present_link": {
        "wide": "present_wide",
        "close": "present_closeup",
        "diagram": "present_section",
    },
    "ending_echo": {
        "wide": ECHO_HOOK,
        "close": "subject_closeup",
        "diagram": "section_diagram",
    },
}

# --- 오버레이 (specs/03 "오버레이 타입 enum", ADR-0002 2계층, ADR-0019) ------


@dataclass(frozen=True)
class Overlay:
    """오버레이 타입 하나.

    `layer`는 ADR-0002가 정한 두 계층이다. ADR-0019로 레이어 A가 폐기돼 지금은 B뿐이다 —
    베이스 이미지는 전 씬 클린이고, 화면에 얹히는 것은 전부 후처리 합성이다.
    """

    type: str
    layer: str
    #: 베이스(클린) 이미지에서 배제할 문구.
    negative: str
    #: emphasis.value 같은 표시할 문자열이 필요한가.
    needs_value: bool = False


OVERLAYS: dict[str, Overlay] = {
    o.type: o
    for o in (
        # context_number / solution_number "대형 빨간 숫자 텍스트 (후처리 합성)"
        Overlay("big_red_text", "B", "large red number text", needs_value=True),
        # 전 씬 공통. 씬 항목으로는 붙지 않지만 emphasis.type enum에는 들어간다.
        Overlay("sparkle_particles", "B", "sparkle particle overlay"),
    )
}

#: specs/02가 emphasis.type을 "specs/03의 오버레이 타입 enum"이라고 했다. 그 enum이
#: 곧 위 레지스트리의 키다. 계약에 없는 타입이 오면 지어내지 않고 오류로 돌린다.
OVERLAY_TYPES = tuple(OVERLAYS)

# --- 비트별 오버레이·카메라 (specs/03 "비트별 오버레이·카메라" 12행) ---------


@dataclass(frozen=True)
class BeatRule:
    overlays: tuple[str, ...]
    #: '카메라 기본값' 열. 여러 개면 스펙이 "pan 또는 tilt"처럼 폭을 준 것이다.
    cameras: tuple[str, ...]


BEAT_RULES: dict[str, BeatRule] = {
    "hook_fact": BeatRule((), ("slow_zoom_in",)),
    "hook_twist": BeatRule((), ("static",)),
    "context": BeatRule((), ("pan_left", "pan_right", "tilt_down", "tilt_up")),
    "context_number": BeatRule(("big_red_text",), ("slow_zoom_in",)),
    "failed_solution": BeatRule((), ("static",)),
    "failure_reason": BeatRule((), ("slow_zoom_in",)),
    "dilemma_peak": BeatRule((), ("static",)),
    "turning_point": BeatRule((), ("slow_zoom_in",)),
    "solution_step": BeatRule((), ("tilt_down", "slow_zoom_in")),
    "solution_number": BeatRule(("big_red_text",), ("static",)),
    "present_link": BeatRule((), ("slow_zoom_out",)),
    "ending_echo": BeatRule((), ("slow_zoom_out",)),
}


def resolve_framing(
    beat: str,
    scale: str,
    *,
    prev: tuple[str, str, int] | None = None,
    hook: tuple[str, str, int] | None = None,
) -> tuple[str, str, int | None]:
    """`(구도 토큰, 출처, 참조 씬 id)`. specs/03 "비트 × 스케일 → 구도 토큰".

    `prev`/`hook`은 `(구도 토큰, subject_scale, scene_id)`다. 스펙 03이 참조를 푸는 순서를
    그대로 따른다 — 가리키는 씬의 스케일이 **같을 때만** 구도를 잇고, 다르거나 가리킬 씬이
    없으면 `REFERENCE_FALLBACK`을 쓴다. 스케일이 다른 씬의 구도를 그대로 이으면 애초에
    이 축을 도입한 이유(ADR-0018)가 무너진다.
    """
    token = FRAMING_TABLE[beat][scale]
    reference = {INHERIT_PREV: prev, ECHO_HOOK: hook}.get(token)
    if token not in REFERENCE_FALLBACK:
        return token, "beat_rule", None
    if reference is not None and reference[1] == scale:
        source = "prev_scene" if token == INHERIT_PREV else "hook_echo"
        return reference[0], source, reference[2]
    return REFERENCE_FALLBACK[token], "scale_fallback", None


# --- 프롬프트 조립 -----------------------------------------------------------


def build_prompt(shot: str, subject: str, visual_goal: str = "") -> str:
    """베이스(클린) 이미지 프롬프트.

    `subject`는 한국어 그대로 넣는다. 번역하면 `[1]`이 고른 피사체가 이 단계의
    창의적 판단으로 바뀌고(ADR-0001·0014 위반), 외부 의존도 생긴다. 대신 그 한국어가
    화면에 글자로 그려지지 않도록 못을 박는다 (ADR-0002).

    `visual_goal`도 한국어 그대로 싣는다 (ADR-0022). **이 그림이 무엇을 설명해야
    하는지를 모델에게 알려 주는 줄이다** — 피사체만 주면 모델은 그것을 그릴 뿐이고,
    그 그림이 왜 거기 있는지는 모른다. 필드가 없는 옛 대본도 있으므로 비면 뺀다.
    """
    lines = [
        "Subject (Korean description — depict it; "
        f"do not write these words in the image): {subject}",
    ]
    if visual_goal.strip():
        lines.append(
            "This image must explain (Korean; depict it, do not write it): "
            f"{visual_goal}"
        )
    lines.extend((f"Shot: {shot}", f"Style: {BASE_STYLE}", f"Framing: {COMPOSITION}"))
    return "\n".join(lines)


def build_negative(overlay_types: tuple[str, ...]) -> str:
    """베이스 이미지에서 배제할 것 = 전 씬 공통 + 이 씬에 붙는 오버레이 전부.

    레이어 A가 없어져(ADR-0019) 베이스에 그려야 할 오버레이는 하나도 없다. 씬에 붙는
    오버레이는 전부 레이어 B이므로 여기서는 **전부 배제 대상**이다.
    """
    items = list(GLOBAL_NEGATIVES)
    for name in overlay_types:
        negative = OVERLAYS[name].negative
        if negative not in items:
            items.append(negative)
    return "Do not include: " + "; ".join(items) + "."


# --- prompts.json 스키마 -----------------------------------------------------

OVERLAY_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type", "layer", "value"],
    "additionalProperties": False,
    "properties": {
        "type": {"enum": list(OVERLAY_TYPES)},
        "layer": {"enum": ["A", "B"]},
        "value": {"type": ["string", "null"]},
    },
}

PROMPT_SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "scene_id",
        "beat",
        "subject_scale",
        "camera",
        "motion",
        "framing",
        "framing_source",
        "prompt",
        "negative_prompt",
        "overlays",
    ],
    "additionalProperties": False,
    "properties": {
        "scene_id": {"type": "integer", "minimum": 1},
        "beat": {"type": "string", "minLength": 1},
        "subject_scale": {"enum": list(SUBJECT_SCALES)},
        "camera": {"type": "string", "minLength": 1},
        "motion": {"type": "string", "minLength": 1},
        "framing": {"enum": list(FRAMINGS)},
        "framing_source": {
            "enum": ["beat_rule", "prev_scene", "hook_echo", "scale_fallback"]
        },
        "framing_reuse_of": {"type": "integer", "minimum": 1},
        "prompt": {"type": "string", "minLength": 1},
        "negative_prompt": {"type": "string", "minLength": 1},
        "overlays": {"type": "array", "items": OVERLAY_ITEM_SCHEMA},
    },
}

PROMPTS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "prompts.json ([5. prompt] 산출물)",
    "type": "object",
    "required": ["run_id", "topic", "source_script", "style", "scenes"],
    "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "topic": {"type": "string", "minLength": 1},
        "source_script": {"type": "string", "minLength": 1},
        "style": {
            "type": "object",
            "required": [
                "base_style",
                "composition",
                "aspect_ratio",
                "resolution",
                "style_anchors",
                "global_overlays",
            ],
            "additionalProperties": False,
            "properties": {
                "base_style": {"type": "string", "minLength": 1},
                "composition": {"type": "string", "minLength": 1},
                "aspect_ratio": {"const": ASPECT_RATIO},
                "resolution": {"const": RESOLUTION},
                "style_anchors": {"type": "string", "minLength": 1},
                "global_overlays": {"type": "array", "items": {"type": "object"}},
            },
        },
        "scenes": {"type": "array", "minItems": 1, "items": PROMPT_SCENE_SCHEMA},
    },
}

_VALIDATOR = Draft202012Validator(PROMPTS_SCHEMA)


def schema_errors(data: Any) -> list[str]:
    """prompts.json 스키마 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors
