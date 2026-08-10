"""비트 → 시각 연출 룰 테이블과 이미지 프롬프트 조립. specs/03-visual-rules.md.

`[5. prompt]`가 쓰는 유일한 판단 근거다. ADR-0001에 따라 여기 없는 연출은 만들어 내지
않는다 — 스펙 03이 택일이나 조건부로 남겨 둔 자리는 임의로 메우는 대신 `rule_gaps`에
어떤 자리를 무엇으로 채웠는지 적어 내보낸다.

## 이 모듈이 스펙 03에서 그대로 옮겨온 것

- 베이스 스타일 3줄 (`BASE_STYLE`, `COMPOSITION`, 우하단 파티클 = `GLOBAL_OVERLAYS`)
- 비트별 룰 테이블 12행 (`BEAT_RULES`: 구도 / 오버레이 / 카메라 기본값)
- 텍스트 2계층 원칙 → 오버레이 타입마다 `layer`가 붙는다 (ADR-0002)
- 모션별 어노테이션 규칙 → `kenburns`는 2-pass 편집, `kling`은 클린 이미지 (ADR-0006)

## 이 모듈이 다루지 않는 것

- 전환 규칙·자막 스타일: 스펙 03에 있지만 `[9. assemble]` 소관이다
- 카메라 값 결정: `camera`는 이미 씬 계약에 들어 있다 (ADR-0014에서 `[1]`이 채운다).
  여기서는 비트 기본값과 어긋나는지 **검사만** 한다
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

# --- 베이스 스타일 (specs/03 "베이스 스타일 (전 씬 공통)") -------------------

#: "photorealistic 3D 디오라마/조감 렌더 스타일, 자연광"
BASE_STYLE = "photorealistic 3D diorama / aerial-render look, natural light"

#: "9:16 구도, 피사체는 중앙~상단 1/3에 배치 (하단 1/3은 자막 영역으로 비움)"
COMPOSITION = (
    "vertical 9:16; subject placed in the centre to upper third; "
    "bottom third left empty for subtitles"
)

ASPECT_RATIO = "9:16"

#: ADR-0005 "해상도: 2K (… 4K 금지)"
RESOLUTION = "2K"

#: ADR-0005 "스타일 앵커 이미지 3~5장을 … 모든 생성 호출에 레퍼런스로 첨부"
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
)

# --- 구도 (specs/03 룰 테이블 '구도' 열) --------------------------------------


@dataclass(frozen=True)
class Framing:
    """구도 토큰 하나. `shot`이 프롬프트에 그대로 들어간다."""

    token: str
    shot: str
    #: 야외 광각/조감을 전제하는 구도인가. 프롬프트를 바꾸지 않고 경고 판정에만 쓴다.
    wide_exterior: bool = False


FRAMINGS: dict[str, Framing] = {
    f.token: f
    for f in (
        # hook_fact "드론 뷰/광각 전경"
        Framing("drone_wide", "aerial drone establishing shot, wide view of the site",
                wide_exterior=True),
        # hook_twist 두 번째 선택지 "피사체 클로즈업"
        Framing("subject_closeup", "close-up of the main subject"),
        # context / context_number "조감 디오라마"
        Framing("aerial_diorama", "bird's-eye diorama view of the scene",
                wide_exterior=True),
        # failed_solution "해법 대상 미디엄 샷"
        Framing("medium_shot", "medium shot of the attempted solution"),
        # failure_reason "실패 결과 묘사 (무너짐, 넘침 등)"
        Framing("failure_result", "shot that shows the failure outcome itself"),
        # dilemma_peak "문제 상황 와이드 뷰"
        Framing("problem_wide", "wide view of the whole problem situation",
                wide_exterior=True),
        # turning_point "핵심 피사체 정면, 대칭 구도"
        Framing("frontal_symmetric",
                "frontal view of the key subject, symmetrical composition"),
        # solution_step "단면(cross-section) 컷"
        Framing("cross_section", "cross-section cutaway, cut plane facing the camera"),
        # solution_number "해결책 디테일 클로즈업"
        Framing("detail_closeup", "tight detail close-up of the solution"),
        # present_link "현재 실사풍 전경"
        Framing("present_photoreal_wide", "present-day photoreal wide shot",
                wide_exterior=True),
    )
}

#: 구도가 앞 씬에서 이어진다는 표시 (hook_twist "전경 유지").
INHERIT_PREV = "@prev"
#: 구도가 훅 씬에서 온다는 표시 (ending_echo "훅과 동일/유사 구도 재사용").
ECHO_HOOK = "@hook"

# --- 오버레이 (specs/03 룰 테이블 '오버레이' 열 + ADR-0002 2계층) ------------


@dataclass(frozen=True)
class Overlay:
    """오버레이 타입 하나.

    `layer`는 ADR-0002가 정한 두 계층이다.
    - A: 이미지 생성 단계에서 그린다. 분위기용이라 글자 정확도를 요구하지 않는다
    - B: 후처리 합성. 시청자가 읽어야 하는 텍스트가 들어가는 것은 전부 여기다
    """

    type: str
    layer: str
    #: 레이어 A 2-pass 편집 지시문. `{subject}`가 씬 피사체로 치환된다.
    annotation: str | None
    #: 베이스(클린) 이미지에서 배제할 문구. 레이어 A든 B든 베이스에는 없어야 한다
    #: (ADR-0005 "클린 베이스 생성 → 편집 2-pass", ADR-0006 "클린 이미지를 Kling에 입력").
    negative: str
    #: emphasis.value 같은 표시할 문자열이 필요한가. 레이어 B 텍스트 오버레이만 참이다.
    needs_value: bool = False


OVERLAYS: dict[str, Overlay] = {
    o.type: o
    for o in (
        # hook_twist / failure_reason "빨간 크레용 X"
        Overlay(
            "red_crayon_x", "A",
            "a big hand-drawn red crayon X scrawled over {subject}",
            "red crayon X mark",
        ),
        # context "빨간 측정선/영역 표시"
        Overlay(
            "red_measure_line", "A",
            "thin red measurement lines and a red outlined area marking {subject}",
            "red measurement lines or outlined areas",
        ),
        # context_number / solution_number "대형 빨간 숫자 텍스트 (후처리 합성)"
        Overlay("big_red_text", "B", None, "large red number text", needs_value=True),
        # failed_solution "빨간 라벨 박스 (지도핀 스타일)"
        Overlay(
            "red_label_box", "A",
            "a red map-pin style label box pointing at {subject}",
            "red label boxes or map pins",
        ),
        # dilemma_peak "빨간 X 대형"
        Overlay(
            "red_x_large", "A",
            "an oversized red X drawn across the whole frame",
            "large red X mark",
        ),
        # turning_point "빨간 크레용 X → 사라짐". 시간 변화라 정지 이미지에 담을 수 없어
        # 레이어 B로 올린다 (rule_gaps: turning_point_overlay_temporal).
        Overlay("red_crayon_x_fadeout", "B", None, "red crayon X mark"),
        # solution_step "빨간 치수선/화살표"
        Overlay(
            "red_dimension_arrow", "A",
            "red dimension lines and arrows annotating {subject}",
            "red dimension lines or arrows",
        ),
        # hook_fact "장소명 라벨" / present_link "장소명 라벨 박스".
        # ADR-0002가 "장소명 라벨"을 레이어 B로 못 박았다.
        Overlay("place_label", "B", None, "place-name label", needs_value=True),
        Overlay("place_label_box", "B", None, "place-name label box", needs_value=True),
    )
}

#: specs/02가 emphasis.type을 "specs/03의 오버레이 타입 enum"이라고 했다. 그 enum이
#: 곧 위 레지스트리의 키다. 계약에 없는 타입이 오면 지어내지 않고 오류로 돌린다.
OVERLAY_TYPES = tuple(OVERLAYS)

# --- 비트 룰 테이블 (specs/03 "비트별 룰" 12행) ------------------------------


@dataclass(frozen=True)
class BeatRule:
    framing: str
    overlays: tuple[str, ...]
    #: '카메라 기본값' 열. 여러 개면 스펙이 "pan 또는 tilt"처럼 폭을 준 것이다.
    cameras: tuple[str, ...]


BEAT_RULES: dict[str, BeatRule] = {
    "hook_fact": BeatRule("drone_wide", (), ("slow_zoom_in",)),
    "hook_twist": BeatRule(INHERIT_PREV, ("red_crayon_x",), ("static",)),
    "context": BeatRule(
        "aerial_diorama", ("red_measure_line",),
        ("pan_left", "pan_right", "tilt_down", "tilt_up"),
    ),
    "context_number": BeatRule("aerial_diorama", ("big_red_text",), ("slow_zoom_in",)),
    "failed_solution": BeatRule("medium_shot", ("red_label_box",), ("static",)),
    "failure_reason": BeatRule("failure_result", ("red_crayon_x",), ("slow_zoom_in",)),
    "dilemma_peak": BeatRule("problem_wide", ("red_x_large",), ("static",)),
    "turning_point": BeatRule(
        "frontal_symmetric", ("red_crayon_x_fadeout",), ("slow_zoom_in",)
    ),
    "solution_step": BeatRule(
        "cross_section", ("red_dimension_arrow",), ("tilt_down", "slow_zoom_in")
    ),
    "solution_number": BeatRule("detail_closeup", ("big_red_text",), ("static",)),
    "present_link": BeatRule(
        "present_photoreal_wide", ("place_label_box",), ("slow_zoom_out",)
    ),
    "ending_echo": BeatRule(ECHO_HOOK, (), ("slow_zoom_out",)),
}

#: hook_twist가 첫 씬이라 이어받을 앞 씬이 없을 때 쓰는 두 번째 선택지.
HOOK_TWIST_FALLBACK = "subject_closeup"

# --- 스펙 03이 결정을 남겨 둔 자리 -------------------------------------------


@dataclass(frozen=True)
class RuleGap:
    """룰 테이블만으로 값이 정해지지 않아 이 모듈이 한 번 더 고른 자리.

    스펙에 없는 연출을 만든 것이 아니라, 스펙이 준 선택지 중 하나를 고르거나
    물리적으로 불가능한 조합을 피한 기록이다. 산출물에 그대로 실어 보내
    ADR/스펙 수정의 입력이 되게 한다 (CLAUDE.md 원칙 3의 "룰 테이블에 추가하는 PR").
    """

    code: str
    beats: tuple[str, ...]
    issue: str
    resolution: str


RULE_GAPS: tuple[RuleGap, ...] = (
    RuleGap(
        "hook_fact_overlay_choice", ("hook_fact",),
        "오버레이가 '없음 또는 장소명 라벨' 택일인데 고르는 기준이 없다. "
        "라벨에 넣을 장소명의 출처도 06-script.json 계약에 없다",
        "'없음'을 쓴다. 다른 선택지는 계약에 없는 텍스트를 지어내야 성립한다",
    ),
    RuleGap(
        "hook_twist_framing_choice", ("hook_twist",),
        "구도가 '전경 유지 or 피사체 클로즈업' 택일인데 고르는 기준이 없다",
        "'전경 유지'를 앞 씬 구도 상속으로 읽어 적용한다 "
        "(앞 씬이 없으면 피사체 클로즈업). 이미지 재사용이 아니라 구도만 잇는다 — "
        "hook_twist의 subject는 앞 씬과 다른 대상이다",
    ),
    RuleGap(
        "context_number_framing_choice", ("context_number",),
        "구도가 '조감 or 대상 클로즈업' 택일인데 고르는 기준이 없다",
        "먼저 적힌 '조감 디오라마'를 쓴다",
    ),
    RuleGap(
        "solution_step_cross_section_conditional", ("solution_step",),
        "구도가 '단면 컷 우선 고려'로 조건부인데 언제 다른 컷을 쓰는지가 없다",
        "항상 단면 컷을 쓴다",
    ),
    RuleGap(
        "turning_point_overlay_temporal", ("turning_point",),
        "'빨간 크레용 X → 사라짐'은 시간에 따른 변화라 정지 이미지(레이어 A)에 담을 수 없다",
        "레이어 B(후처리 오버레이)로 올린다. 베이스 이미지에는 X를 넣지 않는다",
    ),
    RuleGap(
        "failed_solution_label_text", ("failed_solution",),
        "'빨간 라벨 박스(지도핀 스타일)'에 들어갈 문구의 출처가 계약에 없다",
        "레이어 A로 둔다. ADR-0002가 레이어 A의 글자 정확도를 요구하지 않으므로 "
        "문구 없이 성립한다",
    ),
    RuleGap(
        "present_link_place_label_text", ("present_link",),
        "'장소명 라벨 박스'는 ADR-0002상 레이어 B인데, 넣을 장소명의 출처가 계약에 없다",
        "레이어 B 항목으로 싣되 value는 null로 둔다. [8. overlay]가 채울 출처가 필요하다",
    ),
)

#: 야외 광각/조감 구도와 어울리지 않는 피사체를 걸러내는 조언용 키워드.
#: 스펙 03의 구도 열은 한국 건축 3편 실측에서 나와 드론 뷰·조감 디오라마로 기운다.
#: **프롬프트를 바꾸지 않는다** — 룰과 피사체가 부딪히는 씬을 경고로 세어 보고할 뿐이다.
CLOSE_SUBJECT_KEYWORDS: tuple[str, ...] = (
    "단면", "내부", "도면", "평면도", "클로즈업", "확대",
    "표면", "끝단", "접합면", "눈금", "센서", "일러스트", "노트",
)


def framing_conflicts(scenes: list[dict[str, Any]], framings: list[str]) -> list[int]:
    """구도가 야외 광각인데 피사체는 근접·내부·도해인 씬의 scene_id 목록."""
    hits = []
    for scene, token in zip(scenes, framings):
        framing = FRAMINGS.get(token)
        if framing is None or not framing.wide_exterior:
            continue
        subject = scene.get("subject", "")
        if any(word in subject for word in CLOSE_SUBJECT_KEYWORDS):
            hits.append(scene["scene_id"])
    return hits


# --- 프롬프트 조립 -----------------------------------------------------------


def build_prompt(shot: str, subject: str) -> str:
    """베이스(클린) 이미지 프롬프트.

    `subject`는 한국어 그대로 넣는다. 번역하면 `[1]`이 고른 피사체가 이 단계의
    창의적 판단으로 바뀌고(ADR-0001·0014 위반), 외부 의존도 생긴다. 대신 그 한국어가
    화면에 글자로 그려지지 않도록 못을 박는다 (ADR-0002).
    """
    return "\n".join(
        (
            "Subject (Korean description — depict it; "
            f"do not write these words in the image): {subject}",
            f"Shot: {shot}",
            f"Style: {BASE_STYLE}",
            f"Framing: {COMPOSITION}",
        )
    )


def build_negative(overlay_types: tuple[str, ...]) -> str:
    """베이스 이미지에서 배제할 것 = 전 씬 공통 + 이 씬에 붙는 오버레이 전부."""
    items = list(GLOBAL_NEGATIVES)
    for name in overlay_types:
        negative = OVERLAYS[name].negative
        if negative not in items:
            items.append(negative)
    return "Do not include: " + "; ".join(items) + "."


def build_annotation(overlay_types: tuple[str, ...], subject: str) -> str | None:
    """레이어 A 어노테이션 2-pass 편집 지시 (ADR-0005). 없으면 None."""
    fragments = [
        OVERLAYS[name].annotation.format(subject=subject)
        for name in overlay_types
        if OVERLAYS[name].annotation
    ]
    if not fragments:
        return None
    return "\n".join(
        (
            "Edit the given image. Keep the existing composition, subject and "
            "lighting unchanged.",
            "Add: " + "; ".join(fragments) + ".",
            "Hand-drawn red annotation look, as if scrawled on top of the render. "
            "Any lettering inside the annotation does not need to be legible.",
        )
    )


# --- prompts.json 스키마 -----------------------------------------------------

OVERLAY_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type", "layer", "value"],
    "additionalProperties": False,
    "properties": {
        "type": {"enum": list(OVERLAY_TYPES)},
        "layer": {"enum": ["A", "B"]},
        "value": {"type": ["string", "null"]},
        "layer_note": {"type": "string"},
    },
}

PROMPT_SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "scene_id",
        "beat",
        "camera",
        "motion",
        "framing",
        "framing_source",
        "prompt",
        "negative_prompt",
        "annotation_prompt",
        "overlays",
    ],
    "additionalProperties": False,
    "properties": {
        "scene_id": {"type": "integer", "minimum": 1},
        "beat": {"type": "string", "minLength": 1},
        "camera": {"type": "string", "minLength": 1},
        "motion": {"type": "string", "minLength": 1},
        "framing": {"enum": list(FRAMINGS)},
        "framing_source": {"enum": ["beat_rule", "prev_scene", "hook_echo"]},
        "framing_reuse_of": {"type": "integer", "minimum": 1},
        "prompt": {"type": "string", "minLength": 1},
        "negative_prompt": {"type": "string", "minLength": 1},
        "annotation_prompt": {"type": ["string", "null"]},
        "overlays": {"type": "array", "items": OVERLAY_ITEM_SCHEMA},
    },
}

PROMPTS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "prompts.json ([5. prompt] 산출물)",
    "type": "object",
    "required": ["run_id", "topic", "source_script", "style", "scenes", "rule_gaps"],
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
        "rule_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["code", "scene_ids", "issue", "resolution"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string", "minLength": 1},
                    "scene_ids": {"type": "array", "items": {"type": "integer"}},
                    "issue": {"type": "string", "minLength": 1},
                    "resolution": {"type": "string", "minLength": 1},
                },
            },
        },
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
