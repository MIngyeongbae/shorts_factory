# worklog

세션을 마무리할 때 맨 위에 항목 1개를 추가한다. 역순 누적이며 기존 항목은 지우지 않는다.
세션 시작 훅이 맨 위 항목 1개만 주입하므로, 다음 세션이 알아야 할 것만 그 항목에 담는다.

## 2026-08-09 — [1. script] 슬라이스 1: 씬 계약 검증기

- **세션 목표**: `[1. script]`를 한입에 만들지 않기 위해, 생성기보다 **검증기를 먼저** 만든다
- **배경 — 대본 단계 슬라이스 계획 (이 순서로 간다)**
  - 0) 골든 대본 픽스처(사람이 직접) → 1) **씬 스키마 검증기(이번 세션)** → 2) 스펙 01 대본 규칙 검증기
    → 3) 그라운딩 검증기(ADR-0007) → 4) 생성기(후보 **1개**, FakeLLM) → 5) 실전 세션 1회
    → 6+) 재생성 루프 · 후보 N개 · `[1b] score` · `[2b] judge`
  - 이유: `[1. script]`는 LLM 한 번의 출력에 7가지(7단 구조·시그니처·분량·비트 enum·씬 필드·그라운딩·후보 N개)가
    동시에 들어간다. 검증기가 먼저 있어야 생성 실패 시 **어디가 깨졌는지** 알 수 있다
  - 슬라이스마다 "이번에 안 만드는 것"을 먼저 못박고 시작한다. 종료 조건은 항상 테스트 통과
- **완료**
  - `src/shorts_factory/schemas/scenes.py` 신규 — `validate_scenes(data) -> (errors, warnings)`
    (스펙 02 씬 스키마 + 스펙 05 봉투 `run_id/topic/total_duration/scenes`)
  - `tests/fixtures/scenes_pass.json` — 12개 비트를 모두 한 번씩 쓰는 15씬 픽스처
  - 테스트 23건 추가 (총 173건 통과)
- **결정**
  - **`emphasis.type`에 enum을 걸지 않았다.** 스펙 02는 이 값을 "스펙 03의 오버레이 타입 enum"이라고
    하는데 **스펙 03에 그 enum이 없다** (오버레이 열이 한국어 산문이다). CLAUDE.md 원칙 3에 따라
    지어내지 않고 `minLength:1` 문자열로 두고, `test_emphasis_type_is_not_yet_an_enum`으로 공백을 고정했다
  - 시간 역행·겹침 금지는 **스펙에 명시된 문장이 아니라 파생 규칙**이다 ("scene_id는 문장 순서와 일치"에서 파생).
    코드 주석에 그렇게 표시해 뒀다 — 아니라고 보면 지우면 된다
  - `total_duration` 불일치는 error가 아니라 warning (허용 오차 0.5초)
  - TTS 후 필드명이 `start`/`end`로 바뀌는 형태(스펙 02)는 다루지 않았다. `[3. tts+sync]` 시점 계약이다
  - 패키지 `__init__`에는 `validate_scenes`와 상수만 내보냈다. `schema_errors` 등 3개는
    factsheet 쪽과 이름이 겹쳐서 모듈 안에만 둔다
- **남은 일 (다음 세션 = 슬라이스 2)**
  - 스펙 01 대본 규칙 검증기: 7단 순서 · 시그니처 문구 2개 · 545~575자 · 23~28문장 · 수미상관
  - 그 전에 **골든 대본 픽스처**가 필요하다 (아래 주의 참고)
- **주의**
  - `scenes_pass.json`은 **스키마 픽스처이지 골든 대본이 아니다.** 15씬/63.2초라 스펙 01의
    분량 규칙(23~28문장, 90~100초)을 만족하지 않고, 숫자("11만 8천", "97")도 팩트시트에
    그라운딩되지 않은 더미다. 슬라이스 2·3은 각자 다른 픽스처를 새로 만들어야 한다
  - 실전 대본을 뽑으려면 소재가 필요한데 현재 유일한 토픽(각자성석)은 `no-go`다.
    슬라이스 5 전에 백로그 소재 하나를 `package`로 통과시켜 둬야 한다

