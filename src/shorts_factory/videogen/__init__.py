"""영상 생성 어댑터 — `[7] videogen`의 텍스트→영상 경로 (ADR-0056).

주 경로는 Gemini Omni Flash(`omni.py`, A2에서 신설)이고 `veo.py`는 같은 REST·`transport`
경계를 쓰는 선행 어댑터다. `comfy_h3.py`는 로컬 GPU 라인(ADR-0059)이다.

MJ 영상 어댑터(`midjourney.py`)는 ADR-0056이 지웠다가 **ADR-0070이 되살렸다** — `art`
라인이 CLEAN·INFO 두 장을 first/last로 잇기 때문에 이미지 입력이 다시 생겼다.
"""
