"""로컬 ComfyUI + MiniMax H3 텍스트→영상 어댑터 — `video_line: local`의 어댑터. ADR-0059 결정 3.

변동비 0인 라인이다. 같은 PC의 ComfyUI(Desktop)가 띄운 HTTP API를 친다 — SDK 없음, 공용
`transport.py`, `omni.py`와 같은 경계(관용적 파싱 + 요란한 실패). 호출 규약은 2026-08-23
프로브 3회(`runs/20260823-comfy-*`)의 `comfy_queue.py`가 원형이다.

- 제출 `POST {COMFY_URL}/prompt` 본문 `{"prompt": <API 포맷 워크플로>, "client_id"}` →
  `{"prompt_id", "number", "node_errors"}`
- 폴링 `GET /history/{prompt_id}` — 비어 있으면 아직 큐·실행 중. 끝나면
  `{prompt_id: {"status": {"status_str": "success"|"error", "completed", "messages"},
  "outputs": {node_id: {"images"|"videos"|"gifs": [{"filename", "subfolder", "type"}]}}}}`
- 내려받기 `GET /view?filename=&subfolder=&type=` → mp4 바이트

## 워크플로는 템플릿이다 — `config/comfy/h3-t2v.api.json`

ComfyUI Desktop의 H3 템플릿 실행 한 번에서 꺼낸 API 포맷 그래프다(모델·샘플러·20 steps·24fps·
17k+5 프레임 격자 전부 그쪽 것). 어댑터는 노드를 **id가 아니라 `class_type`으로 찾아** 다섯
자리만 꽂는다: 프롬프트, 해상도(종횡비·메가픽셀), 시드, 길이(초), 저장 접두. 그래프를 코드가
들고 있지 않다 (ADR-0034의 태도 — 바뀌는 것은 파일이다).

## 출력 규격 (프로브 실측)

0.4MP 기준 480×864 / 24fps / h264 + 오디오, 길이는 요청 초를 17k+5 프레임 격자로 올린 값
(5초 → 5.167초). 규격 맞추기(1080×1920·30fps·무음·정확한 길이)는 `[7]`의 정규화가 한다
(`video/clips.py`) — 여기서는 mp4인지만 본다. 소요는 0.4MP 165~255초, 1.0MP 720~990초 —
GPU 하나라 `concurrency()`는 1이고 기본 상한은 20분이다 (ADR-0035 — 주인은 어댑터).

## 실패의 급

- ComfyUI에 **연결이 안 되면** 프로바이더 전체 문제다 (`VideoProviderNotConfigured`) — 씬
  수만큼 같은 오류로 실패하는 것은 결과가 아니라 소음이다 (`omni.py`와 같은 판단)
- 제출이 `node_errors`를 돌려주면(모델 파일 없음, 노드 없음) 역시 전체 문제다
- 한 잡이 `status_str: error`로 끝나면 그 씬의 실패다 — `[7]`의 사다리가 받는다
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from ..schemas import vocab
from ..transport import Transport, TransportError, TransportTimeout, urllib_transport
from .base import (
    GeneratedClip,
    VideoClient,
    VideoGenError,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)

log = logging.getLogger(__name__)

#: ComfyUI HTTP API 주소. 같은 PC의 Desktop 기본 포트다.
URL_ENV = "COMFY_URL"
DEFAULT_URL = "http://127.0.0.1:8188"
#: 렌더 해상도(메가픽셀). 비우면 템플릿 값(0.4 — ADR-0059 결정 7, 첫 실편 뒤 사람이 정한다).
MEGAPIXELS_ENV = "COMFY_H3_MEGAPIXELS"

#: 리포지토리의 워크플로 템플릿 (ADR-0059 결정 3). src/shorts_factory/videogen/comfy_h3.py → parents[3]이 루트다.
TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "config" / "comfy" / "h3-t2v.api.json"

#: 템플릿에서 값을 꽂는 노드 — `class_type` → 입력 키 (모듈 독스트링).
NODE_PROMPT = "MiniMaxH3ImageToVideo"
NODE_RESOLUTION = "ResolutionSelector"
NODE_SEED = "RandomNoise"
NODE_SECONDS = "PrimitiveFloat"
NODE_SAVE = "SaveVideo"
NODE_UNET = "UNETLoader"

#: H3가 받는 클립 길이(초). 하한은 모델 문서의 4초, 상한은 스펙 05 `[7]`의 10초.
MIN_SECONDS = 4
MAX_SECONDS = 10

#: 잡 하나의 기본 상한(초) — 1.0MP 프로브 최장 990초에 여유 (ADR-0059 결정 3).
DEFAULT_TIMEOUT = 1200
#: 폴링 간격(초). 클립이 분 단위라 촘촘할 이유가 없다.
POLL_INTERVAL = 10.0
#: HTTP 한 번의 상한(초) — 제출·폴링·내려받기 각각.
HTTP_TIMEOUT = 60

#: 세로 쇼츠. 값은 어휘의 `style.aspect_ratio`다 — 손으로 적지 않는다 (ADR-0034).
ASPECT_RATIO: str = str(vocab.style("aspect_ratio"))

#: 시드 범위 — ComfyUI `RandomNoise.noise_seed`의 64비트 안에서 프로브가 쓴 폭.
SEED_MAX = 2**48


def load_template(path: Path = TEMPLATE_PATH) -> dict[str, Any]:
    """템플릿 파일 → 워크플로 그래프(dict). 없거나 모양이 다르면 프로바이더 전체 문제다."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VideoProviderNotConfigured(f"ComfyUI 워크플로 템플릿이 없다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VideoProviderNotConfigured(f"ComfyUI 워크플로 템플릿을 읽을 수 없다: {path} — {exc}") from exc
    workflow = document.get("workflow") if isinstance(document, dict) else None
    if not isinstance(workflow, dict) or not workflow:
        raise VideoProviderNotConfigured(f"템플릿에 `workflow` 그래프가 없다: {path}")
    return workflow


