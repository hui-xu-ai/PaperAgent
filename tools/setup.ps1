# setup.ps1 —— 一键复现可运行环境：建 venv → 装依赖（pyproject 或 lock）→ 可选跑测试。
# 用法：
#   tools\setup.ps1                     # 建 venv + 安装（用 pyproject.toml 或 requirements.txt）
#   tools\setup.ps1 -Sync               # 先用 pip-compile 生成 requirements.lock，再按 lock 装（可复现）
#   tools\setup.ps1 -Test               # 装完后跑 pytest tests\ -q
# 说明：venv 建在项目内 .venv；迁移到别的沙箱/机器后跑本脚本即可复现。
param(
  [string]$Root = $PWD,
  [switch]$Sync,
  [switch]$Test
)
$ErrorActionPreference = 'Stop'
$venv = Join-Path $Root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'
if (Test-Path $venv) { Write-Host "venv 已存在 : $venv" }
else { Write-Host "创建 venv..."; & python -m venv $venv; Write-Host "venv 已创建" }
Write-Host "升级 pip..."; & $py -m pip install --upgrade pip
if ($Sync) {
  if (-not (Get-Command pip-compile -ErrorAction SilentlyContinue)) { & $py -m pip install pip-tools }
  & $py -m piptools compile -o (Join-Path $Root 'requirements.lock') (Join-Path $Root 'pyproject.toml')
  & $py -m pip install -r (Join-Path $Root 'requirements.lock')
} else {
  if (Test-Path (Join-Path $Root 'pyproject.toml')) { & $py -m pip install -e (Join-Path $Root '.') }
  elseif (Test-Path (Join-Path $Root 'requirements.txt')) { & $py -m pip install -r (Join-Path $Root 'requirements.txt') }
  else { Write-Host '未找到 pyproject.toml / requirements.txt，跳过安装依赖' }
}
if ($Test) { & $py -m pytest (Join-Path $Root 'tests') -q }
Write-Host "setup 完成。运行：`"$py`" ..."
