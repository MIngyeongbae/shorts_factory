# STATUS: go

- 소재: 주민등록번호 — 간첩을 찾으려 만든 13자리
- slug: `korean-id-number`
- run_id: `20260824-korean-id-number`
- 갱신: 2026-08-24T01:04:13+00:00
- 판정자: 사람 (2026-08-24, "새 토픽 6개를 2부 끝까지 진행해서 쇼츠를 만들어")

## 사유

사람 일괄 지시로 6편 동시 go (2026-08-24). 영상 라인은 기본 라인 local(ComfyUI + MiniMax H3, 변동비 0) — judgment/human.json 참조. 화면 번호는 사람이 「완전한 13자리를 화면에 세우지 않는다」로 정했다 — 칸 구조·자리 뜻·가중치까지만 그린다.

## 1부 진행 상황

- [x] 0. seed
- [x] 0f. seedfetch
- [x] 1. draft
- [x] 2. factcheck
- [x] 2l. localize

## 게이트 규칙 (ADR-0009)

이 파일이 `go`가 되기 전에는 2부(영상 생산)로 진입할 수 없다.
`go` / `no-go`는 script.md(대본)와 factcheck.md(검증)를 사람이 읽은 뒤 직접 기록한다 (ADR-0049).
