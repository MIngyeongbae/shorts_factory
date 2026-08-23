"""씬 계약(`scenes.json`) 검증. specs/02-beat-schema.md + specs/schema/scene.schema.json.

**스키마도 어휘도 이 파일에 없다.** `specs/schema/`에서 로드한다 (ADR-0034 §3).
여기 있는 것은 스키마로 표현할 수 없는 교차 규칙뿐이다.

씬 1개 = 자막 줄(SRT 큐) 1개다 (ADR-0013). 문장이 아니라서 한 씬의 `text`에 문장이
1~3개 들어갈 수 있다.

스펙 01의 대본 규칙(글자 수, 자막 줄 수, 수미상관)과 ADR-0007 그라운딩은 이 모듈이
손대지 않는다 — 별도 검증기가 맡는다.

`est_start`/`est_end`는 TTS 이전 추정치다. TTS 후 `start`/`end`로 바뀐 형태는
`timed_scenes.py`가 이 스키마에서 파생시킨다.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

#: specs/schema/vocab.json — 손으로 옮겨 적지 않는다 (ADR-0034 §3).
BEATS: tuple[str, ...] = vocab.values("beat")
CAMERAS: tuple[str, ...] = vocab.values("camera")
SUBJECT_SCALES: tuple[str, ...] = vocab.values("subject_scale")
FRAMING_TOKENS: tuple[str, ...] = vocab.values("framing")
TRANSITIONS: tuple[str, ...] = vocab.values("transition")
#: ADR-0056 — 무대(결정 4)와 계측 표시 방식(결정 3). `motion` 어휘는 없다 — 전 씬이
#: 영상 클립이라 고를 값이 사라졌고, 편당 영상 씬 상한도 함께 갔다.
STAGINGS: tuple[str, ...] = vocab.values("staging")
ANNOTATIONS: tuple[str, ...] = vocab.values("annotation")

#: `visual_goal`이 `text`를 되풀이했다고 볼 겹침 비율 (ADR-0022).
VISUAL_GOAL_OVERLAP_LIMIT: float = vocab.checks()["visual_goal_overlap_limit"]

#: `info.labels`의 숫자를 말이 되풀이했다고 볼 비율 (ADR-0047). 화면이 지는 숫자는
#: 나레이션이 가리키기만 한다 — 첫 편은 이 비율이 0.75였고 라벨을 소리 내어 읽었다.
#: ADR-0060 결정 4 — 라벨의 숫자는 그 줄이 말하는 숫자여야 한다 (ADR-0047의 에코 상한을 뒤집었다).
LABEL_NUMBERS_FROM_LINE: bool = bool(vocab.checks()["label_numbers_from_line"])

#: total_duration과 마지막 씬 est_end의 허용 오차(초). 경고 판정에만 쓴다.
DURATION_TOLERANCE = 0.5

SCENES_SCHEMA: dict[str, Any] = vocab.SCENE_SCHEMA_DOC
SCENE_SCHEMA: dict[str, Any] = SCENES_SCHEMA["$defs"]["scene"]

_VALIDATOR = Draft202012Validator(SCENES_SCHEMA, registry=vocab.REGISTRY)


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def _bigrams(text: str) -> set[str]:
    """공백·문장부호를 뺀 문자 바이그램 집합."""
    core = "".join(ch for ch in text if ch.isalnum())
    return {core[i : i + 2] for i in range(len(core) - 1)}


def visual_goal_overlap(text: str, visual_goal: str) -> float:
    """`visual_goal`이 `text` 안에 얼마나 들어 있는가 (0~1).

    **그림이 새로 지는 설명이 있는지를 재는 값이다** (ADR-0022). 1에 가까우면
    본문이 이미 한 말을 그림 지시가 되풀이한 것이고, 그 그림은 시간만 채운다.
    """
    goal = _bigrams(visual_goal)
    if not goal:
        return 1.0
    return len(goal & _bigrams(text)) / len(goal)


_NUMBER_TOKEN = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")
#: 나레이션(한국어)의 숫자 — 아라비아 숫자 뒤에 붙는 단위 접미가 값을 키운다 ("13만" = 130000).
_KO_NUMBER = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|만|천|백)?")
_KO_MULT = {"억": 100_000_000, "만": 10_000, "천": 1_000, "백": 100, None: 1}
#: 고유어 수사 — "여섯 달"의 6. 한 글자 수사(한·두·세·네)는 조사·어미와 겹쳐 오탐이 많아 뺐다.
_KO_WORDS = {"다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10, "스무": 20}


def _as_number(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def label_numbers(label: str) -> set[float]:
    """라벨 한 줄의 숫자 값 집합 — `12 cm`→{12}, `130,000`→{130000}, `0.4%`→{0.4}, `Hot air`→∅."""
    found = {_as_number(t) for t in _NUMBER_TOKEN.findall(label or "")}
    return {n for n in found if n is not None}


def line_numbers(text: str) -> set[float]:
    """나레이션 줄이 말하는 숫자 값 집합 — `12센티`→{12}, `13만`→{130000}, `2만 2천`→{22000},
    `여섯 달`→{6}, `5도`→{5}. 단위 접미가 붙은 수는 접미 전 값도 남긴다 (`13만` → 13도)."""
    found: set[float] = set()
    text = text or ""
    # "2만 2천 5백" — 접미가 내려가며 잇달아 오는 덩어리는 한 수다.
    run_total = 0.0
    run_last_mult = None
    run_end = -1
    for match in _KO_NUMBER.finditer(text):
        digits, suffix = match.group(1), match.group(2)
        value = _as_number(digits)
        if value is None:
            continue
        found.add(value)
        mult = _KO_MULT[suffix] if suffix else 1
        scaled = value * mult
        if suffix:
            found.add(scaled)
        adjacent = run_end >= 0 and text[run_end:match.start()].strip() == ""
        if adjacent and run_last_mult is not None and mult < run_last_mult:
            run_total += scaled
            found.add(run_total)
        else:
            run_total = scaled
        run_last_mult = mult
        run_end = match.end()
    for word, value in _KO_WORDS.items():
        if word in text:
            found.add(float(value))
    return found


def labels_not_in_line(text: str, labels: list[str]) -> list[str]:
    """그 줄이 말하지 않는 숫자를 든 라벨 (ADR-0060 결정 4).

    듣는 숫자와 보는 숫자가 어긋나면 청자는 무엇을 설명하는지 모른다 — 석빙고 1차에서
    대사 "0.4%" 위에 화면이 "38.4%"를 세웠다. 숫자가 없는 라벨은 잡지 않는다.
    """
    spoken = line_numbers(text)
    return [
        str(label) for label in labels
        if label_numbers(str(label)) and not label_numbers(str(label)) <= spoken
    ]


def character_errors(data: dict[str, Any]) -> list[str]:
    """인물 블록의 교차 규칙 (ADR-0051).

    스키마는 `characters` 항목과 `cast` 항목의 **모양**만 본다. 씬의 `cast`가
    실재하는 인물 id를 가리키는지는 두 자리를 함께 봐야 하므로 여기다 —
    "`characters`에 없는 id는 계약 위반이다" (specs/02).
    """
    errors: list[str] = []
    characters: list[dict[str, Any]] = data.get("characters") or []

    ids = [str(c.get("id")) for c in characters]
    for cid, count in Counter(ids).items():
        if count > 1:
            errors.append(f"characters: id '{cid}'가 {count}번 정의됐다")

    known = set(ids)
    for scene in data.get("scenes", []):
        sid = scene.get("scene_id", "?")
        for cid in scene.get("cast") or []:
            if cid not in known:
                errors.append(
                    f"scenes/{sid}/cast: '{cid}'가 characters에 없다 — "
                    "계약 위반이다 (ADR-0051)"
                )
    return errors


def character_warnings(data: dict[str, Any]) -> list[str]:
    """인물 판정 기준의 관측 (ADR-0051) — 막지 않고 알린다.

    블록의 존재 기준은 "같은 인물이 **2씬 이상** 반복 등장하고 서사를 지는가"다.
    한 씬뿐인 인물은 시트 생성 비용만 들고 일관성 장치가 할 일이 없다 — 판정을
    되짚으라는 신호이지 계약 위반은 아니다.
    """
    warnings: list[str] = []
    characters: list[dict[str, Any]] = data.get("characters") or []
    if not characters:
        return warnings

    appearances: Counter[str] = Counter()
    for scene in data.get("scenes", []):
        for cid in scene.get("cast") or []:
            appearances[str(cid)] += 1

    for character in characters:
        cid = str(character.get("id"))
        count = appearances.get(cid, 0)
        if count < 2:
            warnings.append(
                f"characters/{cid}: cast된 씬이 {count}개다 — 2씬 이상 반복 등장이 "
                "인물 블록의 기준이다 (ADR-0051). 판정을 되짚어라"
            )
    return warnings


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현 불가한 교차 규칙 (스펙 02).

    **구조 검증은 없다** (ADR-0033 §4). 비트 순서·개수 제약은 폐기됐고, 서사가
    성립하는지는 사람이 대본을 읽고 본다 (ADR-0049 게이트).
    """
    errors: list[str] = list(character_errors(data))
    scenes: list[dict[str, Any]] = data.get("scenes", [])

    # 규칙: scene_id는 1부터 연번이며 자막 줄 순서와 일치 (specs/02, ADR-0013)
    expected = list(range(1, len(scenes) + 1))
    actual = [s.get("scene_id") for s in scenes]
    if actual != expected:
        errors.append(
            f"scenes: scene_id가 1부터 연번이 아니다 (기대 {expected[:3]}…, 실제 {actual[:3]}…)"
        )

    prev_end: float | None = None
    for scene in scenes:
        sid = scene.get("scene_id", "?")
        start = scene.get("est_start")
        end = scene.get("est_end")

        # ADR-0022 — 그림이 본문을 되풀이하면 그 씬의 그림은 하는 일이 없다.
        # 시간만 채우는 그림을 여기서 거른다.
        overlap = visual_goal_overlap(scene.get("text", ""), scene.get("visual_goal", ""))
        if overlap >= VISUAL_GOAL_OVERLAP_LIMIT:
            errors.append(
                f"scenes/{sid}: visual_goal이 text와 {overlap:.0%} 겹친다 "
                f"(상한 {VISUAL_GOAL_OVERLAP_LIMIT:.0%}). 그림이 본문을 되풀이하면 "
                "설명을 지지 않는다 — 본문이 말하지 않고 넘어가는 것을 적어라 (ADR-0022)"
            )

        # ADR-0060 결정 4 — 화면 라벨의 숫자는 그 줄이 말하는 숫자다. 듣는 숫자와 보는
        # 숫자가 어긋나면 그림이 설명을 방해한다 (ADR-0047의 에코 상한은 이것과 반대로 몰았다).
        labels = (scene.get("info") or {}).get("labels") or []
        if LABEL_NUMBERS_FROM_LINE:
            stray = labels_not_in_line(scene.get("text", ""), labels)
            if stray:
                errors.append(
                    f"scenes/{sid}: info.labels {stray}의 숫자를 이 줄의 text가 말하지 않는다 — "
                    "화면 라벨은 그 줄이 말하는 숫자여야 한다 (ADR-0060 결정 4). "
                    "숫자 없는 라벨로 바꾸거나 줄이 말하는 수치를 세워라"
                )

        if start >= end:
            errors.append(f"scenes/{sid}: est_start({start}) >= est_end({end})")
        # 씬은 자막 줄 순서를 그대로 따르므로 시간이 역행하거나 겹칠 수 없다.
        elif prev_end is not None and start < prev_end:
            errors.append(
                f"scenes/{sid}: est_start({start})가 앞 씬의 est_end({prev_end})보다 이르다"
            )
        prev_end = end

    # 편당 영상 씬 상한은 없다 (ADR-0056 — 전 씬이 영상 클립이다). 라벨 ASCII는
    # scene.schema.json의 pattern이 잡는다 — 정규식을 여기 다시 적지 않는다
    # (script-rules.json `_label_ascii`).

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """차단하지는 않지만 하류 단계가 알아야 할 사항."""
    warnings: list[str] = list(character_warnings(data))
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    if not scenes:
        return warnings

    # "숫자 비트인데 emphasis가 없다" 경고는 *_number 비트와 함께 죽었다 (ADR-0047
    # 결정 4, 의도된 삭제) — 숫자를 화면에 세우라고 미는 장치였고 방향이 반대다.

    last_end = scenes[-1].get("est_end")
    total = data.get("total_duration")
    if abs(float(total) - float(last_end)) > DURATION_TOLERANCE:
        warnings.append(
            f"total_duration({total})과 마지막 씬 est_end({last_end})의 차이가 "
            f"{DURATION_TOLERANCE}초를 넘는다"
        )
    return warnings


def validate_scenes(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        # 스키마가 깨졌으면 semantic 검사는 의미가 없다
        return errors, []
    return semantic_errors(data), semantic_warnings(data)


def scene_schema_copy() -> dict[str, Any]:
    """씬 스키마의 깊은 복사본. 파생 스키마(`timed_scenes`)가 손대도 원본이 안 바뀐다."""
    return copy.deepcopy(SCENE_SCHEMA)
