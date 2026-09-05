# STATUS: no-go

- 소재: 떡볶이 — 고추장 떡볶이는 1953년에 태어났다
- slug: `tteokbokki-1953`
- run_id: `20260904-tteokbokki-1953`
- 갱신: 2026-09-04T18:16:41+00:00
- 판정자: (미정 — 인간 게이트)

## 사유

토픽 패키지 생성됨. script.md(대본)와 factcheck.md(검증)가 완성된 뒤 사람이 go / no-go를 기록한다 (ADR-0049).

## 1부 진행 상황

- [x] 0. seed
- [x] 0f. seedfetch
- [x] 1. draft
- [x] 2. factcheck
- [x] 2l. localize

## 게이트 규칙 (ADR-0009 → ADR-0094)

판정은 **`script.md` 맨 위 주석 블록**이다 — 대본을 읽은 그 파일에서 주석만 푼다.
`reject`를 풀면 반려, `ko`·`ja`·`en`을 풀면 그 언어로 2부가 돌고, 아무것도 안 풀면 보류다.
**이 파일은 사람이 훑는 체크리스트이고, 판정을 여기에 적으면 파이프라인은 읽지 못한다.**
머리글은 그 블록의 투영이다 (`tools/backlog_sync.py`가 맞춘다).
