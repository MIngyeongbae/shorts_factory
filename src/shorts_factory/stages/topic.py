"""[0a. topic] — 백로그 항목을 토픽 패키지로 승격한다.

specs/05-pipeline.md:
    topics/backlog.md → [0a. topic] → topics/{slug}/ 폴더 생성 (매체 적합성 판별, 스펙 06)

이 단계는 백로그에 **선언된** 관측 지표를 세어 하류로 넘긴다. **반려하지 않는다**
(ADR-0033 §1) — 지표가 비었다고 소재를 버리지 않고, 사료에 근거한 실제 판정은
[0b. research]가 verdict로 내린다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .. import backlog as backlog_mod
from ..backlog import STATUS_CANDIDATE, STATUS_RESEARCHING, BacklogEntry
from ..config import Paths, make_run_id, write_text
from ..jsonio import dump_json
from ..runstate import RunState
from . import status as status_mod

log = logging.getLogger(__name__)

STAGE = "0a-topic"

#: 지표 키 → 사람이 읽는 이름 (specs/06-topic-research.md "좋은 소재의 특징")
CONDITION_LABELS = {
    "twist": "통념 뒤집기",
    "failed_alternative": "실패한 대안의 기록",
    "numbers": "구체적 숫자",
    "present_link": "현재 접점",
}


class TopicStageError(Exception):
    pass


@dataclass
class TopicResult:
    topic: str
    slug: str
    run_id: str
    topic_dir: Path
    run_dir: Path
    accepted: bool
    unmet_conditions: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def summary(self) -> str:
        if not self.accepted:
            names = ", ".join(CONDITION_LABELS.get(k, k) for k in self.unmet_conditions)
            return f"[0a] 반려: '{self.topic}' — {names}"
        prefix = "[0a] 스킵(이미 생성됨)" if self.skipped else "[0a] 생성"
        line = f"{prefix}: {self.topic} → topics/{self.slug}/ (run_id={self.run_id})"
        if self.unmet_conditions:
            # ADR-0033 §1 — 관측이지 판정이 아니다. 반려하지 않고 세어서 보여 준다.
            names = ", ".join(CONDITION_LABELS.get(k, k) for k in self.unmet_conditions)
            line += f" / 관측: 비어 있는 지표 {len(self.unmet_conditions)}개 ({names})"
        return line


def _pick_entry(entries: list[BacklogEntry], needle: str | None) -> BacklogEntry:
    if needle:
        return backlog_mod.find_entry(entries, needle)

    candidates = [e for e in entries if e.status == STATUS_CANDIDATE]
    if not candidates:
        raise TopicStageError(
            f"백로그에 상태가 '{STATUS_CANDIDATE}'인 항목이 없다. "
            "소재를 지정하려면 --topic 을 쓴다."
        )
    return candidates[0]


def run_topic_stage(
    needle: str | None = None,
    *,
    paths: Paths | None = None,
    today: date | None = None,
    force: bool = False,
) -> TopicResult:
    paths = paths or Paths.from_env()
    entries = backlog_mod.parse_backlog(paths.backlog)
    entry = _pick_entry(entries, needle)

    slug = entry.slug
    run_id = make_run_id(slug, today)
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)

    # --- 관측 지표: 비어 있어도 반려하지 않는다 (ADR-0033 §1) --------------
    #
    # 예전에는 네 조건 중 하나라도 비면 여기서 패키지를 만들지 않았다. 그 조건들은
    # 전부 폐기된 고정 구조를 채우기 위한 것이었고, 지금 게이트는 매체 적합성 하나다
    # (스펙 06). 값은 `[1b] score`의 입력으로 흘려보내고 여기서는 세기만 한다.
    if not entry.all_conditions_met:
        log.info(
            "'%s' 백로그 관측 지표가 비어 있다(반려 아님): %s",
            entry.topic,
            ", ".join(entry.unmet_conditions),
        )

    state = RunState.load_or_create(
        run_dir, run_id, topic=entry.topic, slug=slug, stage_scope="1부"
    )

    already_done = state.is_done(STAGE) and topic_dir.exists()
    if already_done and not force:
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return TopicResult(
            topic=entry.topic,
            slug=slug,
            run_id=run_id,
            topic_dir=topic_dir,
            run_dir=run_dir,
            accepted=True,
            unmet_conditions=entry.unmet_conditions,
            skipped=True,
        )

    state.mark_running(STAGE)
    try:
        # specs/06 토픽 패키지 구조
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / "05-candidates").mkdir(exist_ok=True)

        status_path = topic_dir / "STATUS.md"
        # 사람이 이미 go/no-go를 적었다면 덮어쓰지 않는다
        if not status_path.exists():
            status_mod.write_status(
                status_path,
                status=status_mod.PENDING,
                topic=entry.topic,
                slug=slug,
                run_id=run_id,
                reason=(
                    "토픽 패키지 생성됨. 조사~대본 선발본이 완성된 뒤 "
                    "사람이 go / no-go를 기록한다."
                ),
                done_stages=("topic",),
            )

        # 단계 간 계약: runs/{run_id}/topic.json (specs/05 — 파일 기반 JSON)
        contract = {
            "stage": STAGE,
            "run_id": run_id,
            "topic": entry.topic,
            "slug": slug,
            "topic_dir": topic_dir.relative_to(paths.root).as_posix(),
            "backlog_conditions": entry.conditions,
            "sources_hint": entry.sources,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_text(run_dir / "topic.json", dump_json(contract))

        # 백로그 상태 전이: 후보 → 리서치중 (재선택 방지)
        if entry.status != STATUS_RESEARCHING:
            backlog_mod.update_status(paths.backlog, entry, STATUS_RESEARCHING)

        state.mark_done(
            STAGE,
            outputs=[
                (run_dir / "topic.json").relative_to(paths.root).as_posix(),
                (topic_dir / "STATUS.md").relative_to(paths.root).as_posix(),
            ],
        )
    except Exception as exc:  # 실패는 run 디렉터리에 기록하고 전파 (specs/05 실패 정책)
        state.mark_failed(STAGE, f"{type(exc).__name__}: {exc}")
        raise

    return TopicResult(
        topic=entry.topic,
        slug=slug,
        run_id=run_id,
        topic_dir=topic_dir,
        run_dir=run_dir,
        accepted=True,
        unmet_conditions=entry.unmet_conditions,
    )
