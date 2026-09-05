"""`[6]`의 로컬 first-frame 어댑터 — SDXL + IP-Adapter (ADR-0087 결정 1·2·3).

`reference_frames`가 참인 라인(`local`)의 **`info`가 없는 씬**이 여기로 온다. MJ 경로와
다른 점 셋:

1. **그리드·사분면이 없다.** 한 번 제출하면 낱장 하나가 나온다. 사다리는 사분면 교체가
   아니라 **새 시드로 다시 그리기**다 — 변동비 0이라 그것이 가장 싸다.
2. **주소가 로컬 경로다.** MJ는 자기가 닿는 공개 https를 요구하지만(ADR-0070) 이 프레임을
   읽을 쪽은 같은 기계의 ComfyUI라 R2에 올릴 이유가 없다 (ADR-0087 결정 7).
3. **참조가 없으면 그냥 참조 없이 그린다.** 강등이 아니다 — IP-Adapter 노드 넷을 떼고
   KSampler를 체크포인트에 다시 물린다.

노브(`weight`·`start_at`)는 씬의 `reference` 모드가 정하고 값은 `vocab.json`
`meta.reference_mode`가 진다 — **코드가 상수를 선언하지 않는다** (ADR-0034).
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

from ..transport import Transport, TransportError, TransportTimeout, urllib_transport
from .base import ImageGenError, ImageGenTimeout, ProviderNotConfigured

log = logging.getLogger(__name__)

#: `comfy_h3`와 같은 서버다 — 주소도 같은 환경변수에서 읽는다.
URL_ENV = "COMFY_URL"
DEFAULT_URL = "http://127.0.0.1:8188"

#: 리포지토리의 워크플로 템플릿. src/shorts_factory/imagegen/comfy_sdxl.py → parents[3]이 루트다.
TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "config" / "comfy" / "sdxl-ipa.api.json"

#: 템플릿에서 값을 꽂는 노드 (`_meta.title`). 같은 `class_type`이 둘이라 제목으로 찾는다.
TITLE_POSITIVE = "POSITIVE"
TITLE_NEGATIVE = "NEGATIVE"
TITLE_SAMPLER = "KSampler"
TITLE_IPADAPTER = "IPAdapter"
TITLE_REFERENCE = "REFERENCE"
TITLE_CHECKPOINT = "Checkpoint"
TITLE_SAVE = "Save"

#: 참조가 없을 때 떼는 노드들. 넷을 떼고 KSampler를 체크포인트에 다시 문다.
REFERENCE_TITLES = (TITLE_IPADAPTER, TITLE_REFERENCE, "IPAdapter Model", "CLIP Vision")

UPLOAD_PATH = "/upload/image"

#: 낱장 하나의 기본 상한(초). 실측은 12~20초(832×1472, 28스텝, 모델 로드 뒤 7~8초)라
#: 넉넉하다 — 이 예산이 지키는 것은 **모델 로드·VRAM 스왑이 낀 첫 장**이다.
DEFAULT_TIMEOUT = 600
POLL_INTERVAL = 2.0
HTTP_TIMEOUT = 120

SEED_MAX = 2**31 - 1


def load_template(path: Path = TEMPLATE_PATH) -> dict[str, Any]:
    """`{_comment, workflow}` → 워크플로. `comfy_h3.load_template`과 같은 모양이다."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProviderNotConfigured(f"워크플로 템플릿을 읽을 수 없다 ({path}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderNotConfigured(f"워크플로 템플릿이 JSON이 아니다 ({path}): {exc}") from exc
    workflow = document.get("workflow") if isinstance(document, dict) else None
    if not isinstance(workflow, dict) or not workflow:
        raise ProviderNotConfigured(f"템플릿에 `workflow` 객체가 없다 ({path})")
    return workflow


def find_by_title(workflow: dict[str, Any], title: str) -> str:
    """`_meta.title`이 그것인 노드 id. 없거나 둘 이상이면 템플릿이 계약과 다르다."""
    ids = [
        nid for nid, node in workflow.items()
        if isinstance(node, dict) and (node.get("_meta") or {}).get("title") == title
    ]
    if len(ids) != 1:
        raise ProviderNotConfigured(
            f"템플릿에 제목이 {title!r}인 노드가 {len(ids)}개다 (정확히 1개여야 한다) — {TEMPLATE_PATH}"
        )
    return ids[0]


def build_workflow(
    template: dict[str, Any],
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    reference_name: str,
    weight: float,
    start_at: float,
    filename_prefix: str,
) -> dict[str, Any]:
    """템플릿 + 씬의 값 → 제출할 워크플로. 템플릿은 건드리지 않는다 (씬이 동시에 돈다)."""
    workflow = json.loads(json.dumps(template))
    workflow[find_by_title(workflow, TITLE_POSITIVE)]["inputs"]["text"] = prompt
    workflow[find_by_title(workflow, TITLE_NEGATIVE)]["inputs"]["text"] = negative_prompt
    sampler = workflow[find_by_title(workflow, TITLE_SAMPLER)]["inputs"]
    sampler["seed"] = int(seed)
    workflow[find_by_title(workflow, TITLE_SAVE)]["inputs"]["filename_prefix"] = filename_prefix

    if reference_name and weight > 0:
        ipa = workflow[find_by_title(workflow, TITLE_IPADAPTER)]["inputs"]
        ipa["weight"] = float(weight)
        ipa["start_at"] = float(start_at)
        workflow[find_by_title(workflow, TITLE_REFERENCE)]["inputs"]["image"] = reference_name
        return workflow

    # 참조 없이 그린다 — IP-Adapter 넷을 떼고 KSampler를 체크포인트에 다시 문다.
    # (강등이 아니다: 참조 사진이 없는 씬과 `reference: none`인 씬이 같은 길로 간다.)
    checkpoint = find_by_title(workflow, TITLE_CHECKPOINT)
    sampler["model"] = [checkpoint, 0]
    for title in REFERENCE_TITLES:
        workflow.pop(find_by_title(workflow, title), None)
    return workflow


class ComfySDXLClient:
    """로컬 SDXL + IP-Adapter로 first frame 한 장 (ADR-0087).

    `ImageClient`를 상속하지 않는다 — 그 인터페이스는 MJ의 **그리드 + 사분면 업스케일**을
    전제로 잘려 있고(`generate`가 잡 id를 주고 `upscale`이 낱장을 준다), 여기는 한 번에
    낱장 하나가 나오는 다른 모양이다. 억지로 맞추면 두 경로가 서로의 계약을 비튼다
    (단계 독립 6원칙).
    """

    name = "comfy-sdxl-ipa"
    output_suffix = ".png"

    def __init__(
        self,
        *,
        base_url: str | None = None,
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
        if self._template is None:
            self._template = load_template(self._template_path)
        return self._template

    def concurrency(self) -> int:
        """GPU 하나에 SDXL 하나다 — 동시에 던지면 VRAM만 다툰다."""
        return 1

    # --- HTTP -------------------------------------------------------------

    def _send(self, method: str, path: str, body: bytes | None, headers: dict[str, str], *, what: str) -> tuple[int, bytes]:
        try:
            return self.transport(method, f"{self.base_url}{path}", headers, body, self.http_timeout)
        except TransportTimeout as exc:
            raise ImageGenTimeout(f"{what}: {exc}") from exc
        except TransportError as exc:
            raise ImageGenError(f"{what}: {exc}") from exc

    def _json(self, method: str, path: str, body: dict[str, Any] | None, *, what: str) -> Any:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        status, raw = self._send(method, path, raw_body, headers, what=what)
        if status != 200:
            raise ImageGenError(f"{what}: HTTP {status}: {raw.decode('utf-8', 'replace')[:400]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImageGenError(f"{what}: JSON이 아닌 200 응답이다: {exc}") from exc

    def upload_reference(self, path: Path, name: str, *, what: str) -> str:
        """참조 사진을 ComfyUI로 올려 `LoadImage`가 쓸 이름을 받는다.

        **input 폴더에 직접 쓰지 않는다** — Desktop 인스턴스의 input은 공유 폴더
        (`ComfyUI-Shared/input`)라 인스턴스 경로와 다르고, 직접 쓰면 서버가
        *"Invalid image file"*로 400을 낸다 (실측 2026-09-01).
        """
        boundary = uuid.uuid4().hex
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(),
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
            f"--{boundary}--\r\n".encode(),
        ])
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        status, raw = self._send("POST", UPLOAD_PATH, body, headers, what=what)
        if status != 200:
            raise ImageGenError(f"{what}: 참조 업로드가 거절됐다 (HTTP {status})")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ImageGenError(f"{what}: 업로드 응답이 JSON이 아니다: {exc}") from exc
        name_back = str(payload.get("name") or "")
        if not name_back:
            raise ImageGenError(f"{what}: 업로드 응답에 파일 이름이 없다")
        subfolder = str(payload.get("subfolder") or "")
        return f"{subfolder}/{name_back}" if subfolder else name_back

    def _submit(self, workflow: dict[str, Any], *, what: str) -> str:
        try:
            payload = self._json(
                "POST", "/prompt", {"prompt": workflow, "client_id": self.client_id}, what=what
            )
        except ImageGenError as exc:
            if isinstance(exc.__cause__, TransportError) and not isinstance(exc, ImageGenTimeout):
                raise ProviderNotConfigured(
                    f"{what}: ComfyUI에 연결할 수 없다 ({self.base_url}) — 서버가 떠 있는지, "
                    f"{URL_ENV}가 맞는지 확인하라: {exc}"
                ) from exc
            raise
        node_errors = payload.get("node_errors") if isinstance(payload, dict) else None
        if node_errors:
            raise ProviderNotConfigured(
                f"{what}: 워크플로를 거부했다 (node_errors) — 체크포인트·IP-Adapter·CLIP Vision "
                f"파일이 템플릿과 맞는지 보라: {json.dumps(node_errors, ensure_ascii=False)[:400]}"
            )
        prompt_id = str((payload or {}).get("prompt_id") or "") if isinstance(payload, dict) else ""
        if not prompt_id:
            raise ImageGenError(f"{what}: 응답에 prompt_id가 없다")
        return prompt_id

    def _wait(self, prompt_id: str, *, budget_until: float, what: str) -> dict[str, Any]:
        while True:
            if self._clock() >= budget_until:
                raise ImageGenTimeout(f"{what}: 잡이 제한 시간 안에 끝나지 않았다 ({prompt_id})")
            self._sleep(self.poll_interval)
            try:
                history = self._json("GET", f"/history/{prompt_id}", None, what=what)
            except ImageGenError as exc:
                log.warning("%s: 폴링 실패, 다시 본다 — %s", what, exc)
                continue
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if not isinstance(entry, dict):
                continue
            status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
            if status.get("status_str") == "error":
                raise ImageGenError(
                    f"{what}: ComfyUI 잡이 실패했다 — "
                    f"{json.dumps(status.get('messages') or [], ensure_ascii=False)[:400]}"
                )
            if entry.get("outputs") or status.get("completed"):
                return entry

    def _download(self, item: dict[str, Any], *, what: str) -> bytes:
        query = urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        status, raw = self._send("GET", f"/view?{query}", None, {}, what=what)
        if status != 200:
            raise ImageGenError(f"{what}: 내려받기 HTTP {status}")
        return raw

    # --- 공개 API ---------------------------------------------------------

    def generate_frame(
        self,
        *,
        scene_id: int,
        prompt: str,
        negative_prompt: str,
        reference: Path | None,
        weight: float,
        start_at: float,
        seed: int | None = None,
        timeout: int | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """first frame 한 장 → `(PNG 바이트, 기록)`. 참조가 없으면 참조 없이 그린다."""
        budget_until = self._clock() + (timeout or DEFAULT_TIMEOUT)
        what = f"씬 {scene_id} ComfyUI SDXL"
        seed = int(seed if seed is not None else self._seed_fn())

        reference_name = ""
        if reference is not None and weight > 0:
            reference_name = self.upload_reference(
                reference, f"sf-ref-s{scene_id}{reference.suffix or '.jpg'}",
                what=f"{what} 참조 업로드",
            )
        if not reference_name:
            # 참조가 없으면 노브도 0이다 — 기록이 "w0.7로 물렸다"고 말하면 안 된다.
            # (참조 사진이 없는 씬과 `reference: none`인 씬이 같은 길로 간다.)
            weight, start_at = 0.0, 0.0

        save_prefix = str(
            self.template[find_by_title(self.template, TITLE_SAVE)]["inputs"].get("filename_prefix")
            or "frames/shorts-factory"
        )
        workflow = build_workflow(
            self.template, prompt=prompt, negative_prompt=negative_prompt, seed=seed,
            reference_name=reference_name, weight=weight, start_at=start_at,
            filename_prefix=f"{save_prefix}-s{scene_id}",
        )
        prompt_id = self._submit(workflow, what=f"{what} 제출")
        log.info(
            "%s: 큐에 넣었다 (%s, seed=%d, 참조=%s w%.2f s%.2f)",
            what, prompt_id, seed, reference_name or "없음", weight, start_at,
        )
        entry = self._wait(prompt_id, budget_until=budget_until, what=what)
        for node_output in (entry.get("outputs") or {}).values():
            for item in (node_output or {}).get("images", []) if isinstance(node_output, dict) else []:
                if isinstance(item, dict) and item.get("filename"):
                    data = self._download(item, what=f"{what} 내려받기")
                    return data, {
                        "prompt_id": prompt_id,
                        "seed": seed,
                        "reference": reference_name,
                        "weight": weight,
                        "start_at": start_at,
                    }
        raise ImageGenError(f"{what}: 잡은 끝났는데 이미지 출력이 없다 ({prompt_id})")
