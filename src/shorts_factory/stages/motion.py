"""[7. motion] — 베이스 이미지 + 씬 계약 → 씬마다 클립 하나.

specs/05-pipeline.md:
    [7. motion] → clips/{scene_id}.mp4

    "**전 씬을 영상으로 만든다** (ADR-0039). `mj_video` → relax 엔드포인트, 강등
     사다리는 `mj_video` → `kenburns` → `static`. 클립 길이 = 씬 길이 + 디졸브
     겹침 0.6초. 클립의 로컬 0초가 씬의 `start`이고 0.6초는 전부 꼬리다 (ADR-0024)."

## 입력 (ADR-0020)

| 파일 | 무엇을 읽는가 |
|---|---|
| `runs/{run_id}/scenes.timed.json` | `scene_id`·`beat`·`start`·`end`·`camera`·`motion` |
| `runs/{run_id}/images/{scene_id}.*` | `[6]`의 베이스 이미지. 확장자는 프로바이더가 정한다 (ADR-0021) |
| `runs/{run_id}/image_source.json` | **영상 입력 계약** — 씬별 잡 id·사분면 (ADR-0041) |

**`images.json`·`image_review.json`을 읽지 않는다** (ADR-0024 §2). 둘은 기록이고 계약이
아니다. `[6]`이 폴백으로 같은 그림을 두 씬에 썼다는 사실은 기록이 아니라 **파일 내용**
에서 안다 — 바이트 해시가 같으면 2회차부터 스펙 03의 역방향 워크를 쓴다.

영상 입력만은 파일 내용에서 복원되지 않아 (MJ는 자기 잡 id로 가리킬 때만 이미지를
입력으로 받는다, ADR-0025) `image_source.json`을 따로 읽는다. **그 파일이 담는 것은
`task_id`·`quadrant` 둘뿐이고** `[6r]`의 판정 어휘는 이 단계에 오지 않는다 (ADR-0041).

`prompts.json`도 열지 않는다. 오버레이는 `[8]` 소관이고(ADR-0019) 프롬프트는 이미
그림이 됐다.

## 클립 길이는 여기서 계산하지 않는다

`video/timeline.py`의 `build_timeline`이 씬 계약에서 만든 `clip_length`를 그대로 쓴다.
`[9]`가 조립할 때 쓰는 것과 **같은 함수의 같은 값**이다 — 두 단계가 각자 계산하면
언젠가 갈라지고, 갈라진 쪽만 싱크가 어긋난다.

## 강등 사다리

`veo`(인포씬, ADR-0043) → `mj_video` → `kenburns` → `static`. **아래로만 내려간다.**

인포씬은 `info/{scene_id}.jpg`(`[6i]`의 산출물)의 존재로 골라진다 — 씬의 `motion`
값이 아니다. Veo가 실패하면 일반 영상 입력이 있는 씬은 `mj_video`로, 없으면
`kenburns`로 내려가고 `demoted_from: "veo"`가 남는다.

- **강등을 조용히 하지 않는다.** 어느 경로로 내려가든 `demoted_from`을 채우고 경고를
  남긴다. `mj_video`가 분기 없이 Ken Burns로 렌더되던 것이 정확히 이 사고였다 —
  실패가 아니라 **다른 것을 만들고** 리포트의 강등 수에도 안 잡혔다 (ADR-0039)
- `mj_video`가 내려가는 경우는 넷이다: 영상 프로바이더가 없다 / `image_source.json`에
  그 씬의 입력이 없다(= `[6]`의 인접 씬 폴백 씬, ADR-0041 결정 2) / 계약의 `provider`가
  영상 어댑터가 받는 이미지 프로바이더와 다르다 / 호출이 실패했다
- **프로바이더 전체가 못 쓰는 상태면**(`VideoProviderNotConfigured` — relax 영상이
  Pro 미만에서 막히는 경우가 이것이다) 남은 씬은 시도조차 하지 않는다. 같은 오류로
  27번 실패하는 것은 결과가 아니라 소음이다
- **재시도하지 않는다.** relax 영상 1잡이 206초이고, ADR-0035가 실측한 것이 "짧게 잡힌
  재시도가 이미 성공한 잡을 버린 뒤 큐를 두 배로 만든다"였다. 한 번 실패하면 내려간다
- `kenburns` 렌더가 실패하면 `static`으로 한 번 더 내려간다. specs/05 실패 정책
  ("씬 단위 실패는 폴백 처리하고 파이프라인은 계속 진행")에서 파생한 규칙이다 —
  스펙에 적힌 문장은 아니다. 아니라고 보면 `FALLBACK_CAMERA`를 지우면 된다

## 카메라 워크는 어휘가 정한다

영상 프로바이더에 보내는 영어 구절은 `specs/schema/vocab.json`의
`meta.camera[*].video_prompt`다 (ADR-0034 §3). **여기 적지 않는다** — 같은 워크를
FFmpeg zoompan과 i2v 양쪽에 쓰므로 출처가 하나여야 한다.

## 이어받기

Ken Burns는 과금이 없지만 27씬이 2분쯤 걸린다. 영상은 relax 큐를 씬당 200초 넘게
기다린다. 씬별로 (이미지 해시 + 적용 카메라 + 프레임 수 + 영상 입력)을 기록해 두고
그대로면 건너뛴다. `[6]`의 `digest`와 같은 장치다 — **사분면이 바뀌면 지문이 바뀌어
다시 만든다.**
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..runstate import RunState
from ..schemas import vocab
from ..schemas.image_source import by_scene, validate_image_source
from ..schemas.timed_scenes import validate_timed_scenes
from ..videogen.veo import DURATIONS as VEO_DURATIONS
from ..videogen.base import (
    VideoClient,
    VideoGenError,
    VideoProviderNotConfigured,
    VideoRequest,
)
from ..video.ffmpeg import DEFAULT_FFMPEG, FFmpegError, relative_path, run_ffmpeg
from ..video.kenburns import (
    CLIP_CRF,
    TRAVEL,
    KenBurnsError,
    build_command,
    frame_count,
    resolve_camera,
)
from ..video.timeline import Segment, TimelineError, build_timeline

log = logging.getLogger(__name__)

STAGE = "7-motion"

#: 1부↔2부 경계면 파일 (ADR-0017). `--slug`로 run을 찾을 때만 연다.
SCRIPT_FILE = "06-script.json"

TIMED_SCENES_FILE = "scenes.timed.json"
IMAGES_DIR = "images"

#: 산출물 — specs/05가 못박은 경로. `[8]`은 여기를 덮어쓰지 않는다 (ADR-0024)
CLIPS_DIR = "clips"

#: 이 단계의 **실행 기록**. `images.json`이 `[6]`에 대해 갖는 역할과 같다 (ADR-0020).
RECORD_FILE = "clips.json"

#: 입력 — `[6]`·`[6r]`이 쓴 **영상 입력 계약** (ADR-0041). 씬별 `task_id`·`quadrant`다.
SOURCE_FILE = "image_source.json"

#: 클립 하나 렌더 타임아웃(초). 실측 3.9초/클립이라 넉넉하다.
TIMEOUT = 300

#: 렌더가 실패했을 때 마지막으로 내려가는 자리 (위 docstring "강등 사다리").
#: 값 지목은 `vocab.require`로 한다 — 어휘와 갈리면 로드 시점에 터진다 (ADR-0034 §3).
FALLBACK_CAMERA = vocab.require("camera", "static")

KENBURNS = vocab.require("motion", "kenburns")
KLING = vocab.require("motion", "kling")
MJ_VIDEO = vocab.require("motion", "mj_video")

#: 인포씬의 영상 경로 (ADR-0043). **어휘 값이 아니라 기록 라벨이다** — 씬이 `motion`으로
#: 고를 수 있는 값이 아니고, `info/{scene_id}.jpg`의 존재가 이 경로를 고른다.
#: 강등 사다리: `veo → mj_video → kenburns → static`.
VEO = "veo"

#: `[6i]`의 산출물 — 인포씬의 끝 프레임 (specs/05).
INFO_DIR = "info"

#: Veo가 받는 클립 길이의 상한(초). 출처는 어댑터다 (ADR-0034 §3) — `VEO_DURATIONS[-1]`.

#: 영상 호출은 재시도하지 않는다 (위 docstring "강등 사다리"). relax 1잡이 206초이고,
#: ADR-0035가 실측한 것이 "재시도가 큐를 두 배로 만든다"였다.
VIDEO_ATTEMPTS = 1

#: 씬 하나의 결과 상태.
RENDERED = "rendered"
CACHED = "cached"
FAILED = "failed"

REUSABLE = (RENDERED, CACHED)

#: 같은 그림이 몇 번 나오면 카메라로 가릴 문제가 아닌가 (스펙 03 역방향 워크).
DUPLICATE_ALARM = 3


class MotionStageError(Exception):
    pass


@dataclass
class MotionResult:
    run_id: str
    topic: str
    run_dir: Path
    clips_dir: Path
    record_path: Path | None = None
    scenes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    #: 실제로 동시에 돈 워커 수. `--jobs`가 없으면 영상 프로바이더가 정한다.
    workers: int = 1

    def count(self, status: str) -> int:
        return sum(1 for s in self.scenes if s["status"] == status)

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def demoted(self) -> int:
        """요청받은 모션보다 아래로 내려간 씬. **조용한 강등을 만들지 않는다** (ADR-0039)."""
        return sum(1 for s in self.scenes if s.get("demoted_from"))

    @property
    def video_count(self) -> int:
        """실제로 영상으로 만든 씬. 전 씬 영상이 목표다 (ADR-0039)."""
        return sum(1 for s in self.scenes if s.get("motion_used") == MJ_VIDEO)

    @property
    def veo_count(self) -> int:
        """인포씬 영상 (ADR-0043) — CLEAN에서 시작해 INFO로 수렴하는 클립."""
        return sum(1 for s in self.scenes if s.get("motion_used") == VEO)

    @property
    def reversed_count(self) -> int:
        return sum(1 for s in self.scenes if s.get("camera_reversed"))

    @property
    def passed(self) -> bool:
        return bool(self.scenes) and self.count(FAILED) == 0

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        parts = [
            f"렌더 {self.count(RENDERED)}",
            f"이어받기 {self.count(CACHED)}",
            f"실패 {self.count(FAILED)}",
        ]
        extra = ""
        if self.video_count:
            extra += f" / 영상 {self.video_count}"
        if self.veo_count:
            extra += f" / 인포영상 {self.veo_count}"
        if self.demoted:
            extra += f" / 강등 {self.demoted}"
        if self.reversed_count:
            extra += f" / 역방향 워크 {self.reversed_count}"
        return (
            f"[7] {self.topic} — {self.scene_count}씬 ({' · '.join(parts)})"
            f"{extra} → {CLIPS_DIR}/{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MotionStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 경계면 파일에서 `run_id`를 읽는다 (ADR-0017)."""
    if run_id:
        return run_id
    if not slug:
        raise MotionStageError("run_id 또는 slug 중 하나는 있어야 한다")

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise MotionStageError(
            f"대본이 없다: {script_path}. --run-id로 run을 직접 지정할 수도 있다."
        )
    resolved = _load_json(script_path, f"씬 계약({SCRIPT_FILE})").get("run_id")
    if not resolved:
        raise MotionStageError(f"{script_path}에 run_id가 없다")
    return resolved


