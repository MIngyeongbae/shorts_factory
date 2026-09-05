# STATUS: go

- 소재: 고려청자 — 비색은 유약 색이 아니라 기포다
- slug: `goryeo-celadon`
- run_id: `20260831-goryeo-celadon`
- 갱신: 2026-08-31T14:10:30+00:00
- 판정자: 사람 (2026-09-01, STATUS.md 직접 입력)

## 사유

사람이 script.md·factcheck.md를 읽고 go. 영상 라인은 기본 라인 `local`이다 (사람 상시 지시 2026-08-31). judgment/human.json 참조.

## 1부 진행 상황

- [x] 0. seed
- [x] 0f. seedfetch
- [x] 1. draft
- [x] 2. factcheck
- [x] 2l. localize

## 게이트 규칙 (ADR-0009)

`judgment/human.json`의 `decision`이 `go`가 되기 전에는 2부(영상 생산)로 진입할 수 없다.
**이 파일은 사람이 훑는 체크리스트이고, 판정을 여기에 적으면 파이프라인은 읽지 못한다.**
`go` / `no_go`는 script.md(대본)와 factcheck.md(검증)를 사람이 읽은 뒤
`judgment/human.json`에 기록한다 (ADR-0049, 스펙 07의 판정 스키마).
