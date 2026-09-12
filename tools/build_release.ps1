#!/usr/bin/env pwsh
# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  PaperAgent 一键构建发布产物（onedir）+ 产物自检。

.DESCRIPTION
  流程：发布闸门（release_check.py）→ 清理 → PyInstaller（PaperAgent.spec）→ 后处理
  （exe 同目录放 icon.ico / VERSION.txt）→ 产物自检（release_check.py 的 dist 段）。

  产物：dist/PaperAgent/PaperAgent.exe（双击启动：原生最大化窗口 + 系统托盘）

.EXAMPLE
  pwsh -File tools\build_release.ps1
  pwsh -File tools\build_release.ps1 -Clean -SkipSmoke
#>
param(
    [switch]$SkipGate,      # 跳过发布闸门（不推荐，仅排障时用）
    [switch]$SkipSmoke,     # 跳过构建后的打包版冒烟
    [switch]$Clean,         # 构建前清空 dist/ build/
    [switch]$PruneRuntime,  # 冒烟后清掉 dist 里的运行期目录（⚠️ 会删打包版自己的 data/！）
    [switch]$NoZip,         # 不打"干净分发版" zip（默认打：release/PaperAgent-v<版本>-win64.zip）
    [int]$SmokePort = 8900  # 冒烟用的端口（默认 8900；被占用时换一个，避免打断正在跑的实例）
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$pyi = Join-Path $root '.venv\Scripts\pyinstaller.exe'
foreach ($p in @($py, $pyi)) {
    if (-not (Test-Path $p)) { throw "缺少 $p（先在项目根创建 venv 并安装依赖）" }
}

$version = (& $py -c "import sys;sys.path.insert(0,'backend');sys.path.insert(0,'packages/paperkb');from app.version import APP_VERSION;print(APP_VERSION)").Trim()
Write-Host "=== PaperAgent 构建 v$version ===" -ForegroundColor Cyan

# 清理必须在闸门**之前**：上一次冒烟会在 dist 里留下运行期目录（data/logs/work…），
# 闸门的"产物不得混入运行期目录"检查会因此失败（实测踩到）。
if ($Clean) {
    Write-Host "[1/5] 清理 dist/ build/ ..." -ForegroundColor Cyan
    Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue
} else {
    Write-Host "[1/5] 增量构建（要彻底重建请加 -Clean）" -ForegroundColor Cyan
}

if (-not $SkipGate) {
    Write-Host "[2/5] 发布闸门（版本/数据兼容/登记表/打包自检/文档）..." -ForegroundColor Cyan
    & $py tools\release_check.py
    if ($LASTEXITCODE -ne 0) { throw "发布闸门未通过（exit=$LASTEXITCODE）——先修问题再构建" }
} else {
    Write-Host "[2/5] 跳过发布闸门（-SkipGate）" -ForegroundColor Yellow
}

Write-Host "[3/5] PyInstaller（onedir，spec 驱动）..." -ForegroundColor Cyan
# 期刊分区表：构建期把 xlsx 解析成可随包的 journals.db（用户要求"直接放解析好的 db"）
if (-not (Test-Path (Join-Path $root 'build\reference_seed\journals.db'))) {
    Write-Host "  生成期刊分区表种子（xlsx → journals.db，约 20s）..." -ForegroundColor Cyan
    & $py tools\build_reference_seed.py
    if ($LASTEXITCODE -ne 0) { Write-Host "  ⚠ 生成失败：打包版将没有 IF/分区数据（文献会停在 L1）" -ForegroundColor Yellow }
} else {
    Write-Host "  期刊分区表种子已存在（要更新请先删 build\reference_seed\ 或改 xlsx 后重跑）" -ForegroundColor DarkGray
}
& $pyi PaperAgent.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败（exit=$LASTEXITCODE）" }

$dist = Join-Path $root 'dist\PaperAgent'
$exe = Join-Path $dist 'PaperAgent.exe'
if (-not (Test-Path $exe)) { throw "产物缺 $exe" }

Write-Host "[4/5] 后处理（icon.ico / VERSION.txt）..." -ForegroundColor Cyan
Copy-Item (Join-Path $root 'assets\icon.ico') (Join-Path $dist 'icon.ico') -Force
# .env.example 随包（**不含任何密钥**；用户把自己的 .env 放到 exe 同目录即可）
if (Test-Path (Join-Path $root '.env.example')) {
    Copy-Item (Join-Path $root '.env.example') (Join-Path $dist '.env.example') -Force
}
@(
    "PaperAgent v$version"
    "build_time=$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
    "build_host=$env:COMPUTERNAME"
    "python=$(& $py -c 'import sys;print(sys.version.split()[0])')"
    ""
    "启动：双击 PaperAgent.exe（原生窗口最大化 + 系统托盘；关窗=隐藏到托盘）"
    "退出：托盘图标右键 → 退出 PaperAgent（完全关停并释放端口）；或 PaperAgent.exe --quit"
    "升级：只替换 PaperAgent.exe 与 _internal/，data/ 与 knowledge_base/ 原地保留（见 docs/UPGRADE.md）"
    "配置：.env 放本目录（与 exe 同级）"
    "日志：logs\paperagent.log"
) | Set-Content -Path (Join-Path $dist 'VERSION.txt') -Encoding utf8

Write-Host "[5/5] 产物自检..." -ForegroundColor Cyan
& $py tools\release_check.py
if ($LASTEXITCODE -ne 0) { throw "产物自检未通过" }

if (-not $SkipSmoke) {
    Write-Host "[冒烟] 打包版真实链路（窗口/托盘/退出）..." -ForegroundColor Cyan
    Write-Host "  提示：会短暂弹出应用窗口（端口 $SmokePort）。" -ForegroundColor DarkGray
    & $py tools\smoke_desktop.py --exe $exe --port $SmokePort
    if ($LASTEXITCODE -ne 0) { throw "打包版冒烟未通过（exit=$LASTEXITCODE）" }
    Write-Host "  注意：冒烟会在 $dist 下生成运行期目录（data/ logs/ work/）——" -ForegroundColor DarkGray
    Write-Host "        要打包给别人请用 -PruneRuntime，或从**未跑过冒烟**的 dist 取包。" -ForegroundColor DarkGray
}

if ($PruneRuntime) {
    Write-Host "[清理] 删除 dist 内运行期目录（data/ knowledge_base/ library/ logs/ work/）..." -ForegroundColor Yellow
    Write-Host "  ⚠️ 仅当 dist\PaperAgent 是**构建产物**时才可这么做；若它就是你在用的安装目录，请勿使用本开关。" -ForegroundColor Yellow
    foreach ($d in 'data', 'knowledge_base', 'library', 'logs', 'work') {
        Remove-Item -Recurse -Force (Join-Path $dist $d) -ErrorAction SilentlyContinue
    }
}

$size = [math]::Round(((Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)

# ---------------------------------------------------------------- 干净分发版 zip
# 用户 2026-09-12 要求：**每次测试都从一个干净的分发版开始**（双击即用、不掺任何运行期数据）。
# 所以这里从 dist 里"挑程序文件"另存 staging（**不含** data/knowledge_base/library/logs/work/.env），
# 打包成 release/PaperAgent-v<版本>-win64.zip；解压到空目录即可测。
if (-not $NoZip) {
    Write-Host "[打包] 生成干净分发版 zip..." -ForegroundColor Cyan
    $release = Join-Path $root 'release'
    New-Item -ItemType Directory -Force -Path $release | Out-Null
    $stage = Join-Path $root 'work\scratch\stage-zip'
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    foreach ($item in 'PaperAgent.exe', '_internal', 'icon.ico', 'VERSION.txt', '.env.example') {
        $srcItem = Join-Path $dist $item
        if (Test-Path $srcItem) { Copy-Item -Recurse -Force $srcItem $stage }
    }
    $readme = @(
        "PaperAgent v$version（Windows 64 位）· 干净分发版",
        "",
        "【怎么跑】",
        "1) 解压到任意**空目录**（例：D:\PaperAgent-test）。不要解压进有旧数据的目录。",
        "2) 要用 AI 功能：把你的 .env（含 API Key）放到与 PaperAgent.exe 同级目录（可参照 .env.example）。",
        "3) 双击 PaperAgent.exe：弹出原生窗口（自动最大化），右下角托盘出现同款图标。",
        "   · 点窗口 X = 隐藏到托盘（程序继续跑、任务不中断）；",
        "   · 真正退出：右键托盘图标 → 「退出 PaperAgent」，或命令行 PaperAgent.exe --quit；",
        "   · 重复双击不会开两份：会唤起已有窗口；若端口被别的进程占用，会**弹窗**告知怎么处理。",
        "",
        "【数据在哪】都在本目录下，升级程序时不要动它们：",
        "  data\            五库 + manifest.json（★ 你的数据）",
        "  knowledge_base\  知识库（★ 你的知识资产）",
        "  library\         解析产物  ·  logs\ 日志  ·  work\ 临时（可随时清）",
        "",
        "【端口】默认 8900；可在 .env 里用 PAPERAGENT_PORT 改。",
        "【期刊分区/影响因子】已随包内置（_internal\share\reference\journals.db，首启毫秒级就位，",
        "            决定文献价值分能否判 L2/L3）；**要更新**：把同目录的 JCR分区.xlsx 按原格式填好，",
        "            在程序内「设置 → 期刊」导入即可。",
        "【版本】见 VERSION.txt，或程序内「设置 → 关于」（含前端版本/引擎/知识库库版本）。",
        "【升级】只替换 PaperAgent.exe 与 _internal\，data\ 与 knowledge_base\ 原地保留（详见 docs/UPGRADE.md）。"
    ) -join "`r`n"
    Set-Content -Path (Join-Path $stage '使用说明.txt') -Value $readme -Encoding utf8

    $zip = Join-Path $release "PaperAgent-v$version-win64.zip"
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        [System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip)
    } catch {
        Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
    }
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    $zipMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "  干净分发版：$zip（$zipMB MB）" -ForegroundColor Green
    Write-Host "  SHA256：$((Get-FileHash $zip -Algorithm SHA256).Hash)"
    Write-Host "  用法：解压到空目录 → 放自己的 .env → 双击 PaperAgent.exe" -ForegroundColor DarkGray
}

Write-Host "`n=== 构建完成 v$version ===" -ForegroundColor Green
Write-Host "  产物：$dist（$size MB）"
Write-Host "  冒烟：pwsh -File tools\build_release.ps1（已内含）或 python tools\smoke_desktop.py --exe $exe"
