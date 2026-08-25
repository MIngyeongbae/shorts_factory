"""[6. frames] — `[7]`에 줄 정지 이미지 두 장을 만든다. ADR-0071.

specs/05-pipeline.md:
    [6. frames] → frames/{scene_id}-clean.png (+ -info.jpg) + frames.json
                  (프레임을 입력으로 받는 라인에서만. CLEAN은 MJ, INFO는 그 위의 NB2 편집)

## 왜 있는가 — 코드가 못 하는 일이 하나 있다

계측 표시가 **무엇을 가리키는지**는 그림 안의 위치인데, 계약은 그것을 말로 적는다
(`info.target: "the width of the river channel between its two banks"`). 말에서 좌표로
가려면 그림을 읽어야 하고 코드에 그 함수가 없다. 손으로 좌표를 박으면 지시선이 강물
한가운데 아무 곳을 가리키고 치수선이 강폭 아닌 띠를 잰다 (ADR-0071 실측).

그래서 이 단계는 **그림을 읽는 모델 둘**을 순서대로 부른다: MJ가 CLEAN을 그리고,
편집 모델이 그 위에 표시를 얹는다. 정확성은 여기서 끝나고 `[7]`의 영상 모델은 두 장을
잇기만 한다 (ADR-0070).

## 이 단계가 판단하지 않는 것

- **무엇을 그릴지** — `[5]`의 `mj_subject`가 이미 썼다 (씬 계약을 읽은 세션의 것)
- **라벨 문구·무엇을 잴지** — 씬 계약 `info`의 것이다 (ADR-0020)
- **표시의 배치·색·굵기** — 편집 모델 재량이다 (ADR-0043의 그 선). 검수는 배치를
  판정하지 않고 **가리키는 대상이 맞는가**를 본다

## 사분면은 q0로 시작하고 재시도에서만 바뀐다 (사람 결정 2026-08-25)

MJ imagine이 주는 것은 2×2 그리드다. 그리드를 보고 고르는 세션을 두지 않는다 — 평시
세션은 `info` 씬당 1회(검수)뿐이다. 검수에 걸리면 **다음 사분면의 CLEAN으로 갈아**
한 번 더 만든다. 그리드는 다시 사지 않는다: U 추출은 과금 0이라 재시도가 공짜다.

## 강등은 `info_url`의 부재로 전달된다

INFO가 두 번 걸리면 그 씬은 **CLEAN만 남기고** `demoted_from: info`를 적는다. `[7]`은
`info_url`이 없으면 `endImage` 없이 돌아 MJ가 알아서 움직인다 (옛 ADR-0039 동작).
틀린 표시보다 없는 표시가 낫고, 숫자는 내레이션·자막이 진다.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from ..config import Paths, write_text
from ..imagegen.base import (
    GeneratedImage,
    ImageGenError,
    ImageRequest,
    ProviderNotConfigured,
)
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient, LLMError
from ..runstate import RunState
from ..schemas import promptplan, vocab
from ..schemas.visual_rules import (
    ASPECT_RATIO,
    RESOLUTION,
    build_edit_instruction,
    build_mj_prompt,
    negative_items,
)
from .contract import SceneContractNotFound, find_contract_for_run, load_scene_contract
from .prompt import PROMPTS_FILE
from .session import load_prompt

log = logging.getLogger(__name__)

STAGE = "6-frames"
PROMPT = "14-inforeview.md"

#: 이 단계의 산출 — 계약 파일 하나와 이미지 디렉터리 하나 (specs/05 계약 표).
RECORD_FILE = "frames.json"
FRAMES_DIR = "frames"

#: 검수 세션의 도구 — 두 장을 직접 열어 본다 (`[7]`과 같은 메커니즘).
TOOLS: tuple[str, ...] = ("Read",)
SESSION_TIMEOUT = 300

#: 씬 하나에 쓰는 사분면 수 = 시도 수. q0 → (걸리면) q1, 거기서 끝이다.
#: 사다리가 길수록 NB2 호출과 세션이 늘고, 재생성 루프는 두지 않는다 (ADR-0044).
ATTEMPTS = 2

PASS, FAIL, ERROR = "pass", "fail", "error"
DONE, FAILED = "done", "failed"
DEMOTED_INFO = "info"

#: 파일 이름. 씬 하나에 두 장이고 확장자는 프로바이더가 정한다 (NB2는 JPEG만 낸다).
CLEAN_SUFFIX = "-clean"
INFO_SUFFIX = "-info"


class FramesStageError(Exception):
    """`[6]`이 결과를 낼 수 없는 경우. 씬 하나의 실패는 여기로 오지 않는다."""


class ProviderRefused(FramesStageError):
    """프로바이더 전체 거절 — 남은 씬을 시도하지 않고 멈췄다 (D-5)."""


class ImageEditor(Protocol):
    """`[6]`이 요구하는 편집 표면 하나. `NanoBananaClient.edit`가 구현한다 (ADR-0021)."""

    name: str

    def edit(
        self, instruction: str, image_path: Path, *, timeout: int | None = None
    ) -> GeneratedImage: ...


# --- 입력 ---------------------------------------------------------------------


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 씬 계약에서 읽는다 (`[7]`과 같은 규칙)."""
    if run_id:
        return run_id
    if not slug:
        raise FramesStageError("run_id 또는 slug 중 하나는 있어야 한다")
    try:
        contract, _path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise FramesStageError(str(exc)) from exc
    resolved = contract.get("run_id")
    if not resolved:
        raise FramesStageError(f"씬 계약에 run_id가 없다 (slug={slug})")
    return str(resolved)


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise FramesStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FramesStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


