"""[7. videogen] — 씬당 텍스트→영상 클립 1개 + 검수 + 강등 사다리. ADR-0056.

specs/05-pipeline.md:
    [7. videogen] → clips/{scene_id}.mp4 + clips.json + clip_review.json
                    (씬당 텍스트→영상 클립 1개, Gemini Omni Flash. 길이 = 세 언어 중
                     최장 씬 + 0.6초를 3~10초 안에서 올림. 끝 프레임 OCR + 씬당 비전
                     검수 1회 → 재생성 1회 → 라벨 없는 재생성/인접 씬 재사용 강등)

## 입력 (전부 run 디렉터리, 읽기 전용)

| 파일 | 읽는 것 |
|---|---|
| `scenes.json` | 씬 계약 — `subject`·`subject_anchor`·`visual_goal`·`info` (검수 기준) |
| `prompts.json` | 씬별 `prompt`·`negative_prompt`·`has_info` (`[5]`) |
| `scenes.timed.{lang}.json` | 씬 길이 — **ko 필수, ja·en은 있는 것만** (D-3). 최장이 클립 길이를 정한다 |

## 클립 길이 (스펙 05 `[7]`)

`ceil(max_lang(end − start) + 0.6)`을 3~10초로 클램프한다. 0.6초는 디졸브 꼬리(ADR-0024)이고
값은 어휘(`transition.dissolve.seconds`)에서 온다. 10초를 넘는 씬은 10초로 만들고 경고한다 —
`[9]`가 마지막 프레임을 정지로 늘린다. 클립의 로컬 0초가 씬의 `start`이고 꼬리는 전부 뒤다.

## 검수 (ADR-0056 결정 6) — `--review full|ocr|none`

① **끝 프레임 OCR** (`video/ocr.py`): `info` 씬은 라벨이 전부 읽히고 계약에 없는 숫자가
   없어야, `info` 없는 씬은 글자가 없어야 통과다. tesseract가 없으면 이 게이트만 건너뛰고
   `clip_review.json`에 경고를 남긴다.
② **비전 세션 씬당 1회** (`prompts/16-clipreview.md`): 시작·중간·끝 프레임 3장 + 씬 계약의
   그림 필드를 주고 네 기준으로 판정한다. 나레이션 `text`는 주지 않는다 (ADR-0038). 씬당
   1세션·동시 실행 — `[6r]`이 24장을 한 세션에 넣어 5회 타임아웃한 교훈이다.

## 실패 사다리

    생성 → 검수 실패 → 재생성 1회 (같은 프롬프트)
      → info 씬이면 RED 절을 뺀 프롬프트로 재생성 (demoted_from: "info")
      → 그래도 실패면 **그 씬의 첫 후보를 그대로 채택** (demoted_from: "unreviewed").
        인접 재사용은 하지 않는다 — 옆 씬 복사는 같은 그림을 편 안에서 반복시킨다

**강등을 조용히 하지 않는다** — `clips.json`에 씬마다 `demoted_from`, `clip_review.json`에
시도마다 판정 사유. 인접 재사용은 **모든 씬의 생성이 끝난 뒤** 직렬로 푼다 (앞 씬이 아직
생성 중일 수 있다). 가까운 앞 씬 우선, 없으면 가까운 뒤 씬.

## 동시 제출·429·직렬 기록

워커 수는 어댑터의 `concurrency()`이고 `--jobs`가 이긴다. 429가 오면 **워커 1 + 백오프**로
내려간다. 기록은 씬 하나가 끝날 때마다 잠금 아래 직렬로 쓴다 — 재실행 시 `clips.json`에
`done`인 씬은 다시 사지 않는다 (ADR-0020의 목적).

## 프로바이더 전체 거절 (D-5)

`VideoProviderNotConfigured`(키·플랜·파라미터 거부)는 씬이 아니라 어댑터의 문제다 — 남은
씬을 제출하지 않고 멈춘다. 이미 산 클립과 기록은 남는다.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient, LLMError
from ..runstate import RunState
from ..schemas.timed_scenes import PRIMARY_LANGUAGE, present_languages
from ..schemas import promptplan, vocab
from ..schemas.visual_rules import build_video_prompt, demote_info, fill_seconds
from ..video.clips import FRAME_POSITIONS, FRAME_SUFFIX, frame_command, frame_times, normalize_command
from ..video.ffmpeg import DEFAULT_FFMPEG, FFmpegError, relative_path, run_ffmpeg
from ..video.ocr import OCR, OCRError, detect_ocr, gate as ocr_gate
from ..video.timeline import CLIPS_DIR, DISSOLVE_SECONDS
from ..videogen.base import (
    VideoClient,
    VideoGenError,
    VideoGenRateLimited,
    VideoProviderNotConfigured,
    VideoRequest,
)
from .contract import (
    SceneContractNotFound,
    TimedScenesNotFound,
    find_contract_for_run,
    load_scene_contract,
    load_timed_scenes,
)
from .prompt import PROMPTS_FILE
from .session import load_prompt

log = logging.getLogger(__name__)

STAGE = "7-videogen"
PROMPT = "16-clipreview.md"
#: 고쳐쓰기 세션 프롬프트 (ADR-0067) — 기각 사유를 받아 단락을 다시 쓴다.
FIX_PROMPT = "17-clipfix.md"

#: 이 단계의 **실행 기록** 둘 (specs/05 계약 표).
RECORD_FILE = "clips.json"
REVIEW_FILE = "clip_review.json"
#: 검수 재료 — 프로바이더 원본·정규화 후보·프레임. `clips/`에는 채택된 클립만 놓는다.
REVIEW_DIR = "clip_review"

REVIEW_NONE, REVIEW_OCR, REVIEW_FULL = "none", "ocr", "full"
REVIEW_MODES = (REVIEW_NONE, REVIEW_OCR, REVIEW_FULL)

#: 비전 세션의 도구 — 프레임을 직접 열어 본다 (`[8]`과 같은 메커니즘).
TOOLS: tuple[str, ...] = ("Read",)
#: 비전 세션 상한(초). 프레임 3장을 보는 일이라 짧다.
SESSION_TIMEOUT = 300

#: 끝 프레임 OCR 게이트를 켤지 (ADR-0068). 손으로 적지 않고 계약에서 로드한다 (ADR-0034).
END_FRAME_OCR: bool = bool(vocab.checks().get("end_frame_ocr", True))

#: 같은 프롬프트로 시도하는 횟수 — 생성 1 + 재생성 1 (스펙 05 `[7]`).
ATTEMPTS_PER_PROMPT = 2

#: 검수 세션의 기본 동시 수 (ADR-0072 결정 1). 프로바이더 한도와 **다른 자원**이라 따로
#: 센다 — 검수는 구독 헤드리스라 영상 엔진이 아니라 플랜 한도를 쓴다. `--review-jobs`가 이긴다.
DEFAULT_REVIEW_SLOTS = 4
#: 429 재시도 상한과 백오프 밑(초).
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF = 15.0

PASS, FAIL, ERROR = "pass", "fail", "error"
DONE, FAILED = "done", "failed"
DEMOTED_INFO = "info"
#: 검수를 통과한 시도가 없어 **그 씬의 첫 후보를 그대로 쓴** 표시 (사람 결정 2026-08-25).
#: `video`(인접 재사용)를 대체한다 — 옆 씬 복사는 같은 그림을 편 안에서 반복시킨다.
DEMOTED_UNREVIEWED = "unreviewed"
VARIANT_VIDEO, VARIANT_INFO, VARIANT_NO_RED = "video", "info", "no_red"


class VideogenStageError(Exception):
    """`[7]`이 결과를 낼 수 없는 경우. 씬 하나의 실패는 여기로 오지 않는다."""


class ProviderRefused(VideogenStageError):
    """프로바이더 전체 거절 — 남은 씬을 시도하지 않고 멈췄다 (D-5)."""


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 씬 계약에서 `run_id`를 읽는다 (ADR-0017)."""
    if run_id:
        return run_id
    if not slug:
        raise VideogenStageError("run_id 또는 slug 중 하나는 있어야 한다")
    try:
        contract, _path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise VideogenStageError(str(exc)) from exc
    resolved = contract.get("run_id")
    if not resolved:
        raise VideogenStageError(f"씬 계약에 run_id가 없다 (slug={slug})")
    return str(resolved)


