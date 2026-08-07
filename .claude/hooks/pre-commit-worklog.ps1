# PreToolUse(Bash) 훅 — git commit 직전 docs/worklog.md 갱신 여부를 확인한다.
# 미갱신이어도 차단하지 않고 경고만 돌려준다 (중간 커밋 허용).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$raw = [Console]::In.ReadToEnd()
if (-not $raw) { exit 0 }
try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }

$cmd = $payload.tool_input.command
if (-not $cmd) { exit 0 }
if ($cmd -notmatch '\bgit\b[^&|;]*\bcommit\b') { exit 0 }

$root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$dirty = $false
Push-Location -LiteralPath $root
try {
  # 스테이지/워킹트리 어느 쪽이든 변경이 있으면 갱신된 것으로 본다.
  $status = @(& git status --porcelain -- docs/worklog.md | Where-Object { $_ })
  if ($LASTEXITCODE -ne 0) { exit 0 }
  $dirty = $status.Count -gt 0
} catch { exit 0 } finally { Pop-Location }

if ($dirty) { exit 0 }

$msg = 'worklog.md 미갱신. 세션을 마무리하는 커밋이라면 이번 세션의 완료 작업 / 내린 결정 / 남은 할 일 / 다음 세션 주의사항을 docs/worklog.md 맨 위에 추가한 뒤 커밋하라. 중간 커밋이면 그대로 진행해도 된다.'

@{
  hookSpecificOutput = @{
    hookEventName      = 'PreToolUse'
    permissionDecision = 'defer'
    additionalContext  = $msg
  }
  systemMessage = 'worklog.md 미갱신 (경고만, 차단 안 함)'
} | ConvertTo-Json -Depth 5 -Compress
exit 0