@dataclass
class FrameJob:
    """씬 하나가 이 단계에 요구하는 것 전부."""

    scene_id: int
    mj_prompt: str
    #: `info`가 없으면 None — 그 씬은 CLEAN 한 장으로 끝난다.
    edit_instruction: str | None
    info: dict[str, Any] | None


def build_jobs(
    contract: dict[str, Any], prompts: dict[str, Any], *, line: str
) -> tuple[list[FrameJob], list[str]]:
    """씬 계약 + `[5]` 산출 → 씬별 작업. **호출 전에 전부 조립하고 검사한다.**

    MJ 한 줄의 예산 위반은 `build_mj_prompt`가 여기서 올린다 (ADR-0069) — 제출 뒤에
    걸리면 사유가 "타임아웃"으로 오고 원인이 안 보인다. 한 씬이라도 못 만들면 이 단계는
    시작하지 않는다: 절반만 산 편이 제일 비싸다.
    """
    warnings: list[str] = []
    base_style = vocab.line_style(line)
    #: CLEAN에는 **어느 씬에서도** 글자가 없어야 한다 — 표시는 다음 칸에서 얹는다.
    negatives = negative_items(has_info=False)

    by_id = {
        int(scene["scene_id"]): scene for scene in prompts.get("scenes", [])
    }
    jobs: list[FrameJob] = []
    for scene in contract.get("scenes", []):
        scene_id = int(scene["scene_id"])
        planned = by_id.get(scene_id)
        if planned is None:
            raise FramesStageError(
                f"{PROMPTS_FILE}에 씬 {scene_id}이 없다 — [5]를 다시 돌려야 한다"
            )
        subject = str(planned.get(promptplan.MJ_SUBJECT_FIELD) or "").strip()
        if not subject:
            raise FramesStageError(
                f"씬 {scene_id}에 `{promptplan.MJ_SUBJECT_FIELD}`가 없다. 라인 '{line}'은 "
                f"CLEAN 이미지를 사므로 [5]가 그 단락을 써야 한다 (ADR-0071) — "
                "라인을 고른 뒤 [5]를 다시 돌려라"
            )
        mj_prompt = build_mj_prompt(
            subject=subject, base_style=base_style, negatives=negatives,
            aspect_ratio=ASPECT_RATIO,
        )
        info = scene.get("info") or None
        instruction = (
            build_edit_instruction(
                annotation=str(info["annotation"]),
                target=str(info["target"]),
                labels=list(info["labels"]),
            )
            if info
            else None
        )
        jobs.append(
            FrameJob(
                scene_id=scene_id, mj_prompt=mj_prompt,
                edit_instruction=instruction, info=info,
            )
        )
    if not jobs:
        raise FramesStageError("씬 계약에 씬이 없다")
    if not any(job.info for job in jobs):
        warnings.append(
            "info 씬이 없다 — CLEAN만 만들고 편집 호출도 검수 세션도 돌지 않는다"
        )
    return jobs, warnings


