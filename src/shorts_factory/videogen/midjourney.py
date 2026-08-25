"""Midjourney 영상 어댑터 (`mj-endimage`) — mjopen 프록시 REST. ADR-0070.

ADR-0039의 옛 어댑터를 되살리고 **`endImage`(끝 프레임)**를 얹은 것이다. 구조가 바뀌었다:
예전에는 이미지 한 장을 주고 MJ가 알아서 움직였는데, 이제 **CLEAN → INFO 두 장을 주고
그 사이를 잇게 한다.** 숫자·라벨의 정확성은 정지 이미지가 지고 MJ는 잇기만 한다 (ADR-0070).

## 사슬 — 잡 2~3개로 클립 하나 (실측 2026-08-25)

```
(선택) U{q} 추출 (과금 0, 8초)   →  MJ 그리드에서 낱장 주소
  → VIDEO (fast 60초 / relax 208초)  →  잡에 video_virtual_upscale 버튼이 붙는다
  → U 추출 (과금 0, 12초)          →  mp4
```

`source_task_id`를 주면 첫 칸을 돈다 — MJ imagine이 주는 것은 **2×2 그리드**이고 영상
엔드포인트는 낱장을 받기 때문이다. 그리드 주소를 그대로 넣으면 MJ가 "네 컷 콜라주"로 읽는다.
이미 낱장 주소를 갖고 있으면(`first_frame`) 그 칸을 건너뛴다.

## MJ는 자기가 닿는 주소만 받는다

`first_frame`·`last_frame`은 **공개 https URL**이어야 한다. 로컬 경로나 `localhost` 주소를
주면 MJ가 `Invalid link, Invalid URL format`으로 거절한다 (ADR-0025 실측, 2026-08-25 재현).
우리 INFO 이미지는 프록시의 `imageStorageType = R2`가 `https://pub-….r2.dev/...`로 올려 준다
(ADR-0070). base64를 직접 넣는 길은 **막혀 있다** — 같은 거절이 온다.

## 모드는 프롬프트가 아니라 URL 프리픽스가 정한다

`--fast`·`--relax` 플래그를 붙이지 않는다. 프리픽스를 틀리면 잘못된 프롬프트가 아니라
**아예 다른 엔드포인트**가 된다 (ADR-0039 §4). 실측 2026-08-25:

| 모드 | 1클립 | fast GPU | 동시 |
|---|---|---|---|
| fast (기본) | 60초 | 1.8분 | `coreSize` |
| relax | 208초 | 0 | `relaxCoreSize` |

**turbo는 없다** — MJ가 `Turbo mode isn't supported for video jobs`로 거절한다.

## mp4는 `videoUrl`이 아니다

U 추출 결과의 `videoUrl`·`videoUrls`는 **`null`이다.** mp4 주소는 `imageUrl`·`url`에 온다
(ADR-0025 정정 2번). 여기를 틀리면 성공한 잡에서 빈손으로 돌아온다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from ..transport import CDN_HEADERS, Transport
from ..transport import urllib_transport as _urllib_transport
from .base import (
    GeneratedClip,
    VideoClient,
    VideoGenError,
    VideoGenRateLimited,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)

urllib_transport = _urllib_transport
log = logging.getLogger(__name__)

BASE_URL_ENV = "MJ_PROXY_URL"
SECRET_ENV = "MJ_API_SECRET"
DEFAULT_BASE_URL = "http://127.0.0.1:8086"
DEFAULT_SECRET = "admin"
AUTH_HEADER = "mj-api-secret"

#: 모드별 프리픽스. **기본은 fast다** (ADR-0070 — relax의 3.5배 빠르고 Pro 잔량이 넉넉하다).
#: 편 단위 배치는 relax가 낫다 (동시 3, GPU 0) — 호출자가 `mode`로 고른다.
MODE_PREFIX = {"fast": "/mj-fast", "relax": "/mj-relax"}
DEFAULT_MODE = "fast"

VIDEO_SUBMIT_PATH = "{prefix}/mj/submit/video"
#: 그리드에서 낱장을 뽑는 자리. 과금 0이라 제출과 같은 모드로 던진다.
ACTION_MODE_PATH = "{prefix}/mj/submit/action"
#: U 추출은 영상 잡에 붙은 버튼이라 모드 프리픽스를 붙이지 않는다.
ACTION_PATH = "/mj/submit/action"
FETCH_PATH = "/mj/task/{task_id}/fetch"
#: 워커 수의 출처 — 프록시 관리 API의 계정 목록 (ADR-0031 G3).
ACCOUNTS_PATH = "/mj/admin/accounts"
ACCOUNTS_TIMEOUT = 15

SUBMIT_OK = (1, 21, 22)

#: 큐는 남의 부하가 정한다 (ADR-0035). 어댑터가 그 한도를 안다.
DEFAULT_TIMEOUT = 2400
POLL_INTERVAL = 5.0

#: 잡 하나가 낼 클립 수. 1이면 GPU가 4분의 1이다 (ADR-0039 실측).
BATCH_SIZE = 1

#: 카메라 세기. 지식 쇼츠는 느린 줌·틸트·팬만 쓴다 (CLAUDE.md 레퍼런스 요약) — `low`가 그것이다.
DEFAULT_MOTION = "low"

#: MJ가 Pro 미만을 거절할 때 쓰는 문구 (실측).
PLAN_REFUSAL = "limited to Pro and Mega"
#: MJ가 닿지 못하는 주소를 받았을 때 (실측 2026-08-25). 씬 하나의 실패가 아니라 배선 문제다.
LINK_REFUSAL = "Invalid link"

TERMINAL_FAILURES = ("FAILURE", "CANCEL")

#: U 추출 버튼의 표식. 전체 형식은 `MJ::JOB::video_virtual_upscale::1::{jobId}`이고
#: 잡의 buttons에서 읽는다 — 손으로 조립하지 않는다.
EXTRACT_MARKER = "video_virtual_upscale"


def _load(raw: bytes, *, what: str) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoGenError(f"{what}이(가) JSON이 아니다: {exc}") from exc


def _fail(status: int, body: bytes, *, what: str) -> VideoGenError:
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return VideoGenRateLimited(f"{what}: 사용 한도 도달 (429): {detail}")
    if status in (401, 403):
        return VideoProviderNotConfigured(
            f"{what}: 인증 거부 ({status}): {detail}. "
            f"{SECRET_ENV}가 프록시의 mj-api-secret과 같은지 확인하라"
        )
    return VideoGenError(f"{what}: HTTP {status}: {detail}")


def upsample_button(task: dict[str, Any], quadrant: int) -> str:
    """이미지 잡의 `upsample::{n}` customId. `quadrant`는 0부터다.

    사람·판정이 고른 칸에서 영상이 나와야 한다 — q0 고정으로 두면 화면에 쓰는 그림과
    영상이 다른 그림이 된다 (ADR-0031 §2).
    """
    wanted = f"upsample::{quadrant + 1}::"
    buttons = task.get("buttons") or []
    for button in buttons:
        custom = button.get("customId") or ""
        if wanted in custom:
            return custom
    available = [b.get("customId") for b in buttons if "upsample" in (b.get("customId") or "")]
    raise VideoGenError(
        f"이미지 잡에 q{quadrant}의 upsample 버튼이 없다 (있는 것: {available})"
    )


def extract_button(task: dict[str, Any]) -> str:
    """영상 잡의 U 추출 customId. 없으면 mp4를 꺼낼 길이 없다."""
    for button in task.get("buttons") or []:
        custom = button.get("customId") or ""
        if EXTRACT_MARKER in custom:
            return custom
    raise VideoGenError(
        f"영상 잡 {task.get('id')}에 {EXTRACT_MARKER} 버튼이 없다 "
        f"(있는 것: {[b.get('customId') for b in task.get('buttons') or []]})"
    )


def public_url(value: Any, *, what: str) -> str:
    """MJ에 넘길 주소인지 확인한다. **로컬 주소는 여기서 막는다** — 안 막으면 3분 뒤에
    `Invalid link`로 죽고 사유가 배선 문제라는 것이 안 보인다 (ADR-0070)."""
    url = str(value or "")
    if not url.startswith("https://"):
        raise VideoGenError(
            f"{what}가 https 주소가 아니다: {url!r}. MJ는 자기가 닿는 공개 주소만 받는다 "
            "— 프록시 imageStorageType을 R2/S3로 두어야 한다 (ADR-0070)"
        )
    return url


class MidjourneyEndImageClient(VideoClient):
    """`art` 라인의 영상 엔진 (ADR-0070). CLEAN → INFO를 잇는다."""

    name = "mj-endimage"
    #: first/last를 실제로 싣는 어댑터다 (ADR-0071).
    accepts_frames = True
    output_suffix = ".mp4"
    #: 같은 계정의 잡이라야 U 버튼을 누를 수 있다 (ADR-0041).
    source_provider = "midjourney"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        secret: str | None = None,
        transport: Transport = urllib_transport,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        batch_size: int = BATCH_SIZE,
        mode: str = DEFAULT_MODE,
        motion: str = DEFAULT_MOTION,
    ) -> None:
        if mode not in MODE_PREFIX:
            raise VideoProviderNotConfigured(
                f"모드가 {mode!r}인데 가능한 것은 {', '.join(sorted(MODE_PREFIX))}다 "
                "(turbo는 MJ가 영상에 대해 거절한다 — ADR-0070 실측)"
            )
        self.base_url = (
            base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.secret = secret or os.environ.get(SECRET_ENV) or DEFAULT_SECRET
        self.transport = transport
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self.batch_size = batch_size
        self.mode = mode
        self.motion = motion

    @property
    def headers(self) -> dict[str, str]:
        return {AUTH_HEADER: self.secret, "Content-Type": "application/json"}

    @property
    def prefix(self) -> str:
        return MODE_PREFIX[self.mode]

    # --- 프록시 호출 -------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any], *, timeout: int, what: str) -> str:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, raw = self.transport(
            "POST", f"{self.base_url}{path}", self.headers, payload, timeout
        )
        if status != 200:
            raise _fail(status, raw, what=what)
        data = _load(raw, what=f"{what} 응답")
        code, task_id = data.get("code"), data.get("result")
        if code not in SUBMIT_OK or not task_id:
            description = str(data.get("description") or "")
            if PLAN_REFUSAL in description:
                raise VideoProviderNotConfigured(
                    f"{what}: {description}. relax 영상은 Pro 이상이다 (ADR-0039 §4)"
                )
            raise VideoGenError(f"{what}이(가) 거절됐다 (code={code}): {description!r}")
        return str(task_id)

    def fetch(self, task_id: str, *, timeout: int) -> dict[str, Any]:
        url = f"{self.base_url}{FETCH_PATH.format(task_id=task_id)}"
        status, raw = self.transport("GET", url, self.headers, None, timeout)
        if status != 200:
            raise _fail(status, raw, what=f"태스크 {task_id} 조회")
        return _load(raw, what="태스크 응답")

    def concurrency(self) -> int:
        """활성 계정의 동시 한도. fast는 `coreSize`, relax는 `relaxCoreSize`다.

        **한 번도 예외를 올리지 않는다.** 못 읽으면 1로 떨어져 순차 동작이 된다 —
        느려질 뿐 틀리지 않는다. 계정이 여럿이면 가장 작은 값을 쓴다.
        """
        field = "coreSize" if self.mode == "fast" else "relaxCoreSize"
        try:
            status, raw = self.transport(
                "POST", f"{self.base_url}{ACCOUNTS_PATH}", self.headers,
                b"{}", ACCOUNTS_TIMEOUT,
            )
            if status != 200:
                raise VideoGenError(f"HTTP {status}")
            accounts = _load(raw, what="계정 목록").get("list")
            if not isinstance(accounts, list):
                raise VideoGenError("응답에 list가 없다")
            sizes = [
                int(a[field])
                for a in accounts
                if isinstance(a, dict) and a.get("enable") and a.get(field)
            ]
            if not sizes:
                raise VideoGenError(f"활성 계정에 {field}가 없다")
        except (VideoGenError, OSError, KeyError, TypeError, ValueError) as exc:
            # 비밀이 섞인 응답이라 본문을 로그에 싣지 않는다 (ADR-0032 §3).
            log.warning(
                "%s를 읽지 못해 워커 1로 간다 (%s: %s). --jobs로 직접 줄 수 있다",
                field, type(exc).__name__, exc,
            )
            return 1
        return max(1, min(sizes))

    def _await(self, task_id: str, *, budget: int, what: str) -> dict[str, Any]:
        """잡 하나가 끝날 때까지. 데드라인은 프로바이더가 정한 예산이다 (ADR-0035)."""
        deadline = self.clock() + budget
        while True:
            task = self.fetch(task_id, timeout=budget)
            status = task.get("status")
            if status == "SUCCESS":
                return task
            if status in TERMINAL_FAILURES:
                reason = str(task.get("failReason") or "사유 없음")
                if PLAN_REFUSAL in reason:
                    raise VideoProviderNotConfigured(f"{what}: {reason} (ADR-0039 §4)")
                if LINK_REFUSAL in reason:
                    raise VideoProviderNotConfigured(
                        f"{what}: {reason}. MJ가 닿지 못하는 주소를 줬다 — 프록시 "
                        "imageStorageType이 R2/S3인지 확인하라 (ADR-0070)"
                    )
                raise VideoGenError(f"{what} 태스크 {task_id}가 {status}로 끝났다: {reason}")
            if self.clock() >= deadline:
                # 잡은 프록시에서 계속 돈다. 죽이지 않고 taskId를 남긴다.
                raise VideoGenTimeout(
                    f"{what} 태스크 {task_id}가 {budget}초 안에 끝나지 않았다 "
                    f"(마지막 status={status!r}). 프록시에서는 계속 돈다"
                )
            self.sleep(self.poll_interval)

    def download(self, url: str, *, timeout: int) -> bytes:
        """mp4를 받는다. **CDN 신원이 필요하다** — 아래 `CDN_HEADERS` 주석.

        여기의 403은 프록시 인증이 아니라 CDN의 거절이다. `_fail`을 쓰지 않는 이유가
        그것이다 — 그 함수는 403을 "MJ_API_SECRET을 확인하라"로 읽는다.
        """
        status, raw = self.transport("GET", url, dict(CDN_HEADERS), None, timeout)
        if status != 200:
            detail = raw.decode("utf-8", "replace")[:200]
            raise VideoGenError(
                f"클립 내려받기: HTTP {status}: {detail}. 프록시 인증이 아니라 파일이 놓인 "
                f"CDN의 응답이다 (주소: {url.split('?')[0]})"
            )
        return raw

    # --- 사슬 -------------------------------------------------------------

    def first_frame_url(self, request: VideoRequest, *, budget: int) -> str:
        """CLEAN 낱장 주소. 그리드 잡이 오면 U{q}를 눌러 뽑고, 낱장이면 그대로 쓴다."""
        if not request.source_task_id:
            return public_url(request.first_frame, what="first_frame")
        source = self.fetch(request.source_task_id, timeout=budget)
        custom_id = upsample_button(source, request.quadrant)
        task_id = self._post(
            ACTION_MODE_PATH.format(prefix=self.prefix),
            {"customId": custom_id, "taskId": request.source_task_id},
            timeout=budget, what=f"U{request.quadrant + 1} 추출 제출",
        )
        picked = self._await(task_id, budget=budget, what=f"U{request.quadrant + 1} 추출")
        return public_url(picked.get("url") or picked.get("imageUrl"), what="U 추출 결과")

    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        budget = timeout or DEFAULT_TIMEOUT

        image = self.first_frame_url(request, budget=budget)
        body: dict[str, Any] = {
            "image": image,
            "prompt": request.motion_prompt or request.prompt,
            "motion": self.motion,
            "batchSize": self.batch_size,
        }
        # 끝 프레임은 선택이다 — 없으면 MJ가 알아서 움직인다 (옛 ADR-0039 동작).
        if request.last_frame is not None:
            body["endImage"] = public_url(request.last_frame, what="last_frame")

        video_id = self._post(
            VIDEO_SUBMIT_PATH.format(prefix=self.prefix), body,
            timeout=budget, what="VIDEO 제출",
        )
        video = self._await(video_id, budget=budget, what="VIDEO")

        extract_id = self._post(
            ACTION_PATH,
            {"customId": extract_button(video), "taskId": video.get("id")},
            timeout=budget, what="U 추출 제출",
        )
        extracted = self._await(extract_id, budget=budget, what="U 추출")

        # mp4는 imageUrl에 온다 — videoUrl이 아니다 (ADR-0025 정정 2번).
        mp4_url = extracted.get("imageUrl") or extracted.get("url")
        if not isinstance(mp4_url, str) or not mp4_url:
            raise VideoGenError(
                f"U 추출이 SUCCESS인데 mp4 주소가 없다 (키: {sorted(extracted)})"
            )
        return GeneratedClip(
            data=self.download(mp4_url, timeout=budget),
            request_id=str(video.get("id") or video_id),
            model_id=self.name,
        )
