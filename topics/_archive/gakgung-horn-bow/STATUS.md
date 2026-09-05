# STATUS: go

- 소재: 각궁 — 조선 활의 핵심 재료는 수입품이었다
- slug: `gakgung-horn-bow`
- run_id: `20260831-gakgung-horn-bow`
- 갱신: 2026-08-31T05:30:05+00:00
- 판정자: 사람 (2026-08-31, "2, 4, 7, 10은 쇼츠로 만들고")

## 사유

사람 지시(2026-08-31, "2, 4, 7, 10은 쇼츠로 만들고"). 영상 라인은 `local`이다. judgment/human.json 참조.

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
