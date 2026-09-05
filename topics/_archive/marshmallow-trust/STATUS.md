# STATUS: go

- 소재: 마시멜로 테스트 — 참을성이 아니라 어른을 믿느냐였다
- slug: `marshmallow-trust`
- run_id: `20260902-marshmallow-trust`
- 갱신: 2026-09-02T14:02:20+00:00
- 판정자: 사람 (2026-09-03, "이건 2부진행하고 나머지 토픽은 다 정리해줘 쇼츠로 안만들꺼야 기존에 남아있던것들도 그렇게 해줘")

## 사유

사람 일괄 지시로 7편 동시 go (2026-09-03) — 사람이 제목으로 지목했다. 영상 라인은 local(ComfyUI + MiniMax H3, 변동비 0) — judgment/human.json 참조.

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
