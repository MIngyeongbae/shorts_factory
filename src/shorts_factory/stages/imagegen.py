"""[6. imagegen] — 씬별 이미지 프롬프트 → 베이스 이미지 한 장씩.

specs/05-pipeline.md:
    [6. imagegen] → images/{scene_id}.jpg  (확장자는 프로바이더가 정한다 — ADR-0021)
    "씬당 1회 재시도. 2회 실패 시 인접 씬 이미지 재사용(카메라 워크만 변경)으로
     폴백하고 리포트에 기록. 편당 베이스 호출 = 씬 수 (2-pass 없음, ADR-0019)."

## 입력은 prompts.json 하나다 (ADR-0020)

`[5]`가 쓴 `runs/{run_id}/prompts.json`에서 `prompt`·`negative_prompt`·`style`만 읽는다.

- `scenes.timed.json`을 읽지 않는다. 시간은 `[7]`·`[9]`가 쓰는 값이고 이미지 한 장에
  시각은 없다. `prompts.json`에 시간 정보가 없는 것도 그래서다
- `overlays`를 읽지 않는다. 전부 레이어 B라 `[8. overlay]` 소관이다 (ADR-0019).
  베이스 이미지는 전 씬 클린이다
- **`framing`이 같다고 캐시하지 않는다.** 구도만 같을 뿐 그 씬의 `subject`는 다르다
  (ADR-0020). 씬 수 = 호출 수다

## 실패해도 파이프라인은 계속 간다 (specs/05 실패 정책)

씬당 최대 2회 호출(생성 1 + 재시도 1). 그래도 안 되면 **인접 씬의 성공한 이미지를
복사**해 그 자리를 메우고 기록에 남긴다. 단계 전체를 멈추는 경우는 둘뿐이다.

- 프로바이더 자체가 못 쓰는 상태 (`ProviderNotConfigured`) — 씬마다 같은 오류로 두 번씩
  실패하며 27씬을 도는 것은 결과가 아니라 소음이다
- 성공한 씬이 하나도 없어 복사해 올 곳이 없을 때

## 스타일 앵커가 0장일 때 (ADR-0005)

지금 `assets/style_anchors/`에는 README뿐이다. 룩 일관성 수단 없이 편당 ~$2.5를 쓰면
씬 간 룩이 갈리고 어차피 다시 만들게 된다. 그래서 **과금되는 프로바이더는 앵커 0장이면
호출 전에 막는다**(`state.json`에 blocked 기록). 페이크는 그냥 돈다. 막힌 것을 알고도
돌리려면 `--allow-missing-anchors`로 명시한다 — 경고만 남기고 진행한다.

## 씬을 동시에 제출한다 (ADR-0031 §4)

relax는 제출한 뒤 대부분 **기다리는 시간**이라 한 줄로 세울 이유가 없다. 27씬을 순차로
돌면 잡 하나가 100초여도 45분이 된다 (실측).

- 워커 수는 **하드코딩하지 않는다.** `--jobs`가 없으면 프로바이더에게 묻고
  (`ImageClient.concurrency()`), MJ는 계정의 `relaxCoreSize`를 읽어 준다. 그 값은 구독
  플랜이 정하므로 리포에 적어 두면 플랜을 바꾼 날 조용히 틀린다 (ADR-0031 G3)
- **기록 갱신은 직렬이다.** 씬 하나가 끝날 때마다 쓴다는 계약(ADR-0020)이 지키려는 것이
  "죽어도 다시 사지 않는다"이므로, 병렬이 그것을 깨면 병렬로 번 시간보다 비싸다
- 429가 나오면 워커를 1로 줄이고 쉰다. 한도에 걸린 채로 3개를 계속 던지는 것은 큐만 늘린다
- **이어받는 씬은 워커를 쓰지 않는다.** 호출이 아니라 파일 확인이다

## 대기 상한도 선언하지 않는다 (ADR-0035)

워커 수와 같은 종류의 값이다 — **잡 하나를 얼마나 기다릴지는 relax 큐가 정하고, 그것을
아는 것은 프로바이더뿐이다.** 그래서 이 단계에 숫자가 없고 `timeout=None`이 기본이다.
`ImageClient.generate(timeout=None)`은 "프로바이더가 정한다"이고, MJ 어댑터는 자기
`DEFAULT_TIMEOUT`을 쓴다.

단계가 숫자를 선언하면 두 가지가 같이 일어난다. 어댑터가 이유까지 적어 둔 값이 죽고,
짧게 잡힌 상한이 **이미 성공한 잡을 버린 뒤 재시도로 큐를 두 배로 만든다.** 실측
(2026-08-19): 상한 180초로 4씬을 돌려 전부 `failed`로 기록했는데 프록시에서는 같은
태스크가 전부 `SUCCESS` 100%였다. 같은 날 relax 잡 소요는 241.8·418.3·552.1초였고
닷새 전 실측은 평균 95.6초였다 — **편차 5배를 상수 하나로 맞출 수 없다.**

사람이 정해야 하면 `--timeout`이 이긴다 (`--jobs`가 `concurrency()`를 이기는 것과 같다).

## 돈이 드는 단계다

- 이미 만든 이미지는 다시 사지 않는다. 씬별로 요청 지문(`digest`)을 기록해 두고,
  프롬프트가 그대로면 이어받는다 (`cached`)
- 기록(`images.json`)은 **씬 하나가 끝날 때마다** 갱신한다. 중간에 죽어도 앞서 산
  이미지를 다시 사지 않게 하려는 것이다
- 폴백으로 때운 씬은 다음 실행에서 다시 시도한다. 복사본은 결과가 아니라 임시 땜질이다
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import Paths, write_text
from ..imagegen.base import (
    ImageClient,
    ImageGenError,
    ImageGenRateLimited,
    ImageRequest,
    ProviderNotConfigured,
    discover_style_anchors,
)
from ..jsonio import dump_json
from ..runstate import RunState
from ..schemas.visual_rules import schema_errors

log = logging.getLogger(__name__)

STAGE = "6-imagegen"

#: 입력 — [5]의 산출물 (ADR-0020: 씬별 이미지 지시의 유일한 출처)
PROMPTS_FILE = "prompts.json"

#: 산출물 — specs/05가 못박은 경로
IMAGES_DIR = "images"

#: 이 단계의 **실행 기록**. `timing.json`이 `[3]`에 대해 갖는 역할과 같다 (ADR-0020) —
#: 하류가 판단 근거로 읽는 계약이 아니라 무슨 일이 있었는지의 기록이고, `[11. report]`가
#: "실패 씬, 재시도 이력"을 여기서 읽는다. `[7]`은 이 파일 없이 `images/{scene_id}.jpg`
#: 규약만으로 돈다.
RECORD_FILE = "images.json"

#: 1부↔2부 경계면 파일 (ADR-0017). run_id를 슬러그로 찾을 때만 연다.
SCRIPT_FILE = "06-script.json"

#: specs/05 "씬당 1회 재시도" = 최초 1회 + 재시도 1회
MAX_ATTEMPTS = 2

#: 429를 만났을 때 쉬는 시간(초). 워커를 1로 줄이는 것과 짝이다 (ADR-0031 §4) — 줄이기만
#: 하고 안 쉬면 한도에 걸린 상태로 계속 두드리는 것은 그대로다.
RATE_LIMIT_BACKOFF = 60

#: 씬 하나의 결과 상태.
GENERATED = "generated"  # 이번 실행에서 만들었다 (과금)
CACHED = "cached"  # 앞선 실행 결과를 그대로 쓴다 (과금 없음)
FALLBACK = "fallback"  # 2회 실패 → 인접 씬 이미지 복사 (specs/05)
FAILED = "failed"  # 2회 실패 + 복사해 올 곳도 없다

#: 다음 실행에서 다시 사지 않아도 되는 상태.
REUSABLE = (GENERATED, CACHED)


class ImagegenStageError(Exception):
    pass


class StyleAnchorsMissing(ImagegenStageError):
    """스타일 앵커 0장 + 과금 프로바이더 → 호출 전에 막았다 (ADR-0005).

    실패가 아니라 진입 금지다. 앵커를 넣거나 `allow_missing_anchors`로 명시하면 풀린다.
    """


class DialectMismatch(ImagegenStageError):
    """`prompts.json`의 방언과 프로바이더가 다르다 → 호출 전에 막았다 (ADR-0027).

    이것도 실패가 아니라 진입 금지다. 고치는 방법은 `[5]`를 맞는 방언으로 다시 돌리는
    것 하나뿐이고, 무료다. **조용히 진행하면 MJ 문법 문자열이 NB2에 그대로 들어가
    `--ar 9:16`이 그릴 대상이 되고, 그 사실은 편당 과금이 끝난 뒤에 드러난다.**
    """


@dataclass
class ImagegenResult:
    run_id: str
    topic: str
    run_dir: Path
    images_dir: Path
    record_path: Path | None = None
    scenes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    blocked: bool = False
    #: 실제로 쓴 워커 수 (ADR-0031 §4). 1이면 순차다.
    workers: int = 1

    def count(self, status: str) -> int:
        return sum(1 for s in self.scenes if s["status"] == status)

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def calls(self) -> int:
        """**이번 실행에서** 나간 생성 호출 수 = 과금 단위. `cached` 씬은 0이다.

        스킵한 실행은 0이다 — 기록에 남은 지난 호출 수를 이번 비용처럼 보고하지 않는다.
        """
        if self.skipped:
            return 0
        return sum(s["attempts"] for s in self.scenes)

    @property
    def passed(self) -> bool:
        """모든 씬이 쓸 이미지를 갖췄는가. 폴백도 자리는 채운 것으로 친다."""
        return bool(self.scenes) and self.count(FAILED) == 0

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        if self.workers > 1 and not self.skipped:
            tail += f" · 워커 {self.workers}"
        parts = [
            f"생성 {self.count(GENERATED)}",
            f"이어받기 {self.count(CACHED)}",
            f"폴백 {self.count(FALLBACK)}",
            f"실패 {self.count(FAILED)}",
        ]
        return (
            f"[6] {self.topic} — {self.scene_count}씬 ({' · '.join(parts)}) / "
            f"호출 {self.calls}회 → {IMAGES_DIR}/{tail}"
        )


def resolve_run_id(paths: Paths, slug: str) -> str:
    """슬러그 → run_id. 경계면 파일에 적힌 값을 그대로 쓴다 (ADR-0017 "계보는 run_id").

    `runs/{run_id}/topic.json`(1부 계약)을 뒤지지 않는다. 2부는 `06-script.json` 말고
    1부 산출물에 의존하지 않는다.
    """
    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise ImagegenStageError(
            f"대본이 없다: {script_path}. run_id를 알면 --run-id로 바로 줄 수 있다."
        )
    try:
        run_id = json.loads(script_path.read_text(encoding="utf-8"))["run_id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImagegenStageError(
            f"{script_path}에서 run_id를 읽을 수 없다: {exc}"
        ) from exc
    return run_id


def _load_prompts(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ImagegenStageError(
            f"{PROMPTS_FILE}이 없다: {path}. [5. prompt]를 먼저 실행하라."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImagegenStageError(f"{PROMPTS_FILE}을(를) 읽을 수 없다: {path} — {exc}") from exc

    errors = schema_errors(data)
    if errors:
        # 깨진 지시로 만든 이미지는 돈만 쓰고 버린다. 고치는 자리는 [5]다.
        listed = "; ".join(errors[:5])
        raise ImagegenStageError(f"{path}이(가) {PROMPTS_FILE} 계약을 어겼다: {listed}")

    ids = [scene["scene_id"] for scene in data["scenes"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        # 파일 이름이 scene_id라 겹치면 서로를 덮어쓴다.
        raise ImagegenStageError(
            f"{path}에 scene_id가 겹친다: {duplicates}. 씬 하나에 파일 하나다"
        )
    return data


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _load_previous(record_path: Path) -> dict[int, dict[str, Any]]:
    """앞선 실행의 씬별 기록. 없거나 깨졌으면 빈 채로 시작한다(다시 사는 쪽이 안전하다)."""
    if not record_path.exists():
        return {}
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        return {int(entry["scene_id"]): entry for entry in data.get("scenes", [])}
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        log.warning("[%s] %s를 읽을 수 없어 이어받기 없이 진행한다", STAGE, record_path)
        return {}


def _nearest_source(scene_id: int, available: list[int]) -> int | None:
    """폴백에 쓸 인접 씬. 가까운 쪽, 같은 거리면 앞 씬을 고른다.

    앞을 먼저 보는 것은 이야기 순서 때문이다 — 뒤 씬은 아직 나오지 않은 것을 보여준다.
    첫 씬이 실패하면 앞이 없으니 자연히 뒤에서 가져온다.
    """
    if not available:
        return None
    return min(available, key=lambda cand: (abs(cand - scene_id), cand > scene_id, cand))


def _resolve_workers(
    client: ImageClient, jobs: int | None, *, pending_count: int
) -> int:
    """이번 실행에 쓸 워커 수.

    **하드코딩하지 않는다** (specs/05, ADR-0031 G3). `--jobs`가 있으면 사람 말이 이기고,
    없으면 프로바이더에게 묻는다 — MJ는 계정의 `relaxCoreSize`를 읽어 준다.

    살 씬보다 많은 워커는 만들지 않는다. 스레드가 놀 뿐이지만, 실행 기록의 `workers`가
    실제로 돈 수와 달라지면 나중에 실측을 그 숫자로 읽게 된다.
    """
    if jobs is not None:
        if jobs < 1:
            raise ImagegenStageError(f"--jobs는 1 이상이어야 한다: {jobs}")
        workers = jobs
    else:
        workers = client.concurrency()
    return max(1, min(workers, pending_count or 1))


@contextmanager
def _no_gate() -> Iterator[None]:
    """게이트 없이 도는 경로(워커 1). `with` 자리를 비워 두지 않으려는 것뿐이다."""
    yield


class _Gate:
    """동시에 도는 씬 수를 제한하고, 429가 나면 1로 줄인다 (ADR-0031 §4).

    `ThreadPoolExecutor`의 `max_workers`로는 이걸 못 한다 — 풀을 만든 뒤에는 바꿀 수
    없기 때문이다. 그래서 스레드는 풀이 주고 **몇 개가 동시에 프로바이더를 두드릴지는
    이 게이트가 정한다.**

    줄이기만 하고 되돌리지 않는다. 한도에 걸린 계정을 다시 떠보는 것은 이 단계가 할 일이
    아니고, 27씬을 순차로 도는 것은 느릴 뿐 안전하다.
    """

    def __init__(self, limit: int, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._cv = threading.Condition()
        self._limit = max(1, limit)
        self._active = 0
        self._sleep = sleep
        self.reduced = False

    @property
    def limit(self) -> int:
        with self._cv:
            return self._limit

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._cv:
            while self._active >= self._limit:
                self._cv.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._cv:
                self._active -= 1
                self._cv.notify()

    def rate_limited(self) -> None:
        """429를 만났다. 워커를 1로 줄이고 백오프한다."""
        with self._cv:
            first = self._limit > 1
            self._limit = 1
            if first:
                self.reduced = True
        if first:
            log.warning(
                "[%s] 한도에 걸렸다(429). 워커를 1로 줄이고 %d초 쉰다",
                STAGE, RATE_LIMIT_BACKOFF,
            )
        self._sleep(RATE_LIMIT_BACKOFF)


def _generate_scene(
    client: ImageClient,
    request: ImageRequest,
    image_path: Path,
    *,
    timeout: int | None,
    gate: _Gate | None = None,
) -> dict[str, Any]:
    """씬 하나 — 최대 `MAX_ATTEMPTS`회 호출. specs/05 "씬당 1회 재시도".

    `gate`를 주면 호출이 게이트 안에서 일어난다. 슬롯을 **호출마다** 잡았다 놓는 이유는
    429로 한도가 1로 줄었을 때 재시도가 그 축소를 따르게 하려는 것이다.
    """
    errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with gate.slot() if gate is not None else _no_gate():
                image = client.generate(request, timeout=timeout)
        except ProviderNotConfigured:
            # 씬의 문제가 아니다. 재시도도 폴백도 의미가 없어 위로 올린다.
            raise
        except ImageGenRateLimited as exc:
            errors.append(f"{attempt}회: {exc}")
            log.warning(
                "[%s] 씬 %d 한도 도달 (%d/%d): %s",
                STAGE, request.scene_id, attempt, MAX_ATTEMPTS, exc,
            )
            if gate is not None:
                gate.rate_limited()
            continue
        except ImageGenError as exc:
            errors.append(f"{attempt}회: {exc}")
            log.warning(
                "[%s] 씬 %d 생성 실패 (%d/%d): %s",
                STAGE, request.scene_id, attempt, MAX_ATTEMPTS, exc,
            )
            continue

        _write_bytes(image_path, image.data)
        return {
            "scene_id": request.scene_id,
            "status": GENERATED,
            "file": f"{IMAGES_DIR}/{image_path.name}",
            "digest": request.digest,
            "attempts": attempt,
            "errors": errors,
            "engine": image.meta,
        }

    return {
        "scene_id": request.scene_id,
        "status": FAILED,
        "file": None,
        "digest": request.digest,
        "attempts": MAX_ATTEMPTS,
        "errors": errors,
        "engine": None,
    }


def _apply_fallbacks(
    records: list[dict[str, Any]], images_dir: Path, warnings: list[str],
    *, suffix: str,
) -> None:
    """2회 실패한 씬을 인접 씬 이미지로 메운다 (specs/05).

    카메라 워크는 여기서 바꿀 수 없다 — `camera`는 씬 계약의 값이고 그 출처는
    `06-script.json` 하나다 (ADR-0020). 대신 `camera_variation_required`로 기록해
    `[7. motion]`이 같은 그림을 같은 움직임으로 두 번 쓰지 않도록 남긴다.
    """
    usable = [r["scene_id"] for r in records if r["status"] in REUSABLE]
    for record in records:
        if record["status"] != FAILED:
            continue
        source_id = _nearest_source(record["scene_id"], usable)
        if source_id is None:
            warnings.append(
                f"씬 {record['scene_id']}: {MAX_ATTEMPTS}회 실패했고 복사해 올 인접 씬도 없다"
            )
            continue

        source = images_dir / f"{source_id}{suffix}"
        target = images_dir / f"{record['scene_id']}{suffix}"
        _write_bytes(target, source.read_bytes())
        record.update(
            status=FALLBACK,
            file=f"{IMAGES_DIR}/{target.name}",
            reused_from=source_id,
            camera_variation_required=True,
        )
        warnings.append(
            f"씬 {record['scene_id']}: {MAX_ATTEMPTS}회 실패 → 씬 {source_id} 이미지로 폴백. "
            "[7]에서 카메라 워크를 달리해야 한다 (specs/05)"
        )


def _merge_records(
    prompts: dict[str, Any],
    records: list[dict[str, Any]],
    previous: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """이번 실행의 결과를 앞선 실행의 기록 위에 얹는다. 순서는 `prompts.json` 순이다."""
    done = {record["scene_id"]: record for record in records}
    merged = []
    for scene in prompts["scenes"]:
        entry = done.get(scene["scene_id"]) or previous.get(scene["scene_id"])
        if entry is not None:
            merged.append(entry)
    return merged


def _record_document(
    prompts: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    client: ImageClient,
    anchors: tuple[Path, ...],
    anchor_dir: str,
    warnings: list[str],
) -> dict[str, Any]:
    """images.json — `[6]`의 실행 기록. `[11. report]`가 읽는다 (ADR-0020의 timing.json과 같은 성격).

    계약이 아니라 기록이라 스키마로 조이지 않는다. 하류 단계가 이걸 판단 근거로 읽기
    시작하면 그 순간 계약이고, 그때는 스키마와 ADR이 필요하다.
    """
    return {
        "run_id": prompts["run_id"],
        "topic": prompts["topic"],
        "source_prompts": PROMPTS_FILE,
        "provider": {
            "name": client.name,
            "requires_style_anchors": client.requires_style_anchors,
        },
        "style_anchors": {
            "dir": anchor_dir,
            "count": len(anchors),
            "files": [p.name for p in anchors],
        },
        "max_attempts": MAX_ATTEMPTS,
        "calls": sum(r["attempts"] for r in records),
        "scenes": records,
        "warnings": warnings,
    }


def run_imagegen_stage(
    *,
    images: ImageClient,
    run_id: str | None = None,
    slug: str | None = None,
    paths: Paths | None = None,
    force: bool = False,
    allow_missing_anchors: bool = False,
    timeout: int | None = None,
    jobs: int | None = None,
) -> ImagegenResult:
    paths = paths or Paths.from_env()

    if not run_id:
        if not slug:
            raise ImagegenStageError("run_id나 slug 중 하나는 있어야 한다")
        run_id = resolve_run_id(paths, slug)

    run_dir = paths.run_dir(run_id)
    prompts_path = run_dir / PROMPTS_FILE
    prompts = _load_prompts(prompts_path)

    topic = prompts["topic"]
    style = prompts["style"]
    images_dir = run_dir / IMAGES_DIR
    record_path = run_dir / RECORD_FILE

    # slug는 편의 인자라 없을 수 있다. state.json에 slug: null을 심지 않는다.
    seed: dict[str, Any] = {"topic": topic}
    if slug:
        seed["slug"] = slug
    state = RunState.load_or_create(run_dir, run_id, **seed)
    result = ImagegenResult(
        run_id=run_id, topic=topic, run_dir=run_dir, images_dir=images_dir
    )

    if state.is_done(STAGE) and not force and record_path.exists():
        previous = _load_previous(record_path)
        # 폴백으로 때운 씬이 남아 있으면 스킵하지 않는다. 복사본은 결과가 아니라
        # 임시 땜질이라 다음 실행에서 다시 시도한다 — 성공한 씬은 아래 이어받기
        # 경로가 걸러 주므로 다시 사지 않는다.
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

    # ADR-0027 — 방언 대조. 앵커 검사보다 먼저 본다: 방언이 틀렸으면 앵커가 몇 장이든
    # 이 파일로는 아무것도 만들 수 없다.
    if images.dialect is not None and style.get("dialect") != images.dialect:
        message = (
            f"{PROMPTS_FILE}은 방언 {style.get('dialect')!r}로 쓰였는데 "
            f"프로바이더 {images.name}은 {images.dialect!r}를 읽는다 (ADR-0027). "
            f"`prompt --slug ... --dialect {images.dialect}`로 다시 만들어라 — "
            "[5]는 무료다"
        )
        state.mark_blocked(STAGE, message)
        log.error("[%s] %s", STAGE, message)
        result.blocked = True
        result.warnings = [message]
        raise DialectMismatch(message)

    # ADR-0005 — 룩 일관성 수단. 과금 프로바이더는 앵커 0장이면 호출 전에 막는다.
    anchor_dir = style["style_anchors"]
    anchors = discover_style_anchors(paths.root / anchor_dir)
    warnings: list[str] = []
    if not anchors:
        message = (
            f"{anchor_dir}/에 스타일 앵커 이미지가 0장이다. ADR-0005가 정한 룩 일관성 "
            "수단이 없어 씬 간 그림체가 갈린다"
        )
        if images.requires_style_anchors and not allow_missing_anchors:
            blocked = (
                f"{message}. 편당 과금 호출을 그대로 태우지 않고 막는다 — "
                "앵커 3장을 넣거나(상한도 3장이다 — ADR-0021), "
                "알고도 돌리려면 --allow-missing-anchors를 준다"
            )
            state.mark_blocked(STAGE, blocked)
            log.error("[%s] %s", STAGE, blocked)
            result.blocked = True
            result.warnings = [blocked]
            raise StyleAnchorsMissing(blocked)
        warnings.append(message)

    state.mark_running(STAGE)

    previous = {} if force else _load_previous(record_path)
    records: list[dict[str, Any]] = []
    #: `records`와 `images.json` 쓰기를 지키는 자물쇠. **기록 갱신은 직렬이다**
    #: (ADR-0031 §4) — 씬 하나가 끝날 때마다 쓴다는 계약(ADR-0020)의 목적이 재실행 때
    #: 다시 사지 않는 것이므로, 병렬이 그것을 깨면 병렬로 얻은 시간보다 비싸다.
    record_lock = threading.Lock()

    def flush() -> None:
        """지금까지의 결과 + 아직 안 온 씬의 지난 기록.

        지난 기록을 함께 실어야 하는 이유는 돈이다. 씬마다 기록을 덮어쓰는데 처리한
        씬만 쓰면, 3번 씬에서 죽는 순간 4~27번의 지문이 사라진다 — 이미지는 디스크에
        멀쩡히 있는데 다음 실행이 전부 다시 산다.
        """
        write_text(
            record_path,
            dump_json(
                _record_document(
                    prompts, _merge_records(prompts, records, previous),
                    client=images, anchors=anchors, anchor_dir=anchor_dir,
                    warnings=warnings,
                )
            ),
        )

    def commit(record: dict[str, Any]) -> None:
        """씬 하나의 결과를 기록에 넣고 파일로 내린다. 병렬 경로의 유일한 쓰기 지점이다."""
        with record_lock:
            records.append(record)
            flush()

    # 이어받을 씬은 워커를 쓰지 않는다. 호출이 아니라 파일 확인이라 즉시 끝나고,
    # 슬롯을 잡으면 실제로 살 씬이 그만큼 늦게 출발한다.
    pending: list[tuple[ImageRequest, Path]] = []
    for scene in prompts["scenes"]:
        scene_id = scene["scene_id"]
        request = ImageRequest.from_prompt_scene(
            scene, style, style_anchors=anchors, label=f"{STAGE} 씬 {scene_id}"
        )
        image_path = images_dir / f"{scene_id}{images.output_suffix}"

        prior = previous.get(scene_id)
        if (
            prior is not None
            and prior.get("status") in REUSABLE
            and prior.get("digest") == request.digest
            and image_path.exists()
        ):
            # 프롬프트가 그대로다. 같은 그림을 두 번 사지 않는다.
            records.append({**prior, "status": CACHED, "attempts": 0, "errors": []})
            continue
        pending.append((request, image_path))
    flush()

    workers = _resolve_workers(images, jobs, pending_count=len(pending))
    gate = _Gate(workers)
    result.workers = workers

    try:
        if workers == 1:
            for request, image_path in pending:
                commit(_generate_scene(images, request, image_path, timeout=timeout))
        else:
            log.info(
                "[%s] 씬 %d개를 워커 %d개로 동시에 제출한다 (ADR-0031)",
                STAGE, len(pending), workers,
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        _generate_scene, images, request, image_path,
                        timeout=timeout, gate=gate,
                    )
                    for request, image_path in pending
                ]
                # 예외는 결과를 꺼낼 때 올라온다. 먼저 끝난 순서가 아니라 제출 순서로
                # 꺼내는 이유는 `ProviderNotConfigured`가 첫 씬에서 나면 그것을 가장
                # 먼저 보고 멈추기 위해서다.
                for future in futures:
                    commit(future.result())
    except ProviderNotConfigured as exc:
        message = f"이미지 프로바이더를 쓸 수 없다: {exc}"
        state.mark_failed(STAGE, message, scenes_done=len(records))
        with record_lock:
            flush()
        raise ImagegenStageError(message) from exc

    if gate.reduced:
        warnings.append(
            "429를 만나 워커를 1로 줄였다. 남은 씬은 순차로 돌았다 (ADR-0031 §4)"
        )
    # 폴백은 완료 순서가 아니라 씬 순서로 본다 — 경고 문구의 순서가 실행마다 달라지면
    # 리포트를 눈으로 비교할 수 없다.
    records.sort(key=lambda r: r["scene_id"])
    _apply_fallbacks(records, images_dir, warnings, suffix=images.output_suffix)
    flush()

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result.record_path = record_path
    result.scenes = records
    result.warnings = warnings

    info: dict[str, Any] = {
        "scene_count": len(records),
        "generated": result.count(GENERATED),
        "cached": result.count(CACHED),
        "fallback": result.count(FALLBACK),
        "failed": result.count(FAILED),
        "calls": result.calls,
        "warnings": warnings,
        "outputs": [
            record_path.relative_to(paths.root).as_posix(),
            f"{run_dir.relative_to(paths.root).as_posix()}/{IMAGES_DIR}/",
        ],
    }

    if not result.passed:
        # 성공한 씬이 하나도 없어 복사해 올 곳이 없는 경우다. 여기서 멈춘다.
        message = (
            f"이미지를 한 장도 만들지 못했다 ({len(records)}씬 전부 {MAX_ATTEMPTS}회 실패). "
            "폴백할 인접 씬이 없다"
        )
        state.mark_failed(STAGE, message, **info)
        raise ImagegenStageError(message)

    state.mark_done(STAGE, **info)
    return result
