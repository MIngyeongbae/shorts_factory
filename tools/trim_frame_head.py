"""`[6]` 프레임이 붙은 클립의 **머리에 낀 정지 구간**을 잘라낸다 (2026-09-02 사람 지시).

## 왜 필요한가

ADR-0087이 `local` 라인에 first frame을 달았는데, **H3가 그 그림을 이어 그리지 않는다.**
원본(24fps) 실측: 프레임 0→1 변화량 65, 그 뒤 0.1~0.4. 준 그림을 1프레임만 내보내고
버린 뒤 자기 영상을 그린다. 30fps로 늘어나며 2~4프레임이 되어 **"사진이 잠깐 들어갔다
사라지는"** 것으로 보인다.

사람 결정: **영상을 이미 만든 편은 다시 만들지 않고 머리만 잘라 쓴다.** 잘라내면 그
그림이 화면에서 사라지므로, 라인을 되돌린 것과 결과가 같으면서 재생성 3시간을 아낀다.

## 얼마나 자르는가 — **씬마다 다르다**

튀는 지점이 0.10초~1.33초로 제각각이라 1초 고정은 한쪽으로 틀린다(과하면 뒤에 정지가
생기고, 모자라면 증상이 남는다). 그래서 **그 클립에서 실제로 튄 마지막 지점까지**만
자르고, 남는 길이가 가장 긴 언어의 씬 길이보다 짧아지지 않게 상한을 건다.

`info` 씬은 건드리지 않는다 — 프레임을 안 받으므로 이 문제가 없다.

원본은 `clips.pretrim/`에 남긴다. 다시 돌려도 안전하다(원본에서 다시 자른다).

사용:
    python tools/trim_frame_head.py <run_id> [--apply]
`--apply` 없이 돌리면 무엇을 얼마나 자를지만 보여준다.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

#: 이 값을 넘는 프레임 간 변화를 "튐"으로 본다. 실측: 정상 움직임은 4 이하,
#: 그림이 버려지는 지점은 45~85다. 25는 그 사이의 넓은 골이다.
JUMP = 25.0
#: 머리에서 튐을 찾는 범위(초). 그보다 늦은 변화는 영상 내용이지 프레임 교체가 아니다.
SEARCH_SECONDS = 1.6
FPS = 30
#: 여유를 이만큼까지는 넘겨 자른다. 모자라게 자르면 **앞에서 번쩍임이 남고**, 넘겨 자르면
#: 가장 긴 언어에서만 끝 프레임이 그만큼 정지한다 (`[9]`의 tpad). 눈에 띄는 쪽은 앞이다.
OVERSHOOT = 0.25


def _frames(path: Path, count: int) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=64:-1",
         "-frames:v", str(count), "-f", "image2pipe", "-vcodec", "ppm", "-"],
        capture_output=True).stdout
    ims, buf = [], io.BytesIO(out)
    while True:
        head = buf.readline()
        if not head.startswith(b"P6"):
            break
        w, h = (int(x) for x in buf.readline().split())
        buf.readline()
        ims.append(np.frombuffer(buf.read(w * h * 3), dtype=np.uint8)
                   .reshape(h, w, 3).astype(np.float32))
    return np.stack(ims) if ims else np.empty((0,))


def head_jump_seconds(path: Path) -> float:
    """머리에서 마지막으로 튄 지점(초). 튐이 없으면 0.0."""
    a = _frames(path, int(SEARCH_SECONDS * FPS) + 2)
    if len(a) < 4:
        return 0.0
    diff = np.abs(a[1:] - a[:-1]).mean(axis=(1, 2, 3))
    hits = [i for i, x in enumerate(diff) if x > JUMP]
    return (max(hits) + 1) / FPS if hits else 0.0


def trim(src: Path, dest: Path, start: float) -> None:
    """`start`초부터 끝까지 다시 인코딩. `[9]`가 어차피 한 번 더 인코딩하므로 crf를 낮게 둔다."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src),
         "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-an", str(dest)],
        check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--apply", action="store_true", help="실제로 자른다 (없으면 계획만 보여준다)")
    args = ap.parse_args()

    run = ROOT / "runs" / args.run_id
    clips_dir = run / "clips"
    backup = run / "clips.pretrim"
    contract = json.loads((run / "scenes.json").read_text(encoding="utf-8"))
    record = json.loads((run / "clips.json").read_text(encoding="utf-8"))
    has_info = {int(s["scene_id"]): bool(s.get("info")) for s in contract["scenes"]}

    print(f"{args.run_id} — {'적용' if args.apply else '계획만'}")
    print("씬 | 튐 지점 | 여유  | 자를 길이 | 남는 길이")
    total = 0
    for entry in record["scenes"]:
        sid = int(entry["scene_id"])
        if has_info.get(sid):
            continue
        src = backup / f"{sid}.mp4" if (backup / f"{sid}.mp4").exists() else clips_dir / f"{sid}.mp4"
        if not src.exists():
            continue
        jump = head_jump_seconds(src)
        seconds = float(entry["seconds"])
        longest = max(float(v) for v in entry["lang_seconds"].values())
        room = seconds - longest
        cut = min(jump, max(0.0, room) + OVERSHOOT)
        if cut <= 0:
            print(f"{sid:>2} | {jump:6.2f}s | {room:5.2f} |     —     | (자르지 않음)")
            continue
        total += 1
        print(f"{sid:>2} | {jump:6.2f}s | {room:5.2f} | {cut:8.2f}s | {seconds - cut:6.2f}s")
        if args.apply:
            backup.mkdir(exist_ok=True)
            if not (backup / f"{sid}.mp4").exists():
                shutil.copy2(clips_dir / f"{sid}.mp4", backup / f"{sid}.mp4")
            trim(backup / f"{sid}.mp4", clips_dir / f"{sid}.mp4", cut)

    print(f"\n자를 클립 {total}개. 원본은 clips.pretrim/에 남는다.")
    if args.apply:
        print("다음: python run.py assemble --slug <slug> --force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
