"""[9. assemble]의 순수 부품 — 전환 계획 · ASS 자막 · FFmpeg 명령.

`tts/`가 `[3]`의 판단(경계 추출·배속 보정)을 API 키 없이 검증되게 떼어 놓은 것과 같은
자리다. 여기 있는 것은 전부 **파일도 프로세스도 만지지 않는 순수 함수**이고, 실제
FFmpeg 실행은 `ffmpeg.py`의 얇은 경계 함수 하나뿐이다.

- `timeline.py` — 씬 계약 → 전환 계획 (specs/03 전환 규칙)
- `subtitles.py` — 씬 계약 → ASS 자막 문서 (specs/03 자막 스타일, ADR-0002 레이어 B)
- `verify.py` — 만든 산출물이 씬 계약과 ±200ms 안에서 맞는지 (specs/00 성공 기준 4)
- `ffmpeg.py` — 필터 그래프·인자 배열 조립 + subprocess 경계
- `fake.py` — 테스트용 FFmpeg 대역
"""