# --- 검수 세션 -----------------------------------------------------------------


def render_review_prompt(
    *, topic: str, job: FrameJob, clean: Path, info: Path
) -> str:
    """INFO 검수 세션 프롬프트. 나레이션도 대본도 넣지 않는다 — 계약과 두 장뿐이다."""
    fields = job.info or {}
    labels = ", ".join(f'"{label}"' for label in fields.get("labels", []))
    return load_prompt(PROMPT).safe_substitute(
        topic=topic, scene_id=job.scene_id,
        clean=clean, info=info,
        annotation=fields.get("annotation", ""),
        target=fields.get("target", ""),
        labels=labels,
    )


def parse_review(payload: dict[str, Any]) -> dict[str, Any]:
    """세션 출력 → `{verdict, reasons, target_pointed}`. 모르는 판정은 `fail`로 본다."""
    verdict = str(payload.get("verdict") or "").strip().lower()
    reasons = payload.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    pointed = payload.get("target_pointed")
    return {
        "verdict": verdict if verdict in (PASS, FAIL) else FAIL,
        "reasons": [str(r).strip() for r in reasons if str(r).strip()],
        "target_pointed": bool(pointed) if isinstance(pointed, bool) else None,
    }


# --- 씬 하나 -------------------------------------------------------------------


@dataclass
class SceneOutcome:
    scene_id: int
    status: str = FAILED
    quadrant: int = 0
    grid_task_id: str | None = None
    clean_file: str | None = None
    clean_url: str | None = None
    info_file: str | None = None
    info_url: str | None = None
    labels: list[str] = field(default_factory=list)
    demoted_from: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "status": self.status,
            "quadrant": self.quadrant,
            "grid_task_id": self.grid_task_id,
            "clean_file": self.clean_file,
            "clean_url": self.clean_url,
            "info_file": self.info_file,
            "info_url": self.info_url,
            "labels": list(self.labels),
            "demoted_from": self.demoted_from,
            "attempts": list(self.attempts),
            "warnings": list(self.warnings),
        }


