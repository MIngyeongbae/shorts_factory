"""테스트용 FFmpeg 대역. `tts/fake.py`와 같은 자리다.

이 환경에는 FFmpeg가 없고, 있어도 `[9]`의 판단은 전부 필터 그래프를 만드는 쪽에 있다
(전환 종류·오프셋·트림 길이·자막 시각). 실제 인코딩이 확인해 주는 것은 "FFmpeg가 이
그래프를 받아들이는가" 하나뿐이라 그건 실물 클립이 생기는 `[7]` 이후에 한 번 본다.

그래서 이 대역은 **명령을 기록하고 산출물 자리에 파일 하나를 놓는** 일만 한다. 인코딩을
흉내 내지 않는다 — 흉내 낸 픽셀로는 아무것도 검증되지 않는다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence


class FakeFFmpeg:
    """`run_ffmpeg`가 부르는 subprocess.run 대역."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: str = "",
        writes_output: bool = True,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.writes_output = writes_output
        self.calls: list[dict[str, Any]] = []

    @property
    def last(self) -> list[str]:
        return self.calls[-1]["cmd"]

    def option_of(self, cmd: Sequence[str], name: str) -> str:
        """명령에서 옵션 하나의 값을 꺼낸다."""
        args = list(cmd)
        return args[args.index(name) + 1]

    def graph_of(self, cmd: Sequence[str]) -> str:
        """명령에서 `-filter_complex` 값을 꺼낸다 (`[9]`의 그래프)."""
        return self.option_of(cmd, "-filter_complex")

    @property
    def graph(self) -> str:
        return self.graph_of(self.last)

    @property
    def vf(self) -> str:
        """`[7]`은 입력이 하나라 `-vf`를 쓴다."""
        return self.option_of(self.last, "-vf")

    def __call__(
        self, cmd: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess:
        args = list(cmd)
        cwd = Path(kwargs.get("cwd") or ".")
        self.calls.append({"cmd": args, "cwd": cwd, "kwargs": kwargs})

        if self.returncode != 0:
            return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)

        if self.writes_output:
            output = cwd / args[-1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fake-mp4")

        return subprocess.CompletedProcess(args, 0, "", "")


def write_fake_clips(
    clips_dir: Path, scene_ids: Sequence[int], *, payload: bytes = b"fake-clip"
) -> list[Path]:
    """`[7. motion]`이 놓을 자리에 클립 파일을 만든다 (specs/05 `clips/{scene_id}.mp4`).

    길이도 화면도 없는 껍데기다. `[9]`가 클립에서 읽는 것은 **존재 여부**뿐이고,
    길이는 계약(씬 길이 + 0.6초)으로 알고 있다. `[7]`이 생긴 뒤에도 `[9]` 테스트는
    이 껍데기를 쓴다 — 두 단계를 파일 규약 하나로만 묶어 두려는 것이다.
    """
    clips_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for scene_id in scene_ids:
        path = clips_dir / f"{scene_id}.mp4"
        path.write_bytes(payload)
        paths.append(path)
    return paths