# --- 클립 길이 -----------------------------------------------------------------


def clip_seconds(
    durations: Sequence[float], *, tail: float = DISSOLVE_SECONDS,
    low: int = VideoClient.min_seconds, high: int = VideoClient.max_seconds,
) -> tuple[int, str | None]:
    """`(요청 초, 클램프 방향)` — 세 언어 최장 + 꼬리를 정수로 올려 프로바이더 범위에 가둔다.

    범위는 어댑터의 것이다 (`VideoClient.min_seconds`·`max_seconds`, ADR-0059) — 기본 인자는
    인터페이스의 기본값이고, 단계는 `build_jobs(seconds_range=…)`로 실제 어댑터의 값을 넘긴다.
    """
    if not durations:
        raise VideogenStageError("씬 길이가 없다")
    needed = max(float(d) for d in durations) + tail
    seconds = math.ceil(round(needed, 6))
    if seconds > high:
        return high, "max"
    if seconds < low:
        return low, "min"
    return seconds, None


@dataclass
class SceneJob:
    scene_id: int
    prompt: str
    negative_prompt: str
    has_info: bool
    labels: list[str]
    seconds: int
    lang_seconds: dict[str, float]
    clamped: str | None
    review_fields: dict[str, Any]
    #: `[5]`가 실어 둔 세션 단락 (ADR-0067). 고쳐쓰기 재생성이 이 부분만 고쳐 같은 골격에
    #: 다시 얹는다. 옛 `prompts.json`에는 없으므로 비어 있을 수 있다 (그러면 고쳐쓰지 않는다).
    parts: dict[str, str] = field(default_factory=dict)
    #: 골격을 다시 조립할 때 필요한 씬 계약의 연출 — 세션이 바꿀 수 없는 값이다.
    staging: str = ""
    camera: str = ""
    #: 프레임을 입력으로 받는 라인의 first/last (ADR-0070·0071). `[6]`의 `frames.json`에서
    #: 온 **공개 https 주소**다. 그 라인이 아니면 둘 다 None이고 텍스트→영상으로 간다.
    first_frame: str | None = None
    last_frame: str | None = None
    #: **이 씬이** 프레임을 입력으로 받는가 (ADR-0075 결정 3이 ADR-0072의 라인 단위를
    #: 씬 단위로 좁혔다). 프롬프트 규약이 여기서 갈리므로 고쳐쓰기·강등 재조립도 이 값을
    #: 따라간다 — 프레임을 받는 씬은 카메라 구절 하나라 초 수 자리도 없고, `info` 씬은
    #: 텍스트→영상이라 전체 골격에 STYLE 절까지 있다.
    takes_frames: bool = False
    #: 이 씬의 영상 프롬프트가 쓰는 룩 (`ttv_style`). 프레임을 받는 씬은 빈 문자열이다 —
    #: 스타일을 프레임이 지므로 말로 다시 시키지 않는다 (ADR-0070 규칙 1).
    style: str = ""


def build_jobs(
    contract: dict[str, Any],
    prompts: dict[str, Any],
    timed_by_lang: dict[str, dict[str, Any]],
    *,
    seconds_range: tuple[int, int] = (VideoClient.min_seconds, VideoClient.max_seconds),
    frames: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[SceneJob], list[str]]:
    """세 입력 → 씬별 작업. `{seconds}`를 채우고 길이를 정한다. `(jobs, warnings)`.

    `seconds_range`는 어댑터가 받는 클립 길이 범위다 (ADR-0059 — 라인마다 다르다).
    `frames`는 `[6]`의 씬별 프레임 주소다 (ADR-0071) — 프레임을 입력으로 받는 라인에서만
    넘어오고, 그 라인인데 씬이 비면 **여기서 멈춘다**: 프레임 없이 사면 라벨 없는 클립을
    돈 주고 사게 된다.
    """
    warnings: list[str] = []
    low, high = seconds_range
    #: 이 run의 영상 룩 — `[5]`가 그 라인의 `ttv_style`을 적어 둔 값이다 (ADR-0075 결정 7).
    #: 고쳐쓰기 재조립이 STYLE 절을 되살릴 때 쓴다. 정본은 vocab이고 여기는 그 기록이다.
    video_style = str((prompts.get("style") or {}).get("base_style") or "")
    by_prompt = {int(s["scene_id"]): s for s in prompts.get("scenes", [])}
    contract_scenes = {int(s["scene_id"]): s for s in contract.get("scenes", [])}

    if set(by_prompt) != set(contract_scenes):
        raise VideogenStageError(
            f"{PROMPTS_FILE}의 씬({sorted(by_prompt)[:5]}…)과 씬 계약({sorted(contract_scenes)[:5]}…)이 다르다 — "
            "[5]를 다시 돌려라 (무료다)"
        )

    durations: dict[int, dict[str, float]] = {sid: {} for sid in contract_scenes}
    for lang, timed in timed_by_lang.items():
        scenes = timed.get("scenes", [])
        if len(scenes) != len(contract_scenes):
            raise VideogenStageError(
                f"scenes.timed.{lang}.json의 씬 수({len(scenes)})가 씬 계약({len(contract_scenes)})과 다르다 — "
                "[2l]의 줄 정렬이 깨졌거나 [3]이 다른 대본으로 돌았다"
            )
        for scene in scenes:
            sid = int(scene["scene_id"])
            if sid not in durations:
                raise VideogenStageError(f"scenes.timed.{lang}.json에 계약에 없는 씬 {sid}가 있다")
            durations[sid][lang] = round(float(scene["end"]) - float(scene["start"]), 3)

    jobs: list[SceneJob] = []
    for sid in sorted(contract_scenes):
        scene = contract_scenes[sid]
        entry = by_prompt[sid]
        seconds, clamped = clip_seconds(list(durations[sid].values()), low=low, high=high)
        if clamped == "max":
            longest = max(durations[sid].items(), key=lambda kv: kv[1])
            warnings.append(
                f"씬 {sid}: {longest[0]} {longest[1]:.1f}초 + 꼬리가 {high}초를 넘어 "
                f"{high}초로 만든다 — [9]가 마지막 프레임을 정지로 늘린다"
            )
        info = scene.get("info") or None
        labels = [str(label) for label in (info or {}).get("labels", [])]
        has_info = bool(entry.get("has_info")) or bool(info)
        # **프레임을 받는지는 씬이 정한다** (ADR-0075 결정 1·3). 프레임 라인이라도 `info`
        # 씬은 텍스트→영상으로 가므로 `[6]`이 그 씬의 CLEAN을 만들지 않았다.
        takes_frames = frames is not None and not has_info
        frame_urls = _frame_urls(sid, frames) if takes_frames else (None, None)
        jobs.append(SceneJob(
            scene_id=sid,
            prompt=_with_seconds(
                str(entry["video_prompt"]), seconds, takes_frames=takes_frames
            ),
            negative_prompt=str(entry.get("negative_prompt") or ""),
            has_info=has_info,
            labels=labels,
            seconds=seconds,
            lang_seconds=durations[sid],
            clamped=clamped,
            review_fields={
                "subject": str(scene.get("subject") or ""),
                "subject_anchor": list(scene.get("subject_anchor") or []),
                "visual_goal": str(scene.get("visual_goal") or ""),
                "info": info,
            },
            parts={
                key: str(entry[key])
                for key in (
                    promptplan.SUBJECT_FIELD,
                    promptplan.CAMERA_TARGET_FIELD,
                    promptplan.RED_FIELD,
                )
                if entry.get(key)
            },
            staging=str(entry.get("staging") or ""),
            camera=str(entry.get("camera") or scene.get("camera") or ""),
            first_frame=frame_urls[0],
            last_frame=frame_urls[1],
            takes_frames=takes_frames,
            style="" if takes_frames else video_style,
        ))
    return jobs, warnings


