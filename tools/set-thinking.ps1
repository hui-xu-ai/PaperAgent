# set-thinking.ps1 —— 思考档位自管（切换当前会话 reasoningEffort，含回读确认）
#
# ⚠️ 首选路径（pydev 预设自带）：`set_thinking` 工具（预设行 plugins/thinking-control.js）。
#    它走 host 服务 ctx.sessionController.selectModel，**不经过 web API、不需要凭据**，
#    是 0.1.5 下唯一无摩擦的改档通道。本脚本是"没有该工具时"的兜底（子代理 shell / 非预设会话）。
#
# 本脚本原理：与界面「模型/思考档位」选择器使用同一 RPC（session.models + session.selectModel），
#       通过本机 web API（$env:DSH_WEB_URL，默认 http://127.0.0.1:3080）调用；
#       切换对下一次模型请求立即生效，无需用户手动操作。
#
# 认证（0.1.5 实测）：web API 全站需要浏览器会话 Cookie，**直接 POST 一律 401**
#       （DSH_WEB_URL 不含令牌）。若环境注入 `DSH_WEB_TOKEN`（= 进程启动令牌），本脚本会先
#       `GET /?token=…` 换取 Cookie，再用同一 WebSession 调 RPC；没有该变量时退化为旧行为
#       （报 401 并提示改用 set_thinking 工具）。
#
# 用法：
#   tools\set-thinking.ps1 -Level low          # 切换档位（off|low|high|max）
#   tools\set-thinking.ps1 -Get                # 只读当前档位
# 成功判据：切换后脚本会**回读确认**并输出「已确认：当前档位=X」；没看到这句 = 没切成功。
# 脚本位置：项目 tools\，或预设随带 <DSH_HOME>\.agent-presets\pydev\tools\set-thinking.ps1。
param(
  [ValidateSet("off", "low", "high", "max")][string]$Level,
  [switch]$Get
)

$ErrorActionPreference = "Stop"

$HINT = "提示：优先改用预设自带工具 set_thinking（走 sessionController，无需凭据）；本脚本仅为兜底。"

$webUrl = if ($env:DSH_WEB_URL) { $env:DSH_WEB_URL } else { "http://127.0.0.1:3080" }
$sessionId = $env:DSH_SESSION_ID
if (-not $sessionId) {
  Write-Error "缺少 DSH_SESSION_ID 环境变量（子代理 shell 或非 agent 环境）——本脚本无法切换；请改为协议级声明：汇报里写'本轮建议 low/high'并提示用户可在界面切换，禁止声称已切换。$HINT"
  exit 3
}

# ── 认证：有令牌则先用 GET /?token=… 换 Cookie，后续 RPC 带同一 WebSession ──
$webSession = $null
if ($env:DSH_WEB_TOKEN) {
  try {
    $exchange = Invoke-WebRequest -Uri ($webUrl.TrimEnd('/') + "/?token=" + $env:DSH_WEB_TOKEN) `
      -Method Get -SessionVariable webSession -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
    $cookieCount = 0
    try { $cookieCount = $webSession.Cookies.Count } catch { $cookieCount = 0 }
    if ($cookieCount -lt 1) { Write-Warning "令牌换取的会话没有拿到 Cookie，RPC 可能仍返回 401。" }
  } catch {
    Write-Warning ("DSH_WEB_TOKEN 换取 Cookie 失败：" + $_.Exception.Message)
    $webSession = $null
  }
}

function Invoke-Rpc($method, $payload) {
  $body = @{ type = "client-request"; rpcId = "setthk-" + (Get-Random); method = $method; payload = $payload } | ConvertTo-Json -Depth 6
  $req = @{
    Uri = ("$webUrl/api/$method"); Method = "Post"; ContentType = "application/json"
    Body = $body; TimeoutSec = 15
  }
  if ($webSession) { $req['WebSession'] = $webSession }
  return Invoke-RestMethod @req
}

try {
  $models = Invoke-Rpc "session.models" @{ sessionId = $sessionId }
  $cur = $models.result.value.current
  if (-not $cur) { Write-Error "session.models 返回空 current；无法确定 provider/model。"; exit 1 }

  if ($Get -or -not $Level) {
    $eff = if ($cur.reasoningEffort) { $cur.reasoningEffort } else { "(适配器默认)" }
    Write-Output ("当前思考档位={0}  provider={1}  model={2}" -f $eff, $cur.provider, $cur.model)
    exit 0
  }

  $sel = Invoke-Rpc "session.selectModel" @{ sessionId = $sessionId; provider = $cur.provider; model = $cur.model; reasoningEffort = $Level }
  if (-not $sel.result.ok) {
    Write-Error ("切换失败: " + ($sel.result.error | ConvertTo-Json -Compress))
    exit 1
  }

  # ── 回读确认：selectModel 返回 ok 还不够，读回当前档位核对 ──
  $verify = Invoke-Rpc "session.models" @{ sessionId = $sessionId }
  $nowEff = if ($verify.result.value.current.reasoningEffort) { $verify.result.value.current.reasoningEffort } else { "(default)" }
  if ($nowEff -eq $Level) {
    Write-Output ("已确认：当前档位=" + $nowEff + "（切换成功，已回读校验）")
  } else {
    Write-Warning ("警告：selectModel 返回 ok，但回读档位=" + $nowEff + " ≠ 目标 " + $Level + "——切换可能未生效，请重试或检查会话 id。")
    exit 1
  }
} catch {
  $msg = $_.Exception.Message
  if ($msg -match '401') {
    $msg = "web API 返回 401（未认证）。" + $HINT
  }
  Write-Error ("思考档位操作失败: " + $msg + " 若本机 API 不可达，退化为协议级声明（'本轮建议 low/high'，请用户界面切换），禁止声称已切换。")
  exit 1
}
