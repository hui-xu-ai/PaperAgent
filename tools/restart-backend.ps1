# PaperAgent 后端一键「停 → 起 → 探活」（Windows / pwsh 7）
#
# 为什么有它（2026-09-11~12 实测教训）：
# - 用 Harness 的**后台 job** 起后端时，job 的 pwsh 包装进程会在轮次结束时被回收，
#   但 `python -m app.main` 作为独立子进程**继续存活** → 表现为「job 报 exit 1，
#   服务却还在监听」，或反过来「端口被占、新实例 bind 失败后干净退出」。
# - 反复手打 Stop-Process + 起服务 + Invoke-RestMethod /api/health 既慢又容易杀错。
#
# 安全约定（硬规则）：
# - **只按 PID 停**（PID 从 Get-NetTCPConnection -LocalPort 8900 拿），绝不按进程名批量杀。
# - 起服务用 Start-Process -PassThru（独立进程 + 重定向日志 + 记录 PID 文件）。
# - 探活失败直接报错，不反复重启（避免把端口争用放大）。
#
# 用法：
#   pwsh -File tools\restart-backend.ps1              # 停旧的（若有）→ 起新的 → 探活
#   pwsh -File tools\restart-backend.ps1 -NoStop      # 只在 8900 空闲时才起
#   pwsh -File tools\restart-backend.ps1 -StopOnly    # 只停
param(
    [int]$Port = 8900,
    [switch]$NoStop,
    [switch]$StopOnly,
    [int]$TimeoutSec = 40
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot          # 项目根
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'work\scratch'
$log = Join-Path $logDir 'backend-restart.log'
$pidFile = Join-Path $root 'work\scratch\backend.pid'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Get-Listener {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Stop-Backend {
    $l = Get-Listener
    if (-not $l) { Write-Host "[stop] $Port 无监听者，跳过"; return }
    $procId = $l.OwningProcess
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    Write-Host "[stop] 按 PID 停: $procId ($($p.ProcessName))"
    Stop-Process -Id $procId -Force
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 200
        if (-not (Get-Listener)) { Write-Host "[stop] 端口已释放"; return }
    }
    throw "[stop] PID $procId 已停但端口仍被占用——请手工核查（勿按进程名批量杀）"
}

function Get-BackendPid {
    # 端口监听者可能就是"启动器"（它拉起真正的服务进程后自己退出）→ 顺着父子链
    # 找到最深的那个存活 python 进程（= 真正在服务的进程）。
    $owner = (Get-Listener).OwningProcess
    $all = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue)
    $cur = $owner
    for ($i = 0; $i -lt 5; $i++) {
        $kids = @($all | Where-Object { $_.ParentProcessId -eq $cur })
        if ($kids.Count -eq 0) { break }
        $cur = ($kids | Sort-Object CreationDate -Descending | Select-Object -First 1).ProcessId
    }
    return $cur
}

function Start-Backend {
    if (-not (Test-Path $python)) { throw "找不到 venv python: $python" }
    if (Get-Listener) { throw "[start] $Port 已被占用（先不带 -NoStop 跑一次以停掉旧的）" }
    $backendDir = Join-Path $root 'backend'
    $p = Start-Process -FilePath $python -ArgumentList @('-m', 'app.main') `
        -WorkingDirectory $backendDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Write-Host "[start] 启动器 pid=$($p.Id) 日志=$log"

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 5
            if ($h.status -eq 'ok') {
                Start-Sleep -Milliseconds 800          # 等价换：监听进程可能刚好在换手
                $real = Get-BackendPid
                Set-Content -Path $pidFile -Value $real -Encoding ascii
                Write-Host "[probe] ok  版本=$($h.version) 引擎=$($h.engine_version) llm_ready=$($h.llm_ready)"
                Write-Host "[pid] 真实服务进程=$real（已回写 $pidFile）"
                return
            }
        } catch {
            if (-not (Get-Process -Id $p.Id -ErrorAction SilentlyContinue)) {
                Write-Host '--- 启动日志尾部 ---'
                Get-Content $log -Tail 15 -ErrorAction SilentlyContinue
                throw "[start] 启动器已退出且探活失败——多半是端口占用或导入错误，见上面日志"
            }
        }
    }
    throw "[probe] ${TimeoutSec}s 内 /api/health 未就绪——看 $log"
}

if ($StopOnly) { Stop-Backend; exit 0 }
if (-not $NoStop) { Stop-Backend }
Start-Backend
