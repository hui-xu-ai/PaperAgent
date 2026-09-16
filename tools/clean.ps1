# clean.ps1 —— 清理项目生成物（只清"忽略+可清理"目录），默认 dry-run 先列、确认后才删。
# 用法：
#   tools\clean.ps1                    # 列出将删的生成物（dry-run，不删）
#   tools\clean.ps1 -Run               # 连运行时/草稿目录(work/logs/tmp/out/feedback)一起列出
#   tools\clean.ps1 -Execute           # 真正删除（若不加 -Run，仅删构建/测试缓存类生成物）
# 安全：绝不删 src/tests/resources/docs/smoke 等"版本跟踪"目录。
param(
  [string]$Root = $PWD,
  [switch]$Run,
  [switch]$Execute
)
$ErrorActionPreference = 'Stop'
$Gen = @('dist', 'build', '.pytest_cache', '.mypy_cache', 'htmlcov', '.coverage', '__pycache__')
$Runtime = @('work', 'logs', 'tmp', 'out', 'feedback')
$targets = @()
$targets += $Gen
if ($Run) { $targets += $Runtime }
Write-Host "Root : $Root"
Write-Host "Mode : $(if($Execute){'EXECUTE（真删）'} else {'dry-run（只列）'})"
$found = $false
foreach ($t in $targets) {
  $p = Join-Path $Root $t
  if (Test-Path $p) {
    $found = $true
    if ($Execute) { Remove-Item $p -Recurse -Force; Write-Host "removed   $t" }
    else { Write-Host "would-remove  $t" }
  }
}
if (-not $found) { Write-Host '(本目录无可清理项)' }
if (-not $Execute) { Write-Host '── 提示：加 -Execute 真正删除；加 -Run 连运行时/草稿目录一起处理。危险操作前先 git status 核对。' }
