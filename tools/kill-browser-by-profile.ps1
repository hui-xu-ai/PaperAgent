# kill-browser-by-profile.ps1 —— 只按 user-data-dir 特征杀测试浏览器进程
# 安全铁律：严禁 Stop-Process -Name msedge/chrome 无差别杀（会误杀 Harness 浏览器）。
# 本脚本只匹配命令行里包含指定 profile 路径的浏览器进程。
# 用法：
#   tools/kill-browser-by-profile.ps1 -ProfilePath "D:\proj\work\.edge-test"
#   tools/kill-browser-by-profile.ps1 -ProfilePath "D:\proj\work\.edge-test" -WhatIf   # 先看会杀谁
param(
  [Parameter(Mandatory = $true)][string]$ProfilePath,   # 测试浏览器 user-data-dir 的绝对路径
  [string[]]$Names = @("msedge.exe", "chrome.exe"),
  [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

if ($ProfilePath.Length -lt 8) {
  Write-Error "ProfilePath 太短（<$ProfilePath>），拒绝执行——避免误匹配。请传完整绝对路径。"
  exit 1
}
if (-not [System.IO.Path]::IsPathRooted($ProfilePath)) {
  Write-Error "ProfilePath 必须是绝对路径。"
  exit 1
}

$nameFilter = ($Names | ForEach-Object { "Name='$_'" }) -join " OR "
$candidates = Get-CimInstance Win32_Process -Filter $nameFilter -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains($ProfilePath) }

if (-not $candidates) {
  Write-Output "未找到匹配 profile「$ProfilePath」的浏览器进程。"
  exit 0
}

Write-Output ("匹配到 {0} 个进程（profile: {1}）：" -f $candidates.Count, $ProfilePath)
$candidates | ForEach-Object {
  $snippet = if ($_.CommandLine.Length -gt 120) { $_.CommandLine.Substring(0, 120) + "…" } else { $_.CommandLine }
  Write-Output ("  PID {0}  {1}  {2}" -f $_.ProcessId, $_.Name, $snippet)
}

if ($WhatIf) {
  Write-Output "（-WhatIf：未执行杀进程）"
  exit 0
}

$candidates | ForEach-Object {
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output "已按特征杀掉 $($candidates.Count) 个进程（未触碰其他浏览器）。"
