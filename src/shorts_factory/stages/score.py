"""[1b. score] — 대본 후보를 채점해 하나를 선발한다.

specs/05-pipeline.md:
    [1b. score] → 06-script.json (훅 스코어링: 비판 반영 채점 → 상위 1개)

    "shorts-hook-scorer 루브릭(hook_strength / info_density / standalone) 기반 LLM 채점.
     전 후보가 기준 미달이면 주제 자체를 반려하고 리포트."

## 이 단계가 지는 판단

**어느 후보가 시청자를 잡는가** 하나뿐이다. 고쳐 쓰지 않고, 사실을 검증하지 않는다.

`[1a]`가 각도를 고를 때 비판을 보지 않는 것은 설계다 — 비판 결론이 구성 작가를 물들이면
각도 다양성이 죽는다 (ADR-0009). 대신 **비판은 여기서 반영된다** (specs/05:24 「비판 반영
채점」). 다다미 편 실측에서 `[1a]`는 비판을 못 본 채 비판의 진행 조건 6개 중 5개를 스스로
지켰고, 어긋난 것은 훅 선택 하나였다 — 그 하나가 이 단계의 정의다 (ADR-0040).

## 임계값이 값 파일에 있는 이유

`min_total`·`min_per_axis`는 `specs/schema/script-rules.json`의 `score` 절에 있고 코드는
로드만 한다 (ADR-0034 §3). **캘리브레이션 전이면 `None`이고, 그때 이 단계는 채점만 하고
반려하지 않는다** — 근거 없는 수로 반려선을 그으면 첫 편에서 임의로 갈린다 (ADR-0040).

## 이 단계가 하지 않는 것

- **후보를 만드는 것.** `[1w]`가 만든 것을 읽을 뿐이고, 후보 수를 늘리는 것은 `[1w]` 몫이다
  (specs/05: "후보 수는 `[1b. score]` 도입 전까지 1개다"). D-4를 지킨다
- **검증.** 분량·스키마·그라운딩은 `[2. validate]`가 본다. 미달 판정과는 축이 다르다
- **재채점.** 점수가 낮다고 다시 부르지 않는다 — 같은 후보에 같은 루브릭이면 결과가 같다
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
from ..schemas import score as score_schema
from ..schemas.script_rules import core_chars
from .outline import OUTLINE_FILE, resolve_run
from .session import ScriptSessionError, ask_json, load_prompt

log = logging.getLogger(__name__)

STAGE = "1b-score"
PROMPT = "12-score.md"
SCORE_FILE = "09-score.json"
SCRIPT_FILE = "06-script.json"
CANDIDATES_DIR = "05-candidates"
CRITIQUE_FILE = "03-critique.md"

#: 깨진 JSON을 다시 부르는 상한. `[0b]`의 `MAX_FACTSHEET_ATTEMPTS`와 같은 성격이다 —
#: 생성 슬립은 재시도로 붙고, 계약 위반은 재시도로 안 붙는다.
MAX_SCORE_ATTEMPTS = 3


@dataclass
class ScoreResult:
    topic: str
    slug: str
    run_id: str
    path: Path | None
    score: dict[str, Any] | None
    script_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def valid(self) -> bool:
        return self.score is not None and not self.errors

    @property
    def verdict(self) -> str | None:
        return (self.score or {}).get("verdict")

    @property
    def summary(self) -> str:
        if self.score is None:
            return f"[1b] {self.topic} — 채점 실패"
        scored = self.score.get("candidates", [])
        totals = ", ".join(
            f"{c.get('candidate')}={c.get('total')}" for c in scored
        )
        tail = " (스킵)" if self.skipped else ""
        if self.verdict == "reject":
            return (
                f"[1b] {self.topic} — 후보 {len(scored)}개 채점 ({totals}) → "
                f"**전 후보 기준 미달, 선발 없음**{tail}"
            )
        chosen = self.score.get("chosen")
        return (
            f"[1b] {self.topic} — 후보 {len(scored)}개 채점 ({totals}) → "
            f"{chosen} 선발 → {SCRIPT_FILE}{tail}"
        )


def load_candidates(topic_dir: Path) -> dict[str, dict[str, Any]]:
    """`05-candidates/*.json`을 파일명 → 문서로. 이름 순이다."""
    directory = topic_dir / CANDIDATES_DIR
    if not directory.is_dir():
        raise ScriptSessionError(
            f"후보 디렉터리가 없다: {directory}. [1w. write]를 먼저 실행하라."
        )
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            found[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScriptSessionError(f"후보를 읽을 수 없다: {path} — {exc}") from exc
    if not found:
        raise ScriptSessionError(
            f"채점할 후보가 없다: {directory}. [1w. write]를 먼저 실행하라."
        )
    return found


def format_candidates(candidates: dict[str, dict[str, Any]]) -> str:
    """후보를 채점 세션이 읽을 형태로. 문장과 그림 목표만 준다.

    씬 계약 전체를 그대로 넣지 않는 이유는 연출 필드가 채점 축과 무관하기 때문이다.
    `visual_goal`은 남긴다 — `info_density`가 "그림이 설명을 지면 밀도가 오른다"는
    축이라(스펙 01) 그림이 무엇을 지는지를 봐야 잴 수 있다.
    """
    blocks: list[str] = []
    for name, doc in candidates.items():
        scenes = [s for s in doc.get("scenes", []) if isinstance(s, dict)]
        chars = len(core_chars(" ".join(str(s.get("text", "")) for s in scenes)))
        lines = [f"## 후보 `{name}` — 자막 {len(scenes)}줄 / 본문 {chars}자", ""]
        for scene in scenes:
            goal = str(scene.get("visual_goal") or "").strip()
            suffix = f"  〔그림: {goal}〕" if goal else ""
            lines.append(f"{scene.get('scene_id')}. {scene.get('text')}{suffix}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_axes() -> str:
    """축 설명을 프롬프트 목록으로. 값 파일의 `_axes_meaning`이 정본이다."""
    from ..schemas import vocab

    rules = vocab.score_rules()
    meaning = rules.get("_axes_meaning", {})
    return "\n".join(
        f"- **{axis}** (0~{rules['max_per_axis']}): {meaning.get(axis, '')}".rstrip()
        for axis in rules["axes"]
    )


def read_optional(path: Path) -> str:
    """선택적 입력. 없으면 빈 문자열이고 경고하지 않는다 (specs/05 D-3)."""
    if not path.exists():
        log.info("[%s] 선택 입력이 없어 그대로 진행한다: %s", STAGE, path.name)
        return ""
    return path.read_text(encoding="utf-8")


def apply_thresholds(score: dict[str, Any]) -> list[str]:
    """임계값을 대조해 `verdict`·`chosen`을 확정한다. 미달 사유를 돌려준다.

    **판정은 기계가 한다.** 세션은 점수와 선발만 내고 `verdict`를 쓰지 않는다 —
    임계값 대조까지 세션에 맡기면 반려선이 매 호출 흔들린다.
    """
    reasons: list[str] = []
    passing: list[dict[str, Any]] = []
    for scored in score.get("candidates", []):
        below = score_schema.below_threshold(scored)
        if below:
            reasons.append(f"{scored.get('candidate')}: {', '.join(below)}")
        else:
            passing.append(scored)

    if not passing:
        score["verdict"] = "reject"
        score["chosen"] = None
        return reasons

    chosen = score.get("chosen")
    if chosen not in {c.get("candidate") for c in passing}:
        # 세션이 고른 후보가 미달이면 통과한 것 중 최고점으로 내린다.
        best = max(passing, key=lambda c: int(c.get("total", 0)))
        score["chosen"] = best.get("candidate")
        score.setdefault("notes", "")
        score["notes"] = (
            f"{score['notes']} 세션이 고른 후보가 기준 미달이라 통과 후보 중 "
            f"최고점({best.get('candidate')})으로 바꿨다."
        ).strip()
    score["verdict"] = "pass"
    return reasons


def generate_score(
    *,
    llm: LLMClient,
    topic: str,
    candidates: dict[str, dict[str, Any]],
    outline: str,
    critique: str,
    label: str = STAGE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """후보 → 채점 결과 1개. 파일 쓰기·상태 기록은 호출자 몫이다.

    **깨진 JSON은 재시도한다** (`[0b]`의 팩트시트와 같은 패턴). 첫 실전 호출에서 세션이
    `axes`의 닫는 중괄호를 하나 빠뜨려 파싱이 죽었다 — 3중 중첩에 긴 문자열이 얹히면
    나오는 생성 슬립이고, 같은 프롬프트로 다시 부르면 대개 붙는다. 계약 위반(축 합
    불일치 등)은 여기서 재시도하지 않는다 — 그건 채점 내용의 문제이고 `validate_score`가
    잡아 산출물과 함께 남긴다.
    """
    from ..schemas import vocab

    template = load_prompt(PROMPT)
    context = dict(
        topic=topic,
        candidates=format_candidates(candidates),
        outline=outline or "(구성안이 없다 — 각도 의도 없이 대본만 보고 채점한다)",
        critique=critique or "(비판 산출물이 없다 — 대본만 보고 채점한다)",
        axes=format_axes(),
        max_per_axis=vocab.score_rules()["max_per_axis"],
    )

    feedback = ""
    last: ScriptSessionError | None = None
    for attempt in range(1, MAX_SCORE_ATTEMPTS + 1):
        log.info("[%s] 채점 시도 %d/%d", label, attempt, MAX_SCORE_ATTEMPTS)
        try:
            payload, meta = ask_json(
                llm, template.safe_substitute(**context, feedback=feedback),
                label=f"{label}.try{attempt}",
            )
        except ScriptSessionError as exc:
            last = exc
            feedback = (
                "\n# 앞 시도가 실패했다\n\n"
                f"{exc}\n\n"
                "JSON이 깨졌다. 여는 괄호와 닫는 괄호를 세어 맞추고, "
                "`why`를 짧게 써서 한 객체가 길어지지 않게 하라."
            )
            continue

        if isinstance(payload.get("candidates"), list) and payload["candidates"]:
            payload.setdefault("topic", topic)
            return payload, meta

        last = ScriptSessionError(f"{label}: candidates가 비었다")
        feedback = (
            "\n# 앞 시도가 실패했다\n\n"
            "`candidates` 배열이 비어 있었다. 주어진 후보를 하나도 빼지 말고 채점하라."
        )

    raise last or ScriptSessionError(f"{label}: 채점에 실패했다")


def run_score_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> ScoreResult:
    paths = paths or Paths.from_env()
    run_id, contract = resolve_run(paths, slug, run_id)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    candidates = load_candidates(topic_dir)
    path = topic_dir / SCORE_FILE

    if not force and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("[%s] 기존 채점을 읽을 수 없어 다시 채점한다", STAGE)
        else:
            log.info("[%s] 채점이 이미 있어 스킵한다: %s", STAGE, SCORE_FILE)
            errors, warnings = score_schema.validate_score(existing)
            return ScoreResult(
                topic=topic, slug=slug, run_id=run_id, path=path, score=existing,
                errors=errors, warnings=warnings, skipped=True,
            )

    state.mark_running(STAGE)
    log.info(
        "[%s] 독립 헤드리스 세션 시작 (도구 없음) — 후보 %d개", STAGE, len(candidates)
    )

    try:
        score, meta = generate_score(
            llm=llm,
            topic=topic,
            candidates=candidates,
            outline=read_optional(topic_dir / OUTLINE_FILE),
            critique=read_optional(topic_dir / CRITIQUE_FILE),
        )
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise ScriptSessionError(message) from exc

    unknown = [
        str(c.get("candidate"))
        for c in score.get("candidates", [])
        if c.get("candidate") not in candidates
    ]
    below = apply_thresholds(score)
    write_text(path, dump_json(score))

    errors, warnings = score_schema.validate_score(score)
    if unknown:
        errors.append(
            f"candidates: 존재하지 않는 후보를 채점했다: {', '.join(unknown)}"
        )
    for reason in below:
        warnings.append(f"기준 미달 — {reason}")
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    script_path: Path | None = None
    if not errors and score.get("verdict") == "pass":
        script_path = topic_dir / SCRIPT_FILE
        chosen = str(score["chosen"])
        selected = dict(candidates[chosen])
        selected.setdefault("run_id", run_id)
        selected.setdefault("topic", topic)
        write_text(script_path, dump_json(selected))

    info = {
        "output": path.relative_to(paths.root).as_posix(),
        "candidates": len(candidates),
        "verdict": score.get("verdict"),
        "chosen": score.get("chosen"),
        "below_threshold": below,
        "validation_errors": errors,
        "validation_warnings": warnings,
        **meta,
    }
    if errors:
        log.warning("[%s] 검증 실패 %d건 — 채점 결과는 남긴다", STAGE, len(errors))
        state.mark_failed(STAGE, f"검증 실패 {len(errors)}건", **info)
    elif score.get("verdict") == "reject":
        state.mark_done(STAGE, **info)
        log.warning(
            "[%s] 전 후보가 기준 미달이다 — 주제를 반려하고 리포트한다 (specs/05)", STAGE
        )
    else:
        state.mark_done(STAGE, **info)

    return ScoreResult(
        topic=topic, slug=slug, run_id=run_id, path=path, score=score,
        script_path=script_path, errors=errors, warnings=warnings,
    )