class _Runner:
    """씬 하나를 끝까지 끌고 가는 부분. 상태는 `SceneOutcome`에만 쌓인다."""

    def __init__(
        self,
        *,
        client: Any,
        editor: ImageEditor | None,
        llm: LLMClient | None,
        run_dir: Path,
        topic: str,
        review: bool,
        image_timeout: int | None,
        session_timeout: int,
        on_scene_done: Any,
    ) -> None:
        self.client = client
        self.editor = editor
        self.llm = llm
        self.run_dir = run_dir
        self.frames_dir = run_dir / FRAMES_DIR
        self.topic = topic
        self.review = review
        self.image_timeout = image_timeout
        self.session_timeout = session_timeout
        self.on_scene_done = on_scene_done
        self.refusal: str | None = None
        self.stop = threading.Event()

    # --- MJ CLEAN ---------------------------------------------------------

    def _grid(self, job: FrameJob) -> str:
        """imagine 잡 하나 → 그리드 태스크 id. **씬당 1회만 산다.**"""
        request = ImageRequest(
            scene_id=job.scene_id, prompt=job.mj_prompt, negative_prompt="",
            aspect_ratio=ASPECT_RATIO, resolution=RESOLUTION,
            label=f"{STAGE}:{job.scene_id}",
        )
        image = self.client.generate(request, timeout=self.image_timeout)
        task_id = image.request_id
        if not task_id:
            raise ImageGenError("그리드 잡 id가 없다 — U 추출을 할 수 없다")
        return str(task_id)

    def _clean(self, job: FrameJob, task_id: str, quadrant: int) -> tuple[Path, str]:
        """U{q} 추출 → `(저장한 파일, 공개 주소)`. 과금 0이라 재시도가 공짜다."""
        reference = self.client.upscale(
            task_id, quadrant=quadrant, timeout=self.image_timeout
        )
        url = str(reference.url)
        if not url.startswith("https://"):
            raise ProviderNotConfigured(
                f"CLEAN 주소가 https가 아니다: {url!r}. MJ 영상은 자기가 닿는 주소만 받는다 "
                "— 프록시 imageStorageType을 R2/S3로 두어야 한다 (ADR-0070)"
            )
        data = self.client.download(url, timeout=self.image_timeout or 180)
        path = self.frames_dir / f"{job.scene_id}{CLEAN_SUFFIX}.png"
        path.write_bytes(data)
        return path, url

    # --- NB2 INFO ---------------------------------------------------------

    def _info(self, job: FrameJob, clean_path: Path) -> tuple[Path, GeneratedImage]:
        if self.editor is None:
            raise ProviderNotConfigured(
                "info 씬이 있는데 편집 어댑터가 없다 — INFO를 만들 수 없다"
            )
        edited = self.editor.edit(
            str(job.edit_instruction), clean_path, timeout=self.image_timeout
        )
        suffix = ".jpg" if "jpeg" in edited.mime_type else ".png"
        path = self.frames_dir / f"{job.scene_id}{INFO_SUFFIX}{suffix}"
        path.write_bytes(edited.data)
        return path, edited

    def _vision(self, job: FrameJob, clean: Path, info: Path) -> dict[str, Any]:
        """검수 세션 1회. **세션이 두 번 죽으면 통과시킨다** — 검수기 고장으로 돈을
        더 쓰지 않는다 (`[7]`의 그 규칙과 같다)."""
        if not self.review or self.llm is None:
            return {"verdict": PASS, "reasons": ["검수 없이 채택 (--no-review)"], "target_pointed": None}
        prompt = render_review_prompt(topic=self.topic, job=job, clean=clean, info=info)
        last_error = ""
        for _try in range(2):
            try:
                result = self.llm.run(
                    prompt, allowed_tools=TOOLS, timeout=self.session_timeout,
                    label=f"{STAGE}:{job.scene_id}", add_dirs=(self.frames_dir,),
                )
                payload = extract_json_object(result.text)
            except (JSONExtractionError, LLMError) as exc:
                last_error = str(exc)
                continue
            verdict = parse_review(payload)
            verdict["session"] = result.meta
            return verdict
        return {
            "verdict": ERROR,
            "reasons": [f"검수 세션 실패 2회 → 검수 없이 통과: {last_error}"],
            "target_pointed": None,
        }

    # --- 씬 하나 ----------------------------------------------------------

    def run_scene(self, job: FrameJob) -> SceneOutcome:
        outcome = SceneOutcome(
            scene_id=job.scene_id, labels=list((job.info or {}).get("labels") or [])
        )
        if self.stop.is_set():
            outcome.warnings.append("프로바이더 거절로 시도하지 않았다")
            self.on_scene_done(outcome)
            return outcome
        try:
            outcome.grid_task_id = self._grid(job)
        except ProviderNotConfigured as exc:
            self._refuse(str(exc))
            outcome.warnings.append(f"프로바이더 거절: {exc}")
            self.on_scene_done(outcome)
            return outcome
        except ImageGenError as exc:
            outcome.warnings.append(f"CLEAN 생성 실패: {exc}")
            self.on_scene_done(outcome)
            return outcome

        for quadrant in range(ATTEMPTS):
            attempt: dict[str, Any] = {"quadrant": quadrant}
            outcome.attempts.append(attempt)
            try:
                clean_path, clean_url = self._clean(job, outcome.grid_task_id, quadrant)
            except ProviderNotConfigured as exc:
                self._refuse(str(exc))
                attempt["error"] = str(exc)
                break
            except ImageGenError as exc:
                attempt["error"] = f"U{quadrant + 1} 추출 실패: {exc}"
                continue
            outcome.quadrant = quadrant
            outcome.clean_file = _relative(clean_path, self.run_dir)
            outcome.clean_url = clean_url
            attempt["clean_file"] = outcome.clean_file

            if job.info is None:
                outcome.status = DONE
                self.on_scene_done(outcome)
                return outcome

            try:
                info_path, edited = self._info(job, clean_path)
            except ProviderNotConfigured as exc:
                self._refuse(str(exc))
                attempt["error"] = str(exc)
                break
            except ImageGenError as exc:
                attempt["error"] = f"INFO 편집 실패: {exc}"
                continue
            attempt["info_file"] = _relative(info_path, self.run_dir)
            attempt["editor"] = {"model": edited.model_id, "request_id": edited.request_id}

            verdict = self._vision(job, clean_path, info_path)
            attempt["review"] = verdict
            if verdict["verdict"] == FAIL:
                continue
            if verdict["verdict"] == ERROR:
                outcome.warnings.extend(verdict["reasons"])

            try:
                outcome.info_url = self.client.upload(
                    info_path.read_bytes(),
                    mime_type=edited.mime_type,
                    timeout=self.image_timeout,
                )
            except ProviderNotConfigured as exc:
                self._refuse(str(exc))
                attempt["error"] = str(exc)
                break
            except ImageGenError as exc:
                attempt["error"] = f"INFO 업로드 실패: {exc}"
                continue
            outcome.info_file = attempt["info_file"]
            outcome.status = DONE
            self.on_scene_done(outcome)
            return outcome

        # 사다리 끝 — CLEAN이 있으면 INFO 없이 산다 (D-5).
        if outcome.clean_url:
            outcome.status = DONE
            outcome.demoted_from = DEMOTED_INFO
            outcome.info_file = None
            outcome.info_url = None
            outcome.warnings.append(
                f"INFO가 {ATTEMPTS}회 다 걸려 계측 표시 없이 간다 (demoted_from: info) — "
                "숫자는 내레이션·자막이 진다"
            )
        self.on_scene_done(outcome)
        return outcome

    def _refuse(self, reason: str) -> None:
        """프로바이더 전체 거절 — 남은 씬을 시도하지 않는다 (D-5, 돈이 나가는 경우)."""
        if self.refusal is None:
            self.refusal = reason
        self.stop.set()


