# worklog

세션을 마무리할 때 맨 위에 항목 1개를 추가한다. 역순 누적이며 기존 항목은 지우지 않는다.
세션 시작 훅이 맨 위 항목 1개만 주입하므로, 다음 세션이 알아야 할 것만 그 항목에 담는다.

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
