<#
restart-and-verify.ps1 —— 一键「停 → 起 → 探活 → 冒烟」后端循环

为什么需要：反馈 V17 实测，每个提交都是"读→改→测→重启→真实链路验证→提交"≈6-8 步，
单会话 6 次重启 × 3 个调用 = 大量步数消耗在固定套路上。本脚本把该套路压成 1 次调用。

设计：项目无关 + 配置驱动（按序找配置，找不到用参数/默认值）。
  配置来源：1) -Config 指定  2) 项目根 tools\backend.json  3) 内建默认值
  配置字段（全部可选）：
    StartCommand   : 启动命令（默认 .venv\Scripts\python.exe -m <Module>）
    Module         : 只用默认 StartCommand 时生效（如 app.main）
    WorkDir        : 启动工作目录（默认项目根）
    HealthUrl      : 探活 URL（默认 http://127.0.0.1:8000/health）
    HealthTimeoutS : 探活总超时秒数（默认 30）
    PidFile        : 记录后端 PID 的文件（默认 <WorkDir>\.dsh-backend.pid）
    StopPattern    : 用于按命令行特征识别后端进程的正则（可选；不给则只用 PidFile）
    SmokeCommand   : 冒烟命令（可选；在探活成功后在项目根执行，退出码非 0 视为失败）
    Port           : 监听端口（用于打印提示，可选）

用法：
  pwsh -NoProfile -File tools\restart-and-verify.ps1
  pwsh -NoProfile -File tools\restart-and-verify.ps1 -NoStop        # 只起+验（已在运行且不想重启）
  pwsh -NoProfile -File tools\restart-and-verify.ps1 -Config work\backend.json
  pwsh -NoProfile -File tools\restart-and-verify.ps1 -SkipSmoke

红线（与 pydev-protocol 一致）：杀进程**只按 PID 或命令行特征**，绝不按进程名批量杀。
Harness 页面（127.0.0.1:3080）不在本脚本管辖范围内，永不触碰。
退出码：0 = 探活通过（且冒烟通过，若配置了）；1 = 失败。
#>
[CmdletBinding()]
param(
  [string]$Config,
  [switch]$NoStop,
  [switch]$SkipSmoke,
  [string]$StartCommand,
  [string]$HealthUrl,
  [int]$HealthTimeoutS = 0,
  [string]$SmokeCommand,
  [string]$ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)

function Say($t) { Write-Output $t }
function Step($t) { Write-Output ("── " + $t) }

# ── 1. 载入配置 ──────────────────────────────────────────────────────────────
$cfg = @{}
$cfgPath = if ($Config) { $Config } else { Join-Path $ProjectRoot 'tools\backend.json' }
if (Test-Path -LiteralPath $cfgPath) {
  try {
    $json = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
    foreach ($p in $json.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    Step ("配置已载入：" + $cfgPath)
  } catch {
    Say ("警告：配置解析失败，改用默认值（" + $_.Exception.Message + "）")
  }
} else {
  Step "未找到 tools\backend.json，使用默认值（建议为本项目补一份）"
}
if ($StartCommand) { $cfg['StartCommand'] = $StartCommand }
if ($HealthUrl) { $cfg['HealthUrl'] = $HealthUrl }
if ($HealthTimeoutS -gt 0) { $cfg['HealthTimeoutS'] = $HealthTimeoutS }
if ($SmokeCommand) { $cfg['SmokeCommand'] = $SmokeCommand }

$module = if ($cfg['Module']) { [string]$cfg['Module'] } else { $null }
$workDir = if ($cfg['WorkDir']) { [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $cfg['WorkDir'])) } else { $ProjectRoot }
$healthUrl = if ($cfg['HealthUrl']) { [string]$cfg['HealthUrl'] } else { 'http://127.0.0.1:8000/health' }
$timeoutS = if ($cfg['HealthTimeoutS']) { [int]$cfg['HealthTimeoutS'] } else { 30 }
$pidFile = if ($cfg['PidFile']) { Join-Path $ProjectRoot $cfg['PidFile'] } else { Join-Path $workDir '.dsh-backend.pid' }
$stopPattern = if ($cfg['StopPattern']) { [string]$cfg['StopPattern'] } else { $null }
$startCommand = if ($cfg['StartCommand']) { [string]$cfg['StartCommand'] }
  elseif ($module) { '& "' + (Join-Path $ProjectRoot '.venv\Scripts\python.exe') + '" -m ' + $module }
  else { $null }
