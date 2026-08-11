"""원본 SRT → 골든 픽스처(scenes.json) 변환기.

레퍼런스 채널의 SRT 원본은 남의 저작물이라 저장소에 올리지 않는다. 거기서 만든 골든
픽스처(`tests/fixtures/scenes_golden_baegak.json`)도 대본 전문을 담고 있어 마찬가지다.
둘 다 `.gitignore` 대상이고, 이 스크립트가 로컬에서 픽스처를 재생성한다.

    python tools/srt_to_scenes.py

SRT 원본이 없으면 스펙 01 검증 테스트가 skip된다 (tests/test_script_rules.py).

## 이 스크립트에 사람 판단이 들어간 부분

`BEATS`·`SUBJECTS`·`EMPHASIS`·`KLING`은 백악 편을 보고 손으로 붙인 태깅이다. `CAMERA`는
specs/03-visual-rules.md의 비트별 기본값을 그대로 쓰고, 텍스트와 타임스탬프는 SRT에서
그대로 옮긴다 (ADR-0013: 씬 1개 = 자막 줄 1개).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRT = ROOT / "신비한 건축사전 대본" / "경복궁_뒷산은_왜_500년_동안_도끼가_금지됐을까_ko.srt"
OUT = ROOT / "tests" / "fixtures" / "scenes_golden_baegak.json"

RUN_ID = "reference-baegak"
TOPIC = "경복궁 뒷산 벌목 금지 (백악)"

#: 자막 줄별 비트. 내용 기준 수동 태깅 (specs/02 비트 테이블).
BEATS = (
    "hook_fact", "hook_twist",
    "context", "context", "context", "context", "context", "context",
    "failed_solution", "failure_reason", "failed_solution", "failure_reason",
    "dilemma_peak", "turning_point",
    "solution_step", "solution_step", "solution_step", "solution_number",
    "solution_step", "solution_step", "solution_step", "solution_step",
    "solution_step", "solution_number",
    "present_link", "present_link", "present_link", "ending_echo",
)

SUBJECTS = (
    "백악 능선 원경", "경복궁 뒤편과 백악", "산으로 둘러싸인 한양 분지 조감",
    "도성 안 민가 밀집 조감", "땔감 지게를 진 백성", "산자락에 쌓인 장작더미",
    "나무가 사라진 민둥산", "흙탕물이 쏟아지는 개천", "토사로 메워진 개천 바닥",
    "준설 작업 중인 인부들", "묘목을 심는 손", "도끼로 나무를 베는 손",
    "아궁이에 들어가는 묘목", "백악 정면 대칭 구도", "뿌리가 흙을 붙잡은 산비탈 단면",
    "도성을 두른 네 산 조감", "금표가 선 소나무 숲", "성 밖 십리 경계 지도 조감",
    "산을 도는 산지기", "철거되는 무허가 집", "금산 관련 고문서", "궁궐 기둥용 소나무",
    "흙을 붙든 소나무 뿌리 단면", "세월이 흐르는 백악 능선", "베이지 않은 백악 숲",
    "경복궁 마당에서 올려다본 백악", "궁궐 기둥과 백악 숲", "백악 능선 원경",
)

#: specs/03 비트별 카메라 기본값. 복합 카메라 워크 금지 (AI 영상 왜곡 방지).
CAMERA = {
    "hook_fact": "slow_zoom_in", "hook_twist": "static",
    "context_number": "slow_zoom_in", "failed_solution": "static",
    "failure_reason": "slow_zoom_in", "dilemma_peak": "static",
    "turning_point": "slow_zoom_in", "solution_number": "static",
    "present_link": "slow_zoom_out", "ending_echo": "slow_zoom_out",
}
#: context는 "pan 또는 tilt", solution_step은 "tilt_down 또는 slow_zoom_in" (specs/03)
CONTEXT_CAMERAS = ("pan_right", "tilt_down", "pan_left", "tilt_up", "pan_right", "tilt_down")
SOLUTION_STEP_CAMERA = "tilt_down"

#: SUBJECTS와 1:1. specs/03의 구도 축 (ADR-0018). 실물의 잘린 면은 `close`고
#: `diagram`은 도면·도해·일러스트일 때만 쓴다 (specs/03).
SUBJECT_SCALES = (
    "wide", "wide", "wide", "wide", "wide", "wide", "wide", "wide", "wide", "wide",
    "close", "close", "wide", "wide", "close", "wide", "wide", "wide", "wide", "wide",
    "wide", "wide", "close", "wide", "wide", "wide", "wide", "wide",
)

#: scene_id → (오버레이 타입, 값). 숫자 비트는 필수, 그 외는 옵션 (specs/02).
#: 타입은 specs/03의 오버레이 타입 enum뿐이다 — 빨간 X 계열은 ADR-0019로 폐기됐다.
EMPHASIS = {
    1: ("big_red_text", "500년"), 18: ("big_red_text", "4km"),
    24: ("big_red_text", "500년"),
}
#: 유체 모션(물·비·안개·불)이 서사상 필요한 씬만 kling (ADR-0006)
KLING = {8: "비·흙모래 유체 모션이 서사상 필요한 씬 (ADR-0006)"}


def _seconds(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    secs, millis = rest.split(",")
    return round(int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis) / 1000, 3)


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        start, end = lines[1].split(" --> ")
        cues.append((_seconds(start), _seconds(end), " ".join(lines[2:])))
    return cues


def build(cues: list[tuple[float, float, str]]) -> dict:
    if not len(cues) == len(BEATS) == len(SUBJECTS) == len(SUBJECT_SCALES):
        raise SystemExit(
            f"태깅 테이블과 자막 줄 수가 다르다: 큐 {len(cues)} / 비트 {len(BEATS)} / "
            f"피사체 {len(SUBJECTS)} / 스케일 {len(SUBJECT_SCALES)}"
        )

    scenes, context_seen = [], 0
    for index, (start, end, text) in enumerate(cues, start=1):
        beat = BEATS[index - 1]
        if beat == "context":
            camera = CONTEXT_CAMERAS[context_seen]
            context_seen += 1
        elif beat == "solution_step":
            camera = SOLUTION_STEP_CAMERA
        else:
            camera = CAMERA[beat]

        scene = {"scene_id": index, "beat": beat, "text": text,
                 "est_start": start, "est_end": end}
        if index in EMPHASIS:
            kind, value = EMPHASIS[index]
            scene["emphasis"] = {"type": kind, "value": value}
        scene["subject"] = SUBJECTS[index - 1]
        scene["subject_scale"] = SUBJECT_SCALES[index - 1]
        scene["camera"] = camera
        scene["motion"] = "kling" if index in KLING else "kenburns"
        scene["notes"] = KLING.get(index, "")
        scenes.append(scene)

    return {"run_id": RUN_ID, "topic": TOPIC,
            "total_duration": cues[-1][1], "scenes": scenes}


def main() -> int:
    if not SRT.exists():
        print(f"원본 SRT가 없다: {SRT}", file=sys.stderr)
        print("저작권 있는 원본이라 저장소에 없다. 로컬에 두고 다시 실행할 것.", file=sys.stderr)
        return 1

    document = build(parse_srt(SRT))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)} — {len(document['scenes'])}씬 / {document['total_duration']}초")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