## 2026-08-07 — [0b. research] 소스 카드 라이브러리 (ADR-0012)

- **세션 목표**: 이미 조사한 소스를 다른 토픽에서 재검색·재fetch하지 않게 만들기
- **완료**
  - `knowledge/sources/{source_id}.md` 평면 카드 + `knowledge/index.md` 자동 생성
  - `src/shorts_factory/knowledge.py` 신규 (id 생성·카드 입출력·인덱스·주입문·계약 파싱)
  - 조사(01)·검증(02) 세션에 인덱스 주입 + `--add-dir`로 카드 Read 허용, `## 참조 소스` 수확
  - `python run.py knowledge reindex`, ADR-0012, specs/06·05 반영
  - 테스트 39건 추가 (총 150건 통과)
- **결정**
  - **재조사 방지가 목적이지 사실 추적 강화가 아니다.** 팩트시트 스키마(`fact.source_ids`)는
    건드리지 않았고, 기존 각자성석 패키지 소급 변환도 하지 않았다 — 효과는 다음 조사부터 난다
  - PyYAML 미도입. frontmatter는 `키: 값` 평면이라 `split(":", 1)`로 읽는다 (의존성은 jsonschema 하나 유지)
  - 세션에 카드를 복사해 넣는 대신 `--add-dir`. 중립 cwd·읽기 전용 도구는 그대로라 ADR-0011 격리는 유지
  - 교차확인은 계약 필드 없이 얻는다. 다른 토픽이 같은 소스에서 같은 문장을 뽑으면
    새 줄이 아니라 그 사실의 `교차확인`에 슬러그가 붙는다
  - 03-critique는 라이브러리에 닿지 않는다 (ADR-0009 격리)
- **남은 일**
  - 실제 헤드리스 세션으로 돌려본 적은 없다. 다음 토픽 `package` 실행이 첫 실전 검증이다.
    특히 세션이 `## 참조 소스` JSON을 규격대로 뱉는지 확인 필요 — 안 뱉으면 경고만 뜨고 카드가 안 생긴다
- **주의**
  - 카드 0장이면 주입문이 빈 문자열이라 파이프라인이 종전과 동일하게 돈다. 첫 실행에서
    카드가 안 생기면 그건 세션이 계약을 안 지킨 것이므로 경고 로그를 볼 것
  - `knowledge/index.md`는 자동 생성 파일이다. 손으로 고치지 말고 `knowledge reindex`를 쓴다

## 2026-08-07 — 세션 간 컨텍스트 이어달리기 장치 도입

- **세션 목표**: worklog + 훅 2개로 세션 간 핸드오프 경로 만들기
- **완료**
  - `.claude/hooks/session-start.ps1` — 세션 시작 시 worklog 최신 항목 + git 요약 주입
  - `.claude/hooks/pre-commit-worklog.ps1` — `git commit` 전 worklog 갱신 여부 경고 (차단 없음)
  - `docs/worklog.md` 신설, CLAUDE.md 작업 규칙 2줄 추가
- **결정**
  - 훅 설정은 `.claude/settings.json`(커밋 대상)에 둔다. `settings.local.json`은 `.gitignore` 대상이라 클론 시 사라짐
  - 경고는 `permissionDecision: defer` + `additionalContext`로 반환해 중간 커밋을 막지 않는다
  - PreToolUse matcher는 `Bash`만. PowerShell 툴로 커밋하면 훅이 발동하지 않는다
- **남은 일**
  - 없음
- **주의**
  - 훅 스크립트는 Windows PowerShell 5.1로 실행된다. 수정 시 UTF-8 **BOM** 유지 필수 (BOM 없으면 한글이 cp949로 읽혀 깨짐)
  - PreToolUse 훅의 `git commit` 정규식은 명령 문자열에 그 글자가 들어 있기만 해도 걸린다 (예: `git show HEAD:.claude/hooks/pre-commit-worklog.ps1`). 경고 전용이라 무해하니 헛경고는 무시한다
