# SessionStart 훅 — docs/worklog.md 최신 항목 1개 + git status 요약을 세션 컨텍스트에 주입한다.
# worklog가 없거나 항목이 없으면 아무것도 주입하지 않고 조용히 통과한다.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$worklog = Join-Path $root 'docs\worklog.md'
if (-not (Test-Path -LiteralPath $worklog)) { exit 0 }

$lines = @(Get-Content -LiteralPath $worklog -Encoding UTF8)
if ($lines.Count -eq 0) { exit 0 }

# 항목 구분자는 '## ' 로 시작하는 줄. 첫 항목(=최신)만 잘라낸다.
$starts = @(0..($lines.Count - 1) | Where-Object { $lines[$_] -match '^##\s' })
if ($starts.Count -eq 0) { exit 0 }

$from = $starts[0]
$to   = if ($starts.Count -ge 2) { $starts[1] - 1 } else { $lines.Count - 1 }
$entry = ($lines[$from..$to] -join "`n").TrimEnd()
if (-not $entry.Trim()) { exit 0 }
if ($entry.Length -gt 4000) { $entry = $entry.Substring(0, 4000) + "`n… (생략)" }

# git 요약 (짧게)
$gitLines = @()
Push-Location -LiteralPath $root
try {
  $branch = & git rev-parse --abbrev-ref HEAD
  if ($LASTEXITCODE -eq 0 -and $branch) { $gitLines += "브랜치: $branch" }
  $porcelain = @(& git status --porcelain | Where-Object { $_ })
  if ($LASTEXITCODE -eq 0) {
    if ($porcelain.Count -eq 0) {
      $gitLines += '워킹트리: clean'
    } else {
      $head = ($porcelain | Select-Object -First 5) -join '; '
      $more = if ($porcelain.Count -gt 5) { " (외 $($porcelain.Count - 5)개)" } else { '' }
      $gitLines += "워킹트리 변경 $($porcelain.Count)개: $head$more"
    }
  }
} catch { } finally { Pop-Location }

$parts = @('[worklog 최신 항목]', $entry)
if ($gitLines.Count -gt 0) { $parts += @('', '[git]') + $gitLines }

@{
  hookSpecificOutput = @{
    hookEventName     = 'SessionStart'
    additionalContext = ($parts -join "`n")
  }
} | ConvertTo-Json -Depth 5 -Compress
exit 0
