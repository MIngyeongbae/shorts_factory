"""[2. validate] — 검증 실패 사유를 피드백해 대본을 재생성한다.

specs/05-pipeline.md:
    [2. validate] → 06-script.json (구조·스키마·그라운딩 검증, 최대 3회 재생성)

    "스펙 01의 검증 5항목 + 스펙 02 스키마 검증 + 그라운딩 검증(대본 숫자 전수 추출 →
     팩트시트 대조, ADR-0007). 실패 시 실패 사유를 프롬프트에 피드백하여 재생성
     (최대 3회, 초과 시 중단·리포트)."

## 왜 '수정'이 아니라 '재생성'인가

스펙은 이 루프를 **재생성**이라 쓰고, 피드백할 것으로 **실패 사유**만 든다. 직전 대본을
프롬프트에 되돌려주라는 말은 없다. 씬 단위 수정 지시(`fix_directives`)로 고쳐 쓰는
revise 루프는 스펙 07의 `[2b. judge]` 소관이고 상한도 2회로 다르다. 두 루프는 입력도
상한도 다르므로 여기서 합치지 않는다.

## 입력이 05-candidates/01.json인 이유 (스펙과의 임시 어긋남)

스펙 05의 파이프라인 순서는 `[1] → [1c] → [1b] → [2]`이고, `06-script.json`의 생산자로
`[1b. score]`와 `[2. validate]` 둘을 모두 적어 뒀다. 원래대로면 `[1b]`가 후보 여럿을
채점해 `06-script.json`으로 선발하고 `[2]`는 그것을 검증한다.

**`[1b]`가 아직 없고 후보도 1개라서**(specs/05: "후보 수는 `[1b. score]` 도입 전까지
1개다") 지금은 `[2]`가 `05-candidates/01.json`을 직접 읽는다. `[1b]`가 들어오면 이
입력은 `06-script.json`으로 바뀌어야 한다 — 그때 이 주석을 지운다.

## 이 단계가 하지 않는 것

- **채점·선발.** 후보가 여럿일 때 고르는 것은 `[1b. score]`다 (위 참고)
- **go/no-go 판정.** `[2b. judge]`와 사람 게이트 몫이다 (ADR-0009)
- 검증기 자체는 여기 없다. 세 검증기를 묶어 부르는 `validate_candidate`는 `[1]`과 공유한다
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunState
from .research import find_run_for_slug
from .script import (
    ScriptStageError,
    generate_candidate,
    validate_candidate,
)

log = logging.getLogger(__name__)

STAGE = "2-validate"

#: specs/05 "최대 3회". `[1]`이 만든 후보가 실패했을 때 다시 만들 수 있는 횟수다.
#: 소진하면 중단하고 리포트한다 — 무한 루프와 한도 소모를 동시에 막는 상한이다.
MAX_REGENERATIONS = 3

#: `[1]`이 쓰는 첫 후보. 이 단계의 입력이다.
FIRST_CANDIDATE = "01.json"

CANDIDATES_DIR = "05-candidates"
SCRIPT_FILE = "06-script.json"


class ValidateStageError(Exception):
    pass


@dataclass
class Attempt:
    """후보 하나의 검증 결과. state.json 리포트에 그대로 실린다."""

    candidate: str
    regenerated: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 세션이 씬을 못 내놓은 경우. 검증 실패와 구분한다 (검증기가 아니라 생성이 깨진 것)
    generation_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "candidate": self.candidate,
            "regenerated": self.regenerated,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        if self.generation_error:
            record["generation_error"] = self.generation_error
        return record


@dataclass
class ValidateResult:
    topic: str
    slug: str
    run_id: str
    attempts: list[Attempt] = field(default_factory=list)
    script_path: Path | None = None
    scenes: dict[str, Any] | None = None
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.script_path is not None

    @property
    def regenerations(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.regenerated)

    @property
    def errors(self) -> list[str]:
        """마지막 시도에 남은 검증 오류. 통과했으면 빈 목록이다."""
        return self.attempts[-1].errors if self.attempts else []

    @property
    def warnings(self) -> list[str]:
        return self.attempts[-1].warnings if self.attempts else []

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        regen = f"재생성 {self.regenerations}회"
        if self.passed:
            assert self.scenes is not None
            return (
                f"[2] {self.topic} — {regen} / {len(self.scenes['scenes'])}줄 / "
                f"{self.scenes['total_duration']:.1f}초 → 검증 통과 → {SCRIPT_FILE}{tail}"
            )
        return (
            f"[2] {self.topic} — {regen}(상한 {MAX_REGENERATIONS}) 소진 → "
            f"중단. 검증 실패 {len(self.errors)}건{tail}"
        )


def build_feedback(errors: list[str]) -> str:
    """검증 실패 사유를 프롬프트 꼬리(`${feedback}`)에 붙일 지시문으로 만든다.

    사유만 넣는다 — 직전 대본은 되돌려주지 않는다 (모듈 독스트링 참고). 오류 문구는
    검증기가 낸 그대로 쓴다. "594자 (범위 545~575자)"처럼 이미 무엇을 어디로 옮겨야
    하는지가 들어 있어, 여기서 다시 풀어 쓰면 스펙에 없는 지시를 지어내게 된다.
    """
    lines = "\n".join(f"{i}. {error}" for i, error in enumerate(errors, 1))
    return (
        "\n\n# 재생성 지시\n\n"
        "직전 대본이 아래 검증을 통과하지 못했다. 이번 대본은 전부 만족시켜라.\n\n"
        f"{lines}\n"
    )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise ValidateStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidateStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def run_validate_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> ValidateResult:
    paths = paths or Paths.from_env()

    if run_id:
        contract = _load_json(paths.run_dir(run_id) / "topic.json", "topic.json")
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    script_path = topic_dir / SCRIPT_FILE
    if state.is_done(STAGE) and not force and script_path.exists():
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return ValidateResult(
            topic=topic, slug=slug, run_id=run_id, skipped=True,
            script_path=script_path,
            scenes=_load_json(script_path, SCRIPT_FILE),
        )

    factsheet = _load_json(topic_dir / "04-factsheet.json", "팩트시트")
    candidates_dir = topic_dir / CANDIDATES_DIR
    first_path = candidates_dir / FIRST_CANDIDATE
    if not first_path.exists():
        raise ValidateStageError(
            f"대본 후보가 없다: {first_path}. [1. script]를 먼저 실행하라."
        )

    state.mark_running(STAGE)

    scenes = _load_json(first_path, FIRST_CANDIDATE)
    errors, warnings = validate_candidate(scenes, factsheet)
    attempts = [Attempt(FIRST_CANDIDATE, regenerated=False,
                        errors=errors, warnings=warnings)]
    log.info("[%s] %s 검증 — 오류 %d건 / 경고 %d건",
             STAGE, FIRST_CANDIDATE, len(errors), len(warnings))

    regenerations = 0
    while errors and regenerations < MAX_REGENERATIONS:
        regenerations += 1
        name = f"{regenerations + 1:02d}.json"
        label = f"{STAGE}.regen{regenerations}"
        log.info("[%s] 재생성 %d/%d — 실패 사유 %d건 피드백",
                 STAGE, regenerations, MAX_REGENERATIONS, len(errors))

        try:
            candidate, _meta = generate_candidate(
                llm=llm, topic=topic, run_id=run_id, factsheet=factsheet,
                feedback=build_feedback(errors), label=label,
            )
        except ScriptStageError as exc:
            # 생성이 깨진 것은 검증 실패와 다르다. 남은 재생성 횟수를 버리지 않고
            # 같은 사유로 한 번 더 간다 — errors를 갱신하지 않으므로 피드백은 그대로다.
            log.warning("[%s] 재생성 %d 실패: %s", STAGE, regenerations, exc)
            attempts.append(Attempt(name, regenerated=True, errors=errors,
                                    warnings=warnings, generation_error=str(exc)))
            continue

        write_text(candidates_dir / name, dump_json(candidate))
        scenes = candidate
        errors, warnings = validate_candidate(scenes, factsheet)
        attempts.append(Attempt(name, regenerated=True, errors=errors, warnings=warnings))
        log.info("[%s] %s 검증 — 오류 %d건 / 경고 %d건",
                 STAGE, name, len(errors), len(warnings))

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info: dict[str, Any] = {
        "regenerations": regenerations,
        "max_regenerations": MAX_REGENERATIONS,
        "report": [attempt.as_dict() for attempt in attempts],
    }

    if errors:
        # specs/05 "초과 시 중단·리포트". 후보 파일은 전부 남기고 사유를 state에 적는다.
        message = (
            f"재생성 {regenerations}회를 소진하고도 검증 실패 {len(errors)}건이 남았다"
        )
        state.mark_failed(STAGE, message, **info)
        log.warning("[%s] %s", STAGE, message)
        return ValidateResult(
            topic=topic, slug=slug, run_id=run_id, attempts=attempts, scenes=scenes,
        )

    write_text(script_path, dump_json(scenes))
    info["output"] = script_path.relative_to(paths.root).as_posix()
    info["source_candidate"] = attempts[-1].candidate
    state.mark_done(STAGE, **info)

    return ValidateResult(
        topic=topic, slug=slug, run_id=run_id, attempts=attempts,
        script_path=script_path, scenes=scenes,
    )
