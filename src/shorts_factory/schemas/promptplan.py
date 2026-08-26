"""`promptplan.schema.json` — `[5] prompt` 세션 산출(씬별 샷 서술)의 계약. ADR-0060.

세션이 쓰는 것은 프롬프트 전체가 아니라 **SUBJECT 단락·카메라 착지·RED 기하**다. 골격
(FORMAT·STAGING·CAMERA 문구·NEGATIVE)은 코드가 `vocab.json`에서 조립한다 (ADR-0034). 이 모듈은

- 스키마 검증(영어·ASCII 패턴, 길이) — 값은 전부 `specs/schema/promptplan.schema.json`에 있다
- 씬 계약과의 교차 규칙 — 씬 id 일치, `info` 씬에만 `red_prompt`, 라벨 문자열이 따옴표째
  들어 있는가, `shot2` 씬에만 `subject_prompt_shot2`, `camera_target`에 어휘 밖 카메라 워크
  단어가 없는가 (`vocab.camera_target_forbidden_words()`)

를 맡는다. 실패는 목록으로 돌려주고 `[5]`가 보고·중단한다 (ADR-0044 — 재생성 루프 없음).
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

PROMPTPLAN_SCHEMA: dict[str, Any] = vocab.PROMPTPLAN_SCHEMA_DOC
PLANNED_PROMPT_SCHEMA: dict[str, Any] = PROMPTPLAN_SCHEMA["$defs"]["scene"]

_VALIDATOR = Draft202012Validator(PROMPTPLAN_SCHEMA, registry=vocab.REGISTRY)

SUBJECT_FIELD = "subject_prompt"
CAMERA_TARGET_FIELD = "camera_target"
RED_FIELD = "red_prompt"
SHOT2_FIELD = "subject_prompt_shot2"
#: 프레임을 입력으로 받는 라인만 쓰는 필드 (ADR-0071) — CLEAN 정지 이미지용 MJ 한 줄의 소재부.
MJ_SUBJECT_FIELD = "mj_subject"


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록 — ASCII 패턴·길이 위반이 여기서 잡힌다."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        message = err.message
        if err.validator == "pattern":
            message = "영어·ASCII만 허용된다 — 한국어·일본어·전각 문자가 영상 모델에 가면 글자로 그려진다 (ADR-0060)"
        elif err.validator in ("minLength", "maxLength"):
            message = f"{err.message} — 길이 범위는 promptplan.schema.json이 정한다 (ADR-0060 결정 3)"
        errors.append(f"{location}: {message}")
    return errors


def length_limits(field: str) -> tuple[int, int]:
    """필드의 (minLength, maxLength) — 세션 프롬프트에 보여 줄 값. 출처는 스키마 하나다."""
    spec = PLANNED_PROMPT_SCHEMA["properties"][field]
    return int(spec["minLength"]), int(spec["maxLength"])


def _words_in(text: str, words: tuple[str, ...]) -> list[str]:
    found = []
    for word in words:
        if re.search(r"\b" + re.escape(word) + r"\b", text, flags=re.IGNORECASE):
            found.append(word)
    return found


def _forbidden_camera_words(text: str) -> list[str]:
    return _words_in(text, vocab.camera_target_forbidden_words())


def _red_words_outside_red(text: str) -> list[str]:
    return _words_in(text, vocab.only_in_red_words())


def _mj_errors(
    sid: int, entry: dict[str, Any], *, line: str | None, has_info: bool
) -> list[str]:
    """`mj_subject`의 유무와 예산 (ADR-0071, ADR-0075가 씬 단위로 좁혔다).

    예산은 여기서 세지 않는다 — **조립한 한 줄**을 `visual_rules.check_mj_prompt`가 잰다.
    단어 수 상한이 라인의 `mj_style` 길이에 딸린 값이라, 스키마에 적으면 라인마다
    다른 값을 계약 하나가 들게 된다 (ADR-0034).

    **프레임을 받는지는 씬이 정한다** (ADR-0075 결정 1·3). 프레임 라인이라도 `info` 씬은
    텍스트→영상이라 MJ를 타지 않으므로 `mj_subject`가 **없어야** 한다.
    """
    from .visual_rules import MJPromptError, build_mj_prompt, negative_items

    if line is None:
        return []
    wanted = vocab.scene_takes_frames(line, has_info=has_info)
    text = str(entry.get(MJ_SUBJECT_FIELD) or "").strip()
    if wanted and not text:
        return [
            f"scenes/{sid}: 라인 '{line}'은 이 씬의 CLEAN 이미지를 사는데 {MJ_SUBJECT_FIELD}가 "
            "없다 — MJ에 보낼 소재 한 줄이 필요하다 (ADR-0071)"
        ]
    if not wanted:
        if text and has_info and vocab.style_in_frames(line):
            return [
                f"scenes/{sid}: info 씬인데 {MJ_SUBJECT_FIELD}가 있다 — info 씬은 텍스트→영상이라 "
                "MJ를 타지 않는다 (ADR-0075 결정 1)"
            ]
        if text:
            return [
                f"scenes/{sid}: 라인 '{line}'은 프레임을 안 받는데 {MJ_SUBJECT_FIELD}가 있다 "
                "— 쓰이지 않는 필드다"
            ]
        return []
    try:
        build_mj_prompt(
            subject=text,
            mj_style=vocab.line_style(line, engine=vocab.MJ_ENGINE),
            negatives=negative_items(has_info=False),
        )
    except MJPromptError as exc:
        return [f"scenes/{sid}: {MJ_SUBJECT_FIELD} — {exc}"]
    return []


def cross_errors(
    data: dict[str, Any], contract: dict[str, Any], *, line: str | None = None
) -> list[str]:
    """씬 계약과의 교차 규칙. 스키마를 통과한 입력을 전제한다.

    `line`이 있으면 **라인이 요구하는 필드**까지 본다 (ADR-0071) — 프레임을 입력으로
    받는 라인은 씬마다 `mj_subject`가 있어야 하고, 안 받는 라인에는 없어야 한다.
    라인을 모르면(None) 그 축은 보지 않는다 — 옛 산출물이나 라인 밖 호출이다.
    """
    errors: list[str] = []
    contract_scenes = {int(s["scene_id"]): s for s in contract.get("scenes", [])}
    planned = {}
    for entry in data.get("scenes", []) or []:
        sid = int(entry["scene_id"])
        if sid in planned:
            errors.append(f"scenes/{sid}: 같은 씬이 두 번 나왔다")
        planned[sid] = entry

    missing = sorted(set(contract_scenes) - set(planned))
    if missing:
        errors.append(
            f"씬 {', '.join(str(s) for s in missing)}의 샷 서술이 없다 — 씬 계약의 모든 씬에 하나씩 있어야 한다"
        )
    stray = sorted(set(planned) - set(contract_scenes))
    if stray:
        errors.append(f"씬 계약에 없는 씬을 서술했다: {', '.join(str(s) for s in stray)}")

    for sid, entry in sorted(planned.items()):
        scene = contract_scenes.get(sid)
        if scene is None:
            continue
        info = scene.get("info") or None
        red = entry.get(RED_FIELD)
        if info and not red:
            errors.append(f"scenes/{sid}: info 씬인데 {RED_FIELD}가 없다 — 계측 표시의 기하를 적어라")
        if red and not info:
            errors.append(f"scenes/{sid}: info가 없는 씬에 {RED_FIELD}가 있다 — 빨강은 계측 씬에만 쓴다")
        if info and red:
            for label in info.get("labels", []):
                if f'"{label}"' not in red:
                    errors.append(
                        f"scenes/{sid}: {RED_FIELD}에 라벨 \"{label}\"이 따옴표째 정확히 들어 있지 않다 — "
                        "화면 글자는 계약의 문자열 그대로다"
                    )
        shot2 = scene.get("shot2") or None
        shot2_prompt = entry.get(SHOT2_FIELD)
        if shot2 and not shot2_prompt:
            errors.append(f"scenes/{sid}: shot2 씬인데 {SHOT2_FIELD}가 없다")
        if shot2_prompt and not shot2:
            errors.append(f"scenes/{sid}: shot2가 없는 씬에 {SHOT2_FIELD}가 있다")
        for field in (SUBJECT_FIELD, CAMERA_TARGET_FIELD, SHOT2_FIELD, MJ_SUBJECT_FIELD):
            leaked = _red_words_outside_red(str(entry.get(field, "") or ""))
            if leaked:
                errors.append(
                    f"scenes/{sid}: {field}가 계측 표시를 언급한다 ({', '.join(leaked)}) — 빨강·화살표·라벨은 "
                    f"{RED_FIELD}에만 쓴다. RED를 뗀 강등 재생성에서 빨강이 남는다 (ADR-0060)"
                )
        errors.extend(
            _mj_errors(sid, entry, line=line, has_info=bool(scene.get("info")))
        )
        bad_words = _forbidden_camera_words(str(entry.get(CAMERA_TARGET_FIELD, "")))
        if bad_words:
            errors.append(
                f"scenes/{sid}: {CAMERA_TARGET_FIELD}가 카메라 워크를 새로 지시한다 ({', '.join(bad_words)}) — "
                "워크는 씬 계약의 camera 어휘에서만 온다. 착지(무엇에 닿는가)만 적어라 (ADR-0033 §3)"
            )
    return errors


def validate_promptplan(
    data: Any, contract: dict[str, Any], *, line: str | None = None
) -> list[str]:
    """스키마 → 교차 규칙. 스키마가 깨지면 교차 규칙은 보지 않는다."""
    errors = schema_errors(data)
    if errors:
        return errors
    return cross_errors(data, contract, line=line)
