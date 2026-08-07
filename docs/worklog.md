# worklog

세션을 마무리할 때 맨 위에 항목 1개를 추가한다. 역순 누적이며 기존 항목은 지우지 않는다.
세션 시작 훅이 맨 위 항목 1개만 주입하므로, 다음 세션이 알아야 할 것만 그 항목에 담는다.

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
