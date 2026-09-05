"""[8. ending] — `[4]`가 모아 둔 실사진을 큐레이션해 엔딩 컷으로 만든다. ADR-0055.

specs/05-pipeline.md:
    [8. ending] → ending/{n}.mp4 + ending.json + ending/credits.txt
    (엔딩 실사 컷. 인물·워터마크 기각, 무수정 표시)

## 왜 이 단계가 있는가

본편은 전부 AI 그림이다. 마지막 몇 초를 **실제 사진**으로 닫으면 설명(그림은 이해를
보조한다 — ADR-0047)과 신뢰(방금 본 것이 실재한다)를 함께 닫을 수 있다.

**재료는 이미 있었다.** `[4] refpack`이 출처 페이지에서 라이선스를 확인하고(WebFetch)
허용 라이선스인 사진만 내려받아 `refs/`에 두는데, ADR-0046이 MJ 첨부 경로를 보류한 뒤로
**그 파일들을 읽는 단계가 0개였다.** 이 단계는 새로 수집하지 않는다 — 검증까지 끝난
재고를 고를 뿐이다.

## 세 칸으로 돈다

| 칸 | 누가 | 무엇 |
|---|---|---|
| ① 대조 | 기계 | 게시 가능 라이선스인가, 표시 의무를 지킬 `credit`이 있는가 |
| ② 판정 | 비전 세션 1회 | 실사인가, 대상이 보이는가, **인물이 있는가**, 사고가 없는가 |
| ③ 렌더 | FFmpeg | 무수정 표시 + 크레딧 번인 |

## 산출은 화면 순서가 아니라 **풀**이다 (ADR-0092)

통과분을 `max_photos`가 아니라 **`pool_size()`(= `max_photos × 언어 수`)까지** 렌더하고,
어느 사진이 어느 언어로 가는지는 **이미 언어별로 도는 `[9]`가 회전으로 정한다**. 이 단계는
언어 파일을 하나도 읽지 않는다 — 언어를 모른 채 남는 것이 단계 독립이다.

**풀 클립은 전부 꼬리를 달고 렌더한다** (`clip_lengths`) — 어느 장이 마지막인지가 언어마다
다르기 때문이다.

세 채널에 같은 화면이 올라가는 것을 줄이려는 결정이고, 근거와 되돌릴 조건은 ADR-0092에
있다. **원인이 확인된 것은 아니다** — 변수 하나짜리 업로드 테스트가 따로 돈다.

**①과 ②를 가른 이유는 `[4]`가 `attachable`을 세션에서 뺏어 온 것과 같다** — 저작권이
걸린 축이라 편마다 기준이 흔들리면 안 된다. 반대로 **인물 판정은 세션 몫이다**:
라이선스가 답할 수 없는 축이고(자유 라이선스 사진에도 초상권은 남는다) 파일을 봐야 안다.

## 없는 것이 정상인 편이 있다

실물이 없는 소재(개념·도해 중심)에는 붙일 사진이 없다. 후보가 0장이거나 전부 기각이면
**`ending.json`을 쓰지 않고** 그 사실만 기록한다 — `[9]`는 파일이 없으면 지금과 똑같이
돈다 (specs/05 D-3). 부재를 경고로 만들면 편마다 경고가 쏟아진다.

기각 사유는 `ending.json`이 아니라 `state.json`에 남는다(계약 파일을 안 쓰므로) — 재고로
엔딩이 안 서는 편이 잦은지가 ADR-0055의 되돌릴 조건이다.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient, LLMError
from ..runstate import RunState
from ..schemas import ending as ending_schema
from ..schemas import refs as refs_schema
from ..video.ending import (
    build_command,
    credit_line,
    credits_document,
    frame_count,
)
from ..video.ffmpeg import (
    DEFAULT_FFMPEG,
    FFmpegError,
    escape_filter_path,
    relative_path,
    run_ffmpeg,
)
from ..video.subtitles import FONT_SUFFIXES, FONTS_DIR
from ..video.timeline import DISSOLVE_SECONDS
from .contract import (
    SceneContractNotFound,
    find_contract_for_run,
    load_scene_contract,
)
from .session import load_prompt

log = logging.getLogger(__name__)

STAGE = "8-ending"
PROMPT = "15-ending.md"

REFS_FILE = refs_schema.RECORD_FILE
RECORD_FILE = ending_schema.RECORD_FILE
ENDING_DIR = ending_schema.ENDING_DIR
CREDITS_FILE = ending_schema.CREDITS_FILE

#: 판정 세션의 도구 — 사진을 직접 열어 본다 (`[6r]`과 같은 메커니즘, ADR-0031).
TOOLS: tuple[str, ...] = ("Read",)

#: 판정 세션 상한(초). 사진 몇 장을 열어 보는 일이라 조사 단계보다 짧다.
TIMEOUT = 600

KEEP, REJECT = "keep", "reject"


class EndingStageError(Exception):
    """`[8]`이 결과를 낼 수 없는 경우. 사진 하나의 실패는 여기로 오지 않는다."""


@dataclass
class EndingResult:
    run_id: str
    topic: str
    run_dir: Path
    record_path: Path | None = None
    photos: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def burned(self) -> int:
        """크레딧을 화면에 실제로 구운 컷 수."""
        return sum(1 for p in self.photos if p.get("burned"))

    def rejected_by(self, by: str) -> int:
        return sum(1 for r in self.rejected if r.get("by") == by)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        if not self.photos:
            return (
                f"[8] {self.topic} — 쓸 사진이 없어 엔딩 없이 간다 "
                f"(대조 기각 {self.rejected_by(ending_schema.BY_MACHINE)} · "
                f"판정 기각 {self.rejected_by(ending_schema.BY_REVIEW)}, D-3){tail}"
            )
        per_language = min(ending_schema.max_photos(), len(self.photos))
        split = (
            "언어별로 갈린다" if len(self.photos) >= 2 * ending_schema.max_photos()
            else "풀이 얕아 언어끼리 겹친다" if len(self.photos) > 1
            else "1장뿐이라 세 언어가 같다"
        )
        return (
            f"[8] {self.topic} — 엔딩 풀 {len(self.photos)}장 "
            f"(언어당 {per_language}컷, {split} · "
            f"크레딧 번인 {self.burned}/{len(self.photos)} · "
            f"기각 {len(self.rejected)}) → {RECORD_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise EndingStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EndingStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(paths: Paths, slug: str) -> str:
    """슬러그 → run_id. 계보는 run_id로 잇는다 (ADR-0017)."""
    try:
        script, _path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise EndingStageError(str(exc)) from exc
    run_id = script.get("run_id")
    if not run_id:
        raise EndingStageError(f"씬 계약에 run_id가 없다 (slug={slug})")
    return str(run_id)


def _topic_for(paths: Paths, run_id: str, slug: str | None) -> str:
    """세션 프롬프트에 실을 소재명. **없어도 돈다** — 판정에 쓰는 맥락이지 계약이 아니다."""
    try:
        script = (
            load_scene_contract(paths, slug)[0] if slug
            else find_contract_for_run(paths, run_id)
        )
    except SceneContractNotFound:
        return run_id
    return str(script.get("topic") or run_id)


def collect_candidates(refs: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """`refs.json` → `(판정에 올릴 후보, 기계가 뺀 것)`. **대조이지 판단이 아니다.**

    거르는 축은 둘이다 (ADR-0055):

    1. **게시 가능 라이선스인가** — `attachable`(프롬프트에 붙여도 되는가)과 값이 같아도
       묻는 것이 다르다. 여기서는 사진이 그대로 화면에 나간다
    2. **표시 의무를 지킬 수 있는가** — `cc-by` 계열인데 `credit`이 비어 있으면 저작자를
       표시할 방법이 없다. 라이선스를 어기는 쪽보다 후보에서 빼는 쪽이 안전하다

    같은 사진이 여러 씬의 참조로 잡혔을 수 있으므로 `source_url`과 파일 경로로 한 번만
    본다 — 같은 그림이 엔딩에 두 번 나오면 마감이 아니라 사고다.
    """
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in refs.get("scenes", []):
        scene_id = entry.get("scene_id")
        for image in entry.get("images", []):
            file = str(image.get("file") or "").strip()
            if not file:
                # 내려받지 않은 사진이다 (서술 경로만 탄 것). 화면에 올릴 파일이 없다.
                continue

            source_url = str(image.get("source_url") or "").strip()
            license_name = str(image.get("license") or "unknown")
            credit = str(image.get("credit") or "").strip()
            base: dict[str, Any] = {
                "source": file,
                "source_url": source_url,
                "license": license_name,
            }
            if isinstance(scene_id, int):
                base["scene_id"] = scene_id

            key = source_url or file
            if key in seen:
                continue
            seen.add(key)

            if not ending_schema.is_publishable(license_name):
                rejected.append({
                    **base, "by": ending_schema.BY_MACHINE,
                    "reason": f"게시할 수 없는 라이선스다 ({license_name})",
                })
                continue
            if not ending_schema.credit_ok(license_name, credit):
                rejected.append({
                    **base, "by": ending_schema.BY_MACHINE,
                    "reason": (
                        f"{license_name}은 저작자 표시가 의무인데 credit이 비어 있다"
                    ),
                })
                continue

            candidates.append({
                **base,
                "credit": credit,
                "shows": str(image.get("shows") or "").strip(),
            })

    return candidates, rejected


def prompt_ids(candidates: list[dict[str, Any]], run_dir: Path) -> dict[str, str]:
    """`{세션에 보여 줄 절대 경로: refs.json의 상대 경로}`.

    **세션은 임시 디렉터리에서 돈다** (`llm/claude_code.py`의 `cwd=workdir`). `refs.json`이
    적어 둔 `refs/7/01.jpg`는 거기서 열리지 않으므로 프롬프트에는 절대 경로를 주고
    `add_dirs`로 run 디렉터리를 열어 준다 — `[7]`의 비전 검수와 같은 메커니즘이다.
    기록(`ending.json`·`state.json`)은 계속 run 디렉터리 기준 상대 경로로 남는다.
    """
    return {str((run_dir / c["source"]).resolve()): c["source"] for c in candidates}


def relabel_verdicts(payload: dict[str, Any], alias: dict[str, str]) -> dict[str, Any]:
    """세션이 돌려준 `id`(절대 경로)를 기록용 상대 경로로 되돌린다.

    상대 경로를 그대로 돌려주는 세션도 있으므로 매핑에 없는 `id`는 손대지 않는다 —
    `parse_verdicts`가 후보 집합과 대조해 걸러 낸다 (관용적 파싱).
    """
    photos = payload.get("photos")
    if not isinstance(photos, list):
        return payload
    relabeled = []
    for item in photos:
        if isinstance(item, dict):
            key = str(item.get("id") or "").strip()
            source = alias.get(key)
            if source is None and key:
                try:
                    source = alias.get(str(Path(key).resolve()))
                except OSError:
                    source = None
            if source:
                item = {**item, "id": source}
        relabeled.append(item)
    return {**payload, "photos": relabeled}


def render_candidates(
    candidates: list[dict[str, Any]], run_dir: Path | None = None
) -> str:
    """세션에 보여줄 후보 목록. 씬 번호도 라이선스도 보여 주지 않는다.

    **판정은 그림만 보고 한다.** 라이선스는 기계가 이미 걸렀고, 그 값을 세션에 주면
    "출처가 좋으니 통과"처럼 그림 밖의 근거가 판정에 섞인다 (`[6r]`이 대본 문장을 받지
    않는 것과 같은 태도 — ADR-0038).

    `run_dir`을 주면 `Read`가 실제로 열 수 있는 절대 경로를 적는다 (`prompt_ids`).
    """
    blocks: list[str] = []
    for candidate in candidates:
        shown = (
            str((run_dir / candidate["source"]).resolve())
            if run_dir is not None else candidate["source"]
        )
        lines = [f"- `{shown}`"]
        if candidate.get("shows"):
            lines.append(f"  - 무엇이 찍혔다고 기록됐나: {candidate['shows']}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def parse_verdicts(
    payload: dict[str, Any], known: set[str]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """세션 출력 → `({source: {verdict, order, reason}}, 경고)`.

    **빠진 후보는 기각으로 본다.** `[6i]`가 판정 누락을 오류로 올리는 것과 갈리는
    자리인데, 거기서는 누락이 "검수 안 된 글자가 화면에 나간다"였고 여기서는 "사진 한 장을
    덜 쓴다"다. 엔딩은 없어도 되는 마감이므로 **모자란 쪽으로 떨어뜨린다** (D-5).
    """
    verdicts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for item in payload.get("photos") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("id") or "").strip()
        if source not in known:
            if source:
                warnings.append(f"후보에 없는 사진을 판정해 버렸다: {source}")
            continue
        verdict = item.get("verdict")
        if verdict not in (KEEP, REJECT):
            warnings.append(f"{source}: 알 수 없는 판정 '{verdict}' → 기각으로 본다")
            verdict = REJECT
        order = item.get("order")
        verdicts[source] = {
            "verdict": verdict,
            "order": order if isinstance(order, int) else None,
            "reason": str(item.get("reason") or "").strip(),
        }

    for source in sorted(known - set(verdicts)):
        warnings.append(f"{source}: 세션이 판정하지 않았다 → 기각으로 본다")
        verdicts[source] = {"verdict": REJECT, "order": None, "reason": "판정 누락"}

    return verdicts, warnings


def select(
    candidates: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """판정 결과 → `(고른 것, 기각된 것)`. 세션이 매긴 `order`가 표시 순서다.

    `order`를 안 준 `keep`은 후보 목록 순서로 뒤에 붙인다 — 순서 하나가 비었다고 쓸 수
    있는 사진을 버리지 않는다. 상한을 넘긴 몫은 **기각이 아니라 잘림이므로** 사유를
    그렇게 적는다.
    """
    kept: list[tuple[int, int, dict[str, Any]]] = []
    rejected: list[dict[str, Any]] = []

    for position, candidate in enumerate(candidates):
        verdict = verdicts[candidate["source"]]
        if verdict["verdict"] != KEEP:
            rejected.append({
                "source": candidate["source"],
                "source_url": candidate["source_url"],
                "license": candidate["license"],
                **({"scene_id": candidate["scene_id"]} if "scene_id" in candidate else {}),
                "by": ending_schema.BY_REVIEW,
                "reason": verdict["reason"] or "판정 기각",
            })
            continue
        order = verdict["order"] if verdict["order"] is not None else len(candidates) + position
        kept.append((order, position, {**candidate, "reason": verdict["reason"]}))

    kept.sort(key=lambda row: (row[0], row[1]))

    chosen = [row[2] for row in kept[:limit]]
    for _order, _position, extra in kept[limit:]:
        rejected.append({
            "source": extra["source"],
            "source_url": extra["source_url"],
            "license": extra["license"],
            **({"scene_id": extra["scene_id"]} if "scene_id" in extra else {}),
            "by": ending_schema.BY_MACHINE,
            "reason": f"상한 {limit}장을 넘어 잘렸다",
        })
    return chosen, rejected


def find_font(paths: Paths) -> Path | None:
    """크레딧을 구울 폰트 파일. 없으면 `None`이고 그때는 굽지 않는다.

    ADR-0002가 레이어 B 폰트를 리포지토리 에셋으로 두기로 했다 (`[9]`의 `_fonts_dir`와
    같은 자산). 없으면 **굽기만 강등되고 기록은 남는다** — 표시 의무를 지키는 수단이
    화면 하나뿐이면 폰트가 빠진 체크아웃에서 조용히 의무를 어기게 된다.
    """
    fonts = paths.root / FONTS_DIR
    if not fonts.is_dir():
        return None
    found = sorted(p for p in fonts.iterdir() if p.suffix.lower() in FONT_SUFFIXES)
    return found[0] if found else None


def clip_lengths(count: int, *, seconds: float, dissolve: float) -> list[float]:
    """컷마다 렌더할 길이. **전부 꼬리를 단다** (ADR-0092).

    꼬리는 다음 컷과 겹치는 디졸브 몫이고, `[9]`의 `trim`이 필요한 만큼만 잘라 쓴다.

    **「마지막만 꼬리가 없다」던 옛 불변식은 여기서 놓는다** — `photos`가 화면 순서가
    아니라 언어별로 갈라 쓸 풀이 되면서 **어느 장이 마지막인지가 언어마다 다르다.**
    한 언어의 마지막 장이 다른 언어에서는 가운데라 꼬리가 필요하다. 남는 꼬리를 두는
    비용은 파일 0.6초뿐이고, 계약은 `seconds`(표시 초)이지 파일 길이가 아니다
    (`ending.schema.json` `photo.seconds`).
    """
    return [round(seconds + dissolve, 3) for _ in range(count)]


def run_ending_stage(
    *,
    llm: LLMClient,
    run_id: str | None = None,
    slug: str | None = None,
    paths: Paths | None = None,
    force: bool = False,
    timeout: int | None = None,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner=subprocess.run,
) -> EndingResult:
    paths = paths or Paths.from_env()

    if not run_id:
        if not slug:
            raise EndingStageError("run_id나 slug 중 하나는 있어야 한다")
        run_id = resolve_run_id(paths, slug)

    run_dir = paths.run_dir(run_id)
    topic = _topic_for(paths, run_id, slug)
    record_path = run_dir / RECORD_FILE
    result = EndingResult(run_id=run_id, topic=topic, run_dir=run_dir)

    seed: dict[str, Any] = {"topic": topic}
    if slug:
        seed["slug"] = slug
    state = RunState.load_or_create(run_dir, run_id, **seed)

    if state.is_done(STAGE) and not force:
        if record_path.exists():
            previous = _load_json(record_path, RECORD_FILE)
            log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
            result.skipped = True
            result.record_path = record_path
            result.photos = previous.get("photos", [])
            result.rejected = previous.get("rejected", [])
            return result
        if state.stage(STAGE).get("photos") == 0:
            # 지난 실행이 "엔딩 없음"으로 끝난 편이다. 계약 파일이 없는 것이 결과다.
            log.info("[%s] 지난 실행에서 쓸 사진이 없었다 — 스킵 (D-3)", STAGE)
            result.skipped = True
            return result

    def finish_without_ending(reason: str, rejected: list[dict[str, Any]]) -> EndingResult:
        """엔딩 없이 끝낸다. **계약 파일을 남기지 않는다.**

        지난 실행의 `ending.json`이 남아 있으면 `[9]`가 그것을 읽고 **이번에 만들지 않은
        클립을 찾는다.** 파일의 부재가 곧 "엔딩 없음"이라는 신호이므로 치우고 나간다
        (`[6i]`가 강등한 씬의 INFO를 지우는 것과 같은 이유).
        """
        record_path.unlink(missing_ok=True)
        log.info("[%s] %s — 엔딩 없이 간다 (D-3)", STAGE, reason)
        result.rejected = rejected
        state.mark_done(
            STAGE, photos=0, note=reason,
            rejected=len(rejected),
            rejected_by_machine=result.rejected_by(ending_schema.BY_MACHINE),
            rejected_by_review=result.rejected_by(ending_schema.BY_REVIEW),
            rejections=rejected,
        )
        return result

    refs_path = run_dir / REFS_FILE
    if not refs_path.exists():
        # 선택적 입력의 부재는 경고가 아니다 (D-3). `[4]`를 안 돌린 편은 엔딩이 없다.
        return finish_without_ending(f"{REFS_FILE}이 없다 ([4] refpack 미실행)", [])

    refs = _load_json(refs_path, f"[4]의 산출물({REFS_FILE})")
    candidates, machine_rejected = collect_candidates(refs)
    if not candidates:
        return finish_without_ending("게시할 수 있는 사진이 없다", machine_rejected)

    state.mark_running(STAGE)
    log.info(
        "[%s] 판정 세션 시작 — 후보 %d장 (대조 기각 %d, Read 도구)",
        STAGE, len(candidates), len(machine_rejected),
    )

    alias = prompt_ids(candidates, run_dir)
    prompt = load_prompt(PROMPT).safe_substitute(
        topic=topic,
        photos=render_candidates(candidates, run_dir),
        max_photos=ending_schema.max_photos(),
    )
    try:
        # `ask_json`이 아니라 직접 부른다 — 사진이 있는 run 디렉터리를 `add_dirs`로 열어
        # 줘야 세션이 `Read`로 그림을 본다 (`[7]`의 비전 검수와 같은 메커니즘).
        session = llm.run(
            prompt, allowed_tools=TOOLS, timeout=timeout or TIMEOUT,
            label=STAGE, add_dirs=(run_dir,),
        )
        payload = relabel_verdicts(extract_json_object(session.text), alias)
        meta = dict(session.meta)
    except (LLMError, JSONExtractionError) as exc:
        message = f"{STAGE}: 판정 세션이 실패했다 — {exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise EndingStageError(message) from exc

    verdicts, warnings = parse_verdicts(payload, {c["source"] for c in candidates})
    # 상한은 **한 언어의 장수가 아니라 풀 크기**다 (ADR-0092) — `[9]`가 여기서 언어별로
    # 갈라 쓴다. 예전에는 넘긴 몫을 "상한을 넘어 잘렸다"로 버렸는데, 그게 언어를 가를
    # 재고였다.
    chosen, review_rejected = select(
        candidates, verdicts, limit=ending_schema.pool_size()
    )
    rejected = machine_rejected + review_rejected
    result.warnings = warnings

    if not chosen:
        return finish_without_ending("후보가 전부 기각됐다", rejected)

    # --- 렌더 -----------------------------------------------------------------

    ending_dir = run_dir / ENDING_DIR
    ending_dir.mkdir(parents=True, exist_ok=True)
    font = find_font(paths)
    if font is None:
        warnings.append(
            f"{FONTS_DIR}/에 폰트가 없어 크레딧을 화면에 굽지 못했다 — "
            f"{ENDING_DIR}/{CREDITS_FILE}에는 남는다 (ADR-0055 강등 사다리)"
        )

    seconds = ending_schema.photo_seconds()
    lengths = clip_lengths(len(chosen), seconds=seconds, dissolve=DISSOLVE_SECONDS)

    photos: list[dict[str, Any]] = []
    for position, candidate in enumerate(chosen):
        index = len(photos) + 1
        source = run_dir / candidate["source"]
        if not source.exists():
            rejected.append({
                "source": candidate["source"],
                "source_url": candidate["source_url"],
                "license": candidate["license"],
                "by": ending_schema.BY_MACHINE,
                "reason": "내려받은 파일이 사라졌다",
            })
            warnings.append(f"{candidate['source']}: 파일이 없어 건너뛴다")
            continue

        line = credit_line(candidate.get("credit", ""), candidate["license"])
        credit_path: Path | None = None
        if line and font is not None:
            credit_path = ending_dir / f"{index}.credit.txt"
            write_text(credit_path, line + "\n")

        target = ending_dir / f"{index}.mp4"
        cmd = build_command(
            relative_path(source, run_dir),
            relative_path(target, run_dir),
            frames=frame_count(lengths[position]),
            credit_textfile=(
                escape_filter_path(credit_path, run_dir) if credit_path else None
            ),
            fontfile=escape_filter_path(font, run_dir) if font else None,
            executable=ffmpeg,
        )
        try:
            run_ffmpeg(cmd, cwd=run_dir, produces=target, runner=runner)
        except FFmpegError as exc:
            # 컷 하나의 실패로 엔딩 전체를 버리지 않는다 (D-5).
            rejected.append({
                "source": candidate["source"],
                "source_url": candidate["source_url"],
                "license": candidate["license"],
                "by": ending_schema.BY_MACHINE,
                "reason": f"렌더 실패 ({exc})",
            })
            warnings.append(f"{candidate['source']}: 렌더 실패로 건너뛴다 — {exc}")
            continue

        entry: dict[str, Any] = {
            "index": index,
            "file": f"{ENDING_DIR}/{target.name}",
            "seconds": seconds,
            "source": candidate["source"],
            "source_url": candidate["source_url"],
            "license": candidate["license"],
            "burned": bool(credit_path),
        }
        for key in ("scene_id", "credit", "shows", "reason"):
            value = candidate.get(key)
            if value not in (None, ""):
                entry[key] = value
        if line:
            entry["credit_line"] = line
        photos.append(entry)

    if not photos:
        return finish_without_ending("고른 사진을 하나도 렌더하지 못했다", rejected)

    # 마지막 컷은 꼬리가 없다. 중간에 렌더가 빠지면 장수가 줄어 꼬리 배치가 어긋나므로
    # 실제로 남은 장수로 다시 계산해 기록한다 — `[9]`는 이 `seconds`로 trim한다.
    if len(photos) != len(chosen):
        warnings.append(
            f"엔딩 컷이 {len(chosen)}장에서 {len(photos)}장으로 줄었다 — "
            "마지막 컷의 꼬리가 남지만 [9]의 trim이 잘라 쓴다"
        )

    document = ending_schema.build_document(
        run_id,
        photos,
        topic=topic,
        source_refs=REFS_FILE,
        rejected=rejected,
        warnings=warnings,
    )
    errors = ending_schema.validate_ending(document)
    write_text(record_path, dump_json(document))
    write_text(ending_dir / CREDITS_FILE, credits_document(photos))

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result.record_path = record_path
    result.photos = photos
    result.rejected = rejected
    result.errors = errors

    info = {
        "output": record_path.relative_to(paths.root).as_posix(),
        "photos": len(photos),
        "burned": result.burned,
        "rejected": len(rejected),
        "rejected_by_machine": result.rejected_by(ending_schema.BY_MACHINE),
        "rejected_by_review": result.rejected_by(ending_schema.BY_REVIEW),
        "warnings": warnings,
        "validation_errors": errors,
        **meta,
    }
    if errors:
        # 계약을 어긴 파일을 남기면 `[9]`가 그것을 읽는다. 지우고 실패로 남긴다.
        record_path.unlink(missing_ok=True)
        result.record_path = None
        log.warning("[%s] 계약 위반 %d건 — %s를 쓰지 않는다", STAGE, len(errors), RECORD_FILE)
        state.mark_failed(STAGE, f"계약 위반 {len(errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)
    return result
