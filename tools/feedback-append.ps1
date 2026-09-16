# feedback-append.ps1 —— 多会话安全的反馈追加（防并发写冲突）
# 用法：
#   tools/feedback-append.ps1 -Title "V15（xx 主题）" -Draft "feedback\2026-08-27-xx.md"
# 行为：
#   1) 用独占锁文件串行化并发写入（重试等待，避免多会话互相覆盖/写失败）；
#   2) 在锁内做"读-改-写"：把草稿作为新块**插入文件顶部**（保持 V 编号最新在顶的惯例）；
#   3) 追加后 git 提交（index.lock 竞争时重试）。
param(
  [Parameter(Mandatory = $true)][string]$Title,     # 新块标题，如 "V15（xxx 主题）"
  [Parameter(Mandatory = $true)][string]$Draft,     # 草稿文件路径（相对仓库根），如 feedback\2026-08-27-xx.md
  [string]$FeedbackFile = "AGENT_FEEDBACK.md",
  [string]$RepoRoot = ".",
  [int]$LockRetries = 40,
  [int]$LockSleepMs = 250,
  [int]$GitRetries = 5
)

$ErrorActionPreference = "Stop"
Set-Location $RepoRoot

$feedbackPath = Join-Path (Get-Location) $FeedbackFile
$draftPath = Join-Path (Get-Location) $Draft
$lockPath = "$feedbackPath.lock"

if (-not (Test-Path $draftPath)) { Write-Error "草稿不存在: $draftPath"; exit 1 }

# 标题规范化：与文件惯例一致（# AGENT_FEEDBACK Vxx（…））
if ($Title -match '^V\d') { $Title = "AGENT_FEEDBACK $Title" }

# ── 1. 取锁（独占打开，失败即重试；超时则放弃，绝不写主文件）──
$lock = $null
for ($i = 0; $i -lt $LockRetries; $i++) {
  try {
    $lock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    break
  } catch {
    if ($i -eq $LockRetries - 1) { Write-Error "取锁超时（$LockRetries 次重试）：可能有多会话正在写，稍后重试。"; exit 2 }
    Start-Sleep -Milliseconds $LockSleepMs
  }
}

try {
  # ── 2. 读草稿与现有主文件，组新块（锁内读-改-写，安全串行化）──
  $draftText = [System.IO.File]::ReadAllText($draftPath, [System.Text.Encoding]::UTF8).TrimEnd()
  # 草稿若自带 H1（以 # 开头），去掉——包装标题（$Title）是唯一的 H1，避免重复标题
  $draftLines = $draftText -split "`r?`n"
  if ($draftLines.Count -gt 0 -and $draftLines[0].TrimStart().StartsWith("# ")) {
    $draftLines = $draftLines | Select-Object -Skip 1
  }
  $draftText = ($draftLines -join "`n").Trim()
  $block = "# $Title`n`n$draftText`n"
  $existing = ""
  if (Test-Path $feedbackPath) {
    $existing = [System.IO.File]::ReadAllText($feedbackPath, [System.Text.Encoding]::UTF8).TrimEnd()
  }
  $newText = if ($existing) { "$block`n`n---`n`n$existing`n" } else { "$block`n" }

  # ── 3. 写回（无 BOM UTF-8）──
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($feedbackPath, $newText, $utf8NoBom)
  Write-Output "已前置追加「$Title」到 $FeedbackFile（最新在顶）"
} finally {
  $lock.Dispose()
  Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}

# ── 4. git 提交（带重试：多会话并发 commit 会争 index.lock）──
for ($i = 0; $i -lt $GitRetries; $i++) {
  try {
    git add -- $FeedbackFile 2>$null
    git commit -m "docs: 追加 $Title 反馈" 2>$null
    Write-Output "已提交 git（$Title）"
    break
  } catch {
    if ($i -eq $GitRetries - 1) { Write-Warning "git 提交失败（可能并发）：请稍后手动提交 $FeedbackFile" }
    Start-Sleep -Milliseconds 500
  }
}