def _relative(path: Path, run_dir: Path) -> str:
    try:
        return path.relative_to(run_dir).as_posix()
    except ValueError:
        return path.as_posix()


# --- 결과 ---------------------------------------------------------------------


@dataclass
class FramesResult:
    run_id: str
    run_dir: Path
    topic: str
    line: str
    provider: str
    outcomes: list[SceneOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    record_path: Path | None = None
    skipped: bool = False

    @property
    def scene_count(self) -> int:
        return len(self.outcomes)

    @property
    def info_scenes(self) -> int:
        return sum(1 for o in self.outcomes if o.info_url)

    @property
    def demoted(self) -> int:
        return sum(1 for o in self.outcomes if o.demoted_from == DEMOTED_INFO)

    @property
    def quadrant_swaps(self) -> int:
        """q0이 아닌 칸으로 간 씬 수 — q0 고정이 값을 하는지 보는 지표 (ADR-0071)."""
        return sum(1 for o in self.outcomes if o.quadrant > 0)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[6] {self.topic} — {self.scene_count}씬 CLEAN / INFO {self.info_scenes} "
            f"(강등 {self.demoted} · 사분면 교체 {self.quadrant_swaps}) → {RECORD_FILE}{tail}"
        )


def _existing(run_dir: Path, force: bool) -> dict[int, dict[str, Any]]:
    """지난 실행에서 `done`인 씬. 다시 사지 않는다 (ADR-0020의 목적)."""
    if force:
        return {}
    path = run_dir / RECORD_FILE
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        int(entry["scene_id"]): entry
        for entry in previous.get("scenes", [])
        if entry.get("status") == DONE and "scene_id" in entry
    }


