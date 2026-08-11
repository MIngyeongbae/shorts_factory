"""[1. script] — 팩트시트 → 대본 후보.

specs/05-pipeline.md:
    [1. script] → 05-candidates/*.json (대본+비트 태그, 팩트시트 그라운딩)

## 역할 분담 (ADR-0001, ADR-0003)

헤드리스 세션은 `beat`·`text`·`subject`·`subject_scale` 넷만 출력한다. 나머지는 코드가
룰 테이블로 채운다.

| 필드 | 정하는 주체 | 근거 |
|---|---|---|
| `text`, `beat`, `subject`, `subject_scale` | 세션 | ADR-0001 "창의적 판단은 subject와 대본 내용에만". `subject_scale`은 연출이 아니라 피사체 서술이라 같은 자리다 (ADR-0018) |
| `est_start`/`est_end` | 코드 (글자 수 ÷ 명목 속도) | 산술을 LLM에 맡기지 않는다 |
| `camera` | 코드 (specs/03 비트별 기본값) | CLAUDE.md 원칙 3 |
| `emphasis` | 코드 (숫자 비트 → 대형 빨간 숫자) | specs/03 오버레이 룰 |
| `motion` | 코드 (전부 kenburns) | 아래 참고 |

`motion`을 전부 `kenburns`로 두는 것은 이 슬라이스의 의도적 축소다. `kling`은 유체 모션이
서사상 필요한 씬을 고르는 판단이 필요한데(ADR-0006), 그 판단은 이미지가 나온 뒤
`[7. motion]`에서 하는 편이 근거가 많다.

## 이 단계가 하지 않는 것

검증 실패 시 **재생성하지 않는다**. 재생성 루프는 `[2. validate]` 소관이다(specs/05, 최대 3회).
여기서는 후보를 쓰고 검증 결과를 함께 돌려준다 — 판정은 하류가 한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas.grounding import extract_values, validate_grounding
from ..schemas.scenes import validate_scenes
from ..schemas.visual_rules import SUBJECT_SCALES
from ..schemas.script_rules import core_chars, validate_script
from .research import find_run_for_slug

log = logging.getLogger(__name__)

STAGE = "1-script"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT = "05-script.md"

#: 헤드리스 세션 타임아웃(초). 웹 도구 없이 생성만 하므로 조사 단계보다 짧다.
TIMEOUT = 600

#: 세션에 준 도구. 팩트시트는 프롬프트에 주입되므로 읽을 것이 없다 (ADR-0011).
TOOLS: tuple[str, ...] = ()

#: est_* 계산용 명목 발화 속도(자/초). 원본 3편 실측 5.396~6.217의 중앙 부근.
#: 545~575자를 넣으면 93~98초가 나와 specs/01의 90~102초 안에 떨어진다.
NOMINAL_SPEED = 5.85

#: specs/06 — confidence: low 사실은 대본에 사용 금지. 주입 단계에서 아예 뺀다.
EXCLUDED_CONFIDENCE = "low"

#: specs/03 비트별 카메라 기본값. 복합 카메라 워크 금지 (AI 영상 왜곡 방지).
CAMERA_BY_BEAT: dict[str, str] = {
    "hook_fact": "slow_zoom_in",
    "hook_twist": "static",
    "context": "pan_right",
    "context_number": "slow_zoom_in",
    "failed_solution": "static",
    "failure_reason": "slow_zoom_in",
    "dilemma_peak": "static",
    "turning_point": "slow_zoom_in",
    "solution_step": "tilt_down",
    "solution_number": "static",
    "present_link": "slow_zoom_out",
    "ending_echo": "slow_zoom_out",
}

#: specs/03 — 숫자 비트의 오버레이는 대형 빨간 숫자 텍스트 (후처리 합성, ADR-0002)
NUMBER_BEATS = ("context_number", "solution_number")
NUMBER_EMPHASIS_TYPE = "big_red_text"

DEFAULT_MOTION = "kenburns"


class ScriptStageError(Exception):
    pass


@dataclass
class ScriptResult:
    topic: str
    slug: str
    run_id: str
    candidate_path: Path | None
    scenes: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def valid(self) -> bool:
        return self.scenes is not None and not self.errors

    @property
    def summary(self) -> str:
        if self.scenes is None:
            return f"[1] {self.topic} — 대본 생성 실패"
        count = len(self.scenes["scenes"])
        verdict = "검증 통과" if self.valid else f"검증 실패 {len(self.errors)}건"
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[1] {self.topic} — 후보 1개 / {count}줄 / "
            f"{self.scenes['total_duration']:.1f}초 → {verdict}{tail}"
        )


def _load_prompt() -> Template:
    path = PROMPTS_DIR / PROMPT
    if not path.exists():
        raise ScriptStageError(f"프롬프트 파일이 없다: {path}")
    return Template(path.read_text(encoding="utf-8"))


def groundable_factsheet(factsheet: dict[str, Any]) -> dict[str, Any]:
    """세션에 주입할 팩트시트. confidence=low 사실을 제거한다 (specs/06).

    프롬프트로 "쓰지 마라"라고 이르는 대신 아예 안 보여준다. 하류 그라운딩 검증도
    같은 기준으로 허용 집합을 만들므로 두 곳의 판단이 어긋나지 않는다.
    """
    trimmed = dict(factsheet)
    trimmed["facts"] = [
        fact
        for fact in factsheet.get("facts", [])
        if isinstance(fact, dict) and fact.get("confidence") != EXCLUDED_CONFIDENCE
    ]
    trimmed.pop("verdict", None)
    trimmed.pop("conditions", None)
    trimmed.pop("reject_reason", None)
    return trimmed


def build_scenes(
    raw_scenes: list[dict[str, Any]], *, run_id: str, topic: str
) -> dict[str, Any]:
    """세션 출력(beat/text/subject)에 룰 테이블 필드를 채워 scenes.json을 만든다."""
    scenes: list[dict[str, Any]] = []
    cursor = 0.0

    for index, item in enumerate(raw_scenes, start=1):
        if not isinstance(item, dict):
            raise ScriptStageError(f"{index}번째 씬이 객체가 아니다: {item!r}")

        beat = str(item.get("beat", "")).strip()
        text = str(item.get("text", "")).strip()
        subject = str(item.get("subject", "")).strip()
        scale = str(item.get("subject_scale", "")).strip()
        if beat not in CAMERA_BY_BEAT:
            raise ScriptStageError(f"{index}번째 씬의 beat가 비트 테이블에 없다: {beat!r}")
        if not text or not subject:
            raise ScriptStageError(f"{index}번째 씬에 text 또는 subject가 비어 있다")
        # ADR-0018 — 구도가 이 값에 걸려 있다. 없으면 wide로 때우지 않는다:
        # 조용히 채우면 스펙 03 구도 표의 close/diagram 열이 영영 안 쓰인다.
        if scale not in SUBJECT_SCALES:
            raise ScriptStageError(
                f"{index}번째 씬의 subject_scale이 {'/'.join(SUBJECT_SCALES)} 중에 없다: "
                f"{scale!r}"
            )

        # 자막 줄 길이에 비례해 시간을 나눈다. TTS 이후 실측으로 갱신된다 (specs/05).
        span = round(len(core_chars(text)) / NOMINAL_SPEED, 3)
        start, cursor = cursor, round(cursor + span, 3)

        scene: dict[str, Any] = {
            "scene_id": index,
            "beat": beat,
            "text": text,
            "est_start": start,
            "est_end": cursor,
        }

        if beat in NUMBER_BEATS:
            numbers = extract_values(text)
            if not numbers:
                raise ScriptStageError(
                    f"{index}번째 씬이 숫자 비트({beat})인데 본문에 숫자가 없다"
                )
            scene["emphasis"] = {"type": NUMBER_EMPHASIS_TYPE, "value": numbers[0][0]}

        scene["subject"] = subject
        scene["subject_scale"] = scale
        scene["camera"] = CAMERA_BY_BEAT[beat]
        scene["motion"] = DEFAULT_MOTION
        scene["notes"] = ""
        scenes.append(scene)

    if not scenes:
        raise ScriptStageError("세션이 씬을 하나도 내놓지 않았다")

    return {
        "run_id": run_id,
        "topic": topic,
        "total_duration": scenes[-1]["est_end"],
        "scenes": scenes,
    }


def validate_candidate(
    scenes: dict[str, Any], factsheet: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """검증기 3개를 한 번에 돌린다. 각 오류에 출처 검증기를 붙여 돌려준다."""
    errors: list[str] = []
    warnings: list[str] = []

    for label, (errs, warns) in (
        ("스키마", validate_scenes(scenes)),
        ("대본규칙", validate_script(scenes)),
        ("그라운딩", validate_grounding(scenes, factsheet)),
    ):
        errors.extend(f"[{label}] {e}" for e in errs)
        warnings.extend(f"[{label}] {w}" for w in warns)

    return errors, warnings


def generate_candidate(
    *,
    llm: LLMClient,
    topic: str,
    run_id: str,
    factsheet: dict[str, Any],
    feedback: str = "",
    label: str = STAGE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """팩트시트 → 후보 1개. 파일 쓰기·상태 기록은 호출자 몫이다.

    `feedback`은 [2. validate]가 검증 실패 사유를 넣는 자리다 (specs/05). 비우면
    [1]의 첫 생성과 같다.

    돌려주는 것은 `(scenes, meta)`. 세션 출력이 씬이 되지 못하면 `ScriptStageError`를
    던지되 문맥은 붙이지 않는다 — 어느 단계가 부른 것인지는 호출자가 안다.
    """
    prompt = _load_prompt().safe_substitute(
        topic=topic,
        factsheet=dump_json(groundable_factsheet(factsheet)),
        feedback=feedback,
    )
    result = llm.run(prompt, allowed_tools=TOOLS, timeout=TIMEOUT, label=label)

    try:
        payload = extract_json_object(result.text)
    except JSONExtractionError as exc:
        raise ScriptStageError(f"세션 출력이 JSON 객체가 아니다: {exc}") from exc

    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list):
        raise ScriptStageError("세션 출력에 scenes 배열이 없다")

    return build_scenes(raw_scenes, run_id=run_id, topic=topic), result.meta


def _load_factsheet(topic_dir: Path) -> dict[str, Any]:
    path = topic_dir / "04-factsheet.json"
    if not path.exists():
        raise ScriptStageError(
            f"팩트시트가 없다: {path}. [0b. research]를 먼저 실행하라."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptStageError(f"팩트시트를 읽을 수 없다: {exc}") from exc


def run_script_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> ScriptResult:
    paths = paths or Paths.from_env()

    if run_id:
        contract_path = paths.run_dir(run_id) / "topic.json"
        if not contract_path.exists():
            raise ScriptStageError(f"topic.json이 없다: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    factsheet = _load_factsheet(topic_dir)
    if factsheet.get("verdict") != "pass":
        reason = (
            f"팩트시트 verdict가 '{factsheet.get('verdict')}'다. "
            "4조건 불충족 소재는 대본 생성에 진입하지 않는다 (specs/06)."
        )
        state.mark_blocked(STAGE, reason=reason)
        raise ScriptStageError(reason)

    candidate_path = topic_dir / "05-candidates" / "01.json"

    if not force and candidate_path.exists():
        try:
            existing = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("[%s] 기존 후보를 읽을 수 없어 다시 생성한다", STAGE)
        else:
            log.info("[%s] 후보가 이미 있어 스킵한다: %s", STAGE, candidate_path.name)
            errors, warnings = validate_candidate(existing, factsheet)
            return ScriptResult(
                topic=topic, slug=slug, run_id=run_id, candidate_path=candidate_path,
                scenes=existing, errors=errors, warnings=warnings, skipped=True,
            )

    state.mark_running(STAGE)
    log.info("[%s] 독립 헤드리스 세션 시작 (도구 없음)", STAGE)

    try:
        scenes, meta = generate_candidate(
            llm=llm, topic=topic, run_id=run_id, factsheet=factsheet,
        )
    except ScriptStageError as exc:
        message = (
            f"{exc} 원본은 {run_dir / 'logs'}에 있다. 재생성은 [2. validate] 소관이다."
        )
        state.mark_failed(STAGE, message)
        raise ScriptStageError(message) from exc

    write_text(candidate_path, dump_json(scenes))
    errors, warnings = validate_candidate(scenes, factsheet)

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "output": candidate_path.relative_to(paths.root).as_posix(),
        "scene_count": len(scenes["scenes"]),
        "est_duration": scenes["total_duration"],
        "validation_errors": errors,
        "validation_warnings": warnings,
        **meta,
    }
    if errors:
        log.warning("[%s] 검증 실패 %d건 — 후보는 남긴다 ([2]가 판정)", STAGE, len(errors))
        state.mark_failed(STAGE, f"검증 실패 {len(errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)

    return ScriptResult(
        topic=topic, slug=slug, run_id=run_id, candidate_path=candidate_path,
        scenes=scenes, errors=errors, warnings=warnings,
    )
