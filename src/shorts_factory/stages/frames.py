"""[6. frames] — `[7]`에 줄 CLEAN 정지 이미지를 만든다. ADR-0071, ADR-0075가 범위를 좁혔다.

specs/05-pipeline.md:
    [6. frames] → frames/{scene_id}-clean.png + frames.json
                  (프레임을 입력으로 받는 라인의 **`info`가 없는 씬**만. CLEAN은 MJ가 그린다)

## 왜 있는가 — 그리는 쪽과 움직이는 쪽을 나눈다

`[7]`의 일반 씬은 MJ `endImage`라 **시작 프레임 한 장**을 요구한다. 그 한 장을 여기서
사고, 영상 모델은 그것을 밀고 당기기만 한다 (ADR-0070). 그림체가 편 안에서 흔들리지
않는 이유도 여기다 — 룩은 정지 이미지가 지고 그 씬의 영상 프롬프트에는 STYLE 절이 없다.

## `info` 씬은 여기 오지 않는다 (ADR-0075 결정 1·2)

옛 경로는 [MJ CLEAN → NB2가 그 위에 빨간 표시를 편집 → H3 first/last 보간]이었다.
기계 지표는 좋았다 — `japan-5060hz` 18씬에서 강등 0, 사분면 교체 2. **사람 판독이
반대였다**: *"그냥 h3가 잡을 때가 훨씬 질이 좋았어"* (2026-08-26). 강등이 적었던 것은
그림이 좋아서가 아니라 정지 이미지가 검수 기준(표시가 대상을 가리키는가)을 쉽게
통과하기 때문이었다. 지표가 아니라 최종 심급이 답했으므로 (ADR-0044) NB2 편집·INFO
검수(`14-inforeview.md`)·INFO 업로드가 통째로 빠졌다.

그래서 `info` 씬은 `[7]`에서 H3 **텍스트→영상**으로 간다. 프레임을 안 받으니 이 단계가
만들 것도 없다 — **건너뛴 것은 강등이 아니라 정상이다** (`skipped_info`, `demoted_*`가 아니다).

## 이 단계가 판단하지 않는 것

- **무엇을 그릴지** — `[5]`의 `mj_image_prompt`가 이미 완성한 한 줄이다. 예산·방언 검사도
  거기서 끝났다 (ADR-0075 결정 3). 이 단계는 조립하지 않는다
- **어떤 룩인지** — 라인의 `mj_style`이고 그것도 `[5]`가 얹었다 (ADR-0075 결정 7)

## 사다리는 사분면 넷 → 소재 교정 → 새 그리드다 (사람 결정 2026-08-25)

MJ imagine이 주는 것은 2×2 그리드다. 그리드를 보고 고르는 세션은 두지 않는다 — 평시
세션은 씬당 1회(CLEAN 검수)뿐이다. 검수에 걸리면 **다음 사분면의 CLEAN으로 갈아**
만든다. **넷을 다 쓴다**: U 추출은 과금 0이라 q0에서 멈출 이유가 없다.

넷이 다 걸리면 그때는 **사분면 운이 아니라 소재 단락의 문제**다. 기각 사유를 넣어 단락을
고치는 세션 1회를 부르고(`15-cleanfix.md`) 새 그리드를 산다 — 여기서만 과금이 는다.
같은 프롬프트로 다시 사면 같은 결함이 네 장 더 나올 뿐이다 (ADR-0067의 태도).

## 사다리 끝은 채택이다

여덟 장이 다 걸려도 CLEAN은 나간다 — 마지막 장을 쓰고 `demoted_from: unreviewed`를 적는다
(ADR-0072의 마지막 칸, `[7]`과 같은 이름). `[7]`은 first 프레임 없이 못 도는데 여기서
씬을 죽이면 편이 멈춘다. 아쉬운 그림이 없는 그림보다 낫고, 다음 심급은 `[7]`의 클립 검수다.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..config import MissingCredential, Paths, write_text
from ..imagegen.base import (
    ImageGenError,
    ImageRequest,
    ProviderNotConfigured,
)
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient, LLMError
from ..runstate import RunState
from ..schemas import promptplan, refs as refs_schema, vocab
from ..storage import r2 as objectstore
from ..schemas.visual_rules import (
    ASPECT_RATIO,
    RESOLUTION,
    build_mj_prompt,
    check_mj_prompt,
    mj_subject_budget,
    negative_items,
)
from .contract import SceneContractNotFound, find_contract_for_run, load_scene_contract
from .prompt import PROMPTS_FILE
from .session import load_prompt

log = logging.getLogger(__name__)

STAGE = "6-frames"
#: 사분면 넷이 다 걸렸을 때 소재 단락을 고치는 세션 (사람 결정 2026-08-25).
FIX_PROMPT = "15-cleanfix.md"
#: CLEAN 게이트 — **`[6]`의 유일한 검수다.** INFO 검수는 NB2와 함께 빠졌다 (ADR-0075 결정 2).
CLEAN_PROMPT = "16-cleanreview.md"

#: 그리드를 사는 횟수. 1이면 옛 동작(사분면 넷을 쓰고 끝), 2면 고쳐쓰기 뒤 한 번 더 산다.
#: **여기만 과금이 는다** — 사분면은 과금 0이고 그리드가 imagine 1잡이다.
GRID_ROUNDS = 2

#: 이 단계의 산출 — 계약 파일 하나와 이미지 디렉터리 하나 (specs/05 계약 표).
RECORD_FILE = "frames.json"
#: `[4]`의 산출 — 실물 참조 사진의 목록 (ADR-0077). 계약 이름은 refs 스키마의 것이다.
REFS_FILE = refs_schema.RECORD_FILE
FRAMES_DIR = "frames"

#: `[5]`가 완성해 실어 주는 MJ 한 줄 (`prompts.json`의 필드 — `visual_rules.PROMPT_SCENE_SCHEMA`).
#: **`info` 씬에는 없는 것이 정상이다** — 그 씬은 MJ를 타지 않는다 (ADR-0075 결정 3).
MJ_IMAGE_PROMPT_FIELD = "mj_image_prompt"

#: 검수 세션의 도구 — CLEAN을 직접 열어 본다 (`[7]`과 같은 메커니즘).
TOOLS: tuple[str, ...] = ("Read",)
SESSION_TIMEOUT = 300

#: 사분면 사다리의 길이. **그리드가 4장이므로 넷 다 쓴다** (사람 결정 2026-08-25) —
#: U 추출은 과금 0이라 q0에서 멈출 이유가 없다. 그리드는 다시 사지 않는다.
ATTEMPTS = 4

PASS, FAIL, ERROR = "pass", "fail", "error"
DONE, FAILED = "done", "failed"
#: 사다리를 다 쓰고도 게이트가 안 열렸을 때 마지막 장을 그대로 쓴 표식 (ADR-0072).
#: `[7]`의 그것과 같은 이름이다 — **강등을 조용히 하지 않는다**.
DEMOTED_UNREVIEWED = "unreviewed"

#: 파일 이름. 씬 하나에 한 장이다 — INFO 두 번째 장은 ADR-0075 결정 2가 지웠다.
CLEAN_SUFFIX = "-clean"


class FramesStageError(Exception):
    """`[6]`이 결과를 낼 수 없는 경우. 씬 하나의 실패는 여기로 오지 않는다."""


class ProviderRefused(FramesStageError):
    """프로바이더 전체 거절 — 남은 씬을 시도하지 않고 멈췄다 (D-5)."""


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
    """씬 하나가 이 단계에 요구하는 것 전부. **`info`가 없는 씬에만 만들어진다.**"""

    scene_id: int
    #: `[5]`가 완성한 MJ 한 줄. 여기서 조립하지 않는다 (ADR-0075 결정 3).
    mj_prompt: str
    #: 그 한 줄의 **원료**. 사분면이 다 걸렸을 때 고쳐 다시 조립하는 것이 이쪽이다
    #: (ADR-0067의 사다리) — 완성본을 고치라고 주면 스타일 나열과 `--ar` 플래그까지
    #: 세션의 손에 들어가 예산이 깨진다.
    mj_subject: str = ""
    #: 검수가 **그림 자체**를 계약과 대조할 때 쓰는 씬 계약의 그림 필드 (2026-08-25).
    #: 실측: 원반이 사람 대비 8배로 그려져 `3.6 m` 라벨과 모순된 씬, 두 섬이 안 갈라져
    #: "400 km 열린 바다"가 안 보이는 씬이 `[6]`을 통과하고 `[7]`에서 기각됐다.
    subject: str = ""
    visual_goal: str = ""
    #: ADR-0077 — 이 씬의 실물 참조 사진 (run 디렉터리 기준 경로). `[4]`가 `reference_ok`로
    #: 고른 것이고, 없으면 참조 없이 그린다. 라이선스가 아니라 **적합성**이 고른 값이다.
    reference_file: str = ""


def pick_reference(scene_refs: dict[str, Any] | None) -> str:
    """씬의 참조 후보 중 **쓸 만하다고 판정됐고 실물이 내려받아진** 첫 장 (ADR-0077).

    `attachable`을 보지 않는다 — 그 필드가 지키는 것은 `[8]`이 사진을 **그대로 싣는**
    경로이고 여기는 MJ가 형태만 참조하는 자리라 축이 다르다 (ADR-0077 맥락 5).
    """
    if not scene_refs:
        return ""
    for image in scene_refs.get("images") or []:
        if not isinstance(image, dict) or not image.get("reference_ok"):
            continue
        path = str(image.get("file") or "").strip()
        if path:
            return path
    return ""


def build_jobs(
    contract: dict[str, Any],
    prompts: dict[str, Any],
    *,
    line: str,
    refs: dict[str, Any] | None = None,
) -> tuple[list[FrameJob], list[str]]:
    """씬 계약 + `[5]` 산출 → 씬별 작업. **`info`가 없는 씬만 나온다** (ADR-0075 결정 1·2).

    MJ 한 줄은 여기서 조립하지 않는다 — `[5]`가 `mj_image_prompt`로 완성했고 예산·방언
    검사도 거기서 최종 전송 문자열에 걸린다 (ADR-0075 결정 3). 이 단계가 다시 조립하면
    `[5]`가 잰 줄과 실제로 보내는 줄이 갈릴 수 있다.

    한 씬이라도 그 필드가 없으면 시작하지 않는다: 절반만 산 편이 제일 비싸다.
    """
    warnings: list[str] = []
    by_id = {
        int(scene["scene_id"]): scene for scene in prompts.get("scenes", [])
    }
    refs_by_id: dict[int, dict[str, Any]] = {
        int(entry["scene_id"]): entry
        for entry in ((refs or {}).get("scenes") or [])
        if isinstance(entry, dict) and entry.get("scene_id") is not None
    }
    jobs: list[FrameJob] = []
    skipped = 0
    scenes = contract.get("scenes", [])
    if not scenes:
        raise FramesStageError("씬 계약에 씬이 없다")
    for scene in scenes:
        scene_id = int(scene["scene_id"])
        if scene.get("info"):
            # `info` 씬은 `[7]`에서 텍스트→영상으로 간다 — 프레임을 안 받으므로 만들 것이
            # 없다 (ADR-0075 결정 1). 건너뛴 것은 강등이 아니다.
            skipped += 1
            continue
        planned = by_id.get(scene_id)
        if planned is None:
            raise FramesStageError(
                f"{PROMPTS_FILE}에 씬 {scene_id}이 없다 — [5]를 다시 돌려야 한다"
            )
        mj_prompt = str(planned.get(MJ_IMAGE_PROMPT_FIELD) or "").strip()
        if not mj_prompt:
            raise FramesStageError(
                f"씬 {scene_id}에 `{MJ_IMAGE_PROMPT_FIELD}`가 없다. 라인 '{line}'은 이 씬의 "
                "CLEAN 이미지를 사므로 [5]가 MJ 한 줄을 완성해 실어야 한다 (ADR-0075 결정 3) "
                "— 옛 prompts.json이면 [5]를 다시 돌려라"
            )
        jobs.append(
            FrameJob(
                scene_id=scene_id, mj_prompt=mj_prompt,
                mj_subject=str(planned.get(promptplan.MJ_SUBJECT_FIELD) or ""),
                subject=str(scene.get("subject") or ""),
                visual_goal=str(scene.get("visual_goal") or ""),
                reference_file=pick_reference(refs_by_id.get(scene_id)),
            )
        )
    if skipped:
        warnings.append(
            f"info {skipped}씬은 건너뛴다 — 텍스트→영상이라 프레임을 안 받는다 "
            "(ADR-0075 결정 1, 강등이 아니다)"
        )
    if not jobs:
        warnings.append(
            "전 씬이 info라 만들 CLEAN이 없다 — 빈 frames.json으로 끝난다. [7]을 바로 돌려라"
        )
    return jobs, warnings


# --- 검수 세션 -----------------------------------------------------------------

#: `[6]`이 얹는 라벨은 없다 — NB2가 빠졌고 화면 글자는 `[7]`의 H3가 그린다 (ADR-0075 결정 2·6).


def render_clean_prompt(
    *, topic: str, job: FrameJob, clean: Path, attempt: int = 0
) -> str:
    """CLEAN 검수 세션 프롬프트 (사람 결정 2026-08-25).

    **`[7]` 앞의 게이트다.** 못 쓸 그림을 여기서 거르는 이유는 하나다 — 이 한 장이 클립의
    first 프레임이 되는데, `[7]`의 사다리는 프롬프트만 고쳐 쓸 뿐 프레임을 갈지 못한다.
    거기서 다시 그리면 클립 하나에 165~255초가 나가고, 여기서 다음 사분면으로 가면 0이다.

    `attempt`가 거듭될수록 잣대가 낮아진다 (`vocab.review_standard`) — 마지막 칸은
    "이거라도 쓴다"라서 사다리가 닫힌다.
    """
    return load_prompt(CLEAN_PROMPT).safe_substitute(
        topic=topic, scene_id=job.scene_id, clean=clean,
        subject=job.subject, visual_goal=job.visual_goal,
        standard=vocab.review_standard(attempt),
    )


def render_fix_prompt(
    *, topic: str, job: FrameJob, mj_subject: str, reasons: Sequence[str], line: str
) -> str:
    """소재 단락 교정 세션 프롬프트. 예산은 어휘가 정한다 (ADR-0034)."""
    low, high = mj_subject_budget(line)
    return load_prompt(FIX_PROMPT).safe_substitute(
        topic=topic, scene_id=job.scene_id,
        subject=job.subject, visual_goal=job.visual_goal,
        mj_subject=mj_subject,
        reasons=chr(10).join(f"- {r}" for r in reasons) or "- (사유 없음)",
        words_min=low, words_max=high,
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
        llm: LLMClient | None,
        run_dir: Path,
        topic: str,
        review: bool,
        image_timeout: int | None,
        session_timeout: int,
        on_scene_done: Any,
        line: str = "",
    ) -> None:
        self.client = client
        self.llm = llm
        self.run_dir = run_dir
        self.frames_dir = run_dir / FRAMES_DIR
        self.topic = topic
        self.review = review
        #: 고친 소재 단락을 다시 조립할 때 쓰는 라인 — 룩과 예산이 라인의 것이다.
        self.line = line
        self.image_timeout = image_timeout
        self.session_timeout = session_timeout
        self.on_scene_done = on_scene_done
        self.refusal: str | None = None
        self.stop = threading.Event()
        #: ADR-0077 — 참조 사진은 여러 씬이 같은 파일을 가리킬 수 있어 한 번만 올린다.
        #: 값은 `(주소, 키)`이고, 실패도 `("", "")`로 캐시해 매 씬 재시도하지 않는다.
        self._reference_cache: dict[str, tuple[str, str]] = {}
        self._reference_warned: set[str] = set()
        self.warnings: list[str] = []

    # --- MJ CLEAN ---------------------------------------------------------

    def _reference(self, job: FrameJob) -> tuple[str, str]:
        """참조 사진 → `(공개 주소, 안정 키)`. **실패해도 그림은 산다** (ADR-0077).

        참조는 그림을 좋게 하는 수단이지 조건이 아니다 — 스토리지가 없거나 업로드가
        깨지면 경고만 남기고 참조 없이 그린다 (specs/05 D-3의 태도).

        키는 **파일 내용 해시**다. 주소가 돌아도 지문이 같아 다시 사지 않고, 참조 없이 산
        씬은 참조가 살아나면 지문이 달라져 다시 산다 (ADR-0051이 `reference_key`를 둔 이유).
        """
        if not job.reference_file:
            return "", ""
        path = self.run_dir / job.reference_file
        cached = self._reference_cache.get(job.reference_file)
        if cached is not None:
            return cached
        if not objectstore.configured():
            self._warn_once(
                "R2 키가 없어 실물 참조를 붙이지 않는다 — 그림은 참조 없이 그린다 "
                f"({', '.join(objectstore.ENV_KEYS)} 중 빈 값이 있다, ADR-0077)"
            )
            self._reference_cache[job.reference_file] = ("", "")
            return "", ""
        try:
            url = objectstore.put_file(path, timeout=self.image_timeout or 60)
        except (objectstore.ObjectStoreError, MissingCredential) as exc:
            self._warn_once(f"씬 {job.scene_id}: 참조 사진을 올리지 못해 참조 없이 그린다 — {exc}")
            self._reference_cache[job.reference_file] = ("", "")
            return "", ""
        key = objectstore.content_key(path.read_bytes(), path.suffix)
        log.info(
            "[%s] 씬 %d: 실물 참조를 붙인다 (%s) — 이 씬만 v7으로 돈다 (ADR-0077)",
            STAGE, job.scene_id, job.reference_file,
        )
        self._reference_cache[job.reference_file] = (url, key)
        return url, key

    def _warn_once(self, message: str) -> None:
        if message in self._reference_warned:
            return
        self._reference_warned.add(message)
        self.warnings.append(message)
        log.warning("[%s] %s", STAGE, message)

    def _grid(self, job: FrameJob, prompt: str | None = None) -> str:
        """imagine 잡 하나 → 그리드 태스크 id.

        **그리드는 사분면을 다 쓴 뒤에만 다시 산다** (사람 결정 2026-08-25). 사분면 넷은
        같은 그리드에서 과금 0으로 뽑으므로 먼저 소진하고, 넷이 다 걸리면 그때 소재 단락을
        고쳐 새 그리드를 산다 — 같은 프롬프트를 다시 사면 같은 결함이 나온다 (ADR-0067의 태도).
        """
        reference_url, reference_key = self._reference(job)
        request = ImageRequest(
            scene_id=job.scene_id, prompt=prompt or job.mj_prompt, negative_prompt="",
            aspect_ratio=ASPECT_RATIO, resolution=RESOLUTION,
            label=f"{STAGE}:{job.scene_id}",
            reference_url=reference_url, reference_key=reference_key,
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

    # --- CLEAN 게이트 ------------------------------------------------------

    def _clean_gate(self, job: FrameJob, clean: Path, attempt: int) -> dict[str, Any]:
        """CLEAN 검수 세션 1회. **세션이 두 번 죽으면 통과시킨다** — 검수기 고장으로 파이프라인을
        세우지 않는다 (`[7]`의 그 규칙과 같다).

        `[6]`에 남은 유일한 검수다 (ADR-0075 결정 2). 이 게이트가 기각하면 할 수 있는 일은
        다음 사분면뿐인데, 그것이 정확히 여기서 해야 하는 이유다 — `[7]`은 프레임을 갈지
        못하고 사분면 교체는 과금 0이다.
        """
        if not self.review or self.llm is None:
            return {
                "verdict": PASS,
                "reasons": ["검수 없이 채택 (--no-review)"],
                "target_pointed": None,
            }
        prompt = render_clean_prompt(topic=self.topic, job=job, clean=clean, attempt=attempt)
        return self._session(prompt, job, what="CLEAN 검수")

    def _session(self, prompt: str, job: FrameJob, *, what: str) -> dict[str, Any]:
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
            "reasons": [f"{what} 세션 실패 2회 → 검수 없이 통과: {last_error}"],
            "target_pointed": None,
        }

    # --- 씬 하나 ----------------------------------------------------------

    def run_scene(self, job: FrameJob) -> SceneOutcome:
        outcome = SceneOutcome(scene_id=job.scene_id)
        if self.stop.is_set():
            outcome.warnings.append("프로바이더 거절로 시도하지 않았다")
            self.on_scene_done(outcome)
            return outcome
        mj_prompt = job.mj_prompt
        for grid_round in range(GRID_ROUNDS):
            if grid_round:
                # 사분면 넷이 다 걸렸다 — 사분면 운이 아니라 소재 단락의 문제다.
                fixed = self._fix_subject(job, outcome)
                if fixed is None:
                    break
                mj_prompt = fixed
            try:
                outcome.grid_task_id = self._grid(job, mj_prompt)
            except ProviderNotConfigured as exc:
                self._refuse(str(exc))
                outcome.warnings.append(f"프로바이더 거절: {exc}")
                self.on_scene_done(outcome)
                return outcome
            except ImageGenError as exc:
                outcome.warnings.append(f"CLEAN 생성 실패: {exc}")
                self.on_scene_done(outcome)
                return outcome
            done = self._run_quadrants(job, outcome, grid_round)
            if done is not None:
                self.on_scene_done(done)
                return done

        # 사다리 끝 — 마지막 CLEAN을 검수 없이 쓴다 (ADR-0072의 마지막 칸, D-5).
        if outcome.clean_url:
            outcome.status = DONE
            outcome.demoted_from = DEMOTED_UNREVIEWED
            outcome.warnings.append(
                f"CLEAN {ATTEMPTS * GRID_ROUNDS}장이 다 걸려 마지막 장을 검수 없이 쓴다 "
                f"(demoted_from: {DEMOTED_UNREVIEWED}) — [7]은 first 프레임 없이 못 돈다"
            )
        self.on_scene_done(outcome)
        return outcome

    def _fix_subject(self, job: FrameJob, outcome: SceneOutcome) -> str | None:
        """기각 사유 → 고친 MJ 한 줄. 못 고치면 None이고 사다리가 끝난다 (D-5).

        같은 프롬프트로 그리드를 다시 사지 않는다 — 같은 결함이 네 장 더 나올 뿐이다.
        **고치는 것은 원료(`mj_subject`)이고 조립은 코드가 다시 한다** (ADR-0067·0034) —
        고친 단락은 예산·방언 검사를 다시 받고, 어기면 되돌린다 (ADR-0069).
        """
        if self.llm is None or not self.review:
            return None
        reasons = [
            reason
            for attempt in outcome.attempts
            for reason in ((attempt.get("review") or {}).get("reasons") or [])
        ]
        prompt = render_fix_prompt(
            topic=self.topic, job=job, mj_subject=job.mj_subject or job.mj_prompt,
            reasons=reasons[-6:], line=self.line,
        )
        try:
            result = self.llm.run(
                prompt, allowed_tools=(), timeout=self.session_timeout,
                label=f"{STAGE}:{job.scene_id}:fix",
            )
            payload = extract_json_object(result.text)
        except (JSONExtractionError, LLMError) as exc:
            outcome.warnings.append(f"소재 단락 교정 세션 실패 — 사다리를 끝낸다: {exc}")
            return None
        subject = str(payload.get("mj_subject") or "").strip()
        if not subject:
            outcome.warnings.append("소재 단락 교정이 빈 값을 냈다 — 사다리를 끝낸다")
            return None
        try:
            # 조립도 검사도 예산을 본다 — 둘 다 여기서 잡는다 (ADR-0069가 제출 전에 멈춘다).
            # 룩은 MJ 엔진의 것을 쓴다 — `ttv_style`은 서술형이라 이 한 줄에 못 눕는다
            # (ADR-0075 결정 7).
            candidate = build_mj_prompt(
                subject=subject,
                mj_style=vocab.line_style(self.line, engine=vocab.MJ_ENGINE),
                negatives=negative_items(has_info=False), aspect_ratio=ASPECT_RATIO,
            )
            check_mj_prompt(candidate)
        except ValueError as exc:
            outcome.warnings.append(f"고친 소재 단락이 예산을 어겨 되돌린다: {exc}")
            return None
        outcome.warnings.append(
            f"사분면 {ATTEMPTS}장이 다 걸려 소재 단락을 고쳐 새 그리드를 산다: "
            f"{payload.get('changed') or '(사유 없음)'}"
        )
        return candidate

    def _run_quadrants(
        self, job: FrameJob, outcome: SceneOutcome, grid_round: int
    ) -> SceneOutcome | None:
        """이 그리드의 사분면 넷. 통과하면 그 `outcome`, 아니면 None (다음 라운드로)."""
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

            # 잣대는 **씬 전체의 시도 번호**로 낮아진다 — 그리드를 새로 샀다고 처음
            # 잣대로 돌아가면 사다리가 닫히지 않는다 (ADR-0072).
            verdict = self._clean_gate(job, clean_path, grid_round * ATTEMPTS + quadrant)
            attempt["review"] = verdict
            if verdict["verdict"] == FAIL:
                continue
            if verdict["verdict"] == ERROR:
                outcome.warnings.extend(verdict["reasons"])

            outcome.status = DONE
            self.on_scene_done(outcome)
            return outcome

        return None  # 이 그리드는 다 걸렸다 — 바깥이 다음 라운드를 정한다

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
    #: `info`라서 만들지 않은 씬 수 — **강등이 아니라 정상이다** (ADR-0075 결정 2).
    #: `[7]`이 그 씬을 텍스트→영상으로 그린다.
    skipped_info: int = 0

    @property
    def scene_count(self) -> int:
        return len(self.outcomes)

    @property
    def quadrant_swaps(self) -> int:
        """q0이 아닌 칸으로 간 씬 수 — q0 고정이 값을 하는지 보는 지표 (ADR-0071)."""
        return sum(1 for o in self.outcomes if o.quadrant > 0)

    @property
    def unreviewed(self) -> int:
        """사다리를 다 쓰고 검수 없이 채택한 씬 수 — 게이트가 닫히는지 보는 지표."""
        return sum(1 for o in self.outcomes if o.demoted_from == DEMOTED_UNREVIEWED)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[6] {self.topic} — CLEAN {self.scene_count}씬 "
            f"(info {self.skipped_info}씬은 [7]이 텍스트→영상으로 그린다 · "
            f"사분면 교체 {self.quadrant_swaps} · 미검수 채택 {self.unreviewed}) "
            f"→ {RECORD_FILE}{tail}"
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

    # 실물 참조 (ADR-0077). `[4]`를 안 돌렸거나 파일이 없으면 참조 없이 도는 것이 정상이다.
    refs_path = run_dir / REFS_FILE
    refs = None
    if refs_path.is_file():
        try:
            refs = json.loads(refs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[%s] %s를 읽지 못해 참조 없이 간다 — %s", STAGE, REFS_FILE, exc)

    all_jobs, warnings = build_jobs(contract, prompts, line=resolved_line, refs=refs)
    with_reference = sum(1 for job in all_jobs if job.reference_file)
    if with_reference:
        log.info(
            "[%s] 실물 참조를 붙일 씬 %d/%d개 — 그 씬만 v7으로 돈다 (ADR-0077)",
            STAGE, with_reference, len(all_jobs),
        )
    #: 만들지 **않은** 씬 수. 지표로 남기지만 강등이 아니다 (ADR-0075 결정 2).
    skipped_info = sum(1 for scene in contract.get("scenes", []) if scene.get("info"))
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
            skipped_info=skipped_info,
        )

    # 만들 것이 없으면 검수기도 필요 없다 — 전 씬이 `info`인 편이 그렇다.
    if all_jobs and review and llm is None:
        raise FramesStageError(
            "검수 세션 클라이언트가 없다 — `review=False`로 끄거나 LLM을 넘겨라"
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
        client=client, llm=llm, run_dir=run_dir, topic=topic,
        review=review, image_timeout=image_timeout, session_timeout=session_timeout,
        on_scene_done=on_scene_done, line=resolved_line,
    )
    write_record()

    workers = max(1, int(jobs)) if jobs else max(1, int(_concurrency(client)))
    log.info(
        "[%s] %d씬 (라인 %s, 워커 %d, 검수 %s) — info로 건너뛴 씬 %d, 지난 done %d",
        STAGE, len(pending), resolved_line, workers, "on" if review else "off",
        skipped_info, len(done_before),
    )
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(runner.run_scene, pending))

    with write_lock:
        write_record()

    # 참조 경로가 남긴 경고(스토리지 없음·업로드 실패)를 단계 경고로 올린다 (ADR-0077).
    warnings.extend(runner.warnings)

    result = FramesResult(
        run_id=run_id, run_dir=run_dir, topic=topic, line=resolved_line,
        provider=getattr(client, "name", ""),
        outcomes=[outcomes[job.scene_id] for job in all_jobs if job.scene_id in outcomes],
        warnings=warnings, record_path=record_path, skipped_info=skipped_info,
    )
    for outcome in result.outcomes:
        for warning in outcome.warnings:
            log.warning("[%s] 씬 %d: %s", STAGE, outcome.scene_id, warning)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "scene_count": result.scene_count,
        "skipped_info": skipped_info,
        "unreviewed": result.unreviewed,
        "quadrant_swaps": result.quadrant_swaps,
        "reused_from_previous_run": len(done_before),
        # 실물 참조가 붙은 씬 수 = v7으로 돈 씬 수 (ADR-0077 되돌릴 조건의 관측 수단).
        "with_reference": with_reference,
        "line": resolved_line,
        "provider": result.provider,
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
