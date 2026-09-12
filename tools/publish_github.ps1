#!/usr/bin/env pwsh
# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  PaperAgent 首次发布到 GitHub（建仓 → push → tag → Release 附 zip + SHA256）。

.DESCRIPTION
  前置：
    1) winget install GitHub.cli   （或 https://cli.github.com/）
    2) gh auth login               （GitHub.com → HTTPS → 浏览器登录；**不要把 PAT 贴进聊天**）
    3) 已跑过 pwsh -File tools\build_release.ps1（产出 release\PaperAgent-v<版本>-win64.zip）

  流程（每一步都会先打印再执行）：
    ① gh auth status 自检；② 校验 zip 存在并算 SHA256；③ git remote（无则 add，有则提示）；
    ④ gh repo create <owner>/<repo> --public --source . --remote origin --push（仓库已存在则直接 push）；
    ⑤ push tag v<版本> + gh release create 附 zip + 发布说明（docs/RELEASE-NOTES-v<版本>.md）；
    ⑥ gh repo edit 补 topics（便于被搜索/收录）。

.EXAMPLE
  pwsh -File tools\publish_github.ps1 -Owner yourname -Repo PaperAgent -DryRun
  pwsh -File tools\publish_github.ps1 -Owner yourname -Repo PaperAgent
#>
param(
    [Parameter(Mandatory = $true)][string]$Owner,
    [string]$Repo = 'PaperAgent',
    [ValidateSet('public', 'private')][string]$Visibility = 'public',
    [string]$Tag = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
$version = (& $py -c "import sys;sys.path.insert(0,'backend');from app.version import APP_VERSION;print(APP_VERSION)").Trim()
if (-not $Tag) { $Tag = "v$version" }
$zip = Join-Path $root "release\PaperAgent-v$version-win64.zip"
$notes = Join-Path $root "docs\RELEASE-NOTES-v$version.md"
$slug = "$Owner/$Repo"

function Step([string]$text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Run([string]$cmd) {
    Write-Host "  > $cmd" -ForegroundColor DarkGray
    if (-not $DryRun) { Invoke-Expression $cmd; if ($LASTEXITCODE -ne 0) { throw "命令失败（exit=$LASTEXITCODE）：$cmd" } }
}

Step "[1/6] gh 登录自检"
$hasGh = [bool](Get-Command gh -ErrorAction SilentlyContinue)
if (-not $hasGh) {
    if ($DryRun) {
        Write-Host "  [DryRun] 未安装 gh（真实运行会中止）：winget install GitHub.cli" -ForegroundColor Yellow
    } else {
        throw "未安装 gh：winget install GitHub.cli"
    }
}
if ($hasGh -and -not $DryRun) { gh auth status; if ($LASTEXITCODE -ne 0) { throw "gh 未登录：先跑 gh auth login" } }

Step "[2/6] 校验分发版 zip 与 SHA256"
if (-not (Test-Path $zip)) { throw "找不到 $zip —— 先跑 pwsh -File tools\build_release.ps1 -Clean" }
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash
Write-Host "  $zip`n  SHA256=$sha"
$notesBody = if (Test-Path $notes) { Get-Content $notes -Raw } else { "# PaperAgent $Tag" }
$tmpNotes = Join-Path $root 'work\scratch\release-notes.md'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tmpNotes) | Out-Null
"$notesBody`n`n## 校验（SHA256）`n`n``````text`n$sha  PaperAgent-v$version-win64.zip`n``````" |
    Set-Content -Path $tmpNotes -Encoding UTF8

Step "[3/6] 工作区自检（发布前最后一次确认无未提交改动）"
Run "git status --short"

Step "[4/6] 远端与仓库"
$hasOrigin = (git remote) -contains 'origin'
if ($hasOrigin) {
    Write-Host "  已存在 origin：$(git remote get-url origin)"
    Run "git push -u origin HEAD"
} else {
    Run "gh repo create $slug --$Visibility --source . --remote origin --push --description `"本机单用户文献 AI 阅读/翻译/知识库应用（FastAPI + 静态前端 + 解析/知识库算法包）`""
}

Step "[5/6] 打 tag 并发布 Release（附 zip）"
Run "git tag -a $Tag -m `"PaperAgent $Tag`""
Run "git push origin $Tag"
Run "gh release create $Tag `"$zip`" --title `"PaperAgent $Tag`" --notes-file `"$tmpNotes`""

Step "[6/6] 仓库元信息（topics 便于搜索/被 awesome-list 收录）"
Run "gh repo edit $slug --add-topic pdf --add-topic literature-review --add-topic llm --add-topic rag --add-topic knowledge-base --add-topic fastapi --add-topic mineru --add-topic chinese"

Write-Host "`n完成：https://github.com/$slug/releases/tag/$Tag" -ForegroundColor Green