$smokeCommand = if ($cfg['SmokeCommand']) { [string]$cfg['SmokeCommand'] } else { $null }

if (-not $startCommand) {
  Say "缺少启动命令：请在 tools\backend.json 配 StartCommand（或 Module），或传 -StartCommand。"
  exit 1
}

Say ("项目根   : " + $ProjectRoot)
Say ("工作目录 : " + $workDir)
Say ("探活地址 : " + $healthUrl)
Say ("PID 文件 : " + $pidFile)

# ── 2. 停（只按 PID / 命令行特征）───────────────────────────────────────────
if (-not $NoStop) {
  Step "停止旧进程"
  $killed = @()
  if (Test-Path -LiteralPath $pidFile) {
    $oldPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($oldPid -match '^\d+$') {
      $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
      if ($proc) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $killed += ("pid=" + $proc.Id + " (" + $proc.ProcessName + ")")
      }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
  }
  if ($stopPattern) {
    # 仅按"命令行特征"匹配，绝不按进程名批量杀
    $matched = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine -match $stopPattern -and $_.ProcessId -ne $PID }
    foreach ($m in $matched) {
      Stop-Process -Id $m.ProcessId -Force -ErrorAction SilentlyContinue
      $killed += ("pid=" + $m.ProcessId + " (cmdline match)")
    }
  }
  if ($killed.Count -gt 0) { Say ("  已停：" + ($killed -join '; ')) } else { Say "  无旧进程（PidFile 不存在或进程已退出）" }
} else { Step "跳过停止（-NoStop）" }

# ── 3. 起（后台）────────────────────────────────────────────────────────────
Step "启动后端（后台）"
$outLog = Join-Path $workDir 'backend.out.log'
$errLog = Join-Path $workDir 'backend.err.log'
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
$proc = Start-Process -FilePath 'pwsh' `
  -ArgumentList @('-NoProfile', '-Command', $startCommand) `
  -WorkingDirectory $workDir -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii
Say ("  pid=" + $proc.Id + "  日志：" + $outLog + " / " + $errLog)

# ── 4. 探活（失败即读日志尾部，不再多轮空转）────────────────────────────────
Step ("探活：" + $healthUrl + "（超时 " + $timeoutS + "s）")
$deadline = (Get-Date).AddSeconds($timeoutS)
$ok = $false
$lastErr = ''
while ((Get-Date) -lt $deadline) {
  if ($proc.HasExited) {
    Say ("  进程已退出（exit=" + $proc.ExitCode + "），不再等待。")
    break
  }
  try {
    $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) { $ok = $true; break }
    $lastErr = "HTTP " + $resp.StatusCode
  } catch {
    $lastErr = $_.Exception.Message
    Start-Sleep -Milliseconds 700
  }
}
if (-not $ok) {
  Say ("探活失败（最后错误：" + $lastErr + "）")
  if (Test-Path -LiteralPath $errLog) {
    Say "── 后端 stderr 末 20 行 ──"
    Get-Content -LiteralPath $errLog -Tail 20 | ForEach-Object { Say ("  " + $_) }
  }
  if (Test-Path -LiteralPath $outLog) {
    Say "── 后端 stdout 末 20 行 ──"
    Get-Content -LiteralPath $outLog -Tail 20 | ForEach-Object { Say ("  " + $_) }
  }
  exit 1
}
Say "  探活通过（HTTP 2xx/3xx）"

# ── 5. 冒烟（可选；沿用项目已有验证命令，不重复实现）────────────────────────
if (-not $SkipSmoke -and $smokeCommand) {
  Step "冒烟：" + $smokeCommand
  Push-Location $ProjectRoot
  try {
    & pwsh -NoProfile -Command $smokeCommand
    $code = $LASTEXITCODE
  } finally { Pop-Location }
  if ($code -ne 0) { Say ("  冒烟失败（exit=" + $code + "）——后端进程仍在运行，日志见 " + $outLog); exit 1 }
  Say "  冒烟通过"
} elseif (-not $SkipSmoke) {
  Step "未配置 SmokeCommand，跳过冒烟（建议在 tools\backend.json 里补一条最便宜的真实链路命令）"
}

$smokeSuffix = ''
if ($smokeCommand -and -not $SkipSmoke) { $smokeSuffix = ' smoke=pass' }

Say ""
Say ("OK：后端已就绪 pid=" + $proc.Id + " health=" + $healthUrl + $smokeSuffix)
exit 0
