"""시각 연출 어휘와 이미지 프롬프트 조립. specs/03-visual-rules.md.

**어휘는 이 파일에 없다.** `specs/schema/vocab.json`에서 로드한다 (ADR-0034 §3) —
구도 토큰·오버레이 타입·스타일 문자열·배제 목록 전부. 여기 있는 것은 그 값을 프롬프트
문자열로 조립하는 방법뿐이다.

## 연출을 고르는 것은 이 모듈이 아니다 (ADR-0033 §3)

씬마다 무엇을 쓸지는 `[1s. sceneplan]`이 정해 씬 계약에 적는다. `[5. prompt]`는
그 값을 방언으로 옮기는 변환기이고, 씬이 값을 비웠을 때만 `beat-defaults.json`의
**기본값**으로 떨어진다 (`vocab.default_framing`). 그 표는 지시가 아니라 폴백이며
ADR-0033을 되돌릴 자리다.

## 방언 (ADR-0027)

프롬프트 **문법**은 프로바이더마다 다르다 (`build_scene_prompt`). 어휘는 다르지 않다 —
스타일 문자열·구도 토큰·배제 항목은 방언과 무관하게 같은 값이고, 바뀌는 것은 그것을
어떻게 적느냐뿐이다.

## 이 모듈이 다루지 않는 것

- 전환·자막 스타일: 스펙 03에 있지만 `[9. assemble]` 소관이다
- 카메라 워크 파라미터: `[7. motion]` 소관이다 (스펙 03의 표)
- 레이어 A 어노테이션: 폐기됐다 (ADR-0019). 베이스 이미지는 전 씬 클린이다
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

# --- 베이스 스타일 (vocab.json meta.style) -----------------------------------
#
# 실호출 6회로 검증한 문구다. "realism dominant"가 핵심이다 — 수채로 뭉개면 재질이
# 사라지고, 이 채널에서 재질은 곧 설명이다 (ADR-0022·0023).

BASE_STYLE: str = vocab.style("base_style")
COMPOSITION: str = vocab.style("composition")
ASPECT_RATIO: str = vocab.style("aspect_ratio")
RESOLUTION: str = vocab.style("resolution")
STYLE_ANCHOR_DIR: str = vocab.style("style_anchor_dir")

#: 어느 씬에서든 베이스 이미지에 들어오면 안 되는 것. 두 목록은 각자의 프로바이더에서
#: 실호출로 검증된 값이라(NB2 6회 / MJ 9잡) 한쪽에서 다른 쪽을 유도하지 않는다.
GLOBAL_NEGATIVES: tuple[str, ...] = tuple(vocab.style("negatives")["nb2"])
MJ_GLOBAL_NEGATIVES: tuple[str, ...] = tuple(vocab.style("negatives")["mj"])

# --- 어휘 -------------------------------------------------------------------

SUBJECT_SCALES: tuple[str, ...] = vocab.values("subject_scale")
FRAMING_TOKENS: tuple[str, ...] = vocab.values("framing")
OVERLAY_TYPES: tuple[str, ...] = vocab.values("overlay_type")
DIALECTS: tuple[str, ...] = vocab.values("dialect")
DEFAULT_DIALECT: str = vocab.meta("dialect")["default"]


@dataclass(frozen=True)
class Framing:
    """구도 토큰 하나. `shot`이 프롬프트에 그대로 들어간다."""

    token: str
    shot: str
    #: 어울리는 `subject_scale`. 표시이지 강제가 아니다 (ADR-0033 §3).
    scale: str


FRAMINGS: dict[str, Framing] = {
    token: Framing(token, item["shot"], item["scale"])
    for token, item in vocab.meta("framing").items()
}


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
    name: Overlay(name, item["layer"], item["negative"], item["needs_value"])
    for name, item in vocab.meta("overlay").items()
}

#: 씬 항목이 아니라 전 씬 공통으로 얹히는 오버레이. `prompts.json`의 style 블록에 실린다.
#: 소비자는 `[8. overlay]`다.
GLOBAL_OVERLAYS: tuple[dict[str, Any], ...] = tuple(
    {
        "type": name,
        "layer": item["layer"],
        "placement": item.get("placement", ""),
        "scope": item["scope"],
    }
    for name, item in vocab.meta("overlay").items()
    if item.get("scope") == "all_scenes"
)


def resolve_framing(scene: dict[str, Any]) -> tuple[str, str]:
    """`(구도 토큰, 출처)`. 씬이 고른 값이 먼저다 (ADR-0033 §3).

    비어 있으면 `beat-defaults.json`의 기본값으로 떨어진다. **경고하지 않는다** —
    선택 필드의 부재는 경고가 아니고(D-3), 대신 `[5]`가 기본값으로 떨어진 씬 수를
    요약에 낸다. 그 수가 관측 수단이다.
    """
    token = scene.get("framing")
    if token in FRAMINGS:
        return token, "scene"
    return vocab.default_framing(scene["beat"], scene["subject_scale"]), "beat_default"


# --- 프롬프트 조립 -----------------------------------------------------------


def clean_anchors(anchors: Sequence[str]) -> list[str]:
    """`subject_anchor`를 프롬프트에 실을 수 있는 형태로 다듬는다 (ADR-0028).

    **거르지 않는다.** 씬마다 어느 명사가 더 센지는 이 모듈이 알 수단이 없다 —
    고르는 것은 `[1s. sceneplan]`이다 (G2). 여기서 하는 일은 공백 제거와 빈 항목
    탈락뿐이다.
    """
    return [item.strip() for item in anchors if item.strip()]


def build_prompt(
    shot: str,
    subject: str,
    visual_goal: str = "",
    anchors: Sequence[str] = (),
) -> str:
    """베이스(클린) 이미지 프롬프트.

    `subject`는 한국어 그대로 넣는다. 번역하면 `[1s]`가 고른 피사체가 이 단계의
    판단으로 바뀌고(ADR-0014·0033), 외부 의존도 생긴다. 대신 그 한국어가 화면에
    글자로 그려지지 않도록 못을 박는다 (ADR-0002).

    `visual_goal`도 한국어 그대로 싣는다 (ADR-0022). **이 그림이 무엇을 설명해야
    하는지를 모델에게 알려 주는 줄이다** — 피사체만 주면 모델은 그것을 그릴 뿐이고,
    그 그림이 왜 거기 있는지는 모른다. 필드가 없는 옛 대본도 있으므로 비면 뺀다.

    `anchors`(ADR-0028)는 **Subject 줄에 콤마 항목으로 잇는다.** 별개 줄로 빼면 그
    한국어가 "글자로 쓰지 마라"는 지시 밖에 놓인다 — 앵커도 한국어라 같은 못이 필요하다.
    """
    subject_line = ", ".join([subject, *clean_anchors(anchors)])
    lines = [
        "Subject (Korean description — depict it; "
        f"do not write these words in the image): {subject_line}",
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


# --- Midjourney 방언 (ADR-0027) ----------------------------------------------
#
# 같은 어휘의 **표기 변환**이다. 스타일 문자열도 구도 토큰도 방언마다 달라지지 않는다 —
# 달라지는 것은 문법뿐이다: 한 줄 콤마 나열, `--ar`, `--no`.
#
# 실측 근거는 `runs/20260812-mj-lang-probe/`와 ADR-0025의 G3·단면 탐침이다. 특히
# **한국어 `subject`를 그대로 넘긴다** — 영어로 옮겨도 품질이 같았고, 실패 지점은
# 언어가 아니라 재질·정체 명사의 부재였다 (ADR-0028).


def flatten_clauses(text: str) -> str:
    """여러 절을 한 줄 콤마 나열로. MJ는 `:`·`;`를 구분자로 읽지 않는다."""
    return text.replace(": ", ", ").replace("; ", ", ")


def _mj_composition(composition: str) -> str:
    """구도 문구에서 MJ가 쓸 부분만 남긴다. `COMPOSITION`이 유일한 출처다.

    두 절을 뗀다.

    - `vertical 9:16` — `--ar`가 정한다. 프롬프트 본문에 남기면 종횡비를 두 곳에서 말한다
    - `for subtitles` — `--no`에 `burned-in subtitles`가 있다. **같은 낱말을 그리라고 하면서
      그리지 말라고 하게 된다.** 자막을 위해 비운다는 것은 스펙의 이유이지 모델에게 할 말이
      아니다 (ADR-0027)
    """
    _aspect, rest = composition.split("; ", 1)
    return flatten_clauses(rest.replace(" for subtitles", ""))


MJ_COMPOSITION = _mj_composition(COMPOSITION)


def build_mj_prompt(
    shot: str,
    subject: str,
    visual_goal: str = "",
    anchors: Sequence[str] = (),
) -> str:
    """MJ 방언의 양성 프롬프트. `--ar`까지 포함하고 `--no`는 `build_mj_negative()`가 낸다.

    라벨(`Subject:`·`Shot:`)을 붙이지 않는다 — MJ는 그것을 구분자가 아니라 그릴 대상으로
    읽는다. 그래서 NB2 프롬프트가 라벨로 하던 일("이 한국어를 그리되 글자로 쓰지 마라")은
    `--no legible text, letters, …`가 대신한다.

    앵커는 **`subject` 바로 뒤**다 (ADR-0028 G1). 실측한 문자열이 그 자리이고
    (`홈이 파인 블록 접합면 클로즈업, 콘크리트` → 콘크리트 4/4), 뒤로 밀면 스타일·구도
    토큰 뒤에 놓여 검증한 적 없는 배치가 된다.
    """
    parts = [subject.strip(), *clean_anchors(anchors)]
    if visual_goal.strip():
        parts.append(visual_goal.strip())
    parts.extend((shot, flatten_clauses(BASE_STYLE), MJ_COMPOSITION))
    return ", ".join(parts) + f" --ar {ASPECT_RATIO}"


def build_mj_negative(overlay_types: tuple[str, ...]) -> str:
    """MJ 방언의 `--no`. 문장이 아니라 항목 나열이다."""
    items = list(MJ_GLOBAL_NEGATIVES)
    for name in overlay_types:
        negative = OVERLAYS[name].negative
        if negative not in items:
            items.append(negative)
    return "--no " + ", ".join(items)


def build_scene_prompt(
    dialect: str,
    *,
    shot: str,
    subject: str,
    visual_goal: str,
    overlay_types: tuple[str, ...],
    anchors: Sequence[str] = (),
) -> tuple[str, str]:
    """`(prompt, negative_prompt)`를 방언에 맞게 조립한다. `[5]`가 부르는 유일한 입구다.

    `anchors`는 선택 필드라 비어 있는 채로 올 수 있다 (ADR-0028). 두 방언 모두 값이
    있을 때만 싣는다 — 빈 목록이면 문자열이 앵커 도입 전과 한 바이트도 다르지 않다.
    """
    if dialect == "mj":
        return (
            build_mj_prompt(shot, subject, visual_goal, anchors),
            build_mj_negative(overlay_types),
        )
    if dialect == "nb2":
        return (
            build_prompt(shot, subject, visual_goal, anchors),
            build_negative(overlay_types),
        )
    raise ValueError(f"모르는 방언이다: {dialect!r} (허용: {', '.join(DIALECTS)})")


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
        "framing": {"enum": list(FRAMING_TOKENS)},
        # ADR-0033 — 구도가 씬 계약에서 왔는지 기본값으로 떨어졌는지. 되돌릴 조건의
        # 관측 수단이라 산출물에 남긴다.
        "framing_source": {"enum": ["scene", "beat_default"]},
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
                "dialect",
                "base_style",
                "composition",
                "aspect_ratio",
                "resolution",
                "style_anchors",
                "global_overlays",
            ],
            "additionalProperties": False,
            "properties": {
                # ADR-0027 — 이 파일이 어느 프로바이더 문법으로 쓰였는가. [6]이 자기
                # 프로바이더와 대조한다. 필수라서 옛 prompts.json은 위반이 되는데,
                # [5]는 무료·결정적이라 다시 돌리면 된다 (마이그레이션 단계 없음).
                "dialect": {"enum": list(DIALECTS)},
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