def _with_seconds(prompt: str, seconds: int, *, takes_frames: bool) -> str:
    """FORMAT 절의 초 수를 채운다. **프레임을 받는 씬은 그 절이 없다** (ADR-0072 결정 3) —
    화면비도 길이도 입력 이미지와 엔진이 정하므로 프롬프트가 초를 말하지 않는다.
    같은 라인의 `info` 씬은 텍스트→영상이라 FORMAT 절이 있고 여기서 채운다 (ADR-0075)."""
    if takes_frames:
        return prompt
    return fill_seconds(prompt, seconds)


def _frame_urls(
    scene_id: int, frames: dict[int, dict[str, Any]] | None
) -> tuple[str | None, str | None]:
    """씬의 `(first, last)` 주소. 프레임을 받는 씬에서만 부른다 (ADR-0071·0075).

    **`last`는 언제나 None이다** — 끝 그림(INFO)은 NB2가 만들던 것이고 ADR-0075 결정 2가
    그 경로를 폐기했다. 계측 표시를 싣는 씬은 이제 텍스트→영상이라 프레임 자체를 안 받는다.
    `first`(CLEAN)의 부재는 정상이 아니다 — `[6]`을 먼저 돌려야 한다.
    """
    if frames is None:
        return None, None
    entry = frames.get(scene_id)
    clean = str((entry or {}).get("clean_url") or "")
    if not clean:
        raise VideogenStageError(
            f"씬 {scene_id}의 CLEAN 주소가 {FRAMES_FILE}에 없다 — [6]을 먼저 돌려야 한다 "
            "(프레임을 입력으로 받는 라인이다, ADR-0071)"
        )
    return clean, None


