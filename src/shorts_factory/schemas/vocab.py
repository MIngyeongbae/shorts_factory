"""`specs/schema/`를 로드한다 — 어휘·스키마·분량 값의 유일한 출처 (ADR-0034 §3).

**이 모듈은 값을 선언하지 않는다.** `MOTIONS = (...)` 같은 손 복사가 사라진 자리다.
`mj_video` 사고(ADR-0025 §3이 승인한 enum 값이 스펙에도 코드에도 없었다)의 재발
방지책이고, 한 곳만 고치면 스펙과 코드가 함께 움직인다.

## 경로

`specs/`는 패키지 밖이라 리포지토리 루트를 기준으로 찾는다. `config.project_root()`를
쓰지 않는 것은 의도적이다 — 그쪽은 테스트가 `SHORTS_FACTORY_ROOT`로 tmp 디렉터리를
가리키게 만드는 값이고, 계약 파일은 tmp에 없다. 계약은 항상 리포지토리의 것을 읽는다.
필요하면 `SHORTS_FACTORY_SCHEMA`로 덮어쓴다.

## `$ref`

`scene.schema.json`이 `vocab.json#/$defs/...`를 가리킨다. 두 파일을 `referencing`
레지스트리에 올려 두면 jsonschema가 그대로 푼다 — 어휘를 스키마 안에 복사하지 않아도
된다.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

#: 리포지토리의 `specs/schema/`. src/shorts_factory/schemas/vocab.py → parents[3]가 루트다.
SCHEMA_DIR = Path(
    os.environ.get("SHORTS_FACTORY_SCHEMA")
    or Path(__file__).resolve().parents[3] / "specs" / "schema"
)


@lru_cache(maxsize=None)
def load(name: str) -> dict[str, Any]:
    """계약 파일 하나를 읽는다. 파일당 한 번만 읽고 캐시한다."""
    path = SCHEMA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"계약 파일이 없다: {path}. specs/schema/가 리포지토리에 있어야 하고, "
            "옮겼다면 SHORTS_FACTORY_SCHEMA로 알려라 (ADR-0034)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


VOCAB: dict[str, Any] = load("vocab.json")
SCENE_SCHEMA_DOC: dict[str, Any] = load("scene.schema.json")
SCENEPLAN_SCHEMA_DOC: dict[str, Any] = load("sceneplan.schema.json")
PROMPTPLAN_SCHEMA_DOC: dict[str, Any] = load("promptplan.schema.json")
REFS_SCHEMA_DOC: dict[str, Any] = load("refs.schema.json")
ENDING_SCHEMA_DOC: dict[str, Any] = load("ending.schema.json")
SUBTITLE_STYLE: dict[str, Any] = load("subtitle-style.json")
SCRIPT_RULES: dict[str, Any] = load("script-rules.json")
BEAT_DEFAULTS: dict[str, Any] = load("beat-defaults.json")

#: `$ref`를 푸는 레지스트리. 파일명이 곧 `$id`다.
REGISTRY: Registry = Registry().with_resources(
    (name, Resource.from_contents(doc, default_specification=DRAFT202012))
    for name, doc in (
        ("vocab.json", VOCAB),
        ("scene.schema.json", SCENE_SCHEMA_DOC),
        ("sceneplan.schema.json", SCENEPLAN_SCHEMA_DOC),
        ("promptplan.schema.json", PROMPTPLAN_SCHEMA_DOC),
        ("refs.schema.json", REFS_SCHEMA_DOC),
        ("ending.schema.json", ENDING_SCHEMA_DOC),
    )
)


def values(name: str) -> tuple[str, ...]:
    """어휘 하나의 값 전부. `vocab.json`의 `$defs.{name}.enum`이다."""
    try:
        return tuple(VOCAB["$defs"][name]["enum"])
    except KeyError as exc:  # pragma: no cover - 계약 파일이 깨진 경우
        raise KeyError(f"vocab.json에 '{name}' 어휘가 없다") from exc


def require(name: str, value: str) -> str:
    """어휘의 한 값을 **이름으로 가리킨다.** 어휘에 없으면 즉시 실패한다.

    코드가 특정 값을 지목해야 할 때 쓴다(`transition == hard_cut`이면 thump 같은 것).
    값 목록을 선언하는 것과는 다르다 — 목록은 여전히 `vocab.json`에만 있고, 여기서는
    그 목록에 이 이름이 실제로 있는지를 로드 시점에 확인한다.
    """
    if value not in values(name):
        raise ValueError(
            f"{name} 어휘에 '{value}'가 없다 (있는 값: {', '.join(values(name))}). "
            "specs/schema/vocab.json과 코드가 갈렸다"
        )
    return value


def meta(name: str) -> dict[str, Any]:
    """어휘 하나의 항목별 설명. `_`로 시작하는 키는 사람에게 하는 말이라 뺀다."""
    block = VOCAB["meta"].get(name, {})
    return {key: value for key, value in block.items() if not key.startswith("_")}


def style(key: str) -> Any:
    """`vocab.json`의 `meta.style` 한 항목. 전 씬 공통의 렌더 언어다 (ADR-0056 결정 4)."""
    return VOCAB["meta"]["style"][key]


def mj_dialect(key: str) -> Any:
    """`vocab.json`의 `meta.mj_dialect` 한 항목 — MJ 한 줄의 형식·예산 (ADR-0069).

    코드가 단어 상한이나 어순을 상수로 들지 않는다 (ADR-0034). 값을 고치는 자리는
    어휘 하나뿐이고, 고치면 `build_mj_prompt()`의 검사가 바로 따라간다.
    """
    return VOCAB["meta"]["mj_dialect"][key]


def line_style(line: str) -> str:
    """라인 고유 `base_style`. 없으면 전역 `meta.style.base_style`로 떨어진다.

    라인이 자기 룩을 질 수 있다 (ADR-0070). 어느 라인이 그러는지는 어휘가 정하고
    코드는 묻지 않는다 — 여기서 라인 이름을 분기하면 출처가 둘이 된다 (ADR-0034).
    """
    require("video_line", line)
    return str(VOCAB["meta"]["video_line"][line].get("base_style") or style("base_style"))


def style_in_frames(line: str) -> bool:
    """이 라인은 스타일을 **프레임이 지는가** (ADR-0070 규칙 1).

    참이면 `[5]`가 영상 프롬프트에 STYLE 절을 싣지 않는다 — 실으면 영상 모델이 자기
    프라이어로 그것을 해석해 first/last 프레임과 싸우고 중간 프레임이 무너진다 (실측).
    """
    require("video_line", line)
    return bool(VOCAB["meta"]["video_line"][line].get("style_in_frames", False))


def negatives(key: str) -> Any:
    """`meta.style.negatives`의 한 묶음 — `always`·`no_text`(목록) 또는 `audio`(문장).

    `[5]`가 NEGATIVE 절을 채우는 출처다 (`negatives._role`). 코드가 배제 항목을
    들고 있지 않는다 (ADR-0034).
    """
    return style("negatives")[key]


def phrase(name: str, value: str) -> str:
    """어휘 한 값의 **영상 프롬프트 문구** (`meta.{name}.{value}.phrase`).

    `staging`·`annotation`이 이 키를 갖는다 — `[5]`가 프롬프트 절에 그대로 넣는 영어
    구절이고 코드는 문장을 들지 않는다 (ADR-0034·0056). 값이 어휘 밖이면 실패한다.
    """
    require(name, value)
    try:
        return str(VOCAB["meta"][name][value]["phrase"])
    except KeyError as exc:  # pragma: no cover - 계약 파일이 깨진 경우
        raise KeyError(f"vocab.json meta.{name}.{value}에 phrase가 없다") from exc


def video_prompt(camera: str) -> str:
    """카메라 워크의 영상 문구 (`meta.camera.{value}.video_prompt`). CAMERA 절에 그대로 들어간다."""
    require("camera", camera)
    try:
        return str(VOCAB["meta"]["camera"][camera]["video_prompt"])
    except KeyError as exc:  # pragma: no cover - 계약 파일이 깨진 경우
        raise KeyError(f"vocab.json meta.camera.{camera}에 video_prompt가 없다") from exc


def info_still() -> str:
    """`art` 라인 `info` 씬의 정지 문구 (`meta.camera._info_still`, ADR-0072 결정 4).

    **카메라 워크 문구를 대체한다** — 프레임이 계측 표시의 정확성을 지므로(ADR-0071)
    그 아래 그림이 움직이면 지시선이 가리키던 지점이 어긋난다. `static`을 쓰지 않는
    이유는 그 문구가 피사체 자신의 움직임을 허용하기 때문이다.
    """
    return str(VOCAB["meta"]["camera"]["_info_still"])


def review_standard(attempt: int) -> str:
    """그 시도의 검수 잣대 (`meta.review_ladder.standards`, 사람 결정 2026-08-25).

    **시도가 거듭될수록 낮아진다.** 같은 잣대로 반복 기각하면 재검수가 시간·세션·종량
    호출을 먹고도 끝나지 않는다 — 마지막 칸이 "이거라도 쓴다"라서 사다리가 닫힌다.
    표를 넘어서는 시도는 마지막 문구를 계속 쓴다.
    """
    ladder = [str(x) for x in VOCAB["meta"]["review_ladder"]["standards"]]
    if not ladder:  # pragma: no cover - 계약 파일이 깨진 경우
        raise KeyError("vocab.json meta.review_ladder.standards가 비어 있다")
    return ladder[min(max(int(attempt), 0), len(ladder) - 1)]


def camera_target_forbidden_words() -> tuple[str, ...]:
    """`camera_target`에 들어오면 안 되는 카메라 워크 단어 (`meta.camera._target_forbidden_words`, ADR-0060).

    워크는 어휘의 `camera` 문구에서만 온다 — 세션의 착지 구절이 새 워크를 지시하면 닫힌 어휘가 샌다.
    """
    return tuple(str(w) for w in VOCAB["meta"]["camera"].get("_target_forbidden_words", []))


def only_in_red_words() -> tuple[str, ...]:
    """SUBJECT·착지에 들어오면 안 되는 계측 표시 단어 (`meta.annotation._only_in_red_words`, ADR-0060).

    빨강·화살표·라벨은 RED 절의 것이다 — 다른 절에 새면 RED를 뗀 강등 재생성에서도 빨강이 남는다.
    """
    return tuple(str(w) for w in VOCAB["meta"]["annotation"].get("_only_in_red_words", []))


def annotation_closing() -> str:
    """RED 절의 마무리 문장 (`meta.annotation._closing`) — 라벨이 유일한 텍스트·유일한 빨강이라는 못."""
    return str(VOCAB["meta"]["annotation"]["_closing"])


def edit_preamble() -> str:
    """INFO 편집 지시의 보존 서두 (`meta.annotation._edit_preamble`, ADR-0071).

    영상 프롬프트의 RED 절과 문구를 공유하되 서두만 다르다 — 영상은 "그려라"이고
    편집은 "그대로 두고 얹어라"다. 문장을 코드가 짓지 않는다 (ADR-0034).
    """
    return str(VOCAB["meta"]["annotation"]["_edit_preamble"])


def label_join() -> str:
    """라벨 둘 이상을 `{label}` 한 자리에 잇는 문구 (`meta.annotation._label_join`)."""
    return str(VOCAB["meta"]["annotation"]["_label_join"])


def video_line_default() -> str:
    """사람이 `video_line`을 비웠을 때의 라인 (`meta.video_line._default`, ADR-0059 결정 2)."""
    return require("video_line", str(VOCAB["meta"]["video_line"]["_default"]))


def video_line_meta(line: str) -> dict[str, Any]:
    """라인 한 값의 설정 (`meta.video_line.{line}`) — `provider`(CLI 어댑터 이름, null이면
    미구현)·`shot2`(그 라인이 2샷을 만드는가). 코드가 라인→어댑터 표를 들지 않는다 (ADR-0034)."""
    require("video_line", line)
    return dict(VOCAB["meta"]["video_line"][line])


def limits() -> dict[str, Any]:
    """`script-rules.json`의 분량 엔벨로프 (스펙 01)."""
    return SCRIPT_RULES["limits"]


def checks() -> dict[str, Any]:
    """`[2] validate`가 쓰는 임계값."""
    return SCRIPT_RULES["checks"]


def locale_limits(lang: str) -> dict[str, Any]:
    """`script-rules.json`의 `locales.{lang}.limits` — 없으면 빈 dict (ADR-0056 결정 5).

    블록이 없는 언어는 `[2l]`·`[3]`이 `total_seconds` 상한만 본다. 값은 실측 5편 뒤에
    채운다 (되돌릴 조건 4). `_`로 시작하는 키는 사람에게 하는 말이다.
    """
    block = SCRIPT_RULES.get("locales", {}).get(lang)
    if not isinstance(block, dict):
        return {}
    limits = block.get("limits")
    return dict(limits) if isinstance(limits, dict) else {}


# score_rules(ADR-0049)·signature_phrases(ADR-0050)는 지웠다 — 해당 블록이 계약에서 사라졌다.

def beat_default(beat: str) -> dict[str, Any]:
    """비트별 연출 **기본값**. 지시가 아니라 폴백이다 (ADR-0033).

    모르는 비트에도 값을 돌려준다 — 어휘가 늘었는데 기본값 표가 안 따라온 것을 이유로
    파이프라인을 세우지 않는다 (단계 독립 D-5).
    """
    return BEAT_DEFAULTS["beat"].get(beat) or BEAT_DEFAULTS["fallback"]


def default_framing(beat: str, scale: str) -> str:
    """씬이 `framing`을 비웠을 때 쓰는 구도 토큰."""
    table = beat_default(beat)["framing"]
    return table.get(scale) or BEAT_DEFAULTS["fallback"]["framing"][scale]


def default_transition(beat: str) -> str:
    """씬이 `transition`을 비웠을 때 쓰는 전환."""
    return beat_default(beat)["transition"]


def default_staging(beat: str) -> str:
    """씬이 `staging`을 비웠을 때 쓰는 무대 (ADR-0056 결정 4).

    비트 표에 열이 없으면 `fallback.staging`이다 — 인포 설명 영상용 파이프라인이라
    기본은 `studio`다 (beat-defaults.json 설명). `motion` 기본값은 없다 — 전 씬이 영상
    클립이라 고를 값이 사라졌다.
    """
    return beat_default(beat).get("staging") or BEAT_DEFAULTS["fallback"]["staging"]
