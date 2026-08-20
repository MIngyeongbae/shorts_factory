"""[6r. imagereview] — 만든 이미지를 판정해 사분면을 고르고 실패 씬만 다시 산다. ADR-0031.

specs/05-pipeline.md:
    [6r. imagereview] → image_review.json  (씬별 판정: pass / 사분면 교체 / redo)

## 왜 이 단계가 있는가

`[6]`이 검사하는 것은 셋뿐이다 — 프로바이더가 살아 있는가, 방언이 맞는가, 바이트가
왔는가. **바이트가 오면 성공이다.** 후버댐 편은 그 검사를 전부 통과하고 86초짜리
`timeline.mp4`까지 나온 뒤, **사람이 재생해 보고서야** 댐이 한 장도 없다는 것이
드러났다. 파이프라인에 이미지가 제 일을 했는지 묻는 자리가 없었다.

## 사분면 선택이 재생성보다 먼저다 (ADR-0031 §2)

MJ는 잡 하나에 4장을 내고 `[6]`이 넷을 전부 `images/_cand/{scene_id}/`에 남긴다.
**이미 산 것이라 고르는 데 추가 과금이 0이다.** 실측에서 지금까지 고정으로 쓰던 `q0`이
넷 중 제일 약했다. 재생성은 넷이 전부 기준 1·2에 걸릴 때만 한다.

## 읽는 파일 — 시각을 쓰지 않는다

| 파일 | 무엇 |
|---|---|
| `runs/{run_id}/images.json` | 판정 대상과 후보 목록 (`[6]`의 실행 기록) |
| `topics/{slug}/06-script.json` | `visual_goal`·`subject`·`subject_anchor` |
| `runs/{run_id}/prompts.json` | `redo` 씬을 다시 살 때만 |

`scenes.timed.json`을 읽지 않는다. 그 파일은 **실측 시각**의 유일한 출처이고(ADR-0020),
이 단계는 시각을 쓰지 않는다. 판정 기준이 대조하는 값은 전부 씬 계약에 있고,
그중 `visual_goal`은 `scenes.timed.json`에 애초에 실리지 않는다 — 그 하나를 위해
파일을 하나 더 열면 값 하나의 출처가 둘이 된다. `[5] prompt`가 같은 이유로
`06-script.json` 하나만 읽는다.

## 자막도 대본 문장도 보지 않는다 (ADR-0038)

ADR-0031은 "네 장이 기준을 다 통과할 때 갈라 줄 근거"로 `text`를 줬는데, **그것은 대본
문장에 대한 의존이고 기각 사유로는 쓰지 못하게 이미 막아 둔 값이었다.** 판정에 필요한
것은 `visual_goal`이 이미 말하고 있다. 네 장이 갈리지 않으면 손댈 곳은 `text`를 다시
끌어오는 것이 아니라 `visual_goal`을 더 구체적으로 쓰게 하는 쪽이다 (`[1s]`).

자막 자리(옛 판정 기준 ③)도 사라졌다 — 실측에서 두 번 다 안 비워졌고, 비워진 만큼은
그림이 아니라 종이였다. **자막 가독성은 `[9]`가 글자 쪽에서 푼다** (specs/05 D-6).

## 스타일은 판정하지 않는다

`BASE_STYLE`은 의도대로 작동하고 있고, 판정 세션에 룩을 맡기면 어휘 밖에서 연출이
결정된다 (ADR-0033 §3).

## redo는 상한 1회이고 다시 판정하지 않는다

판정 → 재생성 → 판정 → 재생성이 되면 끝나는 조건이 사람 말고는 없다. 2회차 결과는
그대로 쓰고 기록에 남긴다. `[6]`의 인접 씬 폴백과는 축이 다르다 — 그건 **호출이
실패했을 때**이고 이건 **결과가 틀렸을 때**다.

재생성할 프로바이더가 없으면 그 씬은 지금 이미지를 그대로 두고 경고만 남긴다.
**실패는 단계 안에서 끝난다** (specs/05 D-5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from ..config import Paths, write_text
from ..imagegen.base import (
    ImageClient,
    ImageGenError,
    ImageRequest,
    ProviderNotConfigured,
)
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas.image_review import PASS, PICK, REDO, ImageReviewError, parse_reviews
from ..schemas.image_source import build_document as build_image_source
from ..schemas.image_source import validate_image_source

log = logging.getLogger(__name__)

STAGE = "6r-imagereview"

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT = "11-imagereview.md"

#: 입력 — `[6]`의 실행 기록. 무엇을 판정할지와 후보가 어디 있는지가 여기 있다.
IMAGES_RECORD = "images.json"

#: 입력 — 1부↔2부 경계 계약 (ADR-0017). 판정 기준의 원본 값이 전부 여기 있다.
SCRIPT_FILE = "06-script.json"

#: 입력 — `redo` 씬을 다시 살 때만 연다 (ADR-0020).
PROMPTS_FILE = "prompts.json"

#: 산출물 — 이 단계의 **실행 기록**. 계약이 아니다 (specs/05 2부 계약 표).
RECORD_FILE = "image_review.json"

#: 입출력 — `[6]`이 쓴 `[7]`의 영상 입력 **계약** (ADR-0041). 이 단계가 사분면을 바꿔
#: 끼우거나 씬을 다시 사면 그 사실이 여기에도 반영돼야 한다. 반영하지 않으면 `[7]`이
#: **화면에 없는 그림으로** 영상을 만든다 — 실패가 아니라 다른 그림이라 조용하다.
SOURCE_FILE = "image_source.json"

IMAGES_DIR = "images"

#: 판정 세션이 쓸 수 있는 도구. **이미지를 직접 열어야 하므로 `Read`가 필요하다**
#: (ADR-0031 G1). 웹도 쓰기도 주지 않는다 — 판정은 화면에 있는 것만 보고 한다.
TOOLS: tuple[str, ...] = ("Read",)

#: `redo` 씬을 다시 사는 횟수. ADR-0031 §3 "상한 1회".
REDO_ATTEMPTS = 1

#: 씬 하나에 일어난 일.
PASSED = "passed"  # 그대로 간다
PICKED = "picked"  # 사분면을 바꿔 끼웠다 (과금 0)
REGENERATED = "regenerated"  # 다시 샀다
REDO_FAILED = "redo_failed"  # 다시 사려다 실패 — 지금 이미지를 그대로 둔다
REDO_SKIPPED = "redo_skipped"  # 다시 살 프로바이더가 없다


class ImagereviewStageError(Exception):
    pass


@dataclass
class ImagereviewResult:
    run_id: str
    topic: str
    run_dir: Path
    record_path: Path | None = None
    scenes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    def count(self, outcome: str) -> int:
        return sum(1 for s in self.scenes if s["outcome"] == outcome)

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        parts = [
            f"통과 {self.count(PASSED)}",
            f"사분면 교체 {self.count(PICKED)}",
            f"재생성 {self.count(REGENERATED)}",
        ]
        stuck = self.count(REDO_FAILED) + self.count(REDO_SKIPPED)
        if stuck:
            parts.append(f"재생성 못함 {stuck}")
        return (
            f"[6r] {self.topic} — {self.scene_count}씬 / "
            + " / ".join(parts)
            + f" → {RECORD_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise ImagereviewStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImagereviewStageError(f"{what}을(를) 읽을 수 없다: {exc}") from exc


def resolve_run_id(paths: Paths, slug: str) -> str:
    """대본에 적힌 `run_id`. 2부 산출물은 대본과 같은 run 디렉터리에 있다 (ADR-0011)."""
    script = _load_json(paths.topic_dir(slug) / SCRIPT_FILE, f"씬 계약({SCRIPT_FILE})")
    run_id = script.get("run_id")
    if not run_id:
        raise ImagereviewStageError(f"{SCRIPT_FILE}에 run_id가 없다 (slug={slug})")
    return str(run_id)


def _load_prompt() -> Template:
    path = PROMPTS_DIR / PROMPT
    if not path.exists():
        raise ImagereviewStageError(f"프롬프트 파일이 없다: {path}")
    return Template(path.read_text(encoding="utf-8"))


def _script_for_run(paths: Paths, run_id: str, slug: str | None) -> dict[str, Any]:
    """씬 계약을 찾는다. slug를 안 주면 run_id로 토픽 폴더를 훑는다."""
    if slug:
        return _load_json(paths.topic_dir(slug) / SCRIPT_FILE, f"씬 계약({SCRIPT_FILE})")

    for candidate in sorted(paths.topics.glob(f"*/{SCRIPT_FILE}")):
        try:
            script = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if script.get("run_id") == run_id:
            return script

    raise ImagereviewStageError(
        f"run_id={run_id}의 씬 계약을 찾지 못했다. --slug로 지정하라"
    )


def reviewable_scenes(record: dict[str, Any]) -> list[dict[str, Any]]:
    """판정할 수 있는 씬 = 쓸 이미지가 실제로 있는 씬.

    `failed`는 파일이 없어 볼 것이 없고, `fallback`은 **인접 씬 이미지를 복사한 자리**라
    이 단계가 판정할 대상이 아니다 — 그 씬의 문제는 그림 내용이 아니라 `[6]`의 호출
    실패이고, 축이 다르다 (ADR-0031 §3).
    """
    return [
        scene
        for scene in record.get("scenes", [])
        if scene.get("status") in ("generated", "cached") and scene.get("file")
    ]


def render_scenes(
    scenes: list[dict[str, Any]],
    contracts: dict[int, dict[str, Any]],
    candidates: dict[int, list[str]],
) -> str:
    """세션에 보여줄 판정 대상. 씬마다 그림 필드 + 열어 볼 파일 경로.

    **대본 문장(`text`)은 주지 않는다** (ADR-0038). 위 독스트링 참고.
    """
    blocks: list[str] = []
    for scene in scenes:
        sid = scene["scene_id"]
        contract = contracts.get(sid, {})
        anchors = contract.get("subject_anchor") or []
        lines = [
            f"## 씬 {sid}",
            f"- 피사체: {contract.get('subject', '(없음)')}",
            f"- 고정 명사: {', '.join(anchors) if anchors else '(없음)'}",
            f"- 그림 목표: {contract.get('visual_goal', '(없음)')}",
            f"- 지금 쓰는 그림: {scene['file']}",
        ]
        if candidates[sid]:
            listed = "\n".join(
                f"  - q{i}: {path}" for i, path in enumerate(candidates[sid])
            )
            lines.append("- 후보:")
            lines.append(listed)
        else:
            lines.append("- 후보: 없음 (사분면 교체 불가)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _apply_pick(
    run_dir: Path, scene: dict[str, Any], candidates: list[str], pick: str
) -> str:
    """고른 후보를 `images/{scene_id}`로 덮어쓴다. 하류 계약은 그대로다 (ADR-0020)."""
    source = run_dir / candidates[int(pick[1:])]
    if not source.exists():
        raise ImagereviewStageError(f"고른 후보 파일이 없다: {source}")
    target = run_dir / scene["file"]
    target.write_bytes(source.read_bytes())
    return candidates[int(pick[1:])]


def _regenerate(
    run_dir: Path,
    scene: dict[str, Any],
    prompt_scene: dict[str, Any],
    style: dict[str, Any],
    *,
    images: ImageClient,
    timeout: int | None,
) -> dict[str, Any]:
    """`redo` 씬 하나를 다시 산다. 상한 1회, 결과는 다시 판정하지 않는다."""
    request = ImageRequest.from_prompt_scene(
        prompt_scene, style, label=f"{STAGE} 씬 {scene['scene_id']} redo"
    )
    image = images.generate(request, timeout=timeout)

    target = run_dir / scene["file"]
    target.write_bytes(image.data)

    saved: list[str] = []
    if image.variants:
        scene_dir = target.parent / "_cand" / target.stem
        scene_dir.mkdir(parents=True, exist_ok=True)
        for index, data in enumerate(image.variants):
            path = scene_dir / f"q{index}{target.suffix}"
            path.write_bytes(data)
            saved.append(f"{IMAGES_DIR}/_cand/{target.stem}/{path.name}")

    return {"engine": image.meta, "candidates": saved}


def _update_image_source(
    run_dir: Path, run_id: str, scenes_out: list[dict[str, Any]], warnings: list[str]
) -> Path | None:
    """`image_source.json`에 이 단계의 결과를 반영한다 (ADR-0041).

    - `picked` — 그 씬의 `quadrant`를 고른 사분면으로 바꾼다. `task_id`는 그대로다
      (같은 잡의 다른 장이다)
    - `regenerated` — `task_id`를 새 잡 것으로 바꾸고 `quadrant`를 0으로 되돌린다.
      새 잡이 id를 안 주면 그 씬은 **항목을 지운다** — 영상 입력이 없어졌다
    - 나머지(`passed`·`redo_failed`·`redo_skipped`) — 화면의 그림이 안 바뀌었으므로
      항목도 그대로다

    `[6]`이 이 파일을 안 남긴 run은 갱신하지 않고 경고만 남긴다. 여기서 새로 만들면
    `[6]`의 판단(어느 씬에 영상 입력이 있는가)을 이 단계가 흉내 내게 된다.

    **읽을 때도 쓸 때도 계약을 검사한다.** 계약을 어긴 문서를 쓰면 `[7]`이 그것을
    읽고 **호출 없이 전 씬을 강등**하므로(`motion.py`), 실패가 이 단계 밖으로 새어
    나간다 (specs/05 D-5). 어느 쪽이든 경고만 남기고 이전 파일을 그대로 둔다.
    """
    path = run_dir / SOURCE_FILE
    if not path.exists():
        warnings.append(
            f"{SOURCE_FILE}이 없어 [7]의 영상 입력을 갱신하지 못했다. "
            "[6]을 --force로 다시 돌리면 생긴다 (ADR-0036: 마이그레이션하지 않는다)"
        )
        return None

    document = _load_json(path, f"[7]의 영상 입력 계약({SOURCE_FILE})")
    broken = validate_image_source(document)
    if broken:
        warnings.append(
            f"{SOURCE_FILE}이 계약을 어겨 갱신하지 않았다 ({'; '.join(broken)}). "
            "[6]을 --force로 다시 돌리면 다시 생긴다 (ADR-0036: 마이그레이션하지 않는다)"
        )
        return None

    entries = {int(e["scene_id"]): dict(e) for e in document.get("scenes", [])}

    for scene in scenes_out:
        sid = scene["scene_id"]
        entry = entries.get(sid)
        if entry is None:
            continue
        if scene["outcome"] == PICKED:
            entry["quadrant"] = int(str(scene["pick"])[1:])
            entry["set_by"] = STAGE
        elif scene["outcome"] == REGENERATED:
            task_id = (scene.get("engine") or {}).get("request_id")
            if not task_id:
                entries.pop(sid)
                continue
            entry["task_id"] = str(task_id)
            entry["quadrant"] = 0
            entry["set_by"] = STAGE

    updated = build_image_source(run_id, document["provider"], list(entries.values()))
    invalid = validate_image_source(updated)
    if invalid:
        warnings.append(
            f"{SOURCE_FILE}을 계약대로 쓸 수 없어 이전 파일을 그대로 뒀다 "
            f"({'; '.join(invalid)}). [7]은 갱신 전 사분면을 쓴다"
        )
        return None

    write_text(path, dump_json(updated))
    return path


def run_imagereview_stage(
    *,
    llm: LLMClient,
    images: ImageClient | None = None,
    run_id: str | None = None,
    slug: str | None = None,
    paths: Paths | None = None,
    force: bool = False,
    timeout: int | None = None,
) -> ImagereviewResult:
    paths = paths or Paths.from_env()

    if not run_id:
        if not slug:
            raise ImagereviewStageError("run_id나 slug 중 하나는 있어야 한다")
        run_id = resolve_run_id(paths, slug)

    run_dir = paths.run_dir(run_id)
    record = _load_json(run_dir / IMAGES_RECORD, f"[6]의 기록({IMAGES_RECORD})")
    script = _script_for_run(paths, run_id, slug)

    topic = record.get("topic") or script.get("topic") or run_id
    record_path = run_dir / RECORD_FILE
    result = ImagereviewResult(run_id=run_id, topic=topic, run_dir=run_dir)

    seed: dict[str, Any] = {"topic": topic}
    if slug:
        seed["slug"] = slug
    state = RunState.load_or_create(run_dir, run_id, **seed)

    if state.is_done(STAGE) and not force and record_path.exists():
        previous = _load_json(record_path, RECORD_FILE)
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        result.skipped = True
        result.record_path = record_path
        result.scenes = previous.get("scenes", [])
        return result

    targets = reviewable_scenes(record)
    if not targets:
        raise ImagereviewStageError(
            f"판정할 이미지가 없다 ({IMAGES_RECORD}). [6. imagegen]을 먼저 실행하라"
        )

    contracts = {scene["scene_id"]: scene for scene in script.get("scenes", [])}
    candidates = {
        scene["scene_id"]: list(scene.get("candidates") or []) for scene in targets
    }

    bare = sorted(sid for sid, files in candidates.items() if not files)
    if bare:
        # 경고가 아니라 사실이다. 후보 없는 씬은 pass/redo만 받는다 (specs/05 D-3).
        log.info(
            "[%s] 후보가 없는 씬 %d개 — 사분면 교체 없이 판정한다", STAGE, len(bare)
        )

    state.mark_running(STAGE)
    prompt = _load_prompt().safe_substitute(
        topic=topic, scenes=render_scenes(targets, contracts, candidates)
    )
    log.info(
        "[%s] 판정 세션 시작 — %d씬 (Read 도구, %s)",
        STAGE, len(targets), run_dir / IMAGES_DIR,
    )
    session = llm.run(
        prompt,
        allowed_tools=TOOLS,
        timeout=timeout,
        label=STAGE,
        add_dirs=(run_dir / IMAGES_DIR,),
    )

    try:
        payload = extract_json_object(session.text)
    except JSONExtractionError as exc:
        state.mark_failed(STAGE, str(exc))
        raise ImagereviewStageError(f"세션 출력이 JSON 객체가 아니다: {exc}") from exc

    try:
        reviews = parse_reviews(payload, candidates)
    except ImageReviewError as exc:
        state.mark_failed(STAGE, str(exc))
        raise ImagereviewStageError(str(exc)) from exc

    redo_ids = [sid for sid, r in reviews.items() if r["verdict"] == REDO]
    prompt_scenes: dict[int, dict[str, Any]] = {}
    style: dict[str, Any] = {}
    if redo_ids and images is not None:
        prompts_doc = _load_json(run_dir / PROMPTS_FILE, f"[5]의 산출물({PROMPTS_FILE})")
        style = prompts_doc.get("style", {})
        prompt_scenes = {s["scene_id"]: s for s in prompts_doc.get("scenes", [])}

    scenes_out: list[dict[str, Any]] = []
    for scene in targets:
        sid = scene["scene_id"]
        review = reviews[sid]
        entry: dict[str, Any] = {
            "scene_id": sid,
            "verdict": review["verdict"],
            "pick": review["pick"],
            "reason": review["reason"],
            "file": scene["file"],
            "outcome": PASSED,
        }

        if review["verdict"] == PICK:
            entry["source"] = _apply_pick(
                run_dir, scene, candidates[sid], review["pick"]
            )
            entry["outcome"] = PICKED

        elif review["verdict"] == REDO:
            if images is None:
                entry["outcome"] = REDO_SKIPPED
                result.warnings.append(
                    f"씬 {sid}: redo 판정인데 프로바이더가 없다 → 지금 이미지를 그대로 둔다"
                )
            elif sid not in prompt_scenes:
                entry["outcome"] = REDO_FAILED
                entry["error"] = f"{PROMPTS_FILE}에 씬 {sid}가 없다"
                result.warnings.append(
                    f"씬 {sid}: redo 판정인데 {PROMPTS_FILE}에 그 씬이 없다"
                )
            else:
                try:
                    outcome = _regenerate(
                        run_dir, scene, prompt_scenes[sid], style,
                        images=images, timeout=timeout,
                    )
                except ProviderNotConfigured:
                    # 씬의 문제가 아니다. 남은 redo도 전부 같은 이유로 실패한다.
                    state.mark_failed(STAGE, "프로바이더를 쓸 수 없다")
                    raise
                except ImageGenError as exc:
                    entry["outcome"] = REDO_FAILED
                    entry["error"] = str(exc)
                    result.warnings.append(
                        f"씬 {sid}: 재생성 {REDO_ATTEMPTS}회 실패 → 지금 이미지를 그대로 둔다 ({exc})"
                    )
                else:
                    entry.update(outcome)
                    entry["outcome"] = REGENERATED

        scenes_out.append(entry)

    _update_image_source(run_dir, run_id, scenes_out, result.warnings)

    document = {
        "run_id": run_id,
        "topic": topic,
        "stage": STAGE,
        "source_record": IMAGES_RECORD,
        "session": session.meta,
        "scenes": scenes_out,
        "warnings": result.warnings,
    }
    write_text(record_path, dump_json(document))

    result.record_path = record_path
    result.scenes = scenes_out
    state.mark_done(
        STAGE,
        scenes=len(scenes_out),
        picked=sum(1 for s in scenes_out if s["outcome"] == PICKED),
        regenerated=sum(1 for s in scenes_out if s["outcome"] == REGENERATED),
    )
    return result