@dataclass
class SceneOutcome:
    scene_id: int
    status: str = FAILED
    file: str | None = None
    seconds: int = 0
    lang_seconds: dict[str, float] = field(default_factory=dict)
    clamped: str | None = None
    has_info: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    demoted_from: str | None = None
    rate_limited: int = 0
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def generated(self) -> int:
        return sum(1 for a in self.attempts if a.get("request_id"))

    def clip_record(self, provider: str) -> dict[str, Any]:
        """`clips.json`의 씬 항목 — 생성 기록 (검수 사유는 `clip_review.json`에)."""
        return {
            "scene_id": self.scene_id,
            "status": self.status,
            "file": self.file,
            "provider": provider,
            "seconds": self.seconds,
            "lang_seconds": self.lang_seconds,
            "clamped": self.clamped,
            "attempts": self.generated,
            "wall_seconds": round(sum(a.get("wall_seconds", 0.0) for a in self.attempts), 1),
            "retries": max(0, self.generated - 1),
            "rate_limited": self.rate_limited,
            "demoted_from": self.demoted_from,
            "request_ids": [a["request_id"] for a in self.attempts if a.get("request_id")],
        }

    def review_record(self) -> dict[str, Any]:
        """`clip_review.json`의 씬 항목 — 시도마다 OCR·비전 판정과 사유."""
        return {
            "scene_id": self.scene_id,
            "has_info": self.has_info,
            "final": (
                f"demoted_{self.demoted_from}" if self.demoted_from else
                (PASS if self.status == DONE else self.status)
            ),
            "attempts": [
                {k: v for k, v in attempt.items() if k != "wall_seconds"}
                for attempt in self.attempts
            ],
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


@dataclass
class VideogenResult:
    run_id: str
    run_dir: Path
    topic: str
    provider: str
    review: str
    outcomes: list[SceneOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    record_path: Path | None = None
    review_path: Path | None = None

    @property
    def scene_count(self) -> int:
        return len(self.outcomes)

    @property
    def generated_clips(self) -> int:
        return sum(o.generated for o in self.outcomes)

    @property
    def purchased_seconds(self) -> int:
        return sum(o.seconds * o.generated for o in self.outcomes)

    def demoted(self, kind: str) -> int:
        return sum(1 for o in self.outcomes if o.demoted_from == kind)

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and all(o.status == DONE for o in self.outcomes)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[7] {self.topic} — {self.scene_count}씬 / 호출 {self.generated_clips}회 "
            f"({self.purchased_seconds}초, {self.provider}) / 검수 {self.review} / "
            f"강등 info {self.demoted(DEMOTED_INFO)} · 미검수채택 {self.demoted(DEMOTED_UNREVIEWED)} "
            f"→ {CLIPS_DIR}/ + {RECORD_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise VideogenStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VideogenStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def render_fix_prompt(
    *, topic: str, scene_id: int, fields: dict[str, Any], parts: dict[str, str],
    reasons: Sequence[str],
) -> str:
    """고쳐쓰기 세션 프롬프트 (ADR-0067). 나레이션 `text`는 넣지 않는다 (ADR-0038과 같은 태도).

    입력은 **바꿀 수 없는 것**(씬 계약의 그림 필드)과 **고칠 것**(세션 단락)과 **왜**(기각
    사유)로 갈라 보여 준다 — 세션이 어느 쪽을 만질 수 있는지 프롬프트에서 읽히게 한다.
    """
    info = fields.get("info") or None
    lines = [
        f"- **subject (그릴 것)**: {fields.get('subject') or '(없음)'}",
        f"- **subject_anchor**: {', '.join(fields.get('subject_anchor') or []) or '(없음)'}",
        f"- **visual_goal (화면이 져야 하는 설명)**: {fields.get('visual_goal') or '(없음)'}",
    ]
    if info:
        labels = ", ".join(f'"{label}"' for label in info.get("labels", []))
        lines.append(
            f"- **계측 표시(info)**: `{info.get('annotation', '')}` — "
            f"**{info.get('target', '')}**를 재고, 라벨은 {labels} 이다"
        )
    red = parts.get(promptplan.RED_FIELD) or ""
    red_block = f"\nRED (계측 표시 기하):\n{red}\n" if red else "\n"
    return load_prompt(FIX_PROMPT).safe_substitute(
        topic=topic, scene_id=scene_id,
        contract="\n".join(lines),
        subject_prompt=parts.get(promptplan.SUBJECT_FIELD) or "(없음)",
        camera_target=parts.get(promptplan.CAMERA_TARGET_FIELD) or "(없음)",
        red_block=red_block,
        reasons="\n".join(f"- {r}" for r in reasons) or "- (사유 없음)",
    )


def render_review_prompt(
    *, topic: str, scene_id: int, fields: dict[str, Any], frames: Sequence[Path],
    attempt: int = 0,
) -> str:
    """비전 세션 프롬프트. 나레이션 `text`는 넣지 않는다 (ADR-0038 — 그림 목표만 본다).

    `attempt`가 거듭될수록 **잣대가 낮아진다** (`vocab.review_standard`) — 같은 잣대로
    반복 기각하면 재생성이 끝나지 않고, 사다리 끝은 옆 씬 클립으로 때우는 것이라
    아쉬운 클립보다 나쁘다.
    """
    info = fields.get("info") or None
    if info:
        labels = ", ".join(f'"{label}"' for label in info.get("labels", []))
        info_block = (
            f"- **계측 표시(info)**: `{info.get('annotation', '')}` — **{info.get('target', '')}**를 "
            f"재야 하고, 라벨은 {labels} 이다. 빨강은 이 표시에만 쓰여야 한다"
        )
    else:
        info_block = "- **계측 표시(info)**: 없다 — 이 씬에는 글자·숫자·빨간 표시가 **없어야** 한다"
    anchors = ", ".join(fields.get("subject_anchor") or []) or "(없음)"
    frame_lines = "\n".join(
        f"- `{path}` ({pos})" for pos, path in zip(FRAME_POSITIONS, frames)
    )
    return load_prompt(PROMPT).safe_substitute(
        topic=topic, scene_id=scene_id,
        subject=fields.get("subject") or "(없음)",
        anchors=anchors,
        visual_goal=fields.get("visual_goal") or "(없음)",
        info_block=info_block,
        frames=frame_lines,
        standard=vocab.review_standard(attempt),
    )


def parse_review(payload: dict[str, Any]) -> dict[str, Any]:
    """세션 출력 → `{verdict, reasons, target_pointed}`. 모르는 판정은 `fail`로 본다."""
    verdict = str(payload.get("verdict") or "").strip().lower()
    reasons = payload.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    reasons = [str(r).strip() for r in reasons if str(r).strip()]
    if verdict not in (PASS, FAIL):
        reasons.append(f"알 수 없는 판정 '{verdict}' → fail로 본다")
        verdict = FAIL
    pointed = payload.get("target_pointed")
    if pointed is not None and not isinstance(pointed, bool):
        pointed = None
    return {"verdict": verdict, "reasons": reasons, "target_pointed": pointed}


class _Runner:
    """씬 작업을 도는 워커의 공유 상태. 단계 함수가 만들고 버린다."""

    def __init__(
        self, *, client: VideoClient, run_dir: Path, topic: str, review: str,
        ocr: OCR | None, llm: LLMClient | None, ffmpeg: str, runner: Callable[..., Any],
        video_timeout: int | None, session_timeout: int, sleep: Callable[[float], None],
        backoff: float, on_scene_done: Callable[[SceneOutcome], None],
        gen_slots: int = 1, review_slots: int = 1,
        info_client: VideoClient | None = None,
    ) -> None:
        self.client = client
        # `info` 씬만 다른 엔진으로 보낸다 (ADR-0072 결정 5). MJ는 끝 이미지를 목표로
        # 접근할 뿐 **들고 가지 못해** 중간 프레임에서 계측 표시가 무너지고(2041→224→3964),
        # H3는 두 프레임을 보간해 2초부터 끝까지 평평하다(4524~4579). 일반 씬은 MJ의
        # 그림체·카메라 워크가 낫고 로컬 GPU도 안 쓴다. None이면 라인 전체가 한 엔진이다.
        self.info_client = info_client
        self.run_dir = run_dir
        self.topic = topic
        self.review = review
        self.ocr = ocr
        self.llm = llm
        self.ffmpeg = ffmpeg
        self.runner = runner
        self.video_timeout = video_timeout
        self.session_timeout = session_timeout
        self._sleep = sleep
        self.backoff = backoff
        self.on_scene_done = on_scene_done
        self.stop = threading.Event()
        # 생성과 검수는 **다른 자원**이다 (ADR-0072 결정 1). 씬마다 스레드가 하나씩 돌되
        # 프로바이더 호출은 `gen`이, 검수 세션은 `review`가 각각 몇 개까지 동시에 도는지
        # 정한다 — 한 씬이 검수 중이면 그 씬이 쥐고 있던 생성 슬롯은 즉시 다음 씬에게
        # 간다. 기각된 씬은 사다리를 돌며 생성 슬롯을 다시 기다리므로 "큐 뒤에 다시
        # 붙는" 것과 같다. 씬당 워커 하나가 둘을 다 지면 검수 동안 프로바이더가 논다.
        # **엔진이 다르면 다른 자원이다** (ADR-0072 되돌릴 조건 6 → 확정 2026-08-25). MJ는
        # 원격 프록시, H3는 이 PC의 GPU라 서로의 한도와 무관한데, 게이트를 공유하면 H3의
        # 205초짜리 잡이 도는 동안 MJ가 슬롯을 못 써 **두 엔진이 직렬로 합산된다** (실측:
        # 18씬에 60분). 엔진 이름마다 자기 게이트를 준다.
        self._gen_gates: dict[str, threading.Semaphore] = {}
        self._gen_slots = max(1, int(gen_slots))
        self._gate_lock = threading.Lock()
        self.review_gate = threading.Semaphore(max(1, int(review_slots)))
        self.refusal: VideoProviderNotConfigured | None = None
        self._serial = False
        self._serial_lock = threading.Lock()
        self.review_dir = run_dir / REVIEW_DIR
        self.clips_dir = run_dir / CLIPS_DIR

    # --- 생성 -------------------------------------------------------------

    def gate_for(self, client: VideoClient) -> threading.Semaphore:
        """그 엔진의 생성 게이트. 처음 보는 엔진이면 만든다 (ADR-0072).

        슬롯 수는 엔진이 말한다 — `--jobs`는 **주 엔진**의 것이고, 곁다리 엔진(로컬 GPU)은
        자기 `concurrency()`를 쓴다. 둘을 한 숫자로 묶으면 한쪽이 다른 쪽을 굶긴다.
        """
        with self._gate_lock:
            gate = self._gen_gates.get(client.name)
            if gate is None:
                slots = self._gen_slots if client is self.client else max(1, int(client.concurrency() or 1))
                gate = threading.Semaphore(slots)
                self._gen_gates[client.name] = gate
            return gate

    def engine_for(self, job: SceneJob, variant: str) -> VideoClient:
        """이 씬·이 변종을 만들 엔진 (ADR-0072 결정 5, ADR-0075 결정 1이 엔진을 TTV로 바꿨다).

        `info` 엔진이 있고 그 씬이 계측 표시를 **실제로 실을 때만** 그쪽으로 간다.
        RED를 뺀 강등 변종(`no_red`)은 표시가 없으므로 일반 엔진으로 돌아가는데, 그 씬은
        프레임을 받지 않아 CLEAN이 없다 — 그래서 강등된 뒤에도 텍스트→영상이어야 한다.
        `takes_frames`가 그것을 가른다: 프레임을 안 받는 씬은 언제나 `info` 엔진이다.
        """
        if self.info_client is None:
            return self.client
        if job.takes_frames:
            return self.client
        return self.info_client

    def _generate(
        self, job: SceneJob, attempt: dict[str, Any], prompt: str, negative: str,
        client: VideoClient | None = None, variant: str = VARIANT_VIDEO,
    ) -> Path | None:
        """호출 1회 (429면 직렬 + 백오프로 재시도) → 원본 파일 경로. 실패면 None + 사유."""
        client = client or self.client
        # **RED를 뺀 변종은 끝 그림도 뺀다** (ADR-0072 정정 2026-08-25). 표시를 그리지 말라고
        # 해 놓고 표시가 그려진 INFO를 끝 그림으로 주면 엔진은 그쪽으로 간다 — 그래서 사다리의
        # 마지막 칸이 **구조적으로 통과할 수 없었다** (실측: "글자가 없어야 하는데 빨간 라벨이
        # 있다"로 씬 4·6이 연속 기각). CLEAN 한 장으로 가면 엔진이 알아서 움직인다.
        last_frame = None if variant == VARIANT_NO_RED else job.last_frame
        request = VideoRequest(
            scene_id=job.scene_id, prompt=prompt, negative_prompt=negative,
            seconds=job.seconds, label=f"scene {job.scene_id} attempt {attempt['attempt']}",
            first_frame=job.first_frame, last_frame=last_frame,
        )
        for retry in range(RATE_LIMIT_RETRIES + 1):
            if self.stop.is_set():
                attempt["error"] = "프로바이더 거절로 중단됐다"
                return None
            started = time.monotonic()
            lock = self._serial_lock if self._serial else None
            if lock:
                lock.acquire()
            try:
                # 프로바이더 동시 한도는 여기서만 진다 (ADR-0072) — 검수·정규화는 이 슬롯을
                # 쥐지 않으므로, 이 씬이 검수로 넘어가면 슬롯은 곧바로 다음 씬에게 간다.
                with self.gate_for(client):
                    clip = client.generate(request, timeout=self.video_timeout)
            except VideoGenRateLimited as exc:
                attempt["rate_limited"] = attempt.get("rate_limited", 0) + 1
                self._serial = True  # 스펙 05 — 429면 워커 1로 줄이고 백오프
                wait = self.backoff * (2 ** retry)
                log.warning("[%s] 씬 %d: 429 → 직렬 + %.0f초 대기 (%s)", STAGE, job.scene_id, wait, exc)
                attempt["wall_seconds"] = attempt.get("wall_seconds", 0.0) + (time.monotonic() - started)
                if retry == RATE_LIMIT_RETRIES:
                    attempt["error"] = f"429가 {RATE_LIMIT_RETRIES + 1}회 이어졌다: {exc}"
                    return None
                self._sleep(wait)
                continue
            except VideoProviderNotConfigured as exc:
                self.refusal = exc
                self.stop.set()
                attempt["error"] = f"프로바이더 거절: {exc}"
                return None
            except VideoGenError as exc:
                attempt["error"] = str(exc)
                attempt["wall_seconds"] = attempt.get("wall_seconds", 0.0) + (time.monotonic() - started)
                return None
            finally:
                if lock:
                    lock.release()

            attempt["wall_seconds"] = attempt.get("wall_seconds", 0.0) + (time.monotonic() - started)
            attempt["engine"] = client.name
            attempt["request_id"] = clip.request_id or f"{client.name}-{job.scene_id}-{attempt['attempt']}"
            attempt["provider"] = {**clip.meta, **{k: v for k, v in clip.raw.items() if k != "uri"}}
            raw_path = self.review_dir / f"{job.scene_id}-{attempt['attempt']}.raw.mp4"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(clip.data)
            return raw_path
        return None  # pragma: no cover

    def _normalize(self, job: SceneJob, attempt: dict[str, Any], raw_path: Path) -> Path | None:
        candidate = self.review_dir / f"{job.scene_id}-{attempt['attempt']}.mp4"
        cmd = normalize_command(
            relative_path(raw_path, self.run_dir), relative_path(candidate, self.run_dir),
            seconds=job.seconds, executable=self.ffmpeg,
        )
        try:
            run_ffmpeg(cmd, cwd=self.run_dir, produces=candidate, runner=self.runner)
        except FFmpegError as exc:
            attempt["error"] = f"정규화 실패: {exc}"
            return None
        return candidate

    def _frames(self, job: SceneJob, attempt: dict[str, Any], candidate: Path) -> list[Path] | None:
        frames: list[Path] = []
        for pos, at in frame_times(job.seconds).items():
            target = self.review_dir / f"{job.scene_id}-{attempt['attempt']}-{pos}{FRAME_SUFFIX}"
            cmd = frame_command(
                relative_path(candidate, self.run_dir), relative_path(target, self.run_dir),
                at=at, executable=self.ffmpeg,
            )
            try:
                run_ffmpeg(cmd, cwd=self.run_dir, produces=target, runner=self.runner)
            except FFmpegError as exc:
                attempt["error"] = f"프레임 추출 실패 ({pos}): {exc}"
                return None
            frames.append(target)
        return frames

    # --- 검수 -------------------------------------------------------------

    def _ocr(self, job: SceneJob, expect_labels: bool, end_frame: Path) -> dict[str, Any] | None:
        if self.review == REVIEW_NONE or self.ocr is None:
            return None
        try:
            text = self.ocr.read(end_frame)
        except OCRError as exc:
            return {"passed": True, "skipped": True, "reasons": [f"OCR 실행 실패 → 게이트 생략: {exc}"]}
        verdict = ocr_gate(text, job.labels if expect_labels else None)
        return verdict.record()

    def _vision(
        self, scene_id: int, fields: dict[str, Any], frames: Sequence[Path],
        attempt_number: int = 0,
    ) -> dict[str, Any] | None:
        if self.review != REVIEW_FULL or self.llm is None:
            return None
        prompt = render_review_prompt(
            topic=self.topic, scene_id=scene_id, fields=fields, frames=frames,
            attempt=attempt_number,
        )
        last_error = ""
        for _try in range(2):
            try:
                # `ask_json`이 아니라 직접 부른다 — 프레임 디렉터리를 `add_dirs`로 열어 줘야
                # 세션이 Read로 그림을 본다 (`[8]`과 같은 메커니즘, ADR-0012의 add_dirs).
                result = self.llm.run(
                    prompt, allowed_tools=TOOLS, timeout=self.session_timeout,
                    label=f"{STAGE}:{scene_id}", add_dirs=(self.review_dir,),
                )
                payload = extract_json_object(result.text)
            except (JSONExtractionError, LLMError) as exc:
                last_error = str(exc)
                continue
            verdict = parse_review(payload)
            verdict["session"] = result.meta
            return verdict
        # 세션이 두 번 죽었다 — 돈을 더 쓰는 대신 경고로 남기고 통과시킨다 (검수 불능은 클립의 잘못이 아니다).
        return {"verdict": ERROR, "reasons": [f"비전 세션 실패 2회 → 검수 없이 통과: {last_error}"], "target_pointed": None}

    def _review_attempt(self, job: SceneJob, attempt: dict[str, Any], variant: str, candidate: Path) -> bool:
        """검수 ①②. 통과면 True. 기록은 `attempt`에 남긴다.

        **프로바이더 슬롯을 쥐지 않는다** (ADR-0072 결정 1) — 검수는 헤드리스 세션이라
        영상 엔진과 다른 자원이고, 여기서 기다리는 동안 엔진은 다른 씬을 돌린다.
        """
        if self.review == REVIEW_NONE:
            attempt["passed"] = True
            return True
        frames = self._frames(job, attempt, candidate)
        if frames is None:
            attempt["passed"] = False
            return False
        attempt["frames"] = [relative_path(f, self.run_dir) for f in frames]
        with self.review_gate:
            return self._judge(job, attempt, variant, frames)

    def _judge(
        self, job: SceneJob, attempt: dict[str, Any], variant: str, frames: Sequence[Path]
    ) -> bool:
        """OCR + 비전 판정. 검수 슬롯 안에서만 돈다 (ADR-0072 결정 1)."""

        expect_labels = variant == VARIANT_INFO
        ocr = self._ocr(job, expect_labels, frames[-1])
        attempt["ocr"] = ocr
        if ocr is not None and not ocr.get("passed", True):
            attempt["passed"] = False
            return False

        # RED 절을 뺀 변종은 계측 표시가 없는 씬으로 본다 — 라벨을 기대하지 않는다.
        fields = {**job.review_fields, "info": None} if variant == VARIANT_NO_RED else job.review_fields
        # 시도 번호가 잣대를 정한다 — 0부터 세고, 사다리를 내려갈수록 느슨해진다.
        vision = self._vision(
            job.scene_id, fields, frames, attempt_number=max(0, int(attempt.get("attempt", 1)) - 1)
        )
        attempt["vision"] = vision
        if vision is not None and vision["verdict"] == FAIL:
            attempt["passed"] = False
            return False
        if vision is not None and vision["verdict"] == ERROR:
            attempt.setdefault("warnings", []).extend(vision["reasons"])
        attempt["passed"] = True
        return True

    # --- 고쳐쓰기 (ADR-0067) ------------------------------------------------

    def _revise(
        self, job: SceneJob, parts: dict[str, str], reasons: Sequence[str],
    ) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        """기각 사유 → `(고친 단락, 기록)`. 못 고치면 단락이 `None`이고 옛 단락으로 간다 (D-5).

        **연출은 입력에만 있고 출력에 없다** — 세션은 `subject_prompt`·`camera_target`·
        `red_prompt`만 쓴다 (ADR-0033 §3). 고친 단락은 `promptplan`의 씬 단위 규칙으로
        다시 재고, 어기면 되돌린다.
        """
        if self.review != REVIEW_FULL or self.llm is None or not parts or not reasons:
            return None, None
        prompt = render_fix_prompt(
            topic=self.topic, scene_id=job.scene_id, fields=job.review_fields,
            parts=parts, reasons=reasons,
        )
        try:
            result = self.llm.run(
                prompt, allowed_tools=(), timeout=self.session_timeout,
                label=f"{STAGE}:fix:{job.scene_id}",
            )
            payload = extract_json_object(result.text)
        except (LLMError, JSONExtractionError) as exc:
            return None, {"reasons": list(reasons), "failed": f"고쳐쓰기 세션 실패: {exc}"}

        revised = dict(parts)
        for key in (promptplan.SUBJECT_FIELD, promptplan.CAMERA_TARGET_FIELD):
            value = str(payload.get(key) or "").strip()
            if value:
                revised[key] = value
        if promptplan.RED_FIELD in parts:
            red = str(payload.get(promptplan.RED_FIELD) or "").strip()
            if red:
                revised[promptplan.RED_FIELD] = red

        errors = self._revision_errors(job, revised)
        record: dict[str, Any] = {
            "reasons": list(reasons),
            "changed": [k for k in revised if revised[k] != parts.get(k)],
            "note": str(payload.get("changed") or "").strip() or None,
        }
        if errors:
            record["rejected"] = errors
            return None, record
        if not record["changed"]:
            record["rejected"] = ["세션이 단락을 하나도 바꾸지 않았다"]
            return None, record
        return revised, record

    def _revision_errors(self, job: SceneJob, revised: dict[str, str]) -> list[str]:
        """고친 단락을 `[5]`와 같은 잣대로 잰다 — 계약은 한 곳에서만 정의된다 (ADR-0034)."""
        entry: dict[str, Any] = {"scene_id": job.scene_id, **revised}
        scene: dict[str, Any] = {
            "scene_id": job.scene_id,
            "info": job.review_fields.get("info") or None,
        }
        return promptplan.cross_errors({"scenes": [entry]}, {"scenes": [scene]})

    def _rebuild(self, job: SceneJob, parts: dict[str, str]) -> tuple[str, str] | None:
        """고친 단락 → 같은 골격의 프롬프트. 골격은 여전히 코드·어휘의 것이다 (ADR-0060)."""
        try:
            prompt, negative = build_video_prompt(
                subject_prompt=parts[promptplan.SUBJECT_FIELD],
                staging=job.staging,
                camera=job.camera,
                camera_target=parts.get(promptplan.CAMERA_TARGET_FIELD, ""),
                red_prompt=parts.get(promptplan.RED_FIELD),
                frames=job.takes_frames,
                style="" if job.takes_frames else job.style,
            )
        except (ValueError, KeyError):
            return None
        return _with_seconds(prompt, job.seconds, takes_frames=job.takes_frames), negative

    # --- 사다리 -----------------------------------------------------------

    def run_scene(self, job: SceneJob) -> SceneOutcome:
        outcome = SceneOutcome(
            scene_id=job.scene_id, seconds=job.seconds, lang_seconds=job.lang_seconds,
            clamped=job.clamped, has_info=job.has_info,
        )
        # 단락과 조립본은 사다리를 도는 동안 **같이** 움직인다 (ADR-0067) — 고쳐쓰기가
        # 단락을 바꾸면 조립본을 다시 만들고, RED 뺀 변종도 그 조립본에서 파생된다.
        parts = dict(job.parts)
        prompt, negative = job.prompt, job.negative_prompt
        kinds = [VARIANT_INFO if job.has_info else VARIANT_VIDEO]
        if job.has_info:
            kinds.append(VARIANT_NO_RED)

        number = 0
        for variant in kinds:
            if variant == VARIANT_NO_RED:
                try:
                    prompt, negative = demote_info(prompt, negative)
                except ValueError as exc:
                    outcome.warnings.append(f"RED 절 강등 변종을 만들 수 없다: {exc}")
                    break
                parts.pop(promptplan.RED_FIELD, None)
                outcome.warnings.append(
                    "검수 2회 실패 → RED 절을 뺀 프롬프트로 재생성한다 (demoted_from: info — 틀린 숫자보다 없는 숫자가 낫다)"
                )
            for index in range(ATTEMPTS_PER_PROMPT):
                if self.stop.is_set():
                    outcome.status = FAILED
                    outcome.reasons.append("프로바이더 거절로 중단")
                    self._finish(outcome)
                    return outcome
                number += 1
                attempt: dict[str, Any] = {"attempt": number, "variant": variant}
                outcome.attempts.append(attempt)
                engine = self.engine_for(job, variant)
                attempt["engine"] = engine.name
                raw = self._generate(job, attempt, prompt, negative, client=engine, variant=variant)
                outcome.rate_limited += attempt.get("rate_limited", 0)
                if raw is None:
                    attempt["passed"] = False
                    if self.stop.is_set():
                        outcome.status = FAILED
                        outcome.reasons.append(attempt.get("error") or "프로바이더 거절")
                        self._finish(outcome)
                        return outcome
                    outcome.reasons.append(f"시도 {number}: {attempt.get('error')}")
                    continue
                candidate = self._normalize(job, attempt, raw)
                if candidate is None:
                    attempt["passed"] = False
                    outcome.reasons.append(f"시도 {number}: {attempt.get('error')}")
                    continue
                if self._review_attempt(job, attempt, variant, candidate):
                    target = self.clips_dir / f"{job.scene_id}.mp4"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidate, target)
                    outcome.file = relative_path(target, self.run_dir)
                    outcome.status = DONE
                    if variant == VARIANT_NO_RED:
                        outcome.demoted_from = DEMOTED_INFO
                    for warning in attempt.get("warnings", []):
                        outcome.warnings.append(f"시도 {number}: {warning}")
                    self._finish(outcome)
                    return outcome
                reasons = []
                if attempt.get("ocr") and not attempt["ocr"].get("passed", True):
                    reasons.extend(attempt["ocr"].get("reasons", []))
                if attempt.get("vision") and attempt["vision"].get("verdict") == FAIL:
                    reasons.extend(attempt["vision"].get("reasons", []))
                if attempt.get("error"):
                    reasons.append(attempt["error"])
                outcome.reasons.append(f"시도 {number} ({variant}): " + ("; ".join(reasons) or "검수 실패"))

                # 같은 프롬프트를 다시 던지지 않는다 (ADR-0067). 이 변종에 시도가 남아 있을
                # 때만 고친다 — 마지막 시도 뒤의 고쳐쓰기는 쓸 곳이 없다.
                if index + 1 >= ATTEMPTS_PER_PROMPT or not reasons:
                    continue
                revised, record = self._revise(job, parts, reasons)
                if record is not None:
                    attempt["revision"] = record
                if revised is None:
                    outcome.warnings.append(
                        f"시도 {number}: 고쳐쓰기가 서지 않아 같은 단락으로 재시도한다"
                    )
                    continue
                rebuilt = self._rebuild(job, revised)
                if rebuilt is None:
                    outcome.warnings.append(
                        f"시도 {number}: 고친 단락을 골격에 얹지 못해 되돌린다"
                    )
                    continue
                parts, (prompt, negative) = revised, rebuilt

        # **인접 재사용은 하지 않는다** (사람 결정 2026-08-25). 그 씬을 위해 만든 클립이
        # 이미 있는데 버리고 옆 씬을 복사하면 **같은 그림이 편 안에서 반복된다** (실측:
        # 한 편에서 씬 7의 클립이 5회 나왔다). 검수를 통과 못 했어도 그 씬의 것이 낫다 —
        # 이미지는 전달을 보조하는 수단이고(ADR-0047) 숫자는 내레이션·자막이 진다.
        salvaged = self._salvage(job, outcome)
        if salvaged is not None:
            outcome.file = relative_path(salvaged, self.run_dir)
            outcome.status = DONE
            outcome.demoted_from = DEMOTED_UNREVIEWED
            outcome.warnings.append(
                "검수를 통과한 시도가 없어 **첫 시도 클립을 그대로 쓴다** "
                f"(demoted_from: {DEMOTED_UNREVIEWED}) — 옆 씬 복사보다 낫다"
            )
        else:
            outcome.status = FAILED
            outcome.reasons.append("쓸 수 있는 클립이 하나도 나오지 않았다")
        self._finish(outcome)
        return outcome

    def _salvage(self, job: SceneJob, outcome: SceneOutcome) -> Path | None:
        """검수를 다 떨어뜨렸을 때 **그 씬의 첫 후보**를 채택한다 (사람 결정 2026-08-25).

        첫 시도를 고르는 이유: 뒤 시도는 기각 사유를 피하려 단락을 고친 것이라 계약에서
        멀어져 있을 수 있고, 첫 시도가 `[5]`가 쓴 원본 그대로다.
        """
        for attempt in outcome.attempts:
            candidate = self.review_dir / f"{job.scene_id}-{attempt.get('attempt')}.mp4"
            if candidate.exists():
                target = self.clips_dir / f"{job.scene_id}.mp4"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, target)
                return target
        return None

    def _finish(self, outcome: SceneOutcome) -> None:
        self.on_scene_done(outcome)


