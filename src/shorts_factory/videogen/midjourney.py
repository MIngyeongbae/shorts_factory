"""Midjourney 영상 어댑터 — mjopen 프록시 REST. ADR-0039 (사슬은 ADR-0025 실측).

`imagegen/midjourney.py`와 같은 프록시를 치지만 계약이 다르다. 이미지는 프롬프트 하나로
끝나는데 **영상은 이미 만든 이미지가 입력**이고, MJ는 자기가 닿을 수 있는 주소만 받는다.

## 사슬 — 잡 3개로 클립 하나 (실측 2026-08-19)

```
UPSCALE (relax, 과금 0, 10초)  →  cdn.discordapp.com 단일 이미지 주소
  → VIDEO (relax, GPU 0, 206초)  →  프록시가 s.mj.run 단축주소로 바꿔 MJ에 넘긴다
  → U1 video_virtual_upscale (과금 0, 10초)  →  mp4
```

UPSCALE이 필요한 이유는 `[6]`이 받은 것이 **2×2 그리드**이기 때문이다. 그리드 주소를
그대로 넣으면 MJ가 "collage of four views"로 읽는다. 우리 로컬 PNG를 올리는 길은
막혀 있다 — 프록시가 돌려주는 `localhost:8086` 주소에 MJ가 닿지 못해
`Invalid link, Invalid URL format`으로 죽는다 (ADR-0025 실측).

## relax로 던진다 — 그리고 그것이 Pro 이상을 요구한다

`POST /mj-relax/mj/submit/video`. **GPU를 쓰지 않는다**(실측 `30.00 → 30.00`)는 것이
전 씬 영상을 가능하게 한 조건이다 (ADR-0039).

Pro 미만에서는 MJ가 이렇게 거절한다 — `Relax mode for video is limited to Pro and Mega
plans.` 이때는 씬 하나의 실패가 아니라 프로바이더 전체의 문제라
`VideoProviderNotConfigured`로 올린다.

**엔드포인트를 프록시의 자동 변환에 맡기지 않는다.** 같은 호출이 2026-08-19 오전에는
조용히 fast로 바뀌어 성공했고 오후에는 거절됐다 (ADR-0039 §4).

## `batchSize: 1`

MJ는 잡 하나에 4개를 낸다. 우리는 U1 하나만 쓰므로 셋은 버려진다. `batchSize: 1`이면
**GPU가 3.25분의 1**이 된다(실측 7.8분 → 2.4분). relax에서는 GPU가 0이라 이득이 없지만,
fast로 되돌아갈 때(밴·플랜 변경) 그 배수가 걸려 있어 켜 둔다. 큐 부하도 준다.

## mp4는 `videoUrl`이 아니다

U1 결과의 `videoUrl`·`videoUrls`는 **`null`이다.** mp4 주소는 `imageUrl`·`url`에 온다
(ADR-0025 정정 2번). 여기를 틀리면 성공한 잡에서 빈손으로 돌아온다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from ..imagegen.midjourney import Transport, urllib_transport
from .base import (
    GeneratedClip,
    VideoClient,
    VideoGenError,
    VideoGenRateLimited,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)

log = logging.getLogger(__name__)

BASE_URL_ENV = "MJ_PROXY_URL"
SECRET_ENV = "MJ_API_SECRET"
DEFAULT_BASE_URL = "http://127.0.0.1:8086"
DEFAULT_SECRET = "admin"
AUTH_HEADER = "mj-api-secret"

#: 영상은 relax다 (ADR-0039). 이미지가 fast인 것과 반대다.
VIDEO_SUBMIT_PATH = "/mj-relax/mj/submit/video"
#: 그리드에서 단일 이미지를 뽑는 자리. 과금 0이라 relax로 던진다.
ACTION_RELAX_PATH = "/mj-relax/mj/submit/action"
#: U 추출은 영상 잡에 붙은 버튼이라 모드 프리픽스를 붙이지 않는다.
ACTION_PATH = "/mj/submit/action"
FETCH_PATH = "/mj/task/{task_id}/fetch"
#: 워커 수의 출처 — 프록시 관리 API의 계정 목록. `[6]`과 같은 자리다 (ADR-0031 G3).
ACCOUNTS_PATH = "/mj/admin/accounts"
ACCOUNTS_TIMEOUT = 15

SUBMIT_OK = (1, 21, 22)

#: relax 큐는 남의 부하가 정한다 (ADR-0035). 어댑터가 그 한도를 안다.
DEFAULT_TIMEOUT = 2400
POLL_INTERVAL = 5.0

#: 잡 하나가 낼 클립 수. 1이면 GPU가 4분의 1이다.
BATCH_SIZE = 1

#: MJ가 Pro 미만을 거절할 때 쓰는 문구 (실측).
PLAN_REFUSAL = "limited to Pro and Mega"

TERMINAL_FAILURES = ("FAILURE", "CANCEL")


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

    `[6r]`이 사분면을 바꿔 끼웠으면 그 번호가 온다 (ADR-0031 §2) — 영상은 **화면에
    실제로 쓰는 그 장**에서 나와야 한다. q0 고정으로 두면 판정이 고른 그림과 영상이
    다른 그림이 된다.
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


class MidjourneyVideoClient(VideoClient):
    """ADR-0039이 고른 실물 영상 경로. 프록시 REST를 직접 친다."""

    name = "midjourney-video"
    output_suffix = ".mp4"
    #: `imagegen/midjourney.py`의 `MidjourneyClient.name`. 같은 계정의 잡이라야
    #: UPSCALE 버튼을 누를 수 있다 (ADR-0041).
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
    ) -> None:
        self.base_url = (
            base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.secret = secret or os.environ.get(SECRET_ENV) or DEFAULT_SECRET
        self.transport = transport
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self.batch_size = batch_size

    @property
    def headers(self) -> dict[str, str]:
        return {AUTH_HEADER: self.secret, "Content-Type": "application/json"}

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
        """활성 계정의 `relaxCoreSize`. `[7]`의 워커 수 기본값이다 (ADR-0031 G3).

        영상 잡은 relax 큐로 간다 (ADR-0039) — 그 큐의 동시 한도가 `relaxCoreSize`다.
        **한 번도 예외를 올리지 않는다.** 이 값은 얼마나 빨리 돌릴지를 정할 뿐이고,
        못 읽으면 1로 떨어져 순차 동작이 된다 — 느려질 뿐 틀리지 않는다.

        활성 계정이 여럿이면 **가장 작은 값**을 쓴다. 잡이 어느 계정으로 갈지는
        프록시가 정하므로, 큰 쪽에 맞추면 작은 쪽 계정이 429를 낸다.
        """
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
                int(a["relaxCoreSize"])
                for a in accounts
                if isinstance(a, dict) and a.get("enable") and a.get("relaxCoreSize")
            ]
            if not sizes:
                raise VideoGenError("활성 계정에 relaxCoreSize가 없다")
        except (VideoGenError, OSError, KeyError, TypeError, ValueError) as exc:
            # 비밀이 섞인 응답이라 본문을 로그에 싣지 않는다 (ADR-0032 §3).
            log.warning(
                "relaxCoreSize를 읽지 못해 워커 1로 간다 (%s: %s). "
                "--jobs로 직접 줄 수 있다", type(exc).__name__, exc,
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
                    raise VideoProviderNotConfigured(
                        f"{what}: {reason} (ADR-0039 §4)"
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
        status, raw = self.transport("GET", url, {}, None, timeout)
        if status != 200:
            raise _fail(status, raw, what="클립 내려받기")
        return raw

    # --- 사슬 -------------------------------------------------------------

    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        budget = timeout or DEFAULT_TIMEOUT

        # 1. 그리드 → 단일 이미지 주소. MJ가 닿는 곳이어야 한다.
        source = self.fetch(request.source_task_id, timeout=budget)
        custom_id = upsample_button(source, request.quadrant)
        upscale_id = self._post(
            ACTION_RELAX_PATH,
            {"customId": custom_id, "taskId": request.source_task_id},
            timeout=budget, what="UPSCALE 제출",
        )
        upscale = self._await(upscale_id, budget=budget, what="UPSCALE")
        image_url = upscale.get("url") or upscale.get("imageUrl")
        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            raise VideoGenError(
                f"UPSCALE이 https 주소를 주지 않았다: {image_url!r}. "
                "MJ는 자기가 닿는 주소만 받는다 (ADR-0025)"
            )

        # 2. 영상. relax라 GPU를 쓰지 않는다.
        body: dict[str, Any] = {
            "image": image_url,
            "prompt": request.motion_prompt,
            "batchSize": self.batch_size,
        }
        video_id = self._post(
            VIDEO_SUBMIT_PATH, body, timeout=budget, what="VIDEO 제출"
        )
        video = self._await(video_id, budget=budget, what="VIDEO")

        # 3. U 추출 — 이미 만든 것 중 하나를 꺼낸다. 과금 0.
        buttons = [
            b.get("customId")
            for b in (video.get("buttons") or [])
            if "video_virtual_upscale" in (b.get("customId") or "")
        ]
        if not buttons:
            raise VideoGenError(
                f"영상 잡 {video_id}에 video_virtual_upscale 버튼이 없다"
            )
        u_id = self._post(
            ACTION_PATH,
            {"customId": buttons[0], "taskId": video.get("id")},
            timeout=budget, what="U 추출 제출",
        )
        extracted = self._await(u_id, budget=budget, what="U 추출")

        # 4. mp4는 imageUrl에 온다 — videoUrl이 아니다 (ADR-0025 정정 2번).
        mp4_url = extracted.get("imageUrl") or extracted.get("url")
        if not isinstance(mp4_url, str) or not mp4_url:
            raise VideoGenError(
                f"U 추출이 SUCCESS인데 mp4 주소가 없다 (키: {sorted(extracted)})"
            )
        data = self.download(mp4_url, timeout=budget)

        return GeneratedClip(
            data=data,
            request_id=str(video.get("id") or video_id),
            model_id=self.name,
            width=extracted.get("width"),
            height=extracted.get("height"),
            raw={
                "mode": video.get("mode"),
                "upscale_task": upscale_id,
                "extract_task": u_id,
                "batch_size": self.batch_size,
                "quadrant": request.quadrant,
                "final_prompt": (video.get("properties") or {}).get("finalPrompt"),
            },
        )
