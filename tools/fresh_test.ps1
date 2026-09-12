#!/usr/bin/env pwsh
# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  从「干净分发版 zip」一键拉起一个全新的测试实例（用户 2026-09-12 要求：
  每次测试都从干净的分发版开始，不掺任何历史运行期数据）。

.DESCRIPTION
  1) 把 release\PaperAgent-v<版本>-win64.zip 解压到一个**全新的空目录**（默认 work\scratch\fresh-<时间戳>）；
  2) （可选）复制你的 .env 到该目录 —— 不复制则 AI 功能不可用；
  3) 打印目录、干净度自检（不该有 data/logs/work）；-Start 则直接双击启动。

.EXAMPLE
  pwsh -File tools\fresh_test.ps1 -Start
  pwsh -File tools\fresh_test.ps1 -Dest D:\PaperAgent-test -NoEnv
#>
param(
    [string]$Zip = "",
    [string]$Dest = "",
    [string]$EnvPath = "",     # 默认用项目根 .env（含 API Key）；-NoEnv 则跳过
    [switch]$NoEnv,
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Zip) {
    $zipFile = Get-ChildItem (Join-Path $root 'release') -Filter 'PaperAgent-v*-win64.zip' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $zipFile) { throw "release\ 下没有分发版 zip —— 先跑 pwsh -File tools\build_release.ps1" }
    $Zip = $zipFile.FullName
}
if (-not (Test-Path $Zip)) { throw "找不到 zip：$Zip" }

if (-not $Dest) { $Dest = Join-Path $root ("work\scratch\fresh-" + (Get-Date -Format 'MMdd-HHmmss')) }
if (Test-Path $Dest) {
    if ((Get-ChildItem $Dest -Force | Measure-Object).Count -gt 0) { throw "目标目录非空：$Dest（请换一个空目录，避免与旧数据混在一起）" }
} else { New-Item -ItemType Directory -Force -Path $Dest | Out-Null }

Write-Host "[1/3] 解压 $(Split-Path -Leaf $Zip) → $Dest" -ForegroundColor Cyan
Expand-Archive -Path $Zip -DestinationPath $Dest -Force

if (-not $NoEnv) {
    if (-not $EnvPath) { $EnvPath = Join-Path $root '.env' }
    if (Test-Path $EnvPath) {
        Copy-Item -Force $EnvPath (Join-Path $Dest '.env')
        Write-Host "[2/3] 已复制 .env（含 API Key）→ 目标目录；不需要就加 -NoEnv" -ForegroundColor Yellow
    } else {
        Write-Host "[2/3] 未找到 .env（$EnvPath）→ AI 功能不可用；可复制 .env.example 后自行填写" -ForegroundColor Yellow
    }
} else { Write-Host "[2/3] 跳过 .env（-NoEnv）" -ForegroundColor DarkGray }

Write-Host "[3/3] 干净度自检" -ForegroundColor Cyan
foreach ($d in 'data', 'knowledge_base', 'library', 'logs', 'work') {
    $exists = Test-Path (Join-Path $Dest $d)
    Write-Host ("    {0,-16} {1}" -f $d, $(if ($exists) { '存在（非干净！）' } else { '不存在 ✓' })) `
        -ForegroundColor $(if ($exists) { 'Red' } else { 'Gray' })
}
Write-Host "  exe：$(Join-Path $Dest 'PaperAgent.exe')"
Write-Host "  端口：默认 8900（被占用时会**弹窗**提示；开发版后端起在 8900 时请先关它）" -ForegroundColor DarkGray

if ($Start) {
    Write-Host "启动中（关窗=隐藏到托盘；真正退出=托盘右键→退出）..." -ForegroundColor Green
    Start-Process -FilePath (Join-Path $Dest 'PaperAgent.exe') -WorkingDirectory $Dest
}
