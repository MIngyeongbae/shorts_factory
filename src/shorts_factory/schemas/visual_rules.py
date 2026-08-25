"""시각 연출 어휘와 영상 프롬프트 골격. specs/03-visual-rules.md 「프롬프트 골격」.

**어휘도 문구도 이 파일에 없다.** `specs/schema/vocab.json`에서 로드한다 (ADR-0034 §3) —
구도 토큰·무대 문구·카메라 문구·계측 표시 문구·스타일 문자열·배제 목록 전부. 여기
있는 것은 **절의 순서와 치환**뿐이다 (스펙 03: "코드는 순서와 치환만 안다").

## 연출을 고르는 것은 이 모듈이 아니다 (ADR-0033 §3)

씬마다 무엇을 쓸지는 `[3s. scenetable]`이 정해 씬 계약에 적는다. `[5. prompt]`는
그 값을 골격에 채우는 변환기이고, 씬이 값을 비웠을 때만 `beat-defaults.json`의
**기본값**으로 떨어진다 (`resolve_framing`·`resolve_staging`). 그 표는 지시가 아니라
폴백이며 ADR-0033을 되돌릴 자리다.

## 골격 (ADR-0056 — 원카 레퍼런스 프롬프트의 구조. ADR-0060 — SUBJECT·RED·착지는 세션이 쓴다)

    FORMAT    A {composition} shot, {seconds} seconds long, {style.base_style}.   ← 초 수는 [7]이 채운다
    STAGING   {staging.*.phrase}
    SUBJECT   {세션의 subject_prompt — 영어 단락, 설명의 무대}
    CAMERA    {camera.*.video_prompt}, {세션의 camera_target}.
    RED       {세션의 red_prompt — 기하 + 라벨 따옴표째} + {annotation._closing}   ← info 씬만
    NEGATIVE  No {negatives.always…}. (info 없는 씬) No {negatives.no_text…}. {negatives.audio}

절 이름은 대문자 표제로 프롬프트에 그대로 박힌다. 세션 단락의 계약(영어·ASCII·길이·라벨 포함·
착지에 워크 단어 금지)은 `promptplan.py`가 잰다. 방언은 없다 — 라인이 달라도 프롬프트는 같다
(ADR-0059).

## 이 모듈이 다루지 않는 것

- 전환·자막 스타일: `[9. assemble]` 소관이다
- 클립 길이: `[7]`이 실측에서 정하고 FORMAT의 `{seconds}`를 그때 채운다
- 단락의 내용: 세션(`prompts/05-prompt.md`)이 대본·씬 계약·팩트체크에서 쓴다. 여기서는
  문장을 짓지 않는다 — 어휘 문구와 세션 단락을 절 순서대로 놓을 뿐이다
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

# --- 베이스 스타일 (vocab.json meta.style) -----------------------------------

BASE_STYLE: str = vocab.style("base_style")
COMPOSITION: str = vocab.style("composition")
ASPECT_RATIO: str = vocab.style("aspect_ratio")
RESOLUTION: str = vocab.style("resolution")

#: 어느 씬에서든 클립에 들어오면 안 되는 것 (`negatives.always`).
GLOBAL_NEGATIVES: tuple[str, ...] = tuple(vocab.negatives("always"))
#: `info`가 없는 씬에만 더하는 글자 금지 (`negatives.no_text`). info 씬은 RED 절의
#: 마무리 문장("the only text in the frame")이 그 역할을 한다.
NO_TEXT_NEGATIVES: tuple[str, ...] = tuple(vocab.negatives("no_text"))
#: Omni가 내는 오디오는 버리지만 프롬프트에서도 막는다 (`negatives.audio`).
AUDIO_NEGATIVE: str = str(vocab.negatives("audio"))

# --- 어휘 -------------------------------------------------------------------

SUBJECT_SCALES: tuple[str, ...] = vocab.values("subject_scale")
FRAMING_TOKENS: tuple[str, ...] = vocab.values("framing")
STAGING_TOKENS: tuple[str, ...] = vocab.values("staging")
ANNOTATION_TOKENS: tuple[str, ...] = vocab.values("annotation")

#: 무대 → 문구. STAGING 절에 그대로 들어간다 (ADR-0056 결정 4).
STAGINGS: dict[str, str] = {token: vocab.phrase("staging", token) for token in STAGING_TOKENS}
#: 계측 표시 방식 → 문구 (`{target}`·`{label}` 자리 포함). RED 절에 들어간다 (결정 3).
ANNOTATIONS: dict[str, str] = {
    token: vocab.phrase("annotation", token) for token in ANNOTATION_TOKENS
}
#: RED 절의 마무리 — 라벨이 유일한 텍스트·유일한 채도 높은 빨강이라는 못.
ANNOTATION_CLOSING: str = vocab.annotation_closing()
#: 카메라 워크 → 영상 문구. CAMERA 절에 그대로 들어간다.
CAMERA_PROMPTS: dict[str, str] = {
    token: vocab.video_prompt(token) for token in vocab.values("camera")
}

#: FORMAT 절의 초 수 자리. `[5]`는 길이를 쓰지 않는다 — `[7]`이 실측에서 채운다 (스펙 05).
SECONDS_PLACEHOLDER = "{seconds}"

#: 골격의 절 이름 — 이 순서로 프롬프트에 박힌다 (스펙 03 「프롬프트 골격」).
SECTIONS: tuple[str, ...] = ("FORMAT", "STAGING", "SUBJECT", "CAMERA", "RED", "NEGATIVE")

#: 구도가 씬 계약에서 왔는지 기본값으로 떨어졌는지 (ADR-0033 되돌릴 조건의 관측 수단).
FROM_SCENE = "scene"
FROM_DEFAULT = "beat_default"


@dataclass(frozen=True)
class Framing:
    """구도 토큰 하나. `shot`이 SUBJECT 절 뒤에 그대로 들어간다."""

    token: str
    shot: str
    #: 어울리는 `subject_scale`. 표시이지 강제가 아니다 (ADR-0033 §3).
    scale: str


FRAMINGS: dict[str, Framing] = {
    token: Framing(token, item["shot"], item["scale"])
    for token, item in vocab.meta("framing").items()
}


def resolve_framing(scene: dict[str, Any]) -> tuple[str, str]:
    """`(구도 토큰, 출처)`. 씬이 고른 값이 먼저다 (ADR-0033 §3).

    비어 있으면 `beat-defaults.json`의 기본값으로 떨어진다. **경고하지 않는다** —
    선택 필드의 부재는 경고가 아니고(D-3), 대신 `[5]`가 기본값으로 떨어진 씬 수를
    요약에 낸다. 그 수가 관측 수단이다.
    """
    token = scene.get("framing")
    if token in FRAMINGS:
        return token, FROM_SCENE
    return vocab.default_framing(scene["beat"], scene["subject_scale"]), FROM_DEFAULT


def resolve_staging(scene: dict[str, Any]) -> tuple[str, str]:
    """`(무대, 출처)`. `resolve_framing`과 같은 태도다 (ADR-0056 결정 4)."""
    token = scene.get("staging")
    if token in STAGINGS:
        return token, FROM_SCENE
    return vocab.default_staging(scene["beat"]), FROM_DEFAULT


# --- 프롬프트 조립 -----------------------------------------------------------


def _sentence(text: str) -> str:
    """끝에 마침표가 없으면 붙인다. 어휘 문구는 마침표로 끝나고 씬 값은 그렇지 않다."""
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _negation(items: Sequence[str]) -> str:
    """배제 항목을 'No a, no b, no c.' 한 문장으로 (vocab `negatives._role`)."""
    items = [item.strip() for item in items if item.strip()]
    if not items:
        return ""
    return "No " + ", no ".join(items) + "."


def _section(name: str, text: str) -> str:
    """`NAME: text`. 어휘 문구가 이미 그 표제로 시작하면(RED 절) 겹쳐 붙이지 않는다."""
    text = text.strip()
    if text.upper().startswith(f"{name}:"):
        return text
    return f"{name}: {text}"


def format_line(*, seconds: str = SECONDS_PLACEHOLDER) -> str:
    """FORMAT 절. 초 수 자리는 `[7]`이 채운다 — 여기서는 자리표시자 그대로다."""
    return f"A {COMPOSITION} shot, {seconds} seconds long, {BASE_STYLE}."


#: 어휘 문구 안의 치환 자리 (vocab `annotation._role`).
TARGET_SLOT = "{target}"
LABEL_SLOT = "{label}"


def negative_items(*, has_info: bool) -> tuple[str, ...]:
    """NEGATIVE 항목 목록 — 프로바이더의 네거티브 필드에 넣는 값. info 씬은 글자 금지를 뺀다."""
    items = list(GLOBAL_NEGATIVES)
    if not has_info:
        items.extend(NO_TEXT_NEGATIVES)
    return tuple(items)


def negative_line(*, has_info: bool) -> str:
    """NEGATIVE 절 — 'No …' 문장 + (info 없는 씬) 글자 금지 + 오디오 금지."""
    sentences = [_negation(GLOBAL_NEGATIVES)]
    if not has_info:
        sentences.append(_negation(NO_TEXT_NEGATIVES))
    sentences.append(AUDIO_NEGATIVE)
    return " ".join(s for s in sentences if s)


def camera_line(camera: str, target: str = "") -> str:
    """CAMERA 절 — 어휘의 워크 문구 + 세션의 착지 구절 (ADR-0060 결정 2).

    "slow push in toward the subject, arriving on the granite doorway." 워크는 어휘에서만
    오고, 착지는 무엇에 닿는가만 말한다 (`promptplan.cross_errors`가 워크 단어를 막는다).
    """
    if camera not in CAMERA_PROMPTS:
        raise ValueError(
            f"camera 어휘에 '{camera}'이 없다 (허용: {', '.join(CAMERA_PROMPTS)})"
        )
    phrase = CAMERA_PROMPTS[camera].strip().rstrip(".")
    target = target.strip().rstrip(".")
    if not target:
        return _sentence(phrase)
    return f"{phrase}, {target[0].lower() + target[1:]}."


def build_video_prompt(
    *,
    subject_prompt: str,
    staging: str,
    camera: str,
    camera_target: str = "",
    red_prompt: str | None = None,
) -> tuple[str, str]:
    """`(prompt, negative_prompt)` — 어휘 골격 + 세션 단락. `[5]`가 부르는 유일한 입구다.

    판단이 없다. 절 순서대로 놓을 뿐이다. `red_prompt`가 없으면 RED 절이 없고 NEGATIVE에
    글자 금지가 든다; 있으면 RED 절이 있고 글자 금지는 RED의 마무리 문장(어휘 `_closing`)이
    대신한다 (vocab `negatives._role`). 단락의 계약은 `promptplan.py`가 먼저 쟀다.
    """
    if staging not in STAGINGS:
        raise ValueError(
            f"staging 어휘에 '{staging}'이 없다 (허용: {', '.join(STAGING_TOKENS)})"
        )
    if not subject_prompt.strip():
        raise ValueError("subject_prompt가 비어 있다 — promptplan 검증이 막았어야 한다")
    has_info = bool(red_prompt and red_prompt.strip())
    lines = [
        _section("FORMAT", format_line()),
        _section("STAGING", STAGINGS[staging]),
        _section("SUBJECT", _sentence(subject_prompt)),
        _section("CAMERA", camera_line(camera, camera_target)),
    ]
    if has_info:
        lines.append(_section("RED", f"{_sentence(red_prompt or '')} {ANNOTATION_CLOSING}"))
    lines.append(_section("NEGATIVE", negative_line(has_info=has_info)))
    return chr(10).join(lines), ", ".join(negative_items(has_info=has_info))


def fill_seconds(prompt: str, seconds: int) -> str:
    """FORMAT 절의 `{seconds}` 자리를 채운다 — `[7]`이 실측에서 정한 길이로 (스펙 05)."""
    if SECONDS_PLACEHOLDER not in prompt:
        raise ValueError(f"프롬프트에 {SECONDS_PLACEHOLDER} 자리가 없다 — [5]의 산출물이 아니다")
    return prompt.replace(SECONDS_PLACEHOLDER, str(int(seconds)))


def demote_info(prompt: str, negative_prompt: str) -> tuple[str, str]:
    """RED 절을 뺀 변종 — 검수 실패 사다리의 `demoted_from: info` 칸 (스펙 05 `[7]`, ADR-0056 결정 6).

    `prompts.json`의 문자열에서 `RED:` 절을 지우고 NEGATIVE 절을 **info 없는 씬의 것**으로
    다시 만든다 (글자 금지가 든다). 어휘 문구만 쓴다 — 여기서 영어 문장을 짓지 않는다.
    RED 절이 없는 프롬프트에 부르면 실패한다 — 강등할 것이 없다.
    """
    kept = [line for line in prompt.split("\n") if not line.upper().startswith("RED:")]
    if len(kept) == len(prompt.split("\n")):
        raise ValueError("RED 절이 없는 프롬프트다 — info 씬이 아니라 강등할 것이 없다")
    kept = [line for line in kept if not line.upper().startswith("NEGATIVE:")]
    kept.append(_section("NEGATIVE", negative_line(has_info=False)))
    return "\n".join(kept), ", ".join(negative_items(has_info=False))


# --- prompts.json 스키마 -----------------------------------------------------

PROMPT_SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "scene_id",
        "beat",
        "subject_scale",
        "camera",
        "staging",
        "staging_source",
        "framing",
        "framing_source",
        "has_info",
        "prompt",
        "negative_prompt",
        "subject_prompt",
    ],
    "additionalProperties": False,
    "properties": {
        "scene_id": {"type": "integer", "minimum": 1},
        "beat": {"type": "string", "minLength": 1},
        "subject_scale": {"enum": list(SUBJECT_SCALES)},
        "camera": {"type": "string", "minLength": 1},
        # ADR-0056 결정 4 — 무대. 씬 계약의 값이거나 기본값이고, 어느 쪽인지는
        # staging_source가 밝힌다 (framing_source와 같은 관측 수단).
        "staging": {"enum": list(STAGING_TOKENS)},
        "staging_source": {"enum": [FROM_SCENE, FROM_DEFAULT]},
        "framing": {"enum": list(FRAMING_TOKENS)},
        # ADR-0033 — 구도가 씬 계약에서 왔는지 기본값으로 떨어졌는지. 되돌릴 조건의
        # 관측 수단이라 산출물에 남긴다.
        "framing_source": {"enum": [FROM_SCENE, FROM_DEFAULT]},
        # ADR-0056 결정 3·6 — 이 씬의 프롬프트에 RED 절이 있는가. [7]의 OCR 대조와
        # 강등 사다리(RED 절을 뺀 재생성)가 이 값으로 갈린다.
        "has_info": {"type": "boolean"},
        "prompt": {"type": "string", "minLength": 1},
        "negative_prompt": {"type": "string", "minLength": 1},
        # ADR-0067 — 조립본을 만든 **세션 단락**을 옆에 그대로 싣는다. `[7]`의 고쳐쓰기
        # 재생성이 부분만 고쳐 같은 골격에 다시 얹으려면 부분이 남아 있어야 한다.
        # 계약의 정본은 promptplan.schema.json이고 여기는 그것을 나른다.
        # `red_prompt`는 info 씬에만, `subject_prompt_shot2`는 2샷 씬에만 있다.
        "subject_prompt": {"type": "string", "minLength": 1},
        "camera_target": {"type": "string"},
        "red_prompt": {"type": "string", "minLength": 1},
        "subject_prompt_shot2": {"type": "string", "minLength": 1},
        # ADR-0051 — 선택. 이 씬에 등장하는 인물 id. 씬 계약의 cast를 그대로 복사해 온
        # 값이다 (고치는 곳은 씬 계약 하나다 — ADR-0020). 외형 서술은 prompt 안에 있다.
        "cast": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

PROMPTS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "prompts.json ([5. prompt] 산출물 — 씬별 영상 지시)",
    "type": "object",
    "required": ["run_id", "topic", "source_script", "style", "scenes"],
    "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "topic": {"type": "string", "minLength": 1},
        "source_script": {"type": "string", "minLength": 1},
        "style": {
            "type": "object",
            "required": ["base_style", "composition", "aspect_ratio", "resolution"],
            "additionalProperties": False,
            "properties": {
                "base_style": {"type": "string", "minLength": 1},
                "composition": {"type": "string", "minLength": 1},
                "aspect_ratio": {"const": ASPECT_RATIO},
                "resolution": {"const": RESOLUTION},
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