def _load_timed_scenes(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        raise MotionStageError(
            f"{TIMED_SCENES_FILE}이 없다: {path}. [3. tts+sync]를 먼저 실행하라 — "
            "총 길이가 102초를 넘어 멈춘 run에는 이 파일이 일부러 없다 (ADR-0017)."
        )
    data = _load_json(path, TIMED_SCENES_FILE)
    errors, warnings = validate_timed_scenes(data)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise MotionStageError(f"{path}이(가) 씬 계약을 위반한다:\n{listed}")
    return data, warnings


def find_image(images_dir: Path, scene_id: int) -> Path:
    """`images/{scene_id}.*` 한 장. 확장자는 프로바이더가 정한다 (ADR-0021).

    같은 씬에 확장자만 다른 파일이 둘 있으면 어느 것이 최신인지 알 수 없다. 조용히
    하나를 고르면 지난 프로바이더의 그림으로 영상을 만들게 되므로 멈춘다.
    """
    found = sorted(p for p in images_dir.glob(f"{scene_id}.*") if p.is_file())
    if not found:
        raise FileNotFoundError(f"{IMAGES_DIR}/{scene_id}.* 가 없다")
    if len(found) > 1:
        raise MotionStageError(
            f"씬 {scene_id}의 이미지가 {len(found)}장이다 "
            f"({', '.join(p.name for p in found)}). 씬 하나에 그림 하나다 — "
            f"쓰지 않는 확장자를 지워라"
        )
    return found[0]


def _collect_images(images_dir: Path, scenes: list[dict[str, Any]]) -> dict[int, Path]:
    """전 씬의 이미지를 먼저 모은다. 하나라도 없으면 렌더를 시작하기 전에 멈춘다.

    27씬 중 20개를 만든 뒤 21번에서 그림이 없다는 것을 아는 것보다, 2분을 쓰기 전에
    아는 편이 낫다. `[6]`이 폴백까지 실패했다는 뜻이기도 하다.
    """
    if not images_dir.is_dir():
        raise MotionStageError(
            f"{images_dir}가 없다. [6. imagegen]을 먼저 실행하라 (specs/05)."
        )
    found: dict[int, Path] = {}
    missing: list[int] = []
    for scene in scenes:
        try:
            found[scene["scene_id"]] = find_image(images_dir, scene["scene_id"])
        except FileNotFoundError:
            missing.append(scene["scene_id"])
    if missing:
        listed = ", ".join(str(i) for i in missing[:8])
        raise MotionStageError(
            f"이미지가 없는 씬이 {len(missing)}개다 ({listed}"
            f"{' …' if len(missing) > 8 else ''}). "
            f"[6. imagegen]이 {images_dir}에 씬마다 한 장씩 놓는다 (specs/05)."
        )
    return found


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _video_prompt(camera: str) -> str:
    """카메라 워크의 영어 구절. 출처는 `vocab.json`의 `meta.camera[*].video_prompt`다.

    **여기 적지 않는다** (ADR-0034 §3). 같은 워크를 FFmpeg zoompan과 i2v 양쪽에 쓰므로
    출처가 하나여야 한다.
    """
    return str((vocab.meta("camera").get(camera) or {}).get("video_prompt") or "")


def _load_video_sources(
    run_dir: Path, run_id: str, video: VideoClient | None, warnings: list[str]
) -> dict[int, dict[str, Any]]:
    """`image_source.json` → `scene_id` → 영상 입력 (ADR-0041).

    **못 읽는 이유는 전부 강등 사유이고, 하나도 조용히 넘기지 않는다.** 빈 dict를
    돌려주면 `mj_video` 씬이 전부 Ken Burns로 내려가고 씬마다 기록이 남는다.
    """
    if video is None:
        warnings.append(
            "영상 프로바이더가 없다 → motion=mj_video 씬을 전부 kenburns로 강등한다"
        )
        return {}

    path = run_dir / SOURCE_FILE
    if not path.exists():
        warnings.append(
            f"{SOURCE_FILE}이 없다 ({path}) → 영상 입력이 없어 전부 kenburns로 강등한다. "
            "[6]을 --force로 다시 돌리면 생긴다 (ADR-0036: 마이그레이션하지 않는다)"
        )
        return {}

    document = _load_json(path, f"영상 입력 계약({SOURCE_FILE})")
    errors = validate_image_source(document)
    if errors:
        warnings.append(
            f"{SOURCE_FILE}이 계약을 위반한다 ({errors[0]}) → 전부 kenburns로 강등한다"
        )
        return {}

    if document["run_id"] != run_id:
        # 계보는 run_id로 잇는다 (ADR-0017). 다른 run의 잡 id로 영상을 만들면
        # 실패가 아니라 **다른 편의 그림**이 나온다.
        warnings.append(
            f"{SOURCE_FILE}의 run_id({document['run_id']})가 대상 run({run_id})과 "
            "다르다 → 전부 kenburns로 강등한다"
        )
        return {}

    if document["provider"] != video.source_provider:
        warnings.append(
            f"{SOURCE_FILE}의 잡은 {document['provider']!r}가 만든 것인데 "
            f"{video.name}은 {video.source_provider!r}의 잡만 받는다 (ADR-0041) → "
            "호출 없이 전부 kenburns로 강등한다"
        )
        return {}

    return by_scene(document)


def _collect_info_images(run_dir: Path, scenes: list[dict[str, Any]]) -> dict[int, Path]:
    """`[6i]`의 산출물 — 씬별 INFO 이미지 (ADR-0043). **부재는 경고가 아니다** (D-3):
    `[6i]`가 강등한 씬은 파일이 없는 것이 정상이고, 그 씬은 일반 경로를 탄다.
    """
    info_dir = run_dir / INFO_DIR
    if not info_dir.is_dir():
        return {}
    found: dict[int, Path] = {}
    for scene in scenes:
        candidate = info_dir / f"{scene['scene_id']}.jpg"
        if candidate.exists():
            found[scene["scene_id"]] = candidate
    return found


def _plan_scene(
    scene: dict[str, Any],
    segment: Segment,
    image: Path,
    *,
    seen: dict[str, int],
    warnings: list[str],
    sources: dict[int, dict[str, Any]] | None = None,
    info_images: dict[int, Path] | None = None,
    has_info_video: bool = False,
) -> dict[str, Any]:
    """씬 하나를 어떻게 그릴지 정한다. 렌더는 하지 않는다 (검사 가능한 순수 판단).

    `seen`은 이미지 해시 → 지금까지 나온 횟수다. 씬 순서대로 채워지므로 앞 씬이
    원본 워크를 갖고 뒤 씬이 역방향을 받는다.
    """
    scene_id = scene["scene_id"]
    image_digest = _digest(image.read_bytes())
    occurrence = seen.get(image_digest, 0) + 1
    seen[image_digest] = occurrence

    camera = scene["camera"]
    used_camera = resolve_camera(camera, occurrence)
    if occurrence >= DUPLICATE_ALARM:
        warnings.append(
            f"씬 {scene_id}: 같은 그림이 {occurrence}번째다 ({image.name}). "
            "카메라 워크로 가릴 문제가 아니라 [6]이 그 씬들을 못 만든 것이다"
        )
    elif occurrence > 1:
        warnings.append(
            f"씬 {scene_id}: 앞 씬과 같은 그림이라 카메라를 "
            f"{camera} → {used_camera}로 뒤집었다 (스펙 03 역방향 워크)"
        )

    # 강등을 조용히 하지 않는다 (위 docstring "강등 사다리"). `motion_used`가
    # `motion`과 다르면 반드시 `demoted_from`이 찬다.
    # `motion`은 선택 필드다 — 비면 기본값이 영상이다 (ADR-0039 결정 1).
    motion = scene.get("motion") or vocab.default_motion(scene["beat"])
    motion_used = KENBURNS
    demoted_from = None
    source: dict[str, Any] | None = None
    info_image = (info_images or {}).get(scene_id)

    # 인포씬이 먼저다 (ADR-0043) — `info/{scene_id}.jpg`의 존재가 Veo 경로를 고른다.
    # 일반 영상 입력(source)도 함께 채워 둔다: Veo가 실패하면 그리로 내려간다.
    if info_image is not None and not has_info_video:
        demoted_from = VEO
        warnings.append(
            f"씬 {scene_id}: INFO 이미지가 있는데 Veo 프로바이더가 없다 → "
            "인포 없는 일반 영상으로 강등 (ADR-0043)"
        )
        info_image = None

    info_digest = _digest(info_image.read_bytes()) if info_image else None

    if info_image is not None:
        motion_used = VEO
        source = (sources or {}).get(scene_id)
        if segment.clip_length > VEO_DURATIONS[-1]:
            warnings.append(
                f"씬 {scene_id}: 클립 길이 {segment.clip_length:.2f}초가 Veo 상한 "
                f"{VEO_DURATIONS[-1]}초를 넘는다 — [9]의 trim은 늘리지 못한다"
            )
    elif motion == MJ_VIDEO:
        source = (sources or {}).get(scene_id)
        if source is not None:
            motion_used = MJ_VIDEO
        else:
            demoted_from = demoted_from or MJ_VIDEO
            warnings.append(
                f"씬 {scene_id}: motion=mj_video인데 {SOURCE_FILE}에 이 씬의 영상 "
                "입력이 없다 → kenburns로 강등 (ADR-0041)"
            )
    elif motion == KLING:
        demoted_from = KLING
        warnings.append(
            f"씬 {scene_id}: motion=kling인데 i2v 경로가 아직 없다 → kenburns로 강등"
        )

    if demoted_from == VEO and motion == MJ_VIDEO and (sources or {}).get(scene_id):
        # Veo 프로바이더 부재로 내려온 인포씬 — 일반 영상 입력이 있으면 그리로 간다.
        source = (sources or {}).get(scene_id)
        motion_used = MJ_VIDEO

    frames = frame_count(segment.clip_length)
    return {
        "scene_id": scene_id,
        "beat": scene["beat"],
        "motion": motion,
        "motion_used": motion_used,
        "demoted_from": demoted_from,
        "source_task_id": (source or {}).get("task_id"),
        "quadrant": (source or {}).get("quadrant"),
        "info_image": f"{INFO_DIR}/{info_image.name}" if info_image else None,
        "info_digest": info_digest,
        "video_prompt": (
            _video_prompt(used_camera) if (source or info_image) else None
        ),
        "camera": camera,
        "camera_used": used_camera,
        "camera_reversed": used_camera != camera,
        "image": f"{IMAGES_DIR}/{image.name}",
        "image_digest": image_digest,
        "occurrence": occurrence,
        "clip_length": segment.clip_length,
        "frames": frames,
        "file": f"{CLIPS_DIR}/{segment.clip_name}",
    }


def _plan_digest(plan: dict[str, Any]) -> str:
    """이어받기 판정용 지문. 이 값이 그대로면 같은 클립이 나온다.

    영상 입력을 함께 넣는 이유는 `[6r]`이다 — **사분면이 바뀌면 화면의 그림이 바뀌므로
    클립도 다시 만들어야 한다.** 그림 해시로는 못 잡는다: `images/{scene_id}`는 바뀌어도
    영상은 `task_id`+`quadrant`로 만들어지고, 반대로 잡이 바뀌어도 파일 해시만 보면
    같은 그림처럼 보일 수 있다.
    """
    parts = [
        plan["image_digest"],
        plan["camera_used"],
        str(plan["frames"]),
        str(CLIP_CRF),
        plan["motion_used"],
        str(plan.get("source_task_id") or ""),
        str(plan.get("quadrant") if plan.get("quadrant") is not None else ""),
        # INFO가 다시 만들어지면([6i] 재생성) 끝 프레임이 바뀌므로 클립도 다시 만든다.
        str(plan.get("info_digest") or ""),
        str(plan.get("video_prompt") or ""),
    ]
    return _digest("|".join(parts).encode())


def _render_video(
    plan: dict[str, Any],
    *,
    video: VideoClient,
    clip_path: Path,
    timeout: int | None,
    provider_down: threading.Event,
    warnings: list[str],
) -> dict[str, Any] | None:
    """영상 클립 하나. **실패하면 `None`을 돌려주고 호출부가 kenburns로 내려간다.**

    돌려주는 값이 `None`인 경우는 셋이고 **전부 경고를 남긴다** — 조용한 강등을
    만들지 않는 것이 이 분기의 존재 이유다 (ADR-0039).

    프로바이더 전체가 막힌 경우(`VideoProviderNotConfigured`)는 깃발을 세워 **남은 씬이
    시도조차 하지 않게** 한다. relax 영상이 Pro 미만에서 막히는 것이 그 경우이고, 같은
    오류로 27번 실패하는 것은 결과가 아니라 소음이다.
    """
    scene_id = plan["scene_id"]
    if provider_down.is_set():
        warnings.append(
            f"씬 {scene_id}: 영상 프로바이더가 이미 막혀 시도하지 않는다 → kenburns로 강등"
        )
        return None

    request = VideoRequest(
        scene_id=scene_id,
        source_task_id=str(plan["source_task_id"]),
        quadrant=int(plan.get("quadrant") or 0),
        motion_prompt=str(plan.get("video_prompt") or ""),
        duration=plan["clip_length"],
        label=f"{STAGE} 씬 {scene_id}",
    )

    try:
        clip = video.generate(request, timeout=timeout)
    except VideoProviderNotConfigured as exc:
        provider_down.set()
        warnings.append(
            f"씬 {scene_id}: 영상 프로바이더 전체를 쓸 수 없다 ({exc}) → "
            "남은 씬도 시도하지 않고 kenburns로 강등한다"
        )
        return None
    except VideoGenError as exc:
        warnings.append(
            f"씬 {scene_id}: 영상 생성 실패 ({exc}) → kenburns로 강등. "
            "재시도하지 않는다 — 재시도가 큐를 두 배로 만든다 (ADR-0035)"
        )
        return None

    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(clip.data)

    # 프로바이더가 길이를 밝히면 계약 길이와 대조한다. `[9]`는 `trim`으로 자를 뿐이라
    # **짧은 클립은 잘리지 않고 그대로 짧다** — 그만큼 타임라인이 당겨진다.
    declared = clip.duration
    if declared is not None and round(declared, 3) < round(plan["clip_length"], 3):
        warnings.append(
            f"씬 {scene_id}: 영상 클립이 {declared:.3f}초인데 계약 길이는 "
            f"{plan['clip_length']:.3f}초다. [9]의 trim은 늘리지 못한다"
        )

    return {
        **plan,
        "status": RENDERED,
        "camera_fallback": False,
        "attempts": VIDEO_ATTEMPTS,
        "errors": [],
        "engine": clip.meta,
        "digest": _plan_digest(plan),
    }


def _render_veo(
    plan: dict[str, Any],
    *,
    info_video: VideoClient,
    image: Path,
    run_dir: Path,
    clip_path: Path,
    timeout: int | None,
    provider_down: threading.Event,
    warnings: list[str],
) -> dict[str, Any] | None:
    """인포씬 클립 하나 (ADR-0043) — first=CLEAN, last=INFO. 실패하면 `None`.

    호출부가 사다리를 내려간다: `veo → mj_video → kenburns`. `_render_video`와 같은
    구조이고, 프로바이더 전체가 막히면(`VideoProviderNotConfigured`) 깃발을 세워 남은
    인포씬이 시도조차 하지 않게 한다.
    """
    scene_id = plan["scene_id"]
    if provider_down.is_set():
        warnings.append(
            f"씬 {scene_id}: Veo가 이미 막혀 시도하지 않는다 → 일반 경로로 강등"
        )
        return None

    request = VideoRequest(
        scene_id=scene_id,
        first_frame=image,
        last_frame=run_dir / plan["info_image"],
        motion_prompt=str(plan.get("video_prompt") or ""),
        duration=plan["clip_length"],
        label=f"{STAGE} 씬 {scene_id} info",
    )

    try:
        clip = info_video.generate(request, timeout=timeout)
    except VideoProviderNotConfigured as exc:
        provider_down.set()
        warnings.append(
            f"씬 {scene_id}: Veo 전체를 쓸 수 없다 ({exc}) → "
            "남은 인포씬도 시도하지 않고 일반 경로로 강등한다"
        )
        return None
    except VideoGenError as exc:
        warnings.append(
            f"씬 {scene_id}: 인포씬 영상 실패 ({exc}) → 일반 경로로 강등. "
            "재시도하지 않는다 (ADR-0035)"
        )
        return None

    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(clip.data)

    declared = clip.duration
    if declared is not None and round(declared, 3) < round(plan["clip_length"], 3):
        warnings.append(
            f"씬 {scene_id}: 인포씬 클립이 {declared:.3f}초인데 계약 길이는 "
            f"{plan['clip_length']:.3f}초다. [9]의 trim은 늘리지 못한다"
        )

    return {
        **plan,
        "status": RENDERED,
        "camera_fallback": False,
        "attempts": VIDEO_ATTEMPTS,
        "errors": [],
        "engine": clip.meta,
        "digest": _plan_digest(plan),
    }


def _render_kenburns(
    plan: dict[str, Any],
    *,
    image: Path,
    clip_path: Path,
    run_dir: Path,
    ffmpeg: str,
    runner: Any,
    timeout: int,
) -> dict[str, Any]:
    """FFmpeg zoompan 클립 하나. 실패하면 `static`으로 한 번 더 내려간다 (강등 사다리).

    계약된 워크가 이미 `static`이면 사다리에 아래가 없다 — 같은 명령을 두 번 돌리지
    않는다.
    """
    ladder = [plan["camera_used"]]
    if FALLBACK_CAMERA not in ladder:
        ladder.append(FALLBACK_CAMERA)

    errors: list[str] = []
    for attempts, camera in enumerate(ladder, start=1):
        cmd = build_command(
            relative_path(image, run_dir),
            relative_path(clip_path, run_dir),
            camera=camera,
            frames=plan["frames"],
            executable=ffmpeg,
        )
        try:
            run_ffmpeg(
                cmd, cwd=run_dir, produces=clip_path, runner=runner, timeout=timeout
            )
        except (FFmpegError, KenBurnsError) as exc:
            errors.append(f"{camera}: {exc}")
            log.warning(
                "[%s] 씬 %s 렌더 실패 (%s): %s", STAGE, plan["scene_id"], camera, exc
            )
            continue

        rendered = {**plan, "camera_used": camera}
        return {
            **rendered,
            "status": RENDERED,
            #: 역방향 워크가 걸렸는가 (스펙 03)와 사다리를 내려갔는가는 다른 일이다.
            "camera_fallback": camera != plan["camera_used"],
            "attempts": attempts,
            "errors": errors,
            "engine": None,
            "digest": _plan_digest(rendered),
        }

    return {
        **plan,
        "status": FAILED,
        "camera_fallback": False,
        "attempts": len(ladder),
        "errors": errors,
        "engine": None,
        "digest": None,
    }


def _render_scene(
    plan: dict[str, Any],
    *,
    image: Path,
    clip_path: Path,
    run_dir: Path,
    ffmpeg: str,
    runner: Any,
    timeout: int,
    video: VideoClient | None = None,
    video_timeout: int | None = None,
    provider_down: threading.Event | None = None,
    warnings: list[str] | None = None,
    info_video: VideoClient | None = None,
    info_provider_down: threading.Event | None = None,
) -> dict[str, Any]:
    """씬 하나를 `motion_used`가 가리키는 경로로 만든다. 실패하면 사다리를 내려간다.

    사다리: `veo → mj_video → kenburns → static` (ADR-0043·0025 §3). 아래로만 간다.
    """
    if plan["motion_used"] == VEO and info_video is not None:
        record = _render_veo(
            plan,
            info_video=info_video,
            image=image,
            run_dir=run_dir,
            clip_path=clip_path,
            timeout=video_timeout,
            provider_down=info_provider_down or threading.Event(),
            warnings=warnings if warnings is not None else [],
        )
        if record is not None:
            return record
        # 사다리 한 칸 아래 (ADR-0043) — 일반 영상 입력이 있으면 mj_video, 없으면 kenburns.
        next_step = MJ_VIDEO if (plan.get("source_task_id") and video is not None) else KENBURNS
        plan = {**plan, "motion_used": next_step, "demoted_from": VEO}

    if plan["motion_used"] == MJ_VIDEO and video is not None:
        record = _render_video(
            plan,
            video=video,
            clip_path=clip_path,
            timeout=video_timeout,
            provider_down=provider_down or threading.Event(),
            warnings=warnings if warnings is not None else [],
        )
        if record is not None:
            return record
        # 사다리 한 칸 아래. 이미 위에서 내려온 씬이면 첫 강등 지점을 남긴다.
        plan = {
            **plan,
            "motion_used": KENBURNS,
            "demoted_from": plan.get("demoted_from") or MJ_VIDEO,
        }

    return _render_kenburns(
        plan,
        image=image,
        clip_path=clip_path,
        run_dir=run_dir,
        ffmpeg=ffmpeg,
        runner=runner,
        timeout=timeout,
    )


def _resolve_workers(
    video: VideoClient | None,
    jobs: int | None,
    *,
    pending: list[tuple[dict[str, Any], Path, Path]],
    info_video: VideoClient | None = None,
) -> int:
    """이번 실행에 쓸 워커 수. **하드코딩하지 않는다** (ADR-0031 G3와 같은 계약).

    영상 씬이 없으면 1이다 — Ken Burns는 로컬 인코딩이라 동시에 돌릴 이유가 프로바이더
    대기가 아니라 CPU뿐이고, 그것은 이 단계가 정할 값이 아니다.
    """
    if jobs is not None:
        if jobs < 1:
            raise MotionStageError(f"--jobs는 1 이상이어야 한다: {jobs}")
        return max(1, min(jobs, len(pending) or 1))
    caps: list[int] = []
    if video is not None and any(p["motion_used"] == MJ_VIDEO for p, _, _ in pending):
        caps.append(video.concurrency())
    if info_video is not None and any(p["motion_used"] == VEO for p, _, _ in pending):
        caps.append(info_video.concurrency())
    if not caps:
        return 1
    return max(1, min(max(caps), len(pending) or 1))


def _load_previous(record_path: Path) -> dict[int, dict[str, Any]]:
    if not record_path.exists():
        return {}
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        return {int(entry["scene_id"]): entry for entry in data.get("scenes", [])}
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        log.warning("[%s] %s를 읽을 수 없어 이어받기 없이 진행한다", STAGE, record_path)
        return {}


def _record_document(
    document: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    warnings: list[str],
    video: VideoClient | None = None,
    info_video: VideoClient | None = None,
) -> dict[str, Any]:
    """clips.json — `[7]`의 실행 기록. 계약이 아니라 기록이라 스키마로 조이지 않는다."""
    return {
        "run_id": document["run_id"],
        "topic": document["topic"],
        "source_scenes": TIMED_SCENES_FILE,
        "video": (
            None
            if video is None
            else {"name": video.name, "source_provider": video.source_provider}
        ),
        "info_video": None if info_video is None else {"name": info_video.name},
        "kenburns": {"travel": TRAVEL, "crf": CLIP_CRF},
        "scenes": records,
        "warnings": warnings,
    }


def run_motion_stage(
    run_id: str,
    *,
    paths: Paths | None = None,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner: Any = subprocess.run,
    timeout: int = TIMEOUT,
    video: VideoClient | None = None,
    video_timeout: int | None = None,
    jobs: int | None = None,
    info_video: VideoClient | None = None,
) -> MotionResult:
    """씬마다 클립 하나. `video`가 없으면 전 씬이 Ken Burns로 돈다 (D-3).

    `info_video`는 인포씬 전용 프로바이더다 (ADR-0043, Veo) — `info/{scene_id}.jpg`가
    있는 씬만 탄다. 없으면 그 씬들은 일반 경로로 강등되고 경고가 남는다.

    `video_timeout=None`은 "프로바이더가 정한다"다 (ADR-0035) — 단계가 숫자를 선언하면
    어댑터가 선언한 값이 죽고, 짧게 잡힌 상한은 이미 성공한 잡을 버린다.
    `timeout`은 그것과 다른 값이다: FFmpeg 프로세스 하나의 상한이다.
    """
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    document, scene_warnings = _load_timed_scenes(run_dir / TIMED_SCENES_FILE)
    if document["run_id"] != run_id:
        # 계보는 run_id로 잇는다 (ADR-0017). 다른 대본의 시각으로 클립을 자르면
        # 길이가 어긋난 클립이 조용히 나온다.
        raise MotionStageError(
            f"{TIMED_SCENES_FILE}의 run_id({document['run_id']})가 "
            f"대상 run({run_id})과 다르다"
        )

    scenes: list[dict[str, Any]] = document["scenes"]
    topic = document["topic"]
    clips_dir = run_dir / CLIPS_DIR
    record_path = run_dir / RECORD_FILE

    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    result = MotionResult(
        run_id=run_id, topic=topic, run_dir=run_dir, clips_dir=clips_dir
    )

    if state.is_done(STAGE) and not force and record_path.exists():
        previous = _load_previous(record_path)
        if previous and all(
            entry.get("status") in REUSABLE
            and entry.get("file")
            and (run_dir / entry["file"]).exists()
            for entry in previous.values()
        ):
            log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
            result.skipped = True
            result.record_path = record_path
            result.scenes = [previous[k] for k in sorted(previous)]
            return result

    try:
        timeline = build_timeline(scenes)
    except TimelineError as exc:
        state.mark_failed(STAGE, str(exc))
        raise MotionStageError(str(exc)) from exc

    images = _collect_images(run_dir / IMAGES_DIR, scenes)

    state.mark_running(STAGE)
    clips_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = list(scene_warnings)
    previous = {} if force else _load_previous(record_path)
    sources = _load_video_sources(run_dir, run_id, video, warnings)
    info_images = _collect_info_images(run_dir, scenes)
    if info_images and info_video is None:
        warnings.append(
            f"INFO 이미지가 {len(info_images)}씬에 있는데 Veo 프로바이더가 없다 → "
            "인포 없는 일반 영상으로 강등한다 (ADR-0043)"
        )
    seen: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    #: `records`와 `clips.json` 쓰기를 지키는 자물쇠. 기록 갱신은 직렬이다 —
    #: 씬 하나가 끝날 때마다 쓴다는 목적이 재실행 때 다시 만들지 않는 것이므로
    #: 병렬이 그것을 깨면 안 된다 (ADR-0031 §4가 `[6]`에 대해 정한 것과 같다).
    record_lock = threading.Lock()

    def flush() -> None:
        """씬 하나가 끝날 때마다 기록을 갱신한다.

        아직 처리하지 않은 씬은 **지난 기록을 그대로 실어 둔다.** 3번 씬에서 죽었을 때
        처리한 것만 쓰면 4번 이후의 지문이 사라지고, 클립은 디스크에 멀쩡히 있는데
        다음 실행이 전부 다시 만든다 (`[6]`과 같은 이유, 여기선 돈이 아니라 시간이다).
        """
        done = {record["scene_id"]: record for record in records}
        merged = [
            entry
            for scene in scenes
            if (entry := done.get(scene["scene_id"]) or previous.get(scene["scene_id"]))
            is not None
        ]
        write_text(
            record_path,
            dump_json(
                _record_document(
                    document, merged, warnings=warnings, video=video,
                    info_video=info_video,
                )
            ),
        )

    def commit(record: dict[str, Any]) -> None:
        """씬 하나의 결과를 기록에 넣고 파일로 내린다. 병렬 경로의 유일한 쓰기 지점이다."""
        with record_lock:
            records.append(record)
            flush()

    # 계획은 **순서대로** 짠다. 역방향 워크가 "앞 씬이 원본, 뒤 씬이 역방향"이라
    # `seen`이 씬 순서대로 차야 한다 (스펙 03). 렌더만 동시에 돈다.
    pending: list[tuple[dict[str, Any], Path, Path]] = []
    for scene, segment in zip(scenes, timeline.segments):
        scene_id = scene["scene_id"]
        image = images[scene_id]
        try:
            plan = _plan_scene(
                scene, segment, image, seen=seen, warnings=warnings, sources=sources,
                info_images=info_images, has_info_video=info_video is not None,
            )
        except KenBurnsError as exc:
            state.mark_failed(STAGE, str(exc))
            flush()
            raise MotionStageError(f"씬 {scene_id}: {exc}") from exc

        clip_path = clips_dir / segment.clip_name
        prior = previous.get(scene_id)
        if (
            prior is not None
            and prior.get("status") in REUSABLE
            and prior.get("digest") == _plan_digest(plan)
            and clip_path.exists()
        ):
            records.append({**prior, "status": CACHED, "attempts": 0, "errors": []})
            continue
        pending.append((plan, image, clip_path))
    flush()

    workers = _resolve_workers(video, jobs, pending=pending, info_video=info_video)
    result.workers = workers
    provider_down = threading.Event()
    info_provider_down = threading.Event()

    def render(item: tuple[dict[str, Any], Path, Path]) -> dict[str, Any]:
        plan, image, clip_path = item
        return _render_scene(
            plan,
            image=image,
            clip_path=clip_path,
            run_dir=run_dir,
            ffmpeg=ffmpeg,
            runner=runner,
            timeout=timeout,
            video=video,
            video_timeout=video_timeout,
            provider_down=provider_down,
            warnings=warnings,
            info_video=info_video,
            info_provider_down=info_provider_down,
        )

    if workers == 1:
        for item in pending:
            commit(render(item))
    else:
        log.info(
            "[%s] 씬 %d개를 워커 %d개로 동시에 만든다 (ADR-0039 §7)",
            STAGE, len(pending), workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(render, item) for item in pending]
            for future in futures:
                commit(future.result())

    # 완료 순서가 아니라 씬 순서로 남긴다 — 리포트를 눈으로 비교할 수 있어야 한다.
    with record_lock:
        records.sort(key=lambda r: r["scene_id"])
        flush()

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result.record_path = record_path
    result.scenes = records
    result.warnings = warnings

    info: dict[str, Any] = {
        "scene_count": len(records),
        "rendered": result.count(RENDERED),
        "cached": result.count(CACHED),
        "failed": result.count(FAILED),
        "video": result.video_count,
        "info_video": result.veo_count,
        "demoted": result.demoted,
        "workers": workers,
        "camera_reversed": result.reversed_count,
        "warnings": warnings,
        "outputs": [
            record_path.relative_to(paths.root).as_posix(),
            f"{run_dir.relative_to(paths.root).as_posix()}/{CLIPS_DIR}/",
        ],
    }

    if not result.passed:
        failed = [r["scene_id"] for r in records if r["status"] == FAILED]
        message = (
            f"클립을 만들지 못한 씬이 {len(failed)}개다 ({failed[:8]}). "
            f"[9]는 씬마다 클립 하나를 요구한다"
        )
        state.mark_failed(STAGE, message, **info)
        raise MotionStageError(message)

    state.mark_done(STAGE, **info)
    return result
