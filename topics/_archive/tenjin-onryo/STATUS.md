# STATUS: no-go

- 소재: 학문의 신 텐진은 원령이었다 (스가와라노 미치자네)
- slug: `tenjin-onryo`
- run_id: `20260822-tenjin-onryo`
- 갱신: 2026-08-21T19:08:28+00:00
- 판정자: 사람 (2026-09-03, "이건 2부진행하고 나머지 토픽은 다 정리해줘 쇼츠로 안만들꺼야 기존에 남아있던것들도 그렇게 해줘")

## 사유

사람 일괄 반려 (2026-09-03). 2부로 보낼 7편을 사람이 지목했고 이 편은 거기 들지 않았다 — 대본 결함이 아니라 편 고르기의 결과다 (reason_code: not_selected). judgment/human.json 참조. ADR-0088 기준 ②에 따라 topics/_archive/로 옮겼다.

## 1부 진행 상황

- [x] 0. seed
- [x] 1. draft
- [x] 2. factcheck

## 게이트 규칙 (ADR-0009)

`judgment/human.json`의 `decision`이 `go`가 되기 전에는 2부(영상 생산)로 진입할 수 없다.
**이 파일은 사람이 훑는 체크리스트이고, 판정을 여기에 적으면 파이프라인은 읽지 못한다.**
`go` / `no_go`는 script.md(대본)와 factcheck.md(검증)를 사람이 읽은 뒤
`judgment/human.json`에 기록한다 (ADR-0049, 스펙 07의 판정 스키마).
