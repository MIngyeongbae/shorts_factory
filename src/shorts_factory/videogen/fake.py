"""테스트·오프라인용 영상 어댑터. `tts/fake.py`·`llm/fake.py`와 같은 자리다.

`[7. videogen]`의 판단(클립 길이 계산·`{seconds}` 치환·검수 사다리·인접 재사용·직렬
기록·재실행 스킵)은 전부 프로바이더 밖에 있다. 이 대역은 **요청을 기록하고 mp4처럼
보이는 바이트를 돌려주는** 일만 한다.

- 기본은 `ftyp` 머리만 있는 **껍데기**다 — `GeneratedClip`이 보는 것이 그것뿐이고,
  테스트는 FFmpeg 없이 돈다 (`video/fake.FakeFFmpeg`가 정규화·프레임 추출을 대신한다)
- `synth=True`면 FFmpeg `lavfi testsrc`로 **재생되는** 클립을 만든다 — CLI `--provider fake`로
  실편 배관을 끝까지 통과시켜 볼 때 쓴다. 네트워크는 여전히 없다

`responses`를 주면 호출 순서대로 돌려준다 (바이트·예외·콜러블). 사다리 테스트가
"1회 실패 → 재시도 성공" 같은 순서를 짜는 자리다.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .base import GeneratedClip, VideoClient, VideoGenError, VideoRequest

#: 껍데기 mp4 — `ftyp` 박스만 있다. 디코더는 못 열지만 `GeneratedClip`은 받아 준다.
STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32

Response = bytes | Exception | Callable[[VideoRequest], bytes]


def synth_mp4(seconds: int, *, executable: str = "ffmpeg", label: str = "") -> bytes:
    """FFmpeg `testsrc`로 만든 실제 mp4 바이트. 720×1280 24fps — Omni 프로브 규격과 같다."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.mp4"
        cmd = [
            executable, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size=720x1280:rate=24:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except FileNotFoundError as exc:
            raise VideoGenError(f"FFmpeg를 찾을 수 없다: {executable!r} (fake synth)") from exc
        if proc.returncode != 0 or not out.exists():
            raise VideoGenError(f"fake synth 실패 ({label}): {proc.stderr[:300]}")
        return out.read_bytes()


class FakeVideoClient(VideoClient):
    """`responses`를 주면 순서대로, 안 주면 껍데기(또는 `synth`) mp4를 돌려준다."""

    name = "fake"
    #: first/last를 실제로 싣는 어댑터다 (ADR-0071).
    accepts_frames = True
    output_suffix = ".mp4"

    def __init__(
        self,
        responses: Sequence[Response] | None = None,
        *,
        synth: bool = False,
        ffmpeg: str = "ffmpeg",
        concurrency: int = 2,
    ) -> None:
        self._responses = None if responses is None else list(responses)
        self.synth = synth
        self.ffmpeg = ffmpeg
        self._concurrency = concurrency
        self.calls: list[dict[str, Any]] = []

    def concurrency(self) -> int:
        return self._concurrency

    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        self.calls.append({
            "scene_id": request.scene_id,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seconds": request.seconds,
            "timeout": timeout,
            "label": request.label,
            # 프레임을 입력으로 받는 라인이 무엇을 실었는지 (ADR-0070·0071).
            # 안 싣는 라인에서는 둘 다 None이고, 그 부재가 곧 계약이다.
            "first_frame": request.first_frame,
            "last_frame": request.last_frame,
        })

        if self._responses is not None:
            if not self._responses:
                raise VideoGenError(f"준비된 픽스처 응답이 없다 (scene={request.scene_id})")
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            data = item(request) if callable(item) else item
        elif self.synth:
            data = synth_mp4(
                request.seconds or 3, executable=self.ffmpeg, label=f"scene {request.scene_id}"
            )
        else:
            data = STUB_MP4

        return GeneratedClip(
            data=data,
            request_id=f"fake-{len(self.calls)}",
            model_id="fake-video",
            duration=float(request.seconds) if request.seconds else None,
        )