def find_node(workflow: dict[str, Any], class_type: str) -> str:
    """`class_type`이 그것인 노드 id. 없거나 둘 이상이면 템플릿이 계약과 다르다."""
    ids = [k for k, v in workflow.items() if isinstance(v, dict) and v.get("class_type") == class_type]
    if len(ids) != 1:
        raise VideoProviderNotConfigured(
            f"템플릿에 `{class_type}` 노드가 {len(ids)}개다 (정확히 1개여야 한다) — {TEMPLATE_PATH}"
        )
    return ids[0]


def build_workflow(
    template: dict[str, Any], request: VideoRequest, *, seed: int,
    megapixels: float | None, filename_prefix: str,
) -> dict[str, Any]:
    """템플릿 + 요청 → 제출할 그래프. 프롬프트는 `[5]`의 것 그대로다 — 문장을 더하지 않는다."""
    if not request.prompt.strip():
        raise VideoGenError(f"씬 {request.scene_id}: 프롬프트가 비어 있다")
    if request.seconds and not MIN_SECONDS <= request.seconds <= MAX_SECONDS:
        raise VideoGenError(
            f"씬 {request.scene_id}: 클립 길이 {request.seconds}초는 H3 범위"
            f"({MIN_SECONDS}~{MAX_SECONDS}) 밖이다 — [7]이 클램프했어야 한다"
        )
    workflow = json.loads(json.dumps(template))
    workflow[find_node(workflow, NODE_PROMPT)]["inputs"]["prompt"] = request.prompt
    resolution = workflow[find_node(workflow, NODE_RESOLUTION)]["inputs"]
    if not str(resolution.get("aspect_ratio", "")).startswith(ASPECT_RATIO):
        raise VideoProviderNotConfigured(
            f"템플릿의 종횡비 {resolution.get('aspect_ratio')!r}가 어휘의 {ASPECT_RATIO}와 다르다"
        )
    if megapixels is not None:
        resolution["megapixels"] = float(megapixels)
    workflow[find_node(workflow, NODE_SEED)]["inputs"]["noise_seed"] = int(seed)
    if request.seconds:
        workflow[find_node(workflow, NODE_SECONDS)]["inputs"]["value"] = float(request.seconds)
    workflow[find_node(workflow, NODE_SAVE)]["inputs"]["filename_prefix"] = filename_prefix
    return workflow


