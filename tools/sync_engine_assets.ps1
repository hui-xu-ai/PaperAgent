# 同步 skill 资源到 engine_assets 快照（打包用）
# 用法: powershell -File tools/sync_engine_assets.ps1 [skill根目录]
param(
    [string]$SkillDir = "D:\Python\DeepSeek\Paper_AI_Reader\skill"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$snap = Join-Path $root "backend\engine_assets"

$items = @("templates", "prompts", "tools", "memory", "rules", "SKILL.md", "SKILL.advanced.md")
foreach ($it in $items) {
    $src = Join-Path $SkillDir $it
    $dst = Join-Path $snap $it
    if (Test-Path $src) {
        if ((Get-Item $src).PSIsContainer) {
            # 内容级复制（通配），避免 Copy-Item 目录在目标存在时嵌套同级目录
            if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Path $dst | Out-Null }
            Copy-Item "$src\*" $dst -Recurse -Force
        } else {
            Copy-Item $src $dst -Force
        }
        Write-Host "synced: $it"
    } else {
        Write-Host "skip (missing): $it"
    }
}
# 记录 skill 版本到快照（可追溯）
$ver = Select-String -Path (Join-Path $SkillDir "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($ver) { Set-Content -Path (Join-Path $snap "VERSION.txt") -Value "paper-reader-skill $($ver.Matches[0].Groups[1].Value) (synced $(Get-Date -Format 'yyyy-MM-dd HH:mm'))" }
Write-Host "done -> snapshot: $snap"
