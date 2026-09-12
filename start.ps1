# PaperAgent 开发启动脚本
# 用法：右键“使用 PowerShell 运行”，或 pwsh .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "缺少虚拟环境 .venv，请先创建：python -m venv .venv"
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已从 .env.example 创建 .env —— 请填入 DEEPSEEK_API_KEY（必填）与 MINERU_API_KEY（可选，精准解析）后重新运行。"
    exit 1
}
Write-Host "启动 PaperAgent: http://127.0.0.1:8900"
Set-Location backend
& "..\.venv\Scripts\python.exe" -m app.main
