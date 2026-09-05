# STATUS: go

- 소재: 천일염 — 한국 전통 소금이 아니라 1907년 수입품이다
- slug: `sun-dried-salt-1907`
- run_id: `20260830-sun-dried-salt-1907`
- 갱신: 2026-08-30T03:47:38+00:00
- 판정자: 사람 (2026-08-29, "1부 다 만들면 go, art 라인으로해서 쭉 다 만들어")

## 사유

사람 지시(2026-08-29, "…이렇게 쇼츠를 만들자 1부 다 만들면 go, art 라인으로해서 쭉 다 만들어"). 영상 라인은 `art`다 (ADR-0070·0075). judgment/human.json 참조.

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