# --- 단계 ---------------------------------------------------------------------


def _existing_done(run_dir: Path, force: bool) -> dict[int, dict[str, Any]]:
    """지난 실행의 `clips.json`에서 `done`이고 파일이 남아 있는 씬 — 다시 사지 않는다."""
    if force:
        return {}
    path = run_dir / RECORD_FILE
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    done: dict[int, dict[str, Any]] = {}
    for entry in previous.get("scenes", []):
        if entry.get("status") == DONE and entry.get("file") and (run_dir / entry["file"]).exists():
            done[int(entry["scene_id"])] = entry
    return done


def _existing_review(run_dir: Path, force: bool) -> dict[int, dict[str, Any]]:
    if force:
        return {}
    path = run_dir / REVIEW_FILE
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {int(e["scene_id"]): e for e in previous.get("scenes", []) if "scene_id" in e}


FRAMES_FILE = "frames.json"


def load_frames(
    paths: Paths, run_dir: Path, run_id: str, *, slug: str | None, line: str | None
) -> dict[int, dict[str, Any]] | None:
    """프레임을 입력으로 받는 라인이면 `[6]`의 `frames.json`을, 아니면 None (ADR-0071).

    **부재가 경고로 끝나지 않는다.** 그 라인인데 파일이 없으면 여기서 멈춘다 — 조용히
    텍스트→영상으로 내려가면 라벨 없는 클립을 돈 주고 사게 된다 (스펙 05 `[7]`).
    """
    from ..judgment import JudgmentError, read_video_line, slug_from_run_id

    try:
        resolved = line or read_video_line(paths, slug or slug_from_run_id(run_id))
    except JudgmentError as exc:
        raise VideogenStageError(str(exc)) from exc
    if not vocab.style_in_frames(resolved):
        return None
    path = run_dir / FRAMES_FILE
    if not path.exists():
        raise VideogenStageError(
            f"영상 라인 '{resolved}'은 프레임을 입력으로 받는데 {FRAMES_FILE}이 없다 — "
            "[6] frames를 먼저 돌려라 (ADR-0071)"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VideogenStageError(f"{FRAMES_FILE}을 읽을 수 없다: {exc}") from exc
    if document.get("run_id") != run_id:
        raise VideogenStageError(
            f"{FRAMES_FILE}의 run_id({document.get('run_id')})가 대상 run({run_id})과 다르다"
        )
    if document.get("line") not in (None, resolved):
        raise VideogenStageError(
            f"{FRAMES_FILE}은 라인 '{document.get('line')}'으로 만들어졌는데 지금 라인은 "
            f"'{resolved}'이다 — [6]을 다시 돌려라"
        )
    return {
        int(entry["scene_id"]): entry
        for entry in document.get("scenes", [])
        if "scene_id" in entry
    }


def run_videogen_stage(
    run_id: str,
    *,
    client: VideoClient,
    paths: Paths | None = None,
    llm: LLMClient | None = None,
    review: str = REVIEW_FULL,
    ocr: OCR | None = None,
    detect: Callable[[], OCR | None] = detect_ocr,
    force: bool = False,
    info_client: VideoClient | None = None,
    jobs: int | None = None,
    review_jobs: int | None = None,
    video_timeout: int | None = None,
    session_timeout: int = SESSION_TIMEOUT,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    backoff: float = RATE_LIMIT_BACKOFF,
    line: str | None = None,
    slug: str | None = None,
) -> VideogenResult:
    if review not in REVIEW_MODES:
        raise VideogenStageError(f"review는 {'|'.join(REVIEW_MODES)} 중 하나다: {review!r}")
    if review == REVIEW_FULL and llm is None:
        raise VideogenStageError("--review full에는 비전 세션 클라이언트가 필요하다 (ocr·none은 없어도 된다)")

    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    try:
        contract = find_contract_for_run(paths, run_id)
    except SceneContractNotFound as exc:
        raise VideogenStageError(str(exc)) from exc
    if contract.get("run_id") != run_id:
        raise VideogenStageError(
            f"scenes.json의 run_id({contract.get('run_id')})가 대상 run({run_id})과 다르다"
        )
    topic = str(contract.get("topic") or run_id)
    prompts = _load_json(run_dir / PROMPTS_FILE, f"[5]의 산출물({PROMPTS_FILE})")
    if prompts.get("run_id") != run_id:
        raise VideogenStageError(
            f"{PROMPTS_FILE}의 run_id({prompts.get('run_id')})가 대상 run({run_id})과 다르다"
        )

    langs = present_languages(run_dir)
    if PRIMARY_LANGUAGE not in langs:
        raise VideogenStageError(
            f"scenes.timed.{PRIMARY_LANGUAGE}.json이 없다: {run_dir}. [3. tts+sync]를 먼저 실행하라 — "
            "ko는 필수이고 ja·en은 있는 것만 본다 (D-3)"
        )
    try:
        timed_by_lang = {lang: load_timed_scenes(run_dir, lang) for lang in langs}
    except TimedScenesNotFound as exc:
        raise VideogenStageError(str(exc)) from exc
    for lang, timed in timed_by_lang.items():
        if timed.get("run_id") != run_id:
            raise VideogenStageError(
                f"scenes.timed.{lang}.json의 run_id({timed.get('run_id')})가 대상 run({run_id})과 다르다"
            )

    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    record_path = run_dir / RECORD_FILE
    review_path = run_dir / REVIEW_FILE

    frames = load_frames(paths, run_dir, run_id, slug=slug, line=line)
    if frames is not None and not client.accepts_frames:
        raise VideogenStageError(
            f"이 라인은 씬마다 프레임 두 장을 주는데 어댑터 '{client.name}'은 그것을 안 받는다 "
            "(ADR-0071) — 프레임을 실어도 버려지므로 [6]이 만든 계측 표시가 화면에서 사라진다. "
            "라인의 provider로 돌리거나 --line으로 텍스트→영상 라인을 지정하라"
        )
    all_jobs, warnings = build_jobs(
        contract, prompts, timed_by_lang, seconds_range=(client.min_seconds, client.max_seconds),
        frames=frames,
    )
    done_before = _existing_done(run_dir, force)
    review_before = _existing_review(run_dir, force)

    if state.is_done(STAGE) and not force and record_path.exists() and len(done_before) == len(all_jobs):
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        previous = json.loads(record_path.read_text(encoding="utf-8"))
        outcomes = []
        for entry in previous.get("scenes", []):
            outcomes.append(SceneOutcome(
                scene_id=int(entry["scene_id"]), status=entry.get("status", DONE),
                file=entry.get("file"), seconds=int(entry.get("seconds") or 0),
                lang_seconds=entry.get("lang_seconds") or {}, clamped=entry.get("clamped"),
                demoted_from=entry.get("demoted_from"),
                attempts=[{"request_id": rid} for rid in entry.get("request_ids", [])],
            ))
        return VideogenResult(
            run_id=run_id, run_dir=run_dir, topic=topic, provider=str(previous.get("provider") or client.name),
            review=str(previous.get("review") or review), outcomes=outcomes,
            warnings=previous.get("warnings", []), skipped=True,
            record_path=record_path, review_path=review_path,
        )

    state.mark_running(STAGE)

    # 게이트를 켤지는 계약이 정한다 (ADR-0068) — 실측에서 끝 프레임 130장 중 6장만 통과했고
    # 질감 잡음의 신뢰도가 진짜 라벨보다 높아 문턱으로 갈리지 않았다. 배선은 남긴다.
    if not END_FRAME_OCR:
        ocr_backend = None
        if review != REVIEW_NONE:
            warnings.append(
                "끝 프레임 OCR 게이트는 계약에서 꺼져 있다 (script-rules.json checks.end_frame_ocr, "
                "ADR-0068) — 라벨 대조는 비전 검수만 본다"
            )
    else:
        ocr_backend = ocr if ocr is not None else (detect() if review != REVIEW_NONE else None)
        if review != REVIEW_NONE and ocr_backend is None:
            warnings.append(
                "tesseract가 PATH에 없어 끝 프레임 OCR 게이트를 건너뛴다 — 라벨 대조는 비전 검수만 본다 (ADR-0056 결정 6)"
            )

    pending = [job for job in all_jobs if job.scene_id not in done_before]
    if done_before:
        log.info("[%s] 지난 실행의 done 씬 %d개는 다시 사지 않는다", STAGE, len(done_before))

    outcomes: dict[int, SceneOutcome] = {}
    for sid, entry in done_before.items():
        outcomes[sid] = SceneOutcome(
            scene_id=sid, status=DONE, file=entry.get("file"), seconds=int(entry.get("seconds") or 0),
            lang_seconds=entry.get("lang_seconds") or {}, clamped=entry.get("clamped"),
            has_info=bool(review_before.get(sid, {}).get("has_info")),
            demoted_from=entry.get("demoted_from"),
            attempts=[
                {"request_id": rid, "attempt": i + 1, "variant": "previous", "passed": True}
                for i, rid in enumerate(entry.get("request_ids", []))
            ],
        )
    write_lock = threading.Lock()

    def write_records(extra_warnings: Sequence[str] = ()) -> None:
        ordered = [outcomes[sid] for sid in sorted(outcomes)]
        record = {
            "run_id": run_id,
            "topic": topic,
            "provider": client.name,
            "model": getattr(client, "model_id", None),
            "review": review,
            "ocr_backend": getattr(ocr_backend, "name", None),
            "languages": langs,
            "scenes": [o.clip_record(client.name) for o in ordered],
            "warnings": list(warnings) + list(extra_warnings),
        }
        review_doc = {
            "run_id": run_id,
            "topic": topic,
            "review": review,
            "ocr_backend": getattr(ocr_backend, "name", None),
            "scenes": [
                review_before[o.scene_id] if (o.scene_id in done_before and o.scene_id in review_before)
                else o.review_record()
                for o in ordered
            ],
            "warnings": list(warnings) + list(extra_warnings),
        }
        write_text(record_path, dump_json(record))
        write_text(review_path, dump_json(review_doc))

    def on_scene_done(outcome: SceneOutcome) -> None:
        with write_lock:
            outcomes[outcome.scene_id] = outcome
            write_records()

    gen_slots = max(1, int(jobs)) if jobs else max(1, int(client.concurrency() or 1))
    review_slots = max(1, int(review_jobs or DEFAULT_REVIEW_SLOTS))
    runner_state = _Runner(
        client=client, run_dir=run_dir, topic=topic, review=review, ocr=ocr_backend, llm=llm,
        ffmpeg=ffmpeg, runner=runner, video_timeout=video_timeout, session_timeout=session_timeout,
        sleep=sleep, backoff=backoff, on_scene_done=on_scene_done,
        gen_slots=gen_slots, review_slots=review_slots, info_client=info_client,
    )
    (run_dir / REVIEW_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / CLIPS_DIR).mkdir(parents=True, exist_ok=True)
    write_records()

    log.info(
        "[%s] %d씬 제출 (생성 %d · 검수 %d 동시, 엔진 %s%s, 검수 %s, OCR %s) — 지난 done %d",
        STAGE, len(pending), gen_slots, review_slots, client.name,
        f" · info는 {info_client.name}" if info_client is not None else "",
        review, getattr(ocr_backend, "name", "없음"), len(done_before),
    )
    if pending:
        # 씬마다 스레드 하나를 띄우고 **한도는 세마포어가 진다** (ADR-0072 결정 1).
        # 스레드 대부분은 슬롯을 기다리며 자고 있고, 생성 슬롯은 검수로 넘어간 씬이
        # 즉시 놓아 준다 — 그래서 프로바이더가 큐가 빌 때까지 쉬지 않는다.
        #
        # **둘 다 1이면 진짜 직렬이다** — 파이프라이닝은 씬의 완료 순서를 뒤섞는데,
        # 재현 가능한 순서가 필요한 자리(디버깅·픽스처)가 있어 그 요청을 그대로 지킨다.
        # 슬롯이 하나뿐이라 성능상 잃는 것도 없다.
        serial = gen_slots == 1 and review_slots == 1 and info_client is None
        threads = 1 if serial else len(pending)
        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(runner_state.run_scene, pending))

    # 인접 재사용은 없다 (사람 결정 2026-08-25) — 검수를 못 통과한 씬은 **자기 첫 후보**를
    # 그대로 쓴다(`run_scene`의 `_salvage`). 옆 씬을 복사하면 같은 그림이 편 안에서
    # 반복되고(실측: 한 편에 씬 7이 5회), 그 씬을 위해 산 클립은 버려진다.
    salvaged = [sid for sid, o in outcomes.items() if o.demoted_from == DEMOTED_UNREVIEWED]
    reuse_warnings: list[str] = []
    if salvaged:
        reuse_warnings.append(
            f"검수를 통과 못 해 첫 후보를 그대로 쓴 씬: {', '.join(map(str, sorted(salvaged)))} "
            f"— 편당 {len(salvaged)}/{len(outcomes)}씬이면 프롬프트나 검수 잣대를 본다"
        )
    with write_lock:
        write_records(reuse_warnings)
    warnings.extend(reuse_warnings)

    result = VideogenResult(
        run_id=run_id, run_dir=run_dir, topic=topic, provider=client.name, review=review,
        outcomes=[outcomes[sid] for sid in sorted(outcomes)], warnings=warnings,
        record_path=record_path, review_path=review_path,
    )
    for outcome in result.outcomes:
        for warning in outcome.warnings:
            log.warning("[%s] 씬 %d: %s", STAGE, outcome.scene_id, warning)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "scene_count": result.scene_count,
        "generated_clips": result.generated_clips,
        "purchased_seconds": result.purchased_seconds,
        "reused_from_previous_run": len(done_before),
        "demoted_info": result.demoted(DEMOTED_INFO),
        "demoted_unreviewed": result.demoted(DEMOTED_UNREVIEWED),
        "rate_limited": sum(o.rate_limited for o in result.outcomes),
        "review": review,
        "ocr_backend": getattr(ocr_backend, "name", None),
        "provider": client.name,
        "warnings": warnings,
        "outputs": [
            p.relative_to(paths.root).as_posix() for p in (record_path, review_path)
        ],
    }

    if runner_state.refusal is not None:
        message = (
            f"프로바이더가 거절해 남은 씬을 시도하지 않았다 (D-5): {runner_state.refusal}. "
            f"산 클립 {result.generated_clips}개와 기록은 남아 있다 — 고치고 다시 돌리면 done 씬은 건너뛴다"
        )
        state.mark_failed(STAGE, message, **info)
        raise ProviderRefused(message)

    failed = [o.scene_id for o in result.outcomes if o.status != DONE]
    if failed:
        message = f"클립을 만들지 못한 씬이 있다: {failed[:8]}{' …' if len(failed) > 8 else ''}"
        state.mark_failed(STAGE, message, **info)
        raise VideogenStageError(message)

    state.mark_done(STAGE, **info)
    return result
