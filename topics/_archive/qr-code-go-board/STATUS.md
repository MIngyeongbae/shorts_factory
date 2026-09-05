# STATUS: go

- 소재: QR 코드 — 바둑판에서 나왔고 특허를 안 걸었다
- slug: `qr-code-go-board`
- run_id: `20260827-qr-code-go-board`
- 갱신: 2026-08-27T05:58:20+00:00
- 판정자: 사람 (2026-08-27, "qr 코드를 순서대로 쭉 쇼츠로 만들어봐 … go art 루트로 해서 계속 진행해")

## 사유

사람의 사전 일괄 지시로 go (2026-08-27). 영상 라인은 `art` — 일반 씬은 MJ CLEAN을 first로 잇고 `info` 씬은 H3 텍스트→영상이다 (ADR-0070·0075). judgment/human.json 참조.

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