def find_output_video(outputs: dict[str, Any]) -> dict[str, Any] | None:
    """`history.outputs`에서 mp4 항목 하나. 키 이름(`images`·`videos`·`gifs`)은 노드마다 달라
    전부 훑는다 — 원하는 것은 `.mp4`로 끝나는 `filename` 하나다."""
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and str(item.get("filename", "")).endswith(".mp4"):
                    return item
    return None


class ComfyH3Client(VideoClient):
    """`video_line: local`의 어댑터 — 로컬 GPU, 변동비 0 (ADR-0059)."""

    name = "comfy-h3"
    output_suffix = ".mp4"
    source_provider = ""
    min_seconds = MIN_SECONDS
    max_seconds = MAX_SECONDS

    def __init__(
        self,
        *,
        base_url: str | None = None,
        megapixels: float | None = None,
        template: dict[str, Any] | None = None,
        template_path: Path = TEMPLATE_PATH,
        transport: Transport = urllib_transport,
        poll_interval: float = POLL_INTERVAL,
        http_timeout: int = HTTP_TIMEOUT,
        seed_fn: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = (base_url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
        env_mp = os.environ.get(MEGAPIXELS_ENV, "").strip()
        self.megapixels = megapixels if megapixels is not None else (float(env_mp) if env_mp else None)
        self._template = template
        self._template_path = template_path
        self.transport = transport
        self.poll_interval = poll_interval
        self.http_timeout = http_timeout
        self._seed_fn = seed_fn or (lambda: random.randint(1, SEED_MAX))
        self._sleep = sleep
        self._clock = clock
        self.client_id = str(uuid.uuid4())

    @property
    def template(self) -> dict[str, Any]:
        """템플릿은 첫 호출에서 읽는다 — 어댑터를 만드는 것만으로(`--help`) 실패하지 않는다."""
        if self._template is None:
            self._template = load_template(self._template_path)
        return self._template

    @property
    def model_id(self) -> str:
        try:
            return str(self.template[find_node(self.template, NODE_UNET)]["inputs"]["unet_name"])
        except (KeyError, VideoProviderNotConfigured):
            return "minimax-h3"

    def concurrency(self) -> int:
        return 1

    # --- HTTP -------------------------------------------------------------

    def _send(self, method: str, path: str, body: bytes | None, *, what: str) -> tuple[int, bytes]:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            return self.transport(method, f"{self.base_url}{path}", headers, body, self.http_timeout)
        except TransportTimeout as exc:
            raise VideoGenTimeout(f"{what}: {exc}") from exc
        except TransportError as exc:
            raise VideoGenError(f"{what}: {exc}") from exc

    def _json(self, method: str, path: str, body: dict[str, Any] | None, *, what: str) -> Any:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        status, raw = self._send(method, path, raw_body, what=what)
        if status != 200:
            raise VideoGenError(f"{what}: HTTP {status}: {raw.decode('utf-8', 'replace')[:400]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VideoGenError(f"{what}: JSON이 아닌 200 응답이다: {exc}") from exc

    def _submit(self, workflow: dict[str, Any], *, what: str) -> str:
        try:
            payload = self._json("POST", "/prompt", {"prompt": workflow, "client_id": self.client_id}, what=what)
        except VideoGenError as exc:
            # 연결 자체가 안 되면 ComfyUI가 안 떠 있는 것이다 — 씬이 아니라 프로바이더의 문제.
            if isinstance(exc.__cause__, TransportError) and not isinstance(exc, VideoGenTimeout):
                raise VideoProviderNotConfigured(
                    f"{what}: ComfyUI에 연결할 수 없다 ({self.base_url}) — Desktop이 떠 있는지, "
                    f"{URL_ENV}가 맞는지 확인하라: {exc}"
                ) from exc
            raise
        node_errors = payload.get("node_errors") if isinstance(payload, dict) else None
        if node_errors:
            raise VideoProviderNotConfigured(
                f"{what}: 워크플로를 거부했다 (node_errors) — 모델 파일·노드가 템플릿과 맞는지 보라: "
                f"{json.dumps(node_errors, ensure_ascii=False)[:400]}"
            )
        prompt_id = str((payload or {}).get("prompt_id") or "") if isinstance(payload, dict) else ""
        if not prompt_id:
            raise VideoGenError(f"{what}: 응답에 prompt_id가 없다 (키: {sorted(payload) if isinstance(payload, dict) else type(payload).__name__})")
        return prompt_id

    def _wait(self, prompt_id: str, *, budget_until: float, what: str) -> dict[str, Any]:
        """`/history/{id}`가 찰 때까지. 폴링 중 일시적 연결 오류는 예산 안에서 다시 본다."""
        while True:
            if self._clock() >= budget_until:
                raise VideoGenTimeout(f"{what}: 잡이 제한 시간 안에 끝나지 않았다 ({prompt_id})")
            self._sleep(self.poll_interval)
            try:
                history = self._json("GET", f"/history/{prompt_id}", None, what=what)
            except VideoGenError as exc:
                log.warning("%s: 폴링 실패, 다시 본다 — %s", what, exc)
                continue
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if not isinstance(entry, dict):
                continue
            status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
            if status.get("status_str") == "error":
                messages = status.get("messages") or []
                raise VideoGenError(
                    f"{what}: ComfyUI 잡이 실패했다 — {json.dumps(messages, ensure_ascii=False)[:400]}"
                )
            if entry.get("outputs") or status.get("completed"):
                return entry

    def _download(self, item: dict[str, Any], *, what: str) -> bytes:
        query = urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        status, raw = self._send("GET", f"/view?{query}", None, what=what)
        if status != 200:
            raise VideoGenError(f"{what}: 내려받기 HTTP {status}")
        return raw

    # --- 공개 API ---------------------------------------------------------

    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        budget = timeout or DEFAULT_TIMEOUT
        budget_until = self._clock() + budget
        what = f"씬 {request.scene_id} ComfyUI H3"

        seed = int(self._seed_fn())
        save_prefix = str(self.template[find_node(self.template, NODE_SAVE)]["inputs"].get("filename_prefix") or "video/shorts-factory")
        workflow = build_workflow(
            self.template, request, seed=seed, megapixels=self.megapixels,
            filename_prefix=f"{save_prefix}-s{request.scene_id}",
        )
        prompt_id = self._submit(workflow, what=f"{what} 제출")
        log.info("%s: 큐에 넣었다 (%s, seed=%d, %s초)", what, prompt_id, seed, request.seconds or "템플릿")

        entry = self._wait(prompt_id, budget_until=budget_until, what=f"{what} 대기")
        item = find_output_video(entry.get("outputs") or {})
        if item is None:
            raise VideoGenError(
                f"{what}: 잡은 끝났는데 mp4 출력이 없다 (outputs 키: {sorted((entry.get('outputs') or {}).keys())})"
            )
        data = self._download(item, what=f"{what} 내려받기")
        megapixels = workflow[find_node(workflow, NODE_RESOLUTION)]["inputs"].get("megapixels")
        return GeneratedClip(
            data=data,
            request_id=prompt_id,
            model_id=self.model_id,
            duration=float(request.seconds) if request.seconds else None,
            raw={
                "filename": item.get("filename"),
                "subfolder": item.get("subfolder", ""),
                "seed": seed,
                "megapixels": megapixels,
            },
        )