def _outcome_from(entry: dict[str, Any]) -> SceneOutcome:
    return SceneOutcome(
        scene_id=int(entry["scene_id"]), status=str(entry.get("status") or DONE),
        quadrant=int(entry.get("quadrant") or 0),
        grid_task_id=entry.get("grid_task_id"),
        clean_file=entry.get("clean_file"), clean_url=entry.get("clean_url"),
        info_file=entry.get("info_file"), info_url=entry.get("info_url"),
        labels=list(entry.get("labels") or []),
        demoted_from=entry.get("demoted_from"),
        attempts=list(entry.get("attempts") or []),
        warnings=list(entry.get("warnings") or []),
    )


def resolve_line(paths: Paths, run_id: str, *, slug: str | None, line: str | None) -> str:
    """사람이 고른 영상 라인 (`[7]`·`[3s]`와 같은 자리). `--line`이 이긴다 (디버깅)."""
    from ..judgment import JudgmentError, read_video_line, slug_from_run_id

    if line:
        vocab.require("video_line", line)
        return line
    try:
        return read_video_line(paths, slug or slug_from_run_id(run_id))
    except JudgmentError as exc:
        raise FramesStageError(str(exc)) from exc


def run_frames_stage(
    run_id: str,
    *,
    client: Any,
    editor: ImageEditor | None = None,
    paths: Paths | None = None,
    llm: LLMClient | None = None,
    line: str | None = None,
    slug: str | None = None,
    review: bool = True,
    force: bool = False,
    jobs: int | None = None,
    image_timeout: int | None = None,
    session_timeout: int = SESSION_TIMEOUT,
) -> FramesResult:
    """`[6]` 본체. 실패 정책은 specs/05 D-5다."""
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    try:
        contract = find_contract_for_run(paths, run_id)
    except SceneContractNotFound as exc:
        raise FramesStageError(str(exc)) from exc
    if contract.get("run_id") != run_id:
        raise FramesStageError(
            f"scenes.json의 run_id({contract.get('run_id')})가 대상 run({run_id})과 다르다"
        )
    topic = str(contract.get("topic") or run_id)
    prompts = _load_json(run_dir / PROMPTS_FILE, f"[5]의 산출물({PROMPTS_FILE})")
    if prompts.get("run_id") != run_id:
        raise FramesStageError(
            f"{PROMPTS_FILE}의 run_id({prompts.get('run_id')})가 대상 run({run_id})과 다르다"
        )

    resolved_line = resolve_line(paths, run_id, slug=slug, line=line)
    if not vocab.style_in_frames(resolved_line):
        raise FramesStageError(
            f"영상 라인 '{resolved_line}'은 프레임을 입력으로 받지 않는다 — 이 단계가 필요 없다 "
            "(vocab.json meta.video_line.{line}.style_in_frames). [7]을 바로 돌려라"
        )

    all_jobs, warnings = build_jobs(contract, prompts, line=resolved_line)
    done_before = _existing(run_dir, force)

    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    record_path = run_dir / RECORD_FILE

    if (
        state.is_done(STAGE) and not force and record_path.exists()
        and len(done_before) == len(all_jobs)
    ):
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        previous = json.loads(record_path.read_text(encoding="utf-8"))
        return FramesResult(
            run_id=run_id, run_dir=run_dir, topic=topic,
            line=str(previous.get("line") or resolved_line),
            provider=str(previous.get("provider") or getattr(client, "name", "")),
            outcomes=[_outcome_from(e) for e in previous.get("scenes", [])],
            warnings=previous.get("warnings", []), record_path=record_path, skipped=True,
        )

    if review and llm is None:
        raise FramesStageError(
            "검수 세션 클라이언트가 없다 — `review=False`로 끄거나 LLM을 넘겨라"
        )
    if any(job.info for job in all_jobs) and editor is None:
        raise FramesStageError(
            "info 씬이 있는데 편집 어댑터가 없다. INFO 없이 CLEAN만 만들 생각이면 "
            "씬 계약에서 info를 빼야 한다 — 조용히 건너뛰지 않는다 (ADR-0071)"
        )

    state.mark_running(STAGE)
    (run_dir / FRAMES_DIR).mkdir(parents=True, exist_ok=True)

    outcomes: dict[int, SceneOutcome] = {
        scene_id: _outcome_from(entry) for scene_id, entry in done_before.items()
    }
    pending = [job for job in all_jobs if job.scene_id not in done_before]
    write_lock = threading.Lock()

    def write_record(extra: Sequence[str] = ()) -> None:
        document = {
            "run_id": run_id,
            "topic": topic,
            "line": resolved_line,
            "provider": getattr(client, "name", ""),
            "editor": getattr(editor, "name", None),
            "scenes": [
                outcomes[job.scene_id].record()
                for job in all_jobs
                if job.scene_id in outcomes
            ],
            "warnings": list(warnings) + list(extra),
        }
        write_text(record_path, dump_json(document))

    def on_scene_done(outcome: SceneOutcome) -> None:
        with write_lock:
            outcomes[outcome.scene_id] = outcome
            write_record()

    runner = _Runner(
        client=client, editor=editor, llm=llm, run_dir=run_dir, topic=topic,
        review=review, image_timeout=image_timeout, session_timeout=session_timeout,
        on_scene_done=on_scene_done,
    )
    write_record()

    workers = max(1, int(jobs)) if jobs else max(1, int(_concurrency(client)))
    log.info(
        "[%s] %d씬 (라인 %s, 워커 %d, 검수 %s) — 지난 done %d",
        STAGE, len(pending), resolved_line, workers, "on" if review else "off",
        len(done_before),
    )
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(runner.run_scene, pending))

    with write_lock:
        write_record()

    result = FramesResult(
        run_id=run_id, run_dir=run_dir, topic=topic, line=resolved_line,
        provider=getattr(client, "name", ""),
        outcomes=[outcomes[job.scene_id] for job in all_jobs if job.scene_id in outcomes],
        warnings=warnings, record_path=record_path,
    )
    for outcome in result.outcomes:
        for warning in outcome.warnings:
            log.warning("[%s] 씬 %d: %s", STAGE, outcome.scene_id, warning)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "scene_count": result.scene_count,
        "info_scenes": result.info_scenes,
        "demoted_info": result.demoted,
        "quadrant_swaps": result.quadrant_swaps,
        "reused_from_previous_run": len(done_before),
        "line": resolved_line,
        "provider": result.provider,
        "editor": getattr(editor, "name", None),
        "warnings": warnings,
        "outputs": [record_path.relative_to(paths.root).as_posix()],
    }

    if runner.refusal is not None:
        message = (
            f"프로바이더가 거절해 남은 씬을 시도하지 않았다 (D-5): {runner.refusal}. "
            "만든 프레임과 기록은 남아 있다 — 고치고 다시 돌리면 done 씬은 건너뛴다"
        )
        state.mark_failed(STAGE, message, **info)
        raise ProviderRefused(message)

    failed = [o.scene_id for o in result.outcomes if o.status != DONE]
    if failed:
        message = f"프레임을 만들지 못한 씬이 있다: {failed[:8]}{' …' if len(failed) > 8 else ''}"
        state.mark_failed(STAGE, message, **info)
        raise FramesStageError(message)

    state.mark_done(STAGE, **info)
    return result


def _concurrency(client: Any) -> int:
    """어댑터가 말하는 동시 한도. 없으면 1 — 느려질 뿐 틀리지 않는다."""
    getter = getattr(client, "concurrency", None)
    if getter is None:
        return 1
    try:
        return int(getter() or 1)
    except Exception:  # 한도를 못 읽었다고 그림을 못 만드는 것이 아니다
        return 1
