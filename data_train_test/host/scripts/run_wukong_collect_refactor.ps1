<#
  run_wukong_collect.ps1  (v2.5)
  - Windows PowerShell 5.1 compatible (ASCII)
  - Key upgrades from v2.4:
      * Build hilog_text_full.log + hilog_index.txt from hilog.*.gz segments
      * Convert dmesg to UTF-8 (dmesg_before.utf8.log / dmesg_after.utf8.log)
      * Keep persistent hilog writer & pruning
#>
# requires -Version 5.1

param(
  [string]$Target = "",
  [string]$SN     = ""     # explicit device serial/TCP target; takes priority over -Target and env vars
)

$HdcTargetHelperPath = Join-Path $PSScriptRoot "tools\hdc_target.ps1"
if (-not (Test-Path -LiteralPath $HdcTargetHelperPath)) {
  throw "Missing HDC target helper: $HdcTargetHelperPath"
}
. $HdcTargetHelperPath

#region Config
# =====================[ Config ]=====================
function Invoke-HdcNoHang {
  param(
    [string]$SN,
    [string[]]$HdcArgs,
    [int]$TimeoutMs = 20000,  # kept for compatibility (unused)
    [int]$Retry = 1,          # kept for compatibility (unused)
    [string]$Tag = "hdc"
  )

  $preview = if ($HdcArgs -and $HdcArgs.Count -gt 0) { $HdcArgs -join " " } else { "<NONE>" }
  #Write-Host ("[{0}] BEGIN hdc {1}" -f $Tag, $preview) -ForegroundColor Cyan

  & hdc -t $SN @HdcArgs 2>&1 | Out-Null
  $ec = $LASTEXITCODE

  #Write-Host ("[{0}] END exit={1}" -f $Tag, $ec) -ForegroundColor Cyan
  return $ec
}

function Invoke-HdcCapture {
  param(
    [string]$SN,
    [string[]]$HdcArgs,
    [int]$TimeoutMs = 20000,  # kept for compatibility (unused)
    [int]$Retry = 1,          # kept for compatibility (unused)
    [string]$Tag = "hdc"
  )

  $preview = if ($HdcArgs -and $HdcArgs.Count -gt 0) { $HdcArgs -join " " } else { "<NONE>" }
  #Write-Host ("[{0}] BEGIN hdc {1}" -f $Tag, $preview) -ForegroundColor Cyan

  $all = (& hdc -t $SN @HdcArgs 2>&1 | Out-String)
  $ec = $LASTEXITCODE

  #Write-Host ("[{0}] END exit={1}" -f $Tag, $ec) -ForegroundColor Cyan
  return @{ exit=$ec; stdout=$all; stderr="" }
}

$SCRIPT_VERSION = "2.6-refactor"   # for run-level metadata

$DefaultHdcTarget = "192.168.3.28:8711"
$ResolvedHdcTarget = ""
if (-not [string]::IsNullOrWhiteSpace($SN)) {
  $ResolvedHdcTarget = $SN.Trim()
} elseif (-not [string]::IsNullOrWhiteSpace($Target)) {
  $ResolvedHdcTarget = $Target.Trim()
} elseif (-not [string]::IsNullOrWhiteSpace($env:WK_DEVICE_TARGET)) {
  $ResolvedHdcTarget = $env:WK_DEVICE_TARGET.Trim()
} elseif (-not [string]::IsNullOrWhiteSpace($env:HDC_TARGET)) {
  $ResolvedHdcTarget = $env:HDC_TARGET.Trim()
} else {
  $ResolvedHdcTarget = $DefaultHdcTarget
}
$ResolvedHdcTarget = Resolve-HdcTarget -Target $ResolvedHdcTarget
$env:WK_DEVICE_TARGET = $ResolvedHdcTarget
$env:HDC_TARGET = $ResolvedHdcTarget

$SN = $ResolvedHdcTarget
$TARGET = $ResolvedHdcTarget

Write-Host ("[hdc] using target: {0}" -f $SN) -ForegroundColor Cyan
$hdcProbeRetry = 3
if (-not [string]::IsNullOrWhiteSpace($env:WK_HDC_PROBE_RETRY)) {
  $tmpRetry = 0
  if ([int]::TryParse($env:WK_HDC_PROBE_RETRY, [ref]$tmpRetry) -and $tmpRetry -gt 0) {
    $hdcProbeRetry = $tmpRetry
  }
}
if ($hdcProbeRetry -gt 5) {
  Write-Host ("[hdc] WK_HDC_PROBE_RETRY={0} exceeds max 5; clamped" -f $hdcProbeRetry) -ForegroundColor DarkYellow
  $hdcProbeRetry = 5
}
$hdcProbeSleepMs = 700
if (-not [string]::IsNullOrWhiteSpace($env:WK_HDC_PROBE_SLEEP_MS)) {
  $tmpSleepMs = 0
  if ([int]::TryParse($env:WK_HDC_PROBE_SLEEP_MS, [ref]$tmpSleepMs) -and $tmpSleepMs -ge 0) {
    $hdcProbeSleepMs = $tmpSleepMs
  }
}
if ($hdcProbeSleepMs -gt 5000) {
  Write-Host ("[hdc] WK_HDC_PROBE_SLEEP_MS={0} exceeds max 5000; clamped" -f $hdcProbeSleepMs) -ForegroundColor DarkYellow
  $hdcProbeSleepMs = 5000
}
$hdcProbe = ""
$hdcProbeExit = $null
for ($hdcProbeAttempt = 1; $hdcProbeAttempt -le $hdcProbeRetry; $hdcProbeAttempt++) {
  $hdcProbe = (& hdc -t $SN shell "echo HDC_OK" 2>&1 | Out-String).Trim()
  $hdcProbeExit = $LASTEXITCODE
  if ($hdcProbeExit -eq 0 -and $hdcProbe -match "HDC_OK") { break }
  if ($hdcProbeAttempt -lt $hdcProbeRetry) {
    Write-Host ("[hdc] probe attempt {0}/{1} failed exit={2}; retrying after {3}ms" -f $hdcProbeAttempt, $hdcProbeRetry, $hdcProbeExit, $hdcProbeSleepMs) -ForegroundColor DarkYellow
    if ($hdcProbeSleepMs -gt 0) { Start-Sleep -Milliseconds $hdcProbeSleepMs }
  }
}
if ($hdcProbeExit -ne 0 -or $hdcProbe -notmatch "HDC_OK") {
  throw ("[hdc] probe failed for target: {0} after {1} attempt(s), last_exit={2}`n{3}" -f $SN, $hdcProbeRetry, $hdcProbeExit, $hdcProbe)
}

. (Join-Path $PSScriptRoot "qwen3_server_helpers.ps1")

# anchor paths to script directory (not current working directory)
if ($PSScriptRoot) {
  $ScriptRoot = $PSScriptRoot
} else {
  $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

$Inbox = Join-Path $ScriptRoot "inbox"
$Runs  = Join-Path $Inbox "runs"
# env var fallbacks (runner uses NET_* / SCENARIO_TAG)
if (-not $env:WK_SCENARIO_TAG -and $env:SCENARIO_TAG) { $env:WK_SCENARIO_TAG = $env:SCENARIO_TAG }
if (-not $env:WK_FAULT_TYPE -and $env:NET_MODE) { $env:WK_FAULT_TYPE = $env:NET_MODE }
if (-not $env:WK_NET_IFACE -and $env:NET_IFACE) { $env:WK_NET_IFACE = $env:NET_IFACE }
if (-not $env:WK_NET_WLAN_IFACE -and $env:NET_WLAN_IFACE) { $env:WK_NET_WLAN_IFACE = $env:NET_WLAN_IFACE }
if (-not $env:WK_NET_PING_IP -and $env:NET_PING_IP) { $env:WK_NET_PING_IP = $env:NET_PING_IP }
if (-not $env:WK_NET_DNS_HOST -and $env:NET_DNS_HOST) { $env:WK_NET_DNS_HOST = $env:NET_DNS_HOST }
if (-not $env:WK_NET_DNS_PROOF_BASE_IP -and $env:NET_DNS_PROOF_BASE_IP) { $env:WK_NET_DNS_PROOF_BASE_IP = $env:NET_DNS_PROOF_BASE_IP }
if (-not $env:WK_NET_BAD_DNS -and $env:NET_BAD_DNS) { $env:WK_NET_BAD_DNS = $env:NET_BAD_DNS }
if (-not $env:WK_NET_PROBE_PROFILE_ID -and $env:NET_PROBE_PROFILE_ID) { $env:WK_NET_PROBE_PROFILE_ID = $env:NET_PROBE_PROFILE_ID }
if (-not $env:WK_NET_ACTIVE_PING_IP -and $env:NET_ACTIVE_PING_IP) { $env:WK_NET_ACTIVE_PING_IP = $env:NET_ACTIVE_PING_IP }
if (-not $env:WK_NET_ACTIVE_DNS_HOST -and $env:NET_ACTIVE_DNS_HOST) { $env:WK_NET_ACTIVE_DNS_HOST = $env:NET_ACTIVE_DNS_HOST }
if (-not $env:WK_NET_BASELINE_POLICY -and $env:NET_BASELINE_POLICY) { $env:WK_NET_BASELINE_POLICY = $env:NET_BASELINE_POLICY }
if (-not $env:WK_NET_BASELINE_MAX_ATTEMPTS -and $env:NET_BASELINE_MAX_ATTEMPTS) { $env:WK_NET_BASELINE_MAX_ATTEMPTS = $env:NET_BASELINE_MAX_ATTEMPTS }
if (-not $env:WK_NET_BASELINE_CONSECUTIVE_REQUIRED -and $env:NET_BASELINE_CONSECUTIVE_REQUIRED) { $env:WK_NET_BASELINE_CONSECUTIVE_REQUIRED = $env:NET_BASELINE_CONSECUTIVE_REQUIRED }
if (-not $env:WK_NET_BASELINE_SLEEP_SEC -and $env:NET_BASELINE_SLEEP_SEC) { $env:WK_NET_BASELINE_SLEEP_SEC = $env:NET_BASELINE_SLEEP_SEC }
if (-not $env:WK_NET_PROBE_PROFILE_REASON -and $env:NET_PROBE_PROFILE_REASON) { $env:WK_NET_PROBE_PROFILE_REASON = $env:NET_PROBE_PROFILE_REASON }
if (-not $env:WK_NET_AP_OR_NETWORK_PROFILE -and $env:NET_AP_OR_NETWORK_PROFILE) { $env:WK_NET_AP_OR_NETWORK_PROFILE = $env:NET_AP_OR_NETWORK_PROFILE }
if (-not $env:WK_NET_PROFILE_EFFECTIVE_FROM -and $env:NET_PROFILE_EFFECTIVE_FROM) { $env:WK_NET_PROFILE_EFFECTIVE_FROM = $env:NET_PROFILE_EFFECTIVE_FROM }
if (-not $env:WK_NET_PROFILE_APPROVED_BY -and $env:NET_PROFILE_APPROVED_BY) { $env:WK_NET_PROFILE_APPROVED_BY = $env:NET_PROFILE_APPROVED_BY }
if (-not $env:WK_NET_PROFILE_APPROVAL_TIME -and $env:NET_PROFILE_APPROVAL_TIME) { $env:WK_NET_PROFILE_APPROVAL_TIME = $env:NET_PROFILE_APPROVAL_TIME }
if (-not $env:WK_NET_DIAGNOSTIC_PROBE_TARGETS -and $env:NET_DIAGNOSTIC_PROBE_TARGETS) { $env:WK_NET_DIAGNOSTIC_PROBE_TARGETS = $env:NET_DIAGNOSTIC_PROBE_TARGETS }

# Optional: tag this run (edit per scenario, e.g. "cpu_stress", "binder_baseline")
if ($env:WK_SCENARIO_TAG -and $env:WK_SCENARIO_TAG -ne "") {
  $SCENARIO_TAG = $env:WK_SCENARIO_TAG
} else {
  $SCENARIO_TAG = ""
}

# Toggles
$CLEAN_HILOG         = $true
$CLEAN_FAULTLOG      = $false
$CLEAN_FAULTMON      = $true   # 婵絽绻嬮柌?run 闁告挸绉电粩濠氭偠?faultmon metrics/procs
# 闁煎浜滄慨鈺冣偓闈涚秺缂嶅牓寮堕崹顔炬憤闁哄啫鐖煎Λ鍧楁晬閸儳甯涢悹浣靛€撴繛鍥偨閵娿儳绉奸柛鎾崇С鐎靛矂寮甸悜妯活槯闂傚倽鎻槐姘舵晬鐏炶偐鈹掑ù?metrics / dmesg / hilog 闁哄啫鐖煎Λ璺ㄧ磼閻斿墎顏?
$SET_DEVICE_TIME     = $true
# 濠碘€冲€归悘澶愭偩濞嗘垟鏁勯柨娑樿嫰閸垶鎮介妸銉хЪ闁告挸绉崇€靛矂寮甸悜妯活槯闂傚倽鎻槐閬嶅触閿曗偓閸垶宕ｉ娑橆杹闁告柣鍔嶇€垫氨鈧?"YYYY-MM-DD HH:MM:SS"
$DEVICE_TIME         = ""
# 濞寸姴娴烽獮鍡樻櫠閸愩劌缍侀梺鎻掔箣閼垫垹鎲撮敐鍡欌偓鐣屾暜閸愩劎姣滈柛濠勩€嬬槐?/true/yes/on 闁?$true闁?/false/no/off 闁?$false闁挎稒绋戦崣鍓р偓?闁?$null闁挎稑鐗愰～瀣▔鐞涒檧鍋撳鍕紦閻犱礁澧介悿鍡涘灳濠垫挾绀?
#endregion Config

#region Functions
function Convert-EnvToBoolOrNull {
  param(
    [string]$Value
  )
  if (-not $Value) { return $null }

  $s = $Value.ToString().ToLowerInvariant()
  if ($s -eq "1" -or $s -eq "true" -or $s -eq "yes" -or $s -eq "y" -or $s -eq "on") {
    return $true
  }
  if ($s -eq "0" -or $s -eq "false" -or $s -eq "no" -or $s -eq "n" -or $s -eq "off") {
    return $false
  }
  return $null
}
function Get-WukongProfile {
  param([bool]$Exec,[bool]$Special)
  if (-not $Exec) { return "off" }
  if ($Exec -and -not $Special) { return "light" }
  return "full"
}

function Infer-GTLabels {
  param(
    [string]$ScenarioTag,
    [string]$FaultType,
    [bool]$EnableFaultInject
  )

  $tag = $ScenarioTag
  if (-not $tag -or $tag -eq "") {
    if ($FaultType -and $FaultType -ne "") { $tag = $FaultType } else { $tag = "unknown" }
  }

  $gt = [ordered]@{
    gt_scenario_tag = $tag
    gt_family       = "other"
    gt_severity     = "unknown"
    gt_run_kind     = "fault"
    gt_is_anomaly   = $true
  }

  # 闁煎啿鏈▍娆撴晬濮濇€焈FAULT_TYPE=none 闁瑰瓨鐗楀Ο澶婎嚕韫囨挸褰犻梻鍌ゅ幗閺佺偤宕?
  if (($FaultType -eq "none") -or (-not $EnableFaultInject)) {
    $gt.gt_family     = "background"
    $gt.gt_is_anomaly = $false
    $gt.gt_run_kind   = "background"

    if ($tag -eq "bg_idle_pure" -or $tag -eq "bg_idle") {
      $gt.gt_severity = "pure"
      $gt.gt_run_kind = "background_pure"
    } elseif ($tag -eq "bg_idle_noise" -or $tag -eq "noise_wukong_only") {
      $gt.gt_severity = "noise"
      $gt.gt_run_kind = "background_noise"
    }
    return [pscustomobject]$gt
  }

  # 闁轰礁鎳樺▓鎵偓纭呭煐濡?+ 濞戞挶鍎甸崳鍛婃償閿旇偐绀勯柟绋款槷缂嶆﹢鎮抽悧鍫熺畳闁告稖妫勯幃鏇犳喆閸曨偄鐏熼柛蹇旂矊缁ㄦ娊鏁?
  if ($FaultType -match '^cpu') {
    $gt.gt_family = "cpu"
    if ($tag -match 'baseline') { $gt.gt_severity = "mild" }
    elseif ($tag -match 'busy_yield') { $gt.gt_severity = "moderate" }
    elseif ($tag -match 'busy_loop|multicore|oversub|thread_leak') { $gt.gt_severity = "severe" }
  } elseif ($FaultType -match '^mem') {
    $gt.gt_family = "mem"
    if ($tag -match 'mild') { $gt.gt_severity = "mild" }
    elseif ($tag -match 'moderate') { $gt.gt_severity = "moderate" }
    elseif ($tag -match 'severe') { $gt.gt_severity = "severe" }
    elseif ($tag -match 'oomsafe') { $gt.gt_severity = "oomsafe" }
  } 
     elseif ($FaultType -match '^net') {
    $gt.gt_family = "net"
    if ($FaultType -match 'dns') { $gt.gt_severity = "mild" }
    elseif ($FaultType -match 'link_down') { $gt.gt_severity = "severe" }
    elseif ($FaultType -match 'link_flap') { $gt.gt_severity = "severe" }
    elseif ($FaultType -match 'wlan_disconnect') { $gt.gt_severity = "severe" }
    elseif ($FaultType -match 'wifi_disconnect') { $gt.gt_severity = "severe" }
    elseif ($FaultType -match 'wifi_auth_fail') { $gt.gt_severity = "severe" }
    elseif ($FaultType -match 'no_default_route') { $gt.gt_severity = "mild" }
    elseif ($FaultType -match 'no_ipv4_on_iface') { $gt.gt_severity = "severe" }
    elseif ($FaultType -match 'wrong_default_route') { $gt.gt_severity = "moderate" }
    else { $gt.gt_severity = "unknown" }
  }
  
  elseif ($FaultType -match '^deadlock') {
    $gt.gt_family   = "deadlock"
    $gt.gt_severity = "fault"
  } elseif ($FaultType -match '^segv') {
    $gt.gt_family   = "segv"
    $gt.gt_severity = "fault"
  }

  return [pscustomobject]$gt
}

#region Faultmon
function Faultmon-Poke {
  param([string]$SN, [string]$Tag)

  if (-not $EnableFaultmon) { return }
  if ([string]::IsNullOrWhiteSpace($FaultmonScript)) { return }

  # Tag 鐎点倝缂氶鍛存晬濮橆偆鐟濋柛姘煎亞閳规牠寮界涵椋庡耿閺夆晜鐟╅崳鐑藉磻濮橆偆顏辨繛鍠°倓姘﹂梺鎻掔箲缁旇煤濡ゅ绀夐梺顒€鐏濋崢銈囨媼閹屾У缂佹棏鍨扮槐鈺呭矗?閻熸瑱绲鹃悗浠嬫⒒椤曗偓椤?
  $safe = $Tag
  if ([string]::IsNullOrWhiteSpace($safe)) { $safe = "poke" }
  $safe = ($safe -replace "\s+","_")
  $safe = ($safe -replace "[^A-Za-z0-9_\-:.]","_")

    try {
    # fire-and-forget：避免 poke 偶发阻塞拖死整条 run
    $cmd = "nohup sh $FaultmonScript poke $safe >/dev/null 2>&1 & echo POKE_SENT"
    $p = Start-Process -FilePath "hdc" -ArgumentList @("-t",$SN,"shell",$cmd) -NoNewWindow -PassThru
    if (-not $p.WaitForExit(5000)) {
      try { $p.Kill() } catch {}
      Write-Host "[faultmon] WARN: poke timeout (ignored)" -ForegroundColor Yellow
    }
  } catch {
    # ignore
  }

}


function Summarize-FaultmonEventsInWindow {
  param([string]$EventsPath,[Int64]$StartMs,[Int64]$EndMs)

  $ret = @{
    total = 0
    cpu_hotspot  = 0
    mem_pressure = 0
    io_pressure  = 0
    poke_begin   = 0
    poke_end     = 0
  }

  if (-not $EventsPath -or -not (Test-Path $EventsPath)) { return $ret }

  foreach ($line in (Get-Content $EventsPath -ErrorAction SilentlyContinue)) {
    if (-not $line) { continue }
    $obj = $null
    try { $obj = $line | ConvertFrom-Json -ErrorAction Stop } catch { continue }

    $ts = 0
    try { $ts = [Int64]$obj.ts } catch { continue }

    if ($ts -lt $StartMs -or $ts -gt $EndMs) { continue }

    $ret.total++

    $tag = ""
    try { $tag = [string]$obj.tag } catch { $tag = "" }

    if ($tag -eq "cpu_hotspot")  { $ret.cpu_hotspot++ }
    if ($tag -eq "mem_pressure") { $ret.mem_pressure++ }
    if ($tag -eq "io_pressure")  { $ret.io_pressure++ }

    if ($tag -eq "poke") {
      $msg = ""
      try { $msg = [string]$obj.msg } catch { $msg = "" }
      if ($msg -like "run_begin:*") { $ret.poke_begin++ }
      if ($msg -like "run_end:*")   { $ret.poke_end++ }
    }
  }

  return $ret
}


# ===== fault injection config =====
$ENABLE_FAULT_INJECT = $true      # 濮掓稒顭堥濠氬礂娴ｇ瓔鍟呴柤濂変簻婵晠寮崨瀛橆唶婵炲鍔岄崣鍡涙晬鐏炶偐鐦嶉柛娆樺灟娴滄帡骞?run 闁衡偓闁稖绀?$false
if ($env:WK_FAULT_TYPE -and $env:WK_FAULT_TYPE -ne "") {
  $FAULT_INJECT_TYPE = $env:WK_FAULT_TYPE
} else {
  # 闁衡偓椤栨稑鐦柣銊ュ鐞氼偊宕圭€ｎ亜鐦堕柟濂夊墾缁?
  #   cpu / cpu_baseline / cpu_busy_loop / cpu_busy_yield /
  #   cpu_multicore / cpu_oversub / cpu_thread_leak
  #   mem / mem_mild / mem_moderate / mem_severe / mem_oomsafe
  #   deadlock / segv
  $FAULT_INJECT_TYPE = "deadlock"
}
# --- [ADD] normalize net fault aliases (avoid missing net snapshot / injector) ---
switch ($FAULT_INJECT_TYPE) {
  "dns_fail"        { $FAULT_INJECT_TYPE = "net_dns_fail" }
  "link_down"       { $FAULT_INJECT_TYPE = "net_link_down" }
  "link_flap"       { $FAULT_INJECT_TYPE = "net_link_flap" }
  "wlan_disconnect" { $FAULT_INJECT_TYPE = "net_wlan_disconnect" }
  # [ADD] bg_net baseline -> net_bg (so net snapshots run)
  "bg_net"          { $FAULT_INJECT_TYPE = "net_bg" }
  # [ADD] new wlan fault type aliases
  "wifi_disconnect"           { $FAULT_INJECT_TYPE = "net_wifi_disconnect" }
  "wifi_auth_fail_wrong_psk"  { $FAULT_INJECT_TYPE = "net_wifi_auth_fail_wrong_psk" }
  "no_default_route"          { $FAULT_INJECT_TYPE = "net_no_default_route" }
  "no_ipv4_on_iface"          { $FAULT_INJECT_TYPE = "net_no_ipv4_on_iface" }
  "gateway_unreachable"       { $FAULT_INJECT_TYPE = "net_gateway_unreachable" }
  default { }
}

$FAULT_BIN_DIR       = "/data/local/tmp/out_static_arm64"
# ===== network fault & snapshot config =====
$tmp = Convert-EnvToBoolOrNull $env:WK_ENABLE_NET_SNAPSHOT
if ($null -ne $tmp) { $ENABLE_NET_SNAPSHOT = $tmp } else { $ENABLE_NET_SNAPSHOT = $false }
$NET_IFACE           = if ($env:WK_NET_IFACE -and $env:WK_NET_IFACE -ne "") { $env:WK_NET_IFACE } else { "eth1" }
$NET_WLAN_IFACE      = if ($env:WK_NET_WLAN_IFACE -and $env:WK_NET_WLAN_IFACE -ne "") { $env:WK_NET_WLAN_IFACE } else { "wlan0" }
$NET_PING_IP         = if ($env:WK_NET_PING_IP -and $env:WK_NET_PING_IP -ne "") { $env:WK_NET_PING_IP } else { "8.8.8.8" }
$NET_DNS_HOST         = if ($env:WK_NET_DNS_HOST -and $env:WK_NET_DNS_HOST -ne "") { $env:WK_NET_DNS_HOST } else { "www.baidu.com" }
$NET_DNS_PROOF_BASE_IP = if ($env:WK_NET_DNS_PROOF_BASE_IP -and $env:WK_NET_DNS_PROOF_BASE_IP -ne "") { $env:WK_NET_DNS_PROOF_BASE_IP } else { $NET_PING_IP }
$NET_PROBE_PROFILE_ID = if ($env:WK_NET_PROBE_PROFILE_ID -and $env:WK_NET_PROBE_PROFILE_ID -ne "") { $env:WK_NET_PROBE_PROFILE_ID } else { "" }
$NET_ACTIVE_PING_IP = if ($env:WK_NET_ACTIVE_PING_IP -and $env:WK_NET_ACTIVE_PING_IP -ne "") { $env:WK_NET_ACTIVE_PING_IP } else { $NET_PING_IP }
$NET_ACTIVE_DNS_HOST = if ($env:WK_NET_ACTIVE_DNS_HOST -and $env:WK_NET_ACTIVE_DNS_HOST -ne "") { $env:WK_NET_ACTIVE_DNS_HOST } else { $NET_DNS_HOST }
$NET_BASELINE_POLICY = if ($env:WK_NET_BASELINE_POLICY -and $env:WK_NET_BASELINE_POLICY -ne "") { $env:WK_NET_BASELINE_POLICY } else { "" }
$NET_BASELINE_MAX_ATTEMPTS = if ($env:WK_NET_BASELINE_MAX_ATTEMPTS -and $env:WK_NET_BASELINE_MAX_ATTEMPTS -ne "") { $env:WK_NET_BASELINE_MAX_ATTEMPTS } else { "" }
$NET_BASELINE_CONSECUTIVE_REQUIRED = if ($env:WK_NET_BASELINE_CONSECUTIVE_REQUIRED -and $env:WK_NET_BASELINE_CONSECUTIVE_REQUIRED -ne "") { $env:WK_NET_BASELINE_CONSECUTIVE_REQUIRED } else { "" }
$NET_BASELINE_SLEEP_SEC = if ($env:WK_NET_BASELINE_SLEEP_SEC -and $env:WK_NET_BASELINE_SLEEP_SEC -ne "") { $env:WK_NET_BASELINE_SLEEP_SEC } else { "" }
$NET_PROBE_PROFILE_REASON = if ($env:WK_NET_PROBE_PROFILE_REASON -and $env:WK_NET_PROBE_PROFILE_REASON -ne "") { $env:WK_NET_PROBE_PROFILE_REASON } else { "" }
$NET_AP_OR_NETWORK_PROFILE = if ($env:WK_NET_AP_OR_NETWORK_PROFILE -and $env:WK_NET_AP_OR_NETWORK_PROFILE -ne "") { $env:WK_NET_AP_OR_NETWORK_PROFILE } else { "" }
$NET_PROFILE_EFFECTIVE_FROM = if ($env:WK_NET_PROFILE_EFFECTIVE_FROM -and $env:WK_NET_PROFILE_EFFECTIVE_FROM -ne "") { $env:WK_NET_PROFILE_EFFECTIVE_FROM } else { "" }
$NET_PROFILE_APPROVED_BY = if ($env:WK_NET_PROFILE_APPROVED_BY -and $env:WK_NET_PROFILE_APPROVED_BY -ne "") { $env:WK_NET_PROFILE_APPROVED_BY } else { "" }
$NET_PROFILE_APPROVAL_TIME = if ($env:WK_NET_PROFILE_APPROVAL_TIME -and $env:WK_NET_PROFILE_APPROVAL_TIME -ne "") { $env:WK_NET_PROFILE_APPROVAL_TIME } else { "" }
$NET_DIAGNOSTIC_PROBE_TARGETS = if ($env:WK_NET_DIAGNOSTIC_PROBE_TARGETS -and $env:WK_NET_DIAGNOSTIC_PROBE_TARGETS -ne "") { $env:WK_NET_DIAGNOSTIC_PROBE_TARGETS } else { "" }
# [MOD] DNS 婵炲鍔岄崣鍡涙偨閵娧勭暠闁?DNS闁挎稑鐗嗚ぐ鏌ユ焻濮樺磭绠栭柣婊庡灠椤ｃ劑宕ｅ鈧崳铏规啺閸℃瑦纾伴柨?
$NET_BAD_DNS = if ($env:WK_NET_BAD_DNS -and $env:WK_NET_BAD_DNS -ne "") { $env:WK_NET_BAD_DNS } else { "127.0.0.2" }
$NET_LINK_DOWN_HOLD_SEC = if ($env:WK_NET_LINK_DOWN_HOLD_SEC -and $env:WK_NET_LINK_DOWN_HOLD_SEC -ne "") { [int]$env:WK_NET_LINK_DOWN_HOLD_SEC } else { 15 }
$NET_FLAP_COUNT         = if ($env:WK_NET_FLAP_COUNT -and $env:WK_NET_FLAP_COUNT -ne "") { [int]$env:WK_NET_FLAP_COUNT } else { 2 }
$NET_FLAP_DOWN_SEC      = if ($env:WK_NET_FLAP_DOWN_SEC -and $env:WK_NET_FLAP_DOWN_SEC -ne "") { [int]$env:WK_NET_FLAP_DOWN_SEC } else { 3 }
$NET_FLAP_UP_SEC        = if ($env:WK_NET_FLAP_UP_SEC -and $env:WK_NET_FLAP_UP_SEC -ne "") { [int]$env:WK_NET_FLAP_UP_SEC } else { 3 }
$NET_TARGET_IP          = if ($env:WK_NET_TARGET_IP -and $env:WK_NET_TARGET_IP -ne "") { $env:WK_NET_TARGET_IP } else { "8.8.4.4" }

function Assert-NetShellToken {
  param(
    [string]$Name,
    [string]$Value,
    [string]$Pattern
  )
  if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch $Pattern) {
    throw ("unsafe {0} for hdc shell probe: {1}" -f $Name, $Value)
  }
}

$ipv4Pattern = '^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$'
$hostPattern = '^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$'
$ifacePattern = '^[A-Za-z0-9_.:-]{1,32}$'
Assert-NetShellToken "NET_IFACE" $NET_IFACE $ifacePattern
Assert-NetShellToken "NET_WLAN_IFACE" $NET_WLAN_IFACE $ifacePattern
Assert-NetShellToken "NET_PING_IP" $NET_PING_IP $ipv4Pattern
Assert-NetShellToken "NET_DNS_PROOF_BASE_IP" $NET_DNS_PROOF_BASE_IP $ipv4Pattern
Assert-NetShellToken "NET_ACTIVE_PING_IP" $NET_ACTIVE_PING_IP $ipv4Pattern
Assert-NetShellToken "NET_DNS_HOST" $NET_DNS_HOST $hostPattern
Assert-NetShellToken "NET_ACTIVE_DNS_HOST" $NET_ACTIVE_DNS_HOST $hostPattern
Assert-NetShellToken "NET_BAD_DNS" $NET_BAD_DNS $ipv4Pattern
Assert-NetShellToken "NET_TARGET_IP" $NET_TARGET_IP $ipv4Pattern

# net_fault (device-side injector)
$NetFaultScriptLocal = $null
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptRoot "..\..\..")).Path
$NetFaultCandidates = @(
  (Join-Path $RepoRoot "data_train_test\board\scripts\net_fault.sh"),
  (Join-Path $ScriptRoot "net_fault.sh"),
  (Join-Path $ScriptRoot "net_fault")
)
foreach ($cand in $NetFaultCandidates) {
  if (Test-Path -LiteralPath $cand) { $NetFaultScriptLocal = $cand; break }
}
$NetFaultScriptRemote = "/data/local/tmp/net_fault.sh"


# 濠碘€冲€归悘澶愬及?net_* 闁轰礁鎳樺▓浼存晬鐏炶棄鐏熷娑欘焾椤撹顕ｉ埀顒勫触?net snapshot
if ($FAULT_INJECT_TYPE -like "net_*") {
  $ENABLE_NET_SNAPSHOT = $true
}

# ===== faultmon metrics integration =====
$EnableFaultmon = $true
$FaultmonScript = "/data/local/tmp/faultmon.sh"   # 闁哄鐏濋悺娆愮▔婵犲嫭鐣遍悗鍦仱濡绢垳鎹勯姘辩獮
$FaultmonRoot   = "/data/faultmon"               # 濞?faultmon.sh 濞?ROOT 濞ｅ洦绻冪€垫梹绋夐埀顒勬嚊?

# Hilog capture
$ENABLE_HILOG_PERSIST = $true      # hilog -w start/stop (device)
$ENABLE_HILOG_STREAM  = $false     # host redirect "hilog -v long" as fallback

# Device-side pruning after recv
$ENABLE_PRUNE_DEVICE_HILOG = $true
$PRUNE_KEEP_SEGMENTS       = 10     # keep last N hilog.*.gz on device

# Wukong
$ENABLE_WUKONG_EXEC   = $true
$EXEC_SEED            = 10
$EXEC_INTERVAL_MS     = 1000
$EXEC_APPSWITCH_PCT   = 0.28
$EXEC_TOUCH_PCT       = 0.72
$EXEC_COUNT           = 180
$EXEC_SCREENSHOT      = $true

$ENABLE_SPECIAL_SWEEP = $true
# 闁稿繋娴囬蹇涙焻濮樺磭绠栭柣婊庡灠椤ｃ劑宕ｅ鈧崳铏规啺閸℃瑦纾?Wukong 閻炴稑濂旂拹鐔兼晬?
#   WK_ENABLE_WUKONG_EXEC=1/0/true/false
#   WK_ENABLE_SPECIAL_SWEEP=1/0/true/false
function Convert-ToBoolFromEnv($val, $default) {
  if (-not $val) { return $default }
  $s = $val.ToString().ToLowerInvariant()
  if ($s -in @("1","true","yes","y","on")) { return $true }
  if ($s -in @("0","false","no","n","off")) { return $false }
  return $default
}

$ENABLE_WUKONG_EXEC   = Convert-ToBoolFromEnv $env:WK_ENABLE_WUKONG_EXEC   $ENABLE_WUKONG_EXEC
$ENABLE_SPECIAL_SWEEP = Convert-ToBoolFromEnv $env:WK_ENABLE_SPECIAL_SWEEP $ENABLE_SPECIAL_SWEEP

$SPECIAL_COUNT        = 50

# 濞戞挸绉撮崯鈧ù?wukong appinfo 闁煎浜滄慨鈺呭箯婢跺﹤寮块梺?APP闁挎稑鑻ぐ褎鎷呯捄銊︽殢闂傚牊鐟﹂埀?APP_LIST
$AUTO_APP_LIST        = $false   # use static APP_LIST; avoid sweeping all apps via wukong appinfo
$APP_LIST             = @('com.ohos.camera','com.ohos.contacts')  # 闁告瑯浜濈粊鎾儎閸涘﹥绨氬☉鎾亾濞?APP闁挎稑鑻々褔妫侀埀顒佸緞濮橆偊鍤嬮柛娆樺灠濠€顏勵潰閵堝牊瀚归柛?

$FAULTLOG_SAMPLE_N    = 0
$WukongCmdline        = ""
$RUN_WINDOW_SEC      = 30   # 闂?wukong 闁革妇鍎ゅ▍娆愮▔鐎ｅ墎绀夊ǎ鍥ㄧ箚閻﹀鎳涢崘鑼瘜闂佹彃娲﹂悧杈╃棯?30 缂?

# 闁告瑯鍨堕埀顒€顧€缁变即鎮?weekend 闁煎瓨纰嶅﹢鐗堝閻樻彃寮抽柨娑樿嫰瀹搁亶宕氶懜鍨粯閻?run window闁挎稑鐗忛～妤呮晬婢舵稓绀夐柣顫妺缁剟鎳楃仦鐐彲/闁革綆浜滈敍鎰板捶閻戞ɑ鐝柟宄邦樀閺嗛亶鏌岄崶銊у缂佹劖顨呰ぐ?
$BASELINE_SEC = 0
try {
  if ($env:WK_BASELINE_SEC) { $BASELINE_SEC = [int]$env:WK_BASELINE_SEC }
} catch { $BASELINE_SEC = 0 }
if ($BASELINE_SEC -lt 0) { $BASELINE_SEC = 0 }

$script:FaultInjectMinRunSec = 0   # 闁?Start-FaultInject 閻犱緤绱曢悾濠氭儍閸曨剚浠橀悘?run window闁? 閻炴稏鍔庨妵姘▔瀹ュ鏉哄鑸电墱鐎规娊寮?

# =====================[ Helpers ]====================
function Ensure-Dir($p) {
  if (-not $p) { return }

  if (Test-Path $p) { return }

  $parent = Split-Path $p -Parent
  if ($parent -and -not (Test-Path $parent)) {
    Ensure-Dir $parent
  }

  if (-not (Test-Path $p)) {
    New-Item -ItemType Directory -Force -Path $p | Out-Null
  }
}
# ---------- Safe Test-Path (avoid null Path errors) ----------
function Test-PathSafe {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  try { return (Test-Path -LiteralPath $Path) } catch { return $false }
}

# ---------- Kill residual inject processes (avoid cross-run pollution) ----------
function Device-KillResidualProcs {
  param(
    [Parameter(Mandatory=$true)][string]$SN,
    [string[]]$Names = @(
      "wukong","faultmon","net_fault.sh","stress_ng","stress-ng",
      "memory_leak_demo","cpu_stress_demo","deadlock_demo","crash_seg_demo",
      "mem_leak","memleak","deadlock_test","segv_test","segv","oom_killer"
    )
  )

  # 闁烩晩鍠楅悥锝夋晬濮樻湹缂夐柛蹇撶Х濞?run 濞戞挻褰冨┃鈧柨娑樼墔缁楀倹绋夐埀顒€鈻庨埄鍐╂殘闁?闁告ê顑嗙粊瀛樻交濞戞埃鏌ら柡鍫邯閳ь兘鍋撻柛鎴犲皑缁辨繃瀵煎顓℃澖闁哄本鎸诲﹢鏉库枎?metrics/events闁?
  $psOut = hdc -t $SN shell "ps -A 2>/dev/null || ps -ef 2>/dev/null || ps 2>/dev/null" 2>$null
  if (-not $psOut) { return }

  foreach ($name in $Names) {
    if ([string]::IsNullOrWhiteSpace($name)) { continue }

    $rx = [regex]("(\\s|/)" + [regex]::Escape($name) + "(\\s|$)")
    $ppids = @()

    foreach ($ln in $psOut) {
      if (-not $ln) { continue }
      if ($ln -match "^\s*PID\b" -or $ln -match "^\s*USER\b" -or $ln -match "^\s*UID\b") { continue }
      if (-not $rx.IsMatch($ln)) { continue }

      $cols = $ln.Trim() -split "\s+"
      $ppid = $null
      if ($cols.Count -ge 1 -and $cols[0] -match "^\d+$") { $ppid = [int]$cols[0] }
      elseif ($cols.Count -ge 2 -and $cols[1] -match "^\d+$") { $ppid = [int]$cols[1] }

      if ($ppid -ne $null -and $ppid -gt 1) { $ppids += $ppid }
    }

    $ppids = $ppids | Sort-Object -Unique
    if ($ppids.Count -le 0) { continue }

    Write-Host ("[clean] kill residual {0}: {1}" -f $name, ($ppids -join ",")) -ForegroundColor DarkYellow

    foreach ($ppid in $ppids) { hdc -t $SN shell ("kill -15 " + $ppid + " 2>/dev/null") 2>$null | Out-Null }
    Start-Sleep -Milliseconds 200
    foreach ($ppid in $ppids) { hdc -t $SN shell ("kill -9 " + $ppid + " 2>/dev/null") 2>$null | Out-Null }
  }
}



function Get-CountFromMap {
  param($Map,[string]$Key)
  if ($null -eq $Map) { return 0 }
  if ($Map.ContainsKey($Key)) { return [int]$Map[$Key] }
  return 0
}

function Summarize-FaultmonEventsForRun {
  param(
    [string]$EventsPath,
    [string]$RunId,
    [Int64]$FallbackStartMs,
    [Int64]$FallbackEndMs
  )

  $ret = [ordered]@{
    has_events = $false
    window_source = 'none'
    window_start_ms = $null
    window_end_ms = $null
    total_in_window = 0
    counts = @{}
    cpu_hotspot = 0
    mem_pressure = 0
    io_pressure = 0
  }

  if (-not (Test-PathSafe $EventsPath)) { return [pscustomobject]$ret }
  $ret.has_events = $true

  # 1) 濞村吋锚閸樻盯鎮?poke(run_begin/run_end) 闁煎浜滄慨鈺冩啑娴ｇ顥呯紒鎰殔瑜版盯鏁嶅畝鍕級闁稿繐绉峰▔?run 濞戞挻褰冨┃鈧?
  $beginTs = $null
  $endTs = $null
  $rxBegin = '^run_begin:' + [regex]::Escape($RunId)
  $rxEnd   = '^run_end:'   + [regex]::Escape($RunId)

  foreach ($ln in (Get-Content -LiteralPath $EventsPath -ErrorAction SilentlyContinue)) {
    if (-not $ln) { continue }
    try { $e = $ln | ConvertFrom-Json -ErrorAction Stop } catch { continue }
    if ($null -eq $e.ts) { continue }
    if ($e.tag -eq 'poke' -and $e.msg) {
      if ($null -eq $beginTs -and ($e.msg -match $rxBegin)) { $beginTs = [Int64]$e.ts }
      if ($null -eq $endTs   -and ($e.msg -match $rxEnd))   { $endTs   = [Int64]$e.ts }
    }
  }

  $startMs = $null
  $endMs   = $null
  if ($null -ne $beginTs -and $null -ne $endTs -and $endTs -ge $beginTs) {
    $startMs = $beginTs
    $endMs   = $endTs
    $ret.window_source = 'poke'
  } elseif ($FallbackStartMs -gt 0 -and $FallbackEndMs -gt 0 -and $FallbackEndMs -ge $FallbackStartMs) {
    $startMs = $FallbackStartMs
    $endMs   = $FallbackEndMs
    $ret.window_source = 'skew'
  }

  $ret.window_start_ms = $startMs
  $ret.window_end_ms   = $endMs

  $counts = @{}
  $total = 0

  foreach ($ln in (Get-Content -LiteralPath $EventsPath -ErrorAction SilentlyContinue)) {
    if (-not $ln) { continue }
    try { $e = $ln | ConvertFrom-Json -ErrorAction Stop } catch { continue }
    if ($null -eq $e.ts) { continue }

    $ts = [Int64]$e.ts
    if ($null -ne $startMs -and $null -ne $endMs) {
      if ($ts -lt $startMs -or $ts -gt $endMs) { continue }
    }

    $tag = if ($e.tag) { [string]$e.tag } else { 'unknown' }
    if (-not $counts.ContainsKey($tag)) { $counts[$tag] = 0 }
    $counts[$tag] = [int]$counts[$tag] + 1
    $total++
  }

  $ret.total_in_window = $total
  $ret.counts = $counts
  $ret.cpu_hotspot = Get-CountFromMap -Map $counts -Key 'cpu_hotspot'
  $ret.mem_pressure = Get-CountFromMap -Map $counts -Key 'mem_pressure'
  $ret.io_pressure  = Get-CountFromMap -Map $counts -Key 'io_pressure'

  return [pscustomobject]$ret
}
function Read-MetricsSummaryInWindow {
  param(
    [string]$CsvPath,
    [Int64]$StartMs,
    [Int64]$EndMs
  )

  $ret = [ordered]@{
    has_metrics          = $false
    rows_in_window       = 0
    ts_field             = $null
    fields               = @()

    load1_peak_x100      = $null
    cpu_util_peak_x100   = $null

    mem_avail_first_kb   = $null
    mem_avail_last_kb    = $null
    mem_avail_min_kb     = $null
    mem_avail_max_kb     = $null
    mem_avail_drop_kb    = $null
    mem_avail_range_kb   = $null
  }

  if (-not (Test-PathSafe $CsvPath)) { return [pscustomobject]$ret }

  $rows = @()
  try {
    $rows = @(Import-Csv -LiteralPath $CsvPath -ErrorAction Stop)
  } catch {
    return [pscustomobject]$ret
  }

  if (-not $rows -or $rows.Count -le 0) { return [pscustomobject]$ret }
  $ret.has_metrics = $true

  $props = $rows[0].PSObject.Properties.Name
  $ret.fields = $props

  # ---- ts field ----
  $tsField = $null
  foreach ($cand in @("ts","ts_ms","timestamp_ms","time_ms")) {
    if ($props -contains $cand) { $tsField = $cand; break }
  }
  if (-not $tsField) { $tsField = "ts" }
  $ret.ts_field = $tsField

  # ---- optional fields ----
  $hasLoad = ($props -contains "load1_x100")
  $hasCpuU = ($props -contains "cpu_util_total_x100")

  $memAvailField = $null
  foreach ($cand in @("mem_available_kb","mem_avail_kb","mem_free_kb")) {
    if ($props -contains $cand) { $memAvailField = $cand; break }
  }

  # ---- filter window ----
  $sel = @()
  foreach ($r in $rows) {
    $ts = 0L
    if (-not [Int64]::TryParse([string]$r.$tsField, [ref]$ts)) { continue }
    if ($StartMs -gt 0 -and $EndMs -gt 0) {
      if ($ts -lt $StartMs -or $ts -gt $EndMs) { continue }
    }
    $sel += $r
  }

  if (-not $sel -or $sel.Count -le 0) {
    $ret.rows_in_window = 0
    return [pscustomobject]$ret
  }
  $ret.rows_in_window = $sel.Count

  try { $sel = $sel | Sort-Object { [Int64]($_.$tsField) } } catch {}

  # ---- load1_peak_x100 (treat <0 as missing) ----
  if ($hasLoad) {
    $vals = @(
      $sel | ForEach-Object {
        $v = 0
        if ([int]::TryParse([string]$_."load1_x100", [ref]$v) -and $v -ge 0) { $v }
      } | Where-Object { $_ -ne $null }
    )
    if ($vals.Count -gt 0) {
      $ret.load1_peak_x100 = [int](($vals | Measure-Object -Maximum).Maximum)
    }
  }

  # ---- cpu_util_peak_x100 (treat <0 as missing) ----
  if ($hasCpuU) {
    $vals = @(
      $sel | ForEach-Object {
        $v = 0
        if ([int]::TryParse([string]$_."cpu_util_total_x100", [ref]$v) -and $v -ge 0) { $v }
      } | Where-Object { $_ -ne $null }
    )
    if ($vals.Count -gt 0) {
      $ret.cpu_util_peak_x100 = [int](($vals | Measure-Object -Maximum).Maximum)
    }
  }

  # ---- mem_available stats (treat <0 as missing) ----
  if ($memAvailField) {
    $vals = @(
      $sel | ForEach-Object {
        $v = 0L
        if ([Int64]::TryParse([string]($_.$memAvailField), [ref]$v) -and $v -ge 0) { $v }
      } | Where-Object { $_ -ne $null }
    )

    if ($vals.Count -gt 0) {
      $ret.mem_avail_first_kb = [Int64]$vals[0]
      $ret.mem_avail_last_kb  = [Int64]$vals[-1]
      $ret.mem_avail_min_kb   = [Int64](($vals | Measure-Object -Minimum).Minimum)
      $ret.mem_avail_max_kb   = [Int64](($vals | Measure-Object -Maximum).Maximum)
      $ret.mem_avail_drop_kb  = [Int64]($ret.mem_avail_first_kb - $ret.mem_avail_min_kb)
      $ret.mem_avail_range_kb = [Int64]($ret.mem_avail_max_kb - $ret.mem_avail_min_kb)
    }
  }

  return [pscustomobject]$ret
}
#endregion Faultmon



function Clamp {
  param([double]$x,[double]$lo,[double]$hi)
  if ($x -lt $lo) { return $lo }
  if ($x -gt $hi) { return $hi }
  return $x
}

function Normalize-WukongPct([double]$v) {
  if ($v -gt 1.0) { return ($v / 100.0) }
  return $v
}

# ---------- Multi-label helpers (WK_LABELS + auto labels) ----------
function Split-RunLabels {
  param([string]$s)

  if ([string]::IsNullOrWhiteSpace($s)) { return @() }

  # allow comma / semicolon / whitespace separators
  $raw = $s -split '[,; ]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }

  # dedup (stable order)
  $seen = @{}
  $out = @()
  foreach ($x in $raw) {
    if (-not $seen.ContainsKey($x)) {
      $seen[$x] = $true
      $out += $x
    }
  }
  return $out
}

function Merge-RunLabels {
  param([object[]]$A, [object[]]$B)

  $seen = @{}
  $out = @()

  foreach ($x in @($A + $B)) {
    if ($null -eq $x) { continue }
    $t = ([string]$x).Trim()
    if ($t -eq "") { continue }
    if (-not $seen.ContainsKey($t)) {
      $seen[$t] = $true
      $out += $t
    }
  }
  return $out
}

function Compute-ObsMultiLabels {
  param(
    [string]$GTFamily,
    [string]$WukongProfile,
    $EventSummary,
    $MetricsSummary,
    [int]$NewFaultCount,
    [int]$BinderDelta,
    [int]$AvcDelta,
    [int]$HungDelta
  )

  $scores = @{ cpu_raw = 0.0; cpu = 0.0; mem = 0.0; other = 0.0 }

  $cpuEv = 1.0 * [math]::Min(4, [int]$EventSummary.cpu_hotspot)
  $memEv = 1.0 * [math]::Min(4, [int]$EventSummary.mem_pressure)

  $cpuMet = 0.0
  if ($null -ne $MetricsSummary.load1_peak_x100) {
    $load = [double]$MetricsSummary.load1_peak_x100 / 100.0
    $cpuMet += (Clamp(($load - 2.0) / 1.5) 0.0 3.0) * 1.0
  }
  if ($null -ne $MetricsSummary.cpu_util_peak_x100) {
    $util = [double]$MetricsSummary.cpu_util_peak_x100 / 100.0
    $cpuMet += (Clamp (($util - 30.0) / 30.0) 0.0 3.0) * 0.8
  }

  $memMet = 0.0
  if ($null -ne $MetricsSummary.mem_avail_drop_kb) {
    $dropMb = [double]$MetricsSummary.mem_avail_drop_kb / 1024.0
    $memMet += (Clamp ($dropMb / 128.0) 0.0 3.0) * 1.0
  }
  if ($null -ne $MetricsSummary.mem_avail_min_kb) {
    $minMb = [double]$MetricsSummary.mem_avail_min_kb / 1024.0
    if ($minMb -lt 200.0) { $memMet += 0.7 }
  }

  $other = 0.0
  if ($NewFaultCount -gt 0) { $other += 2.5 + (Clamp (([double]($NewFaultCount - 1)) / 3.0) 0.0 1.5) }
  if ($HungDelta -gt 0) { $other += 1.0 }
  if ($BinderDelta -gt 0) { $other += 0.4 }
  if ($AvcDelta -gt 0) { $other += 0.4 }

  $cpuRaw = $cpuEv + $cpuMet
  $cpuAdj = $cpuRaw
  if ($WukongProfile -ne 'off' -and [int]$EventSummary.cpu_hotspot -le 0) {
    $cpuAdj = [math]::Max(0.0, $cpuAdj - 0.8)
  }

  $scores.cpu_raw = [math]::Round($cpuRaw, 3)
  $scores.cpu     = [math]::Round($cpuAdj, 3)
  $scores.mem     = [math]::Round(($memEv + $memMet), 3)
  $scores.other   = [math]::Round($other, 3)

  $thrFault = 2.0
  $thrWarn  = 1.0

  $familiesFault = @()
  $familiesWarn  = @()
  foreach ($k in @('cpu','mem','other')) {
    $v = [double]$scores[$k]
    if ($v -ge $thrFault) { $familiesFault += $k }
    elseif ($v -ge $thrWarn) { $familiesWarn += $k }
  }

  $state = 'normal'
  if ($familiesFault.Count -gt 0) { $state = 'fault' }
  elseif ($familiesWarn.Count -gt 0 -or $BinderDelta -gt 0 -or $AvcDelta -gt 0 -or $HungDelta -gt 0) { $state = 'warning' }

  $cand = if ($familiesFault.Count -gt 0) { $familiesFault } else { $familiesWarn }
  $primary = 'none'
  $secondary = @()
  if ($cand.Count -gt 0) {
    $best = $cand | Sort-Object { -1.0 * [double]$scores[$_] } | Select-Object -First 1
    $primary = $best
    $secondary = $cand | Where-Object { $_ -ne $best }
  }

  $conf = @()
  if ($WukongProfile -ne 'off') { $conf += 'wukong_load' }
  if ($GTFamily -eq 'mem' -and $WukongProfile -ne 'off') { $conf += 'cpu_may_be_confounder' }

  return [pscustomobject]@{
    obs_threshold_fault = $thrFault
    obs_threshold_warn  = $thrWarn
    obs_fault_state     = $state
    obs_families        = $familiesFault
    obs_families_warn   = $familiesWarn
    obs_primary_family  = $primary
    obs_secondary_families = $secondary
    obs_scores          = $scores
    obs_confounders     = $conf
  }
}


# ---------- 闂傚啫寮堕娑氭崉?run 婵厜鍓濋悡瀣晬濮橆厾顏搁柣鐐叉閻ｎ偊鎮惧▎鎰珚闂傚懏绮嶉弫鐐哄礂閵夈劎绠荤紒?----------

function Device-GetPsAll {
  param([string]$SN)

  $out = hdc -t $SN shell "ps -A 2>/dev/null" 2>$null
  $txt = ($out -join "`n")
  if (-not $out -or $txt -match "invalid option" -or $txt -match "unknown option") {
    $out = hdc -t $SN shell "ps -ef 2>/dev/null" 2>$null
  }
  if (-not $out) { $out = hdc -t $SN shell "ps" 2>$null }
  return $out
}


function Device-Clean {
  param([string]$SN)

  Device-KillResidualProcs -SN $SN

  # ---------- 婵炴挸鎳愰埞?wukong report闁挎稑鐭傛导鈺呭礂?bg_idle_pure 闁瑰嘲顦崺宀勫籍?report ----------
  hdc -t $SN shell "rm -rf /data/local/tmp/wukong/report/* 2>/dev/null" | Out-Null

  if ($CLEAN_HILOG) {
    Write-Host "Clean hilog on device"
    hdc -t $SN shell 'hilog -w stop 2>/dev/null; rm -rf /data/log/hilog/*; hilog -c' | Out-Null
  }
  if ($CLEAN_FAULTLOG) {
    Write-Host "Clean faultlog on device (DANGEROUS)"
    hdc -t $SN shell 'rm -rf /data/log/faultlog/*' | Out-Null
  }
  if ($CLEAN_FAULTMON -and $EnableFaultmon) {
    Write-Host "[faultmon] clean metrics/procs on device"
    try {
      $stopCmd = "$FaultmonScript stop 2>/dev/null"
      hdc -t $SN shell $stopCmd 2>&1 | Out-Null
    } catch {
      Write-Host "[faultmon] WARN: faultmon stop failed (ignored): $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
    try {
      # 婵炴挸鎳愰幃?metrics / procs / events闁挎稑鐭傛导鈺呭礂?events 濞戞挻褰冨┃鈧慨鍏夊墲閻?
      $rmCmd = "rm -f $FaultmonRoot/metrics/sys_*.csv $FaultmonRoot/procs/procs_*.txt $FaultmonRoot/events/events_*.jsonl 2>/dev/null"
      hdc -t $SN shell $rmCmd 2>&1 | Out-Null
    } catch {
      Write-Host "[faultmon] WARN: clean faultmon metrics/procs failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
  }
}

function Device-SetTime {
  param(
    [string]$SN,
    [string]$Ts
  )

  if (-not $Ts -or $Ts -eq "") {
    $dt = Get-Date
  } else {
    try { $dt = [datetime]::Parse($Ts) }
    catch {
      Write-Host "[time] WARN: failed to parse DEVICE_TIME, fallback to host time" -ForegroundColor Yellow
      $dt = Get-Date
    }
  }

  $dateYMD = $dt.ToString("yyyy-MM-dd")
  $timeHMS = $dt.ToString("HH:mm:ss")
  $tsPretty = "$dateYMD $timeHMS"

  # toybox/busybox 閻㈩垱鐡曢～鍡涘炊閻愯　鍋撻埀顒勫冀閻撳海纭€闁挎稒鐡塎DDhhmmYYYY.ss
  $fallback = $dt.ToString("MMddHHmmyyyy") + "." + $dt.ToString("ss")

  Write-Host ("Set device date -> {0}, time -> {1}" -f $dateYMD, $timeHMS)

  $cmd = "date -s `"$tsPretty`" 2>/dev/null || date $fallback 2>/dev/null || true; date 2>/dev/null || true; echo __TIME_OK__"

  $r = Invoke-HdcCapture -SN $SN -HdcArgs @("shell", $cmd) -TimeoutMs 15000 -Retry 1 -Tag "time.set"
  $all = ($r.stdout + $r.stderr)

  if ($all -notmatch "__TIME_OK__") {
    Write-Host "[time] WARN: time set did not return OK marker" -ForegroundColor DarkYellow
    if ($r.stderr) { Write-Host ("[time] stderr: " + ($r.stderr.Trim())) -ForegroundColor DarkYellow }
  } else {
    # 闁瑰灚鎸稿畵鍐冀閿熺姷宕ｉ弶鍫熸尭閸ゎ參鏁嶉崸濉e 闂侇叏缍€椤㈡垿鏁?
    if ($r.stdout) { Write-Host ($r.stdout.TrimEnd()) }
  }
}

function Get-BoardEpochMs {
  param([string]$SN)

  # 濞达綀娉曢弫?date +%s 闁兼儳鍢茶ぐ鍥级鐠侯煈浼?Unix 闁哄啫鐖煎Λ鍧楀箣缁涘湱绀勭紒澶嬪釜缁辨岸鏁嶇仦钘夋櫃閺夌儐鍓氬畷鍙夌▔閻戞﹩鍤戠紒?
  $cmd  = "date +%s"
  $HdcArgs = @("-t", $SN, "shell", $cmd)
  $out  = hdc @HdcArgs 2>&1 | Select-Object -Last 1

  # 1闁挎稑鈥渄c 閻犲鍟伴弫銈嗗緞鏉堫偉袝闁?out 闁烩晛鐡ㄧ敮鎾及?$null闁挎稑鐗婇惁顔戒繆閸屾碍鍤掑ù鐘€曠槐鎾舵暜閹肩偐鍋撴担绛嬪晭濠㈣泛娲ｆ径宥夊籍閼搁潧绔寸紒鎹愭硶閻℃垿鏁?
  if ($null -eq $out) {
    Write-Host "[time] WARN: failed to get board epoch (hdc returned null)" -ForegroundColor Yellow
    return $null
  }

  # 2闁挎稑顦板﹢浣规綇閹惧啿姣夐柨娑樺缁查箖宕楅妸锔叫︾紒宀冩濞?/ 闁瑰箍鍨奸、?
  $outTrim = $out.Trim()
  if ([string]::IsNullOrWhiteSpace($outTrim)) {
    Write-Host "[time] WARN: failed to parse board epoch from ''" -ForegroundColor Yellow
    return $null
  }

  # 3闁挎稑顦惃鍓ф嫚閺団寬鎺楀几閹邦厼鐏囬柡浣哥摠閺嗙喓绮?
  $sec = 0L
  if (-not [Int64]::TryParse($outTrim, [ref]$sec)) {
    Write-Host ("[time] WARN: failed to parse board epoch from '{0}'" -f $outTrim) -ForegroundColor Yellow
    return $null
  }

  # 閺夌儐鍓氶崹姘掗銈庢健閺夆晜鏌ㄥú?
  return ($sec * 1000)
}


# ----- hilog persistent writer -----
$global:HilogJobId = $null
function Start-HilogPersistent {
  param([string]$SN)
  if (-not $ENABLE_HILOG_PERSIST) { return }

  Write-Host "Start hilog persistent writer"
  hdc -t $SN shell 'mkdir -p /data/log/hilog' | Out-Null

  $out = hdc -t $SN shell 'hilog -w start'
  if ($null -eq $out -or [string]::IsNullOrWhiteSpace($out)) {
    Write-Host "[hilog] hilog -w start returned no output, skip jobid parsing" -ForegroundColor Yellow
    return
  }

  Write-Host $out
  $m = [regex]::Match([string]$out, 'jobid:(\d+)')
  if ($m.Success) {
    $global:HilogJobId = $m.Groups[1].Value
  } else {
    Write-Host "[hilog] could not parse jobid from output" -ForegroundColor Yellow
  }
}


function Stop-HilogPersistent {
  param([string]$SN)
  if (-not $ENABLE_HILOG_PERSIST) { return }
  if ($global:HilogJobId) {
    Write-Host ("Stop hilog persistent writer (jobid {0})" -f $global:HilogJobId)
    hdc -t $SN shell ("hilog -w stop " + $global:HilogJobId) | Out-Null
  } else {
    Write-Host "Stop hilog persistent writer (generic stop)"
    hdc -t $SN shell 'hilog -w stop' | Out-Null
  }
}

# ----- hilog host streaming (optional) -----
$global:HilogStreamProc = $null
function Start-HilogLiveCapture {
  param([string]$SN,[string]$OutPath)
  if (-not $ENABLE_HILOG_STREAM) { return }
  Write-Host "Start hilog live capture -> $OutPath"
  Ensure-Dir (Split-Path $OutPath -Parent)
  $global:HilogStreamProc = Start-Process -FilePath "hdc" `
    -ArgumentList @("-t", $SN, "shell", "hilog -v long") `
    -RedirectStandardOutput $OutPath -NoNewWindow -PassThru
  Start-Sleep -Milliseconds 300
}
function Stop-HilogLiveCapture {
  if ($global:HilogStreamProc -and -not $global:HilogStreamProc.HasExited) {
    try { $global:HilogStreamProc.Kill() } catch {}
  }
  if ($ENABLE_HILOG_STREAM) { Write-Host "Stop hilog live capture" }
}



# ----- dmesg dual-path & UTF-8 convert -----
function Save-Dmesg {
  param([string]$SN,[string]$OutHost,[string]$RunDir,[string]$Tag)
  Write-Host "dmesg($Tag) host redirect -> $OutHost"
  hdc -t $SN shell 'dmesg -T' > $OutHost
  $remoteLog = "/data/local/tmp/_probe_dmesg_${Tag}.log"
  $remoteErr = "/data/local/tmp/_probe_dmesg_${Tag}.err"
  $remoteRc  = "/data/local/tmp/_probe_dmesg_${Tag}.rc"
  hdc -t $SN shell "dmesg -T > $remoteLog 2> $remoteErr; echo RC:\$? > $remoteRc"
  $recvDir = Join-Path $RunDir "_probe_dmesg_recv"
  Ensure-Dir $recvDir
  hdc -t $SN file recv $remoteLog $recvDir | Out-Null
  hdc -t $SN file recv $remoteErr $recvDir | Out-Null
  hdc -t $SN file recv $remoteRc  $recvDir | Out-Null
}

function Convert-FileToUtf8 {
  param([string]$Src,[string]$Dst)
  if (-not (Test-Path $Src)) { return }
  $text = $null
  $encs = @("UTF8","Unicode","BigEndianUnicode")
  foreach ($e in $encs) {
    try {
      $text = Get-Content -LiteralPath $Src -Raw -Encoding $e
      if ($text -ne $null) { break }
    } catch {}
  }
  if ($text -eq $null) {
    $text = Get-Content -LiteralPath $Src -Raw -ErrorAction SilentlyContinue
  }
  if ($text -ne $null) {
    $dir = Split-Path $Dst -Parent
    Ensure-Dir $dir
    [System.IO.File]::WriteAllText($Dst, $text, [System.Text.Encoding]::UTF8)
  }
}

function Start-FaultmonIfNeeded {
    param(
        [string]$SN
    )

    if (-not $EnableFaultmon) { return }

    # 1) 閻忓繑绻嗛惁顖涚┍濠靛﹦妲堥柤瀛樼濠€浼村嫉婢跺鈷旈悶娑樻湰濞煎牓姊介幇鍓佺閻犱警鍨扮欢鐐碘偓闈涙贡濞堟垹鎷犲┑鎾剁閺夆晜鐟ょ粩鏉戭潰閵夛腹鍋撶紒妯恍﹂悗鐟邦槸閸欏繘鎯冮崟鍓佺
    try {
        $chmodCmd  = "chmod +x $FaultmonScript 2>/dev/null || true"
        $chmodHdcArgs = @("-t", $SN, "shell", $chmodCmd)
        hdc @chmodHdcArgs 2>&1 | Out-Null
        hdc -t $SN shell ("sed -i 's/\r$//' " + $NetFaultScriptRemote + " 2>/dev/null || true") 2>&1 | Out-Null
    } catch {
        # 闁告鍘栨繛?chmod 濠㈡儼绮剧憴锕傛晬鐏炶偐鐦嶉柛蹇撶墔缁楀鎯勭€涙ê澶嶇紒鍌欒兌閺併倝鏁嶇仦鑺ュ€甸梻?status/start 闁告劕绉撮崰鍛偓?
        Write-Host "[faultmon] WARN: chmod +x faultmon.sh failed (will still try status/start)" -ForegroundColor Yellow
    }

    # 2) 闁哄被鍎撮?daemon 闁绘鍩栭埀顑跨筏缁变即宕氶埡鍐╂殢濞达絿濮撮崹浼村绩閻熻埇鍋ㄩ柣?faultmon.sh 闁?status 閺夊牊鎸搁崵?
    $statusCmd  = "sh $FaultmonScript status 2>/dev/null"
    $statusHdcArgs = @("-t", $SN, "shell", $statusCmd)

    $statusOut = $null
    try {
        $statusOut = hdc @statusHdcArgs 2>&1
    } catch {
        $statusOut = $null
    }

    if ($statusOut -and ($statusOut -match "daemon:\s*running")) {
        Write-Host "[faultmon] daemon already running on device, skip start." -ForegroundColor DarkGreen
        return
    }

    # 濠碘€冲€归悘澶愭嚇濮橆厽鎷遍柣顏嗗枔濞堟垶绋夊鍛憼闁革富鐓夌槐婵囧濮橆剙姣夐柣婊勫鐞氼偅瀵?"No such file" / "can't open" 濞戞柨顑囩悮顐︽儍閸曨垱鏅╅悹?
    if ($statusOut -and (
            $statusOut -match "No such file" -or
            $statusOut -match "can't open"   -or
            $statusOut -match "not found"
        )) {
        Write-Host "[faultmon] WARN: faultmon.sh not found on device, skip starting (no metrics for this run)." -ForegroundColor Yellow
        # 婵炲鍔嶉崜浼存晬濮橆偆鐟濋柛鎰С閹便劑寮?$EnableFaultmon闁挎稑鐭傛导鈺呭礂瀹ヤ讲鍋撳鍡╁殩濞寸鍊曢幃妤冪磼?run闁?
        return
    }

    # 3) daemon 闁哄牜浜滃﹢顏嗘崉?闁?闁告凹鍨版慨鈺傜▔閳ь剚绂掗懞銉︾厐闁?
    Write-Host "[faultmon] starting daemon (HILOG_ENABLE=0)..." -ForegroundColor Cyan

    # 闁?HILOG_ENABLE=0 + nohup 闁告艾楠歌ぐ鎾触椤栨艾袟闁挎稑鐭傚Σ璇差潰?hdc 閻炴凹鍋勯悾褔骞庨妶鍫㈢缂佸顑囩划锕€顫?
    $startCmd  = "HILOG_ENABLE=0 nohup sh $FaultmonScript start >/dev/null 2>&1 & echo started"
    $startHdcArgs = @("-t", $SN, "shell", $startCmd)

    $startOut = $null
    try {
        $startOut = hdc @startHdcArgs 2>&1 | Select-Object -Last 1
    } catch {
        $startOut = $null
    }

    if ($startOut) {
        Write-Host ("[faultmon] {0}" -f $startOut.ToString().Trim())
    } else {
        Write-Host "[faultmon] start command sent (background)." -ForegroundColor DarkGreen
    }
}

function Collect-FaultmonFiles {
    param(
        [string]$SN,
        [string]$RunDir
    )

    if (-not $EnableFaultmon) { return }

    # ---------- 闁告帗绻傞～鎰板礌?_run_meta.json 闁活潿鍔庡▓鎴﹀礂閵娿儳婀伴柛娆愶耿閸?----------
    $script:FaultmonBoardDate    = $null
    $script:FaultmonMetricsLocal = $null
    $script:FaultmonEventsLocal  = $null
    $script:FaultmonHasMetrics   = $false
    $script:FaultmonHasEvents    = $false

    # ---------- 1. 闁兼儳鍢茶ぐ鍥级閸喚鎽嶉柡鍐﹀劜濠€锟犳晬閸ф妺ultmon 濞戞梻鍠愬Σ鎼佸箰婢跺海绠瑰☉鎿冧簻瑜板洭宕?sys_YYYYMMDD.csv闁?----------
    $boardDate = $null
    try {
        $HdcArgs = @("-t", $SN, "shell", "date +%Y%m%d")
        $out  = hdc @HdcArgs 2>&1 | Select-Object -Last 1
        if ($out) {
            $boardDate = $out.ToString().Trim()
        }
    } catch {
        $boardDate = $null
    }

    if (-not $boardDate) {
        # hdc 鐎殿喖鍊搁悥鍫曞籍閸洍鍋撻埀顒勫炊閻愬樊鍟忓☉鎾剁帛濠р偓闁哄啫鐖煎Λ鍧楁晬閸粎顏遍柤鍓插墮婢х娀妫冮姀鐘插殥 date -s 闁告艾鏈鐐存交閸ラ绀?
        $boardDate = (Get-Date -Format "yyyyMMdd")
    }
    $script:FaultmonBoardDate = $boardDate

    # 閻犱焦鍎抽ˇ顒傜博椤栨繄鐔呯€?
    $remoteMetricsRoot = "$FaultmonRoot/metrics"
    $remoteEventsRoot  = "$FaultmonRoot/events"
    $remoteProcsRoot   = "$FaultmonRoot/procs"

    $remoteMetrics = "$remoteMetricsRoot/sys_${boardDate}.csv"
    $remoteEvents  = "$remoteEventsRoot/events_${boardDate}.jsonl"

    # 闁哄牜鍓欏﹢瀵告崉椤栨氨绐?
    $localMetricsDir = Join-Path $RunDir "metrics"
    $localEventsDir  = Join-Path $RunDir "events"
    $localProcsDir   = Join-Path $RunDir "procs"

    New-Item -ItemType Directory -Path $localMetricsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $localEventsDir  -Force | Out-Null
    New-Item -ItemType Directory -Path $localProcsDir   -Force | Out-Null

    Write-Host "[faultmon] pulling metrics/events/procs for $boardDate ..." -ForegroundColor Cyan

    # ---------- 2. metrics闁挎稒鑹鹃悺銊╁捶閵婏腹鍋撹椤ュ懎霉?+ 缂佺姭鍋撻柡鍕崌閸ｅ摜鎷犻弴顏嗙闂侇剙鐏濋崢?-and闁?----------
    $localMetrics = Join-Path $localMetricsDir ("sys_{0}.csv" -f $boardDate)
    $metricsOk    = $false
    $maxAttempts  = 5
    $sleepSeconds = 3

    for ($i = 0; $i -lt $maxAttempts; $i++) {

        # 2.1 闂傚偆鍠涢鏇熷緞閸ワ妇鐟愰弶鈺傜懁缁斿瓨寰勯埡鍐╃暠 sys_xxx.csv 闁革负鍔嬬粭澶愬捶?
        $checkCmd = "if [ -f '$remoteMetrics' ]; then echo EXISTS; else echo MISSING; fi"
        $HdcArgs     = @("-t", $SN, "shell", $checkCmd)

        $flag = ""
        try {
            $out = hdc @HdcArgs 2>&1 | Select-Object -Last 1
            if ($out) {
                $flag = $out.ToString().Trim()
            }
        } catch {
            $flag = ""
        }

        if ($flag -eq "EXISTS") {
            # 2.2 闁活亞鍠愰婊堝箯?metrics 闁哄倸娲ｅ▎?
            try {
                $HdcArgs = @("-t", $SN, "file", "recv", $remoteMetrics, $localMetrics)
                hdc @HdcArgs 2>&1 | Out-Null

                if (Test-Path $localMetrics) {
                    $len = (Get-Item $localMetrics).Length
                    if ($len -gt 0) {
                        $metricsOk = $true
                        $script:FaultmonMetricsLocal = $localMetrics
                        $script:FaultmonHasMetrics   = $true
                        Write-Host "[faultmon] metrics csv collected: $localMetrics" -ForegroundColor DarkGreen
                    }
                }
            } catch {
                # 闁告瑯浜滃﹢顏堝嫉閳ь剟宕ユ惔婵堫伇婵炲棴绻濋崳鍝ユ嫚閺囩偑浜奸悹鎰╁劜濡炲倿骞嶉幘鍐茬オ鐎殿喖鍊搁悥?
                if ($i -eq ($maxAttempts - 1)) {
                    Write-Host "[faultmon] exception when pulling metrics: $($_.Exception.Message)" -ForegroundColor DarkYellow
                }
            }
        } else {
            Write-Host "[faultmon] metrics not ready on device ($remoteMetrics), attempt $($i+1)/$maxAttempts, flag='$flag'" -ForegroundColor DarkYellow
        }

        if ($metricsOk) {
            break
        }

        if ($i -lt ($maxAttempts - 1)) {
            Start-Sleep -Seconds $sleepSeconds
        }
    }

    if (-not $metricsOk) {
        Write-Host "[faultmon] no metrics csv on device ($remoteMetrics)" -ForegroundColor DarkYellow
    }

    # ---------- 3. events闁挎稒姘ㄩ悾婵嬪础閺囩喎顎欓柛娆愮墧缁旀潙鈻庨垾鍐茬ギ闁?----------
    $localEvents = Join-Path $localEventsDir ("events_{0}.jsonl" -f $boardDate)
    try {
        $HdcArgs = @("-t", $SN, "file", "recv", $remoteEvents, $localEvents)
        hdc @HdcArgs 2>&1 | Out-Null
        if (Test-Path $localEvents) {
            $script:FaultmonEventsLocal = $localEvents
            $script:FaultmonHasEvents   = $true
        } else {
            Write-Host "[faultmon] events file not found after recv ($remoteEvents)" -ForegroundColor DarkYellow
        }
    } catch {
        Write-Host "[faultmon] no events jsonl on device ($remoteEvents)" -ForegroundColor DarkYellow
    }

    # ---------- 4. procs闁挎稒宀告禍鍫曞储?/data/faultmon/procs 濞戞挸顑嗘晶宥夊嫉?procs_*.txt 闂侇偅鍔掗柌?recv ----------
    try {
        Write-Host "[faultmon] pulling process snapshots (procs_*.txt) ..." -ForegroundColor Cyan

        # 闁稿繐鐗嗗﹢顏嗘媼閹屾У濞撴皜鍐ㄦ櫢闁稿繈鍎扮粩瀛樼閽樺绉奸柛鎾崇У濡炲倿宕氶懡銈嗙暠閺夆晜绋撻埢鑹扮疀椤愩倕寮鹃柨娑樼焸娴尖晠宕?procs 闁烩晩鍠栫紞宥嗙▔閾忓厜鏁?
        try {
            $dumpCmd  = "mkdir -p $remoteProcsRoot; ps -A > $remoteProcsRoot/procs_`$(date +%Y%m%d_%H%M%S).txt"
            $dumpHdcArgs = @("-t", $SN, "shell", $dumpCmd)
            hdc @dumpHdcArgs 2>&1 | Out-Null
        } catch {
            Write-Host "[faultmon] WARN: failed to dump live procs snapshot on device (ps -A)" -ForegroundColor DarkYellow
        }

        # 闁活潿鍔岄崙锟犲嫉婢跺本鐣?Get-RemoteFileList 闁哄鐭俊鍥ㄦ交濠婂拋浼?procs 闁烩晩鍠栫紞?
        $procsRelList = Get-RemoteFileList -SN $SN -Root $remoteProcsRoot
        $copied = 0

        foreach ($rel in $procsRelList) {
            if (-not $rel) { continue }
            if (-not ($rel -like "procs_*.txt")) { continue }

            $remote = "$remoteProcsRoot/$rel"
            $local  = Join-Path $localProcsDir $rel

            $localDir = Split-Path $local -Parent
            if ($localDir) {
                Ensure-Dir $localDir
            }

            try {
                hdc -t $SN file recv $remote $local 2>&1 | Out-Null
                if (Test-Path $local) { $copied++ }
            } catch {
                Write-Host "[faultmon] WARN: failed to pull $remote : $($_.Exception.Message)" -ForegroundColor DarkYellow
            }
        }

        if ($copied -gt 0) {
            Write-Host "[faultmon] process snapshots collected: $copied file(s)" -ForegroundColor DarkGreen
        } else {
            Write-Host "[faultmon] no process snapshot files found on device ($remoteProcsRoot)" -ForegroundColor DarkYellow
        }
    } catch {
        Write-Host "[faultmon] exception when pulling procs: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
}

function Ensure-NetFaultScript {
  param([string]$SN)

  if (-not $NetFaultScriptLocal -or -not (Test-Path $NetFaultScriptLocal)) {
    Write-Host "[net] net_fault.sh not found beside ps1; skip pushing." -ForegroundColor DarkYellow
    return
  }

  Write-Host ("[net] push net_fault.sh -> {0} (from {1})" -f $NetFaultScriptRemote, $NetFaultScriptLocal) -ForegroundColor Cyan

  $localSize = (Get-Item $NetFaultScriptLocal).Length
  $remoteSize = 0

  $r = Invoke-HdcCapture -SN $SN -HdcArgs @("shell", "ls -l $NetFaultScriptRemote 2>/dev/null | head -n 1 || true")
  $remoteLine = ($r.stdout).Trim()
  if ($remoteLine -match "^\-") {
    $tok = $remoteLine -split "\s+"
    if ($tok.Count -ge 5) {
      [int64]$remoteSize = $tok[4]
    }
  }

  if ($remoteSize -eq $localSize -and $remoteSize -gt 0) {
    Write-Host ("[net] net_fault already present (size={0}); skip send" -f $remoteSize) -ForegroundColor DarkCyan
  } else {
    Write-Host ("[net] net_fault will be sent (local={0} remote={1})" -f $localSize, $remoteSize) -ForegroundColor Cyan
    & hdc -t $SN file send $NetFaultScriptLocal $NetFaultScriptRemote 2>&1 | Out-Null
  }

  & hdc -t $SN shell ("chmod +x " + $NetFaultScriptRemote + " 2>/dev/null || true") 2>&1 | Out-Null

  Invoke-HdcCapture -SN $SN -HdcArgs @("shell", "ls -l $NetFaultScriptRemote 2>/dev/null || true") -Tag "net.sanity" | Out-Null
}




function Collect-RemoteFaultLog {
  param(
    [string]$SN,
    [string]$RunDir,
    [string]$FaultType
  )
  if (-not $FaultType -or $FaultType -eq "" -or $FaultType -eq "none") { return }

  $remote = ("/data/local/tmp/fault_{0}.log" -f $FaultType)
  $outDir = Join-Path $RunDir "fault_inject"
  Ensure-Dir $outDir
  $local  = Join-Path $outDir ("fault_{0}.log" -f $FaultType)

  try {
    hdc -t $SN file recv $remote $local 2>&1 | Out-Null
  } catch {
    # ignore
  }
}
function Append-RemoteOut {
  param(
    [string]$SN,
    [string]$RemoteCmd,
    [string]$OutPath,
    [string]$Tag = "remote"
  )

  try {
    $r = Invoke-HdcCapture -SN $SN -HdcArgs @("shell", $RemoteCmd) -TimeoutMs 25000 -Retry 1 -Tag $Tag
    $out = ""
    if ($r.stdout) { $out += $r.stdout }
    if ($r.stderr) { $out += $r.stderr }

    if ($out -and $out.Trim() -ne "") {
      Add-Content -Path $OutPath -Value $out -Encoding UTF8
    }
  } catch {
    Add-Content -Path $OutPath -Value ("[ERR] " + $Tag + " : " + $_.Exception.Message) -Encoding UTF8
  }
}

function Convert-RouteGatewayHexToIpv4ForProbe {
  param([string]$Hex)
  if ([string]::IsNullOrWhiteSpace($Hex) -or $Hex -notmatch '^[0-9A-Fa-f]{8}$') { return "" }
  return ("{0}.{1}.{2}.{3}" -f
    ([Convert]::ToInt32($Hex.Substring(6,2),16)),
    ([Convert]::ToInt32($Hex.Substring(4,2),16)),
    ([Convert]::ToInt32($Hex.Substring(2,2),16)),
    ([Convert]::ToInt32($Hex.Substring(0,2),16)))
}

function Get-DefaultGatewayFromProcRouteText {
  param(
    [string]$Text,
    [string]$Iface
  )
  if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
  $wantIface = if ([string]::IsNullOrWhiteSpace($Iface)) { "" } else { $Iface.Trim() }
  foreach ($line in ($Text -split "`r?`n")) {
    $trim = $line.Trim()
    if (-not $trim -or $trim -match '^Iface\s+') { continue }
    $parts = $trim -split '\s+'
    if ($parts.Count -lt 3) { continue }
    if ($wantIface -and $parts[0] -ne $wantIface) { continue }
    if ($parts[1] -ne "00000000") { continue }
    return (Convert-RouteGatewayHexToIpv4ForProbe $parts[2])
  }
  return ""
}

function Append-GatewayPingProbe {
  param(
    [string]$SN,
    [string]$OutPath,
    [string]$Iface,
    [int]$Count = 1,
    [string]$Tag = "net.gateway.ping"
  )
  $safeIface = if ($Iface -and $Iface -match '^[A-Za-z0-9_.:-]+$') { $Iface } else { "wlan0" }
  Add-Content -Path $OutPath -Value "### ping_gateway" -Encoding UTF8
  $gw = ""
  try {
    $route = Invoke-HdcCapture -SN $SN -HdcArgs @("shell", "cat /proc/net/route 2>/dev/null || true") -TimeoutMs 12000 -Retry 1 -Tag "$Tag.route"
    $routeText = ""
    if ($route.stdout) { $routeText += $route.stdout }
    if ($route.stderr) { $routeText += $route.stderr }
    $gw = Get-DefaultGatewayFromProcRouteText -Text $routeText -Iface $safeIface
  } catch {
    Add-Content -Path $OutPath -Value ("[ERR] " + $Tag + ".route : " + $_.Exception.Message) -Encoding UTF8
  }

  Add-Content -Path $OutPath -Value ("gateway={0}" -f $gw) -Encoding UTF8
  if ($gw -match '^\d{1,3}(\.\d{1,3}){3}$' -and $gw -ne "0.0.0.0") {
    $cnt = if ($Count -ge 1 -and $Count -le 5) { $Count } else { 1 }
    Append-RemoteOut $SN ("ping -c {0} -W 2 {1} 2>&1 || true" -f $cnt, $gw) $OutPath $Tag
  } else {
    Add-Content -Path $OutPath -Value "NO_GATEWAY" -Encoding UTF8
  }
}

function Collect-WrongDefaultRouteLiveEvidence {
  param(
    [string]$SN,
    [string]$RunDir,
    [string]$Phase,
    [string]$Iface,
    [string]$When = "live",
    [string]$ReadyFile = "/data/local/tmp/net_fault_state/wrong_route.ready",
    [string]$MarkerPrefix = "WRONG_ROUTE"
  )

  $outDir = Join-Path $RunDir "net"
  Ensure-Dir $outDir
  $probePath = Join-Path $outDir ("probe_{0}.txt" -f $Phase)
  $wdrIface = if (-not [string]::IsNullOrWhiteSpace($Iface)) { $Iface } else { "wlan0" }
  if ($wdrIface -notmatch '^[A-Za-z0-9_.:-]+$') { $wdrIface = "wlan0" }

  Append-RemoteOut $SN ("echo '### {0}_LIVE_ROUTE_BEGIN when={1}'; date" -f $MarkerPrefix, $When) $probePath "net.wdr.live.begin"
  Append-RemoteOut $SN ("echo '### {0}.ready'; cat {1} 2>/dev/null || echo MISSING_READY" -f $MarkerPrefix.ToLowerInvariant(), $ReadyFile) $probePath "net.wdr.ready"
  Append-RemoteOut $SN "echo '### /proc/net/route'; cat /proc/net/route 2>/dev/null || true" $probePath "net.wdr.route.raw"

  $verifyCmd = 'WDR_IFACE=' + $wdrIface + '; echo "### ' + $MarkerPrefix + '_LIVE_ROUTE_VERIFY"; WDR_READY=' + $ReadyFile + '; WDR_FAKE_HEX=$(sed -n "s/^fake_gw_hex=//p" "$WDR_READY" 2>/dev/null | head -n 1); [ -z "$WDR_FAKE_HEX" ] && WDR_FAKE_HEX=UNKNOWN; WDR_DEF=0; WDR_FAKE=0; WDR_SUBNET=0; WDR_GW_HEX=; WDR_TMP=/data/local/tmp/.wdr_live_route_$$; sed "1d" /proc/net/route 2>/dev/null > "$WDR_TMP" 2>/dev/null || true; while IFS= read -r WDR_LINE; do [ -z "$WDR_LINE" ] && continue; set -- $WDR_LINE; [ "$1" = "$WDR_IFACE" ] || continue; if [ "$2" = "00000000" ]; then WDR_DEF=1; WDR_GW_HEX="$3"; [ "$3" = "$WDR_FAKE_HEX" ] && WDR_FAKE=1; echo "WDR_DEFAULT_ROUTE iface=$1 dest=$2 gw_hex=$3 fake_gw_hex=$WDR_FAKE_HEX fake_exact=$WDR_FAKE"; else WDR_SUBNET=1; echo "WDR_SUBNET_ROUTE iface=$1 dest=$2 gw_hex=$3 mask=$8"; fi; done < "$WDR_TMP"; rm -f "$WDR_TMP" 2>/dev/null || true; echo "WDR_LIVE_ROUTE_SUMMARY iface=$WDR_IFACE default=$WDR_DEF fake_default=$WDR_FAKE subnet=$WDR_SUBNET gw_hex=$WDR_GW_HEX fake_gw_hex=$WDR_FAKE_HEX"; if [ "$WDR_DEF" = "1" ] && [ "$WDR_FAKE" = "1" ] && [ "$WDR_SUBNET" = "1" ]; then echo "WDR_LIVE_ROUTE_OK=1"; else echo "WDR_LIVE_ROUTE_OK=0"; echo "WDR_LIVE_ROUTE_WARN=missing_fake_default_or_subnet"; fi'
  Append-RemoteOut $SN $verifyCmd $probePath "net.wdr.route.verify"
  Append-RemoteOut $SN ("echo '### {0}_LIVE_ROUTE_END'" -f $MarkerPrefix) $probePath "net.wdr.live.end"
}


function Detect-ActiveNetIface {
  param(
    [string]$SN,
    [string]$Fallback = "wlan0"
  )

  try {
    $r = Invoke-HdcCapture -SN $SN -HdcArgs @("shell",
      "for n in eth1 eth0 wlan0; do if [ -d /sys/class/net/$n ]; then s=$(cat /sys/class/net/$n/operstate 2>/dev/null); c=$(cat /sys/class/net/$n/carrier 2>/dev/null); echo $n,$s,$c; fi; done"
    ) -TimeoutMs 12000 -Retry 1 -Tag "net.iface.pick"

    $out = $r.stdout
    if (-not $out) { return $Fallback }

    foreach ($ln in ($out -split "`n")) {
      $t = $ln.Trim()
      if (-not $t) { continue }
      $parts = $t.Split(",")
      if ($parts.Count -ge 3) {
        $name  = $parts[0]
        $state = $parts[1]
        $car   = $parts[2]
        if ($state -eq "up" -or $car -eq "1") { return $name }
      }
    }

    return $Fallback
  } catch {
    return $Fallback
  }
}


function Collect-NetSnapshot {
  param(
    [string]$SN,
    [string]$RunDir,
    [string]$Phase,
    [string]$Iface
  )
  if (-not $ENABLE_NET_SNAPSHOT) { return }

  $outDir = Join-Path $RunDir "net"
  Ensure-Dir $outDir

  $snapPath  = Join-Path $outDir ("net_{0}.txt" -f $Phase)
  $probePath = Join-Path $outDir ("probe_{0}.txt" -f $Phase)

  # =========================================================
  # IMPORTANT:
  # 濞戞挸绉烽々锕傚箮婵犲啫顣查柡鍫濐槸閹斥剝绂掗妶鍡楊伝闁瑰瓨鍔掔粩鎾级闄囩粔鎾⒐?`hdc shell "<...;...;...>"`闁?
  # 1) 閻犱焦鍎抽ˇ顒佺瑹?/bin/sh 闁告瑯鍨甸崗姗€宕堕悩宕囧帣闁告瑣鍎撮銏犫枖閺囥垺鏅╅悹鍥跺灥閳ь剙濂旈懙鎴﹀棘椤撶偞鍊电紓渚囧幗婢х晫鎮板畝瀣耿
  # 2) hdc 闁告瑯鍨甸崗妯尖偓鐢殿攰缁夋挳姊归崹顔藉殥濞寸姰鍊撶憰鍡涘箣椤忓懏鐒介柨娑樿嫰椤曢亶鎳涢弶鎴炲€甸柛妤€锕ラ宀勫冀鐟欏嫭鎷辨繛灞勩値娼堕柟绗涘棭鏀介柕?
  # 闁衡偓闁稖绀嬮柛鎺戞椤斿矂骞嶈椤?+ 闂侇偅鍔栭灞炬交閽樺顫ｉ柛鎺斿閺嬪啯绂掔拋鍦濞ｅ洦绻嗛惁澶愬矗椤栨繍娼庢繛鏉戭儍閳ь兛绀佽ぐ鑼偓瑙勭煯缂嶅懘濡?
  # =========================================================

  # --- snapshot (segmented) ---
  "" | Out-File -FilePath $snapPath -Encoding UTF8 -Force

  $parts = @("echo '### NET_SNAPSHOT_MARK=20251223_sysfs_v1'",

    ("echo '### phase={0}'; date" -f $Phase),
    "echo '### ifconfig -a'; ifconfig -a 2>/dev/null || true",
    ("echo ""### ifconfig {0}""; ifconfig {0} 2>/dev/null || true" -f $Iface),
    'echo ''### link_state_sysfs''; for i in /sys/class/net/*; do n=${i##*/}; echo "-- $n"; cat "$i/operstate" 2>/dev/null || true; cat "$i/carrier" 2>/dev/null || echo "NA"; done; echo "__END__"',
'echo ''### resolv_conf_candidates''; for p in /etc/resolv.conf /system/etc/resolv.conf /data/service/el1/public/netmanager/resolv.conf; do echo "-- $p"; if [ -e "$p" ]; then ls -l "$p" 2>/dev/null || echo LS_FAIL; cat "$p" 2>/dev/null || echo CAT_FAIL; else echo MISSING; fi; done; echo "__END__"',
    'echo ''### mounts_etc_data''; cat /proc/mounts 2>/dev/null | grep " /etc " 2>/dev/null || true; cat /proc/mounts 2>/dev/null | grep " /data " 2>/dev/null || true; echo "__END__"',
    "echo '### iptables -L -n -v'; iptables -L -n -v 2>/dev/null || true",
    "echo '### hidumper -l | grep -i net'; hidumper -l 2>/dev/null | grep -i net 2>/dev/null || true",
    ("echo '### wpa_cli status ({0})'; command -v wpa_cli >/dev/null 2>&1 && wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} status 2>/dev/null || true" -f $NET_WLAN_IFACE),
"echo '### netstat -an'; netstat -an 2>/dev/null | head -n 400 || true",
"echo '### netstat -s';  netstat -s  2>/dev/null | head -n 400 || true",
    "echo '### netstat -s';  netstat -s  2>/dev/null || true",
    "echo '### /proc/net/dev';      cat /proc/net/dev      2>/dev/null || true",
    "echo '### /proc/net/route';    cat /proc/net/route    2>/dev/null || true",
    "echo '### /proc/net/arp';      cat /proc/net/arp      2>/dev/null || true",
    "echo '### /proc/net/snmp';     cat /proc/net/snmp     2>/dev/null || true",
    "echo '### /proc/net/netstat';  cat /proc/net/netstat  2>/dev/null || true",
    "echo '### /proc/net/if_inet6'; cat /proc/net/if_inet6 2>/dev/null || true",
    "echo '### /proc/net/tcp (head)';  head -n 60 /proc/net/tcp 2>/dev/null || true",
    "echo '### /proc/net/tcp (tail)';  tail -n 60 /proc/net/tcp 2>/dev/null || true",
    "echo '### /proc/net/udp (head)';  head -n 60 /proc/net/udp 2>/dev/null || true",
    "echo '### /proc/net/udp (tail)';  tail -n 60 /proc/net/udp 2>/dev/null || true",
    "echo '### /proc/net/tcp6 (head)';  head -n 60 /proc/net/tcp6 2>/dev/null || true",
    "echo '### /proc/net/tcp6 (tail)';  tail -n 60 /proc/net/tcp6 2>/dev/null || true",
    "echo '### /proc/net/udp6 (head)';  head -n 60 /proc/net/udp6 2>/dev/null || true",
    "echo '### /proc/net/udp6 (tail)';  tail -n 60 /proc/net/udp6 2>/dev/null || true",
    "echo '### /proc/net/unix (head)'; head -n 80 /proc/net/unix 2>/dev/null || true",
    "echo '### /proc/net/unix (tail)'; tail -n 80 /proc/net/unix 2>/dev/null || true",
    'echo "### conntrack_dns (dport=53/853)"; for f in /proc/net/nf_conntrack /proc/net/ip_conntrack; do echo "-- $f"; if [ -f "$f" ]; then tail -n 2000 "$f" 2>/dev/null | grep -E "dport=53|dport=853" 2>/dev/null | head -n 120 || true; else echo MISSING; fi; done',
    "echo '### arp_table'; cat /proc/net/arp 2>/dev/null || true"
  )

  if ($FAULT_INJECT_TYPE -eq "net_dns_fail") {
    $parts += "echo '### /proc/net/route_resample_dns_fail'; sleep 1; cat /proc/net/route 2>/dev/null || true"
  }

  foreach ($p in $parts) {
    Append-RemoteOut $SN $p $snapPath
  }

  # --- probe (segmented) ---
  $preserveWrongRouteProbe = ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable") -and $Phase -eq "fault" -and (Test-PathSafe $probePath))
  if ($preserveWrongRouteProbe) {
    Add-Content -Path $probePath -Value "" -Encoding UTF8
    Add-Content -Path $probePath -Value "### NET_PROBE_STANDARD_BEGIN" -Encoding UTF8
  } else {
    "" | Out-File -FilePath $probePath -Encoding UTF8 -Force
  }
  $rid = Split-Path $RunDir -Leaf

  $probes = @(
    ("echo '### phase={0}'; date" -f $Phase),
    ("echo '### ping -c 3 {0}'; ping -c 3 {0} 2>&1 || true" -f $NET_PING_IP)
  )

  if ($FAULT_INJECT_TYPE -eq "net_dns_fail" -and $Phase -eq "fault") {
    $probes += @(
    'echo \#\#\# DNS_PROOF_BEGIN',

    'echo \#\#\# marker',
    'ls -l /data/local/tmp/net_fault_state/iptables_dns_block.applied 2>/dev/null || echo NO_MARKER',
    "echo NET_BAD_DNS=$NET_BAD_DNS",
    "echo NET_PING_IP=$NET_PING_IP",

    'echo \#\#\# resolv_conf_etc',
    'ls -l /etc/resolv.conf 2>/dev/null || echo MISSING',
    'grep -n nameserver /etc/resolv.conf 2>/dev/null || echo NO_NAMESERVER',
    'echo \#\#\# resolv_conf_system',
    'ls -l /system/etc/resolv.conf 2>/dev/null || echo MISSING',
    'grep -n nameserver /system/etc/resolv.conf 2>/dev/null || echo NO_NAMESERVER',
    'echo \#\#\# resolv_conf_netmanager',
    'ls -l /data/service/el1/public/netmanager/resolv.conf 2>/dev/null || echo MISSING',
    'grep -n nameserver /data/service/el1/public/netmanager/resolv.conf 2>/dev/null || echo NO_NAMESERVER',

    'echo \#\#\# iptables_OUTPUT_before',
    "iptables -L OUTPUT -n -v 2>/dev/null | head -n 40 || true",

    'echo \#\#\# dns_host_probe_primary',
    ("ping -c 1 -W 1 {0} 2>&1 || true" -f $NET_DNS_HOST),
    'echo \#\#\# dns_host_probe_secondary_nip',
    ("ping -c 1 -W 1 t`$$.{0}.nip.io 2>&1 || true" -f $NET_PING_IP),

    'echo \#\#\# iptables_OUTPUT_after',
    "iptables -L OUTPUT -n -v 2>/dev/null | head -n 40 || true",

    'echo \#\#\# DNS_PROOF_END'
  )

}

if ($FAULT_INJECT_TYPE -eq "net_public_ip_unreachable") {
  $probes += @(
    ("echo '### ping_target_ip'; ping -c 3 {0} 2>&1 || true" -f $NET_TARGET_IP)
  )
  if ($Phase -eq "fault") {
    $probes += @(
      'echo \#\#\# target_block_marker',
      'ls -l /data/local/tmp/net_fault_state/iptables_target_block.applied 2>/dev/null || echo NO_MARKER',
      'cat /data/local/tmp/net_fault_state/iptables_target_block.applied 2>/dev/null || true'
    )
  }
}

# 闁?濞ｅ浂鍠栭ˇ鏌ユ倷?闁挎稒淇虹换鏍ㄧ▔椤忎讲鍋撳鍫氬亾濮樿鲸鏆?net_* 閺夌偛顭烽崳娲箳閵忋倖瀚涢柍銉︾箓缁烩偓濡炪倗绮弬渚€宕?net_dns_fail if 濠㈣埖鐗犲浼存晬鐏炶姤鍎婇柛鎺撶懇閳ь剚妲掔欢顐ょ驳婢跺鑹炬慨婵婎唺閸烆剟鎯?
if ($FAULT_INJECT_TYPE -eq "net_gateway_unreachable") {
  $gwPingCount = if ($Phase -in @("post","post2")) { 1 } else { 3 }
  Append-GatewayPingProbe -SN $SN -OutPath $probePath -Iface $NET_WLAN_IFACE -Count $gwPingCount -Tag "net.gateway_unreachable.gateway.ping"
}

if ($FAULT_INJECT_TYPE -eq "net_wrong_default_route" -and $Phase -in @("post","post2")) {
  Append-GatewayPingProbe -SN $SN -OutPath $probePath -Iface $NET_WLAN_IFACE -Count 1 -Tag "net.wrong_default.gateway.ping"
}

if ($FAULT_INJECT_TYPE -like "net_*" -and -not ($FAULT_INJECT_TYPE -eq "net_dns_fail" -and $Phase -eq "fault")) {
  $probes += @(
    ("echo '### dns_host_probe_primary'; ping -c 1 -W 1 {0} 2>&1 || true" -f $NET_DNS_HOST),
    ("echo '### ping_nip_io'; h=""t`$$.{0}.nip.io""; echo ""host=`$h""; RES_OPTIONS='attempts:1 timeout:1' ping -c 1 -W 1 ""`$h"" 2>&1 || true" -f $NET_PING_IP)
  )
}
# wlan-specific extra probes for new wifi fault types
if ($FAULT_INJECT_TYPE -in @("net_dns_fail","net_wifi_disconnect","net_wifi_auth_fail_wrong_psk","net_no_default_route","net_no_ipv4_on_iface","net_wrong_default_route","net_gateway_unreachable","net_public_ip_unreachable")) {
  $probes += @(
    ("echo '### wpa_cli_probe'; wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} status 2>/dev/null || echo WPA_CLI_FAILED" -f $NET_WLAN_IFACE),
    ("echo '### ifconfig_wlan'; ifconfig {0} 2>/dev/null || true" -f $NET_WLAN_IFACE),
    "echo '### proc_net_route'; cat /proc/net/route 2>/dev/null || true"
  )
  if ($FAULT_INJECT_TYPE -eq "net_dns_fail") {
    $probes += "echo '### proc_net_route_resample_dns_fail'; sleep 1; cat /proc/net/route 2>/dev/null || true"
  }
}


  # IPv6 閺夆晝鍋ら埀顒佺閳ь儸宥囩濞?IP闁挎稑濂旂粭澶屾喆閿曗偓瑜?DNS闁?
  $probes += @(
   'echo "### ping6 -c 3 240c::6666"; command -v ping6 >/dev/null 2>&1; rc=$?; [ $rc -ne 0 ] && { echo "no ping6"; } || { ping6 -c 3 240c::6666 2>&1 || true; }'
  )

  foreach ($p in $probes) {
    Append-RemoteOut $SN $p $probePath
  }

  if ($FAULT_INJECT_TYPE -eq "net_public_ip_unreachable") {
    $publicIpGwPingCount = if ($Phase -in @("post","post2")) { 1 } else { 3 }
    Append-GatewayPingProbe -SN $SN -OutPath $probePath -Iface $NET_WLAN_IFACE -Count $publicIpGwPingCount -Tag "net.public_ip.gateway.ping"
  }

  if ($FAULT_INJECT_TYPE -eq "net_dns_fail" -and $Phase -eq "fault" -and (Test-PathSafe $probePath)) {
    $probeText = Get-Content -LiteralPath $probePath -Raw -ErrorAction SilentlyContinue
    if ($probeText -match "DNS_PROOF_BEGIN" -and $probeText -notmatch "DNS_PROOF_END") {
      Add-Content -Path $probePath -Value "### DNS_PROOF_END" -Encoding UTF8
      Add-Content -Path $probePath -Value "### DNS_PROOF_END_LOCAL_BACKFILL reason=remote_marker_missing" -Encoding UTF8
    }
  }

}

function Parse-LocalNetSnapshotFile {
  param([string]$Path)

  $ret = [ordered]@{
    exists = $false
    phase = $null
    iface_states = @{}
    wpa_status = $null
  }
  if (-not (Test-PathSafe $Path)) { return [pscustomobject]$ret }

  $ret.exists = $true
  $lines = @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)
  $inLinkState = $false
  $inWpa = $false
  $currentIface = $null
  $step = 0

  foreach ($line in $lines) {
    if (-not $ret.phase -and $line -match '^###\s+phase=(.+)$') {
      $ret.phase = $matches[1].Trim()
      continue
    }
    if ($line -match '^###\s+link_state_sysfs$') {
      $inLinkState = $true
      $inWpa = $false
      $currentIface = $null
      $step = 0
      continue
    }
    if ($line -match '^###\s+wpa_cli status') {
      $inWpa = $true
      $inLinkState = $false
      continue
    }
    if ($line -eq '__END__') {
      $inLinkState = $false
      $inWpa = $false
      $currentIface = $null
      $step = 0
      continue
    }
    if ($line -match '^###\s+') {
      $inLinkState = $false
      $inWpa = $false
    }

    if ($inLinkState) {
      if ($line -match '^--\s+(\S+)$') {
        $currentIface = $matches[1]
        $ret.iface_states[$currentIface] = @{ operstate = $null; carrier = $null }
        $step = 1
        continue
      }
      if ($currentIface -and $step -eq 1 -and $line.Trim()) {
        $ret.iface_states[$currentIface].operstate = $line.Trim()
        $step = 2
        continue
      }
      if ($currentIface -and $step -eq 2 -and $line.Trim()) {
        $ret.iface_states[$currentIface].carrier = $line.Trim()
        $step = 0
        $currentIface = $null
        continue
      }
    }

    if ($inWpa -and $line -match 'wpa_state=(.+)$') {
      $ret.wpa_status = $matches[1].Trim()
    }
  }

  if (-not $ret.phase) {
    $ret.phase = [System.IO.Path]::GetFileNameWithoutExtension($Path).Replace('net_', '')
  }
  return [pscustomobject]$ret
}

function Parse-LocalNetProbeFile {
  param([string]$Path)

  $ret = [ordered]@{
    exists = $false
    phase = $null
    ping_ip_ok = $false
    ping_ip_fail = $false
    dns_host_ok = $false
    dns_host_fail = $false
    secondary_dns_ok = $false
    secondary_dns_fail = $false
    target_ping_ok = $false
    target_ping_fail = $false
    gateway_ping_ok = $false
    gateway_ping_fail = $false
  }
  if (-not (Test-PathSafe $Path)) { return [pscustomobject]$ret }

  $ret.exists = $true
  $text = [string](Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue)
  if ($text -match '(?m)^###\s+phase=(.+)$') {
    $ret.phase = $matches[1].Trim()
  } else {
    $ret.phase = [System.IO.Path]::GetFileNameWithoutExtension($Path).Replace('probe_', '')
  }

  $mainPingIp = if ($script:NET_PING_IP -and $script:NET_PING_IP -ne "") {
    $script:NET_PING_IP
  } elseif ($env:WK_NET_PING_IP -and $env:WK_NET_PING_IP -ne "") {
    $env:WK_NET_PING_IP
  } elseif ($env:NET_PING_IP -and $env:NET_PING_IP -ne "") {
    $env:NET_PING_IP
  } else {
    "8.8.8.8"
  }
  function Get-ProbeMarkerBlock {
    param(
      [string]$Text,
      [string]$Marker
    )
    if ([string]::IsNullOrWhiteSpace($Text) -or [string]::IsNullOrWhiteSpace($Marker)) { return $null }
    $esc = [regex]::Escape($Marker)
    $m = [regex]::Match($Text, "(?ms)^###\s+$esc\s*\r?\n(.*?)(?=^###\s|\z)")
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
  }

  $mainPingBlock = Get-ProbeMarkerBlock -Text $text -Marker ("ping -c 3 {0}" -f $mainPingIp)
  if ($mainPingBlock -match '(?m)([1-9][0-9]*\s+(?:packets?\s+)?received|(?<![0-9])0% packet loss)') { $ret.ping_ip_ok = $true }
  if ($mainPingBlock -match '(Network unreachable|Operation not permitted|(?m)100% packet loss|(?m)0\s+(?:packets?\s+)?received)') { $ret.ping_ip_fail = $true }

  $dnsBlock = Get-ProbeMarkerBlock -Text $text -Marker 'dns_host_probe_primary'
  if ($null -ne $dnsBlock -and ($dnsBlock -match '(?m)Name does not resolve' -or $dnsBlock -match '(?m)Temporary failure' -or $dnsBlock -match '(?m)Try again')) { $ret.dns_host_fail = $true }
  if ($null -ne $dnsBlock -and -not $ret.dns_host_fail -and ($dnsBlock -match '(?m)1 received' -or $dnsBlock -match '(?m)0% packet loss')) { $ret.dns_host_ok = $true }

  $nipBlock = Get-ProbeMarkerBlock -Text $text -Marker 'ping_nip_io'
  if ($null -ne $nipBlock -and ($nipBlock -match '(?m)Name does not resolve' -or $nipBlock -match '(?m)Temporary failure' -or $nipBlock -match '(?m)Try again')) { $ret.secondary_dns_fail = $true }
  if ($null -ne $nipBlock -and -not $ret.secondary_dns_fail -and ($nipBlock -match '(?m)1 received' -or $nipBlock -match '(?m)0% packet loss')) { $ret.secondary_dns_ok = $true }

  $targetBlock = Get-ProbeMarkerBlock -Text $text -Marker 'ping_target_ip'
  if ($null -ne $targetBlock -and ($targetBlock -match '(?m)0% packet loss' -or $targetBlock -match '(?m)3 received' -or $targetBlock -match '(?m)1 received')) { $ret.target_ping_ok = $true }
  if ($null -ne $targetBlock -and ($targetBlock -match '(?m)100% packet loss' -or $targetBlock -match '(?m)0 received' -or $targetBlock -match 'Network unreachable' -or $targetBlock -match 'Operation not permitted')) { $ret.target_ping_fail = $true }

  $gatewayBlock = Get-ProbeMarkerBlock -Text $text -Marker 'ping_gateway'
  if ($null -ne $gatewayBlock -and ($gatewayBlock -match '(?m)0% packet loss' -or $gatewayBlock -match '(?m)1 received')) { $ret.gateway_ping_ok = $true }
  if ($null -ne $gatewayBlock -and ($gatewayBlock -match '(?m)100% packet loss' -or $gatewayBlock -match '(?m)0 received' -or $gatewayBlock -match 'Network unreachable' -or $gatewayBlock -match 'NO_GATEWAY')) { $ret.gateway_ping_fail = $true }

  return [pscustomobject]$ret
}

function Get-WlanDefaultRouteGatewayHex {
  param([string]$Text)
  if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
  $m = [regex]::Match($Text, '(?m)^wlan0\s+00000000\s+([0-9A-Fa-f]{8})\b')
  if ($m.Success) { return $m.Groups[1].Value.ToUpperInvariant() }
  return ""
}

function Get-WlanDefaultRouteGatewayHexes {
  param(
    [string]$Text,
    [string]$Iface = "wlan0"
  )
  if ([string]::IsNullOrWhiteSpace($Text)) { return @() }
  if ([string]::IsNullOrWhiteSpace($Iface)) { $Iface = "wlan0" }
  $escIface = [regex]::Escape($Iface)
  $matches = [regex]::Matches($Text, ("(?m)^{0}\s+00000000\s+([0-9A-Fa-f]{{8}})\b" -f $escIface))
  $ret = New-Object System.Collections.Generic.List[string]
  foreach ($m in $matches) {
    [void]$ret.Add($m.Groups[1].Value.ToUpperInvariant())
  }
  return [string[]]$ret.ToArray()
}

function Get-WrongRouteExpectedFakeGatewayHexes {
  param(
    [string]$RunDir
  )
  $ret = New-Object System.Collections.Generic.List[string]
  if ([string]::IsNullOrWhiteSpace($RunDir)) { return @() }
  $faultLogDir = Join-Path $RunDir "fault_inject"
  $candidates = @(
    (Join-Path $faultLogDir "fault_net_wrong_default_route.log"),
    (Join-Path $faultLogDir "fault_net_wrong_default_route.txt")
  )
  foreach ($path in $candidates) {
    if (-not (Test-PathSafe $path)) { continue }
    $text = [System.IO.File]::ReadAllText($path)
    foreach ($m in [regex]::Matches($text, '(?i)\bfake_gw_hex=([0-9a-f]{8})\b')) {
      $hex = $m.Groups[1].Value.ToUpperInvariant()
      if ($ret -notcontains $hex) { [void]$ret.Add($hex) }
    }
  }
  return [string[]]$ret.ToArray()
}

function Get-NetOutcomeMetadata {
  param(
    [string]$RunDir,
    [string]$FaultType,
    [string]$IfaceUsed,
    [string]$WlanIface,
    [bool]$InjectorStarted
  )

  $ret = [ordered]@{
    net_fault_type = $FaultType
    iface_used = $IfaceUsed
    inject_ok = $InjectorStarted
    fault_observed = $false
    recovery_observed = $false
    fault_observation_reason = ''
    recovery_observation_reason = ''
    probe_profile_id = $script:NET_PROBE_PROFILE_ID
    active_ping_ip = $script:NET_ACTIVE_PING_IP
    active_dns_host = $script:NET_ACTIVE_DNS_HOST
    dns_proof_base_ip = $script:NET_DNS_PROOF_BASE_IP
    net_target_ip = $script:NET_TARGET_IP
    target_ip = if ($FaultType -eq "net_public_ip_unreachable") { $script:NET_TARGET_IP } else { "" }
    baseline_policy = $script:NET_BASELINE_POLICY
    baseline_max_attempts = $script:NET_BASELINE_MAX_ATTEMPTS
    baseline_consecutive_required = $script:NET_BASELINE_CONSECUTIVE_REQUIRED
    baseline_sleep_sec = $script:NET_BASELINE_SLEEP_SEC
    probe_profile_reason = $script:NET_PROBE_PROFILE_REASON
    ap_or_network_profile = $script:NET_AP_OR_NETWORK_PROFILE
    profile_effective_from = $script:NET_PROFILE_EFFECTIVE_FROM
    profile_approved_by = $script:NET_PROFILE_APPROVED_BY
    profile_approval_time = $script:NET_PROFILE_APPROVAL_TIME
    diagnostic_probe_targets = $script:NET_DIAGNOSTIC_PROBE_TARGETS
  }

  $netDir = Join-Path $RunDir 'net'
  if (-not (Test-PathSafe $netDir)) { return [pscustomobject]$ret }

  $snapshots = @{}
  foreach ($phase in @('pre','fault','fault2','post','post2')) {
    $path = Join-Path $netDir ("net_{0}.txt" -f $phase)
    if (Test-PathSafe $path) { $snapshots[$phase] = Parse-LocalNetSnapshotFile -Path $path }
  }
  $probes = @{}
  foreach ($phase in @('pre','fault','fault2','post','post2')) {
    $path = Join-Path $netDir ("probe_{0}.txt" -f $phase)
    if (Test-PathSafe $path) { $probes[$phase] = Parse-LocalNetProbeFile -Path $path }
  }

  switch ($FaultType) {
    'net_dns_fail' {
      $ret.iface_used = if (-not [string]::IsNullOrWhiteSpace($WlanIface)) { $WlanIface } else { $IfaceUsed }
      $faultProbe = $null
      if ($probes.ContainsKey('fault')) { $faultProbe = $probes['fault'] }
      $postProbe = $null
      if ($probes.ContainsKey('post2')) { $postProbe = $probes['post2'] }
      elseif ($probes.ContainsKey('post')) { $postProbe = $probes['post'] }

      if ($faultProbe -and $faultProbe.dns_host_fail -and $faultProbe.ping_ip_ok) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'dns_host_probe_failed_while_ip_probe_still_ok'
      } elseif ($faultProbe -and $faultProbe.dns_host_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'dns_host_probe_failed'
      } else {
        $probeFaultPath = Join-Path $netDir 'probe_fault.txt'
        $probeFaultRaw = if (Test-PathSafe $probeFaultPath) { [System.IO.File]::ReadAllText($probeFaultPath) } else { '' }
        if ($probeFaultRaw -match 'DNS_PROOF_BEGIN' -and $probeFaultRaw -match 'DNS_PROOF_END') {
          $ret.fault_observation_reason = 'dns_failure_text_unrecognized'
        }
      }

      if ($postProbe -and $postProbe.dns_host_ok) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'dns_host_probe_recovered'
      }
    }
    'net_public_ip_unreachable' {
      $ret.iface_used = if (-not [string]::IsNullOrWhiteSpace($WlanIface)) { $WlanIface } else { $IfaceUsed }
      $ret.net_target_ip = $script:NET_TARGET_IP
      $ret.target_ip = $script:NET_TARGET_IP
      $faultProbe = if ($probes.ContainsKey('fault')) { $probes['fault'] } else { $null }
      $postProbe  = if ($probes.ContainsKey('post2')) { $probes['post2'] } elseif ($probes.ContainsKey('post')) { $probes['post'] } else { $null }

      if ($faultProbe -and $faultProbe.target_ping_fail -and $faultProbe.ping_ip_ok -and $faultProbe.gateway_ping_ok -and -not $faultProbe.dns_host_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'target_ip_unreachable_while_gateway_and_main_ip_still_ok'
      } elseif ($faultProbe -and $faultProbe.target_ping_fail -and $faultProbe.ping_ip_ok) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'target_ip_unreachable_while_main_ip_still_ok'
      } elseif ($faultProbe -and $faultProbe.target_ping_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'target_ip_unreachable'
      }

      if ($postProbe -and $postProbe.target_ping_ok) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'target_ip_probe_recovered'
      }
    }
    'net_link_down' {
      $pre = if ($snapshots.ContainsKey('pre')) { $snapshots['pre'] } else { $null }
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      if ($pre -and $fault -and $pre.iface_states.ContainsKey($IfaceUsed) -and $fault.iface_states.ContainsKey($IfaceUsed)) {
        $preUp = ($pre.iface_states[$IfaceUsed].operstate -eq 'up' -and $pre.iface_states[$IfaceUsed].carrier -eq '1')
        $faultUp = ($fault.iface_states[$IfaceUsed].operstate -eq 'up' -and $fault.iface_states[$IfaceUsed].carrier -eq '1')
        if ($preUp -and -not $faultUp) {
          $ret.fault_observed = $true
          $ret.fault_observation_reason = 'operstate_or_carrier_down'
        }
      }
      if ($post -and $post.iface_states.ContainsKey($IfaceUsed)) {
        $postUp = ($post.iface_states[$IfaceUsed].operstate -eq 'up' -and $post.iface_states[$IfaceUsed].carrier -eq '1')
        if ($postUp) {
          $ret.recovery_observed = $true
          $ret.recovery_observation_reason = 'operstate_or_carrier_recovered'
        }
      }
    }
    'net_link_flap' {
      $pre = if ($snapshots.ContainsKey('pre')) { $snapshots['pre'] } else { $null }
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $fault2 = if ($snapshots.ContainsKey('fault2')) { $snapshots['fault2'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      $downSeen = $false
      if ($pre -and $fault -and $pre.iface_states.ContainsKey($IfaceUsed) -and $fault.iface_states.ContainsKey($IfaceUsed)) {
        $preUp = ($pre.iface_states[$IfaceUsed].operstate -eq 'up' -and $pre.iface_states[$IfaceUsed].carrier -eq '1')
        $faultUp = ($fault.iface_states[$IfaceUsed].operstate -eq 'up' -and $fault.iface_states[$IfaceUsed].carrier -eq '1')
        if ($preUp -and -not $faultUp) { $downSeen = $true }
      }
      $upSeen = $false
      foreach ($candidate in @($fault2, $post)) {
        if ($candidate -and $candidate.iface_states.ContainsKey($IfaceUsed)) {
          $isUp = ($candidate.iface_states[$IfaceUsed].operstate -eq 'up' -and $candidate.iface_states[$IfaceUsed].carrier -eq '1')
          if ($isUp) { $upSeen = $true; break }
        }
      }
      if ($downSeen) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'down_phase_observed'
      }
      if ($downSeen -and $upSeen) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'down_to_up_transition_observed'
      }
    }
    'net_wlan_disconnect' {
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      if ($fault -and (($fault.wpa_status -eq 'DISCONNECTED') -or ($fault.iface_states.ContainsKey($WlanIface) -and $fault.iface_states[$WlanIface].operstate -eq 'down'))) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wpa_or_iface_disconnected'
      } else {
        $ret.fault_observation_reason = 'unsupported_or_no_wlan_state_change'
      }
      if ($post -and (($post.wpa_status -eq 'COMPLETED') -or ($post.iface_states.ContainsKey($WlanIface) -and $post.iface_states[$WlanIface].operstate -eq 'up'))) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'wlan_reconnected'
      }
    }
    'net_wifi_disconnect' {
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      $activeAuthStates = @('SCANNING','ASSOCIATING','4WAY_HANDSHAKE','AUTHENTICATING','GROUP_HANDSHAKE')
      if ($fault -and ($fault.wpa_status -eq 'DISCONNECTED' -or $fault.wpa_status -in $activeAuthStates -or
          ($fault.iface_states.ContainsKey($WlanIface) -and $fault.iface_states[$WlanIface].operstate -eq 'down'))) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wlan_interface_disconnected'
      } else {
        $ret.fault_observation_reason = 'no_disconnect_evidence'
      }
      if ($post -and $post.wpa_status -eq 'COMPLETED') {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'wpa_state_returned_to_completed'
      }
    }
    'net_wifi_auth_fail_wrong_psk' {
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      $activeAuthStates = @('SCANNING','ASSOCIATING','4WAY_HANDSHAKE','AUTHENTICATING','GROUP_HANDSHAKE')
      if ($fault -and $fault.wpa_status -in $activeAuthStates) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wpa_state_in_active_auth_cycle'
      } else {
        $ret.fault_observation_reason = 'no_active_auth_state_observed'
      }
      if ($post -and $post.wpa_status -eq 'COMPLETED') {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'wpa_state_returned_to_completed'
      }
    }
    'net_no_default_route' {
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      $faultPath = Join-Path $netDir 'net_fault.txt'
      $postPath = Join-Path $netDir $(if ($snapshots.ContainsKey('post2')) { 'net_post2.txt' } else { 'net_post.txt' })
      $faultRaw = if (Test-PathSafe $faultPath) { [System.IO.File]::ReadAllText($faultPath) } else { '' }
      $postRaw = if (Test-PathSafe $postPath) { [System.IO.File]::ReadAllText($postPath) } else { '' }
      $hasIpFault = ($faultRaw -match '(?ms)### ifconfig wlan0.*?inet addr:\s*\d+')
      if (-not $hasIpFault) { $hasIpFault = ($faultRaw -match '(?ms)### ifconfig -a.*?^wlan0\b.*?inet addr:\s*\d+') }
      if (-not $hasIpFault) {
        $probeFaultPath = Join-Path $netDir 'probe_fault.txt'
        $probeFaultRaw  = if (Test-PathSafe $probeFaultPath) { [System.IO.File]::ReadAllText($probeFaultPath) } else { '' }
        $hasIpFault = ($probeFaultRaw -match '(?ms)### ifconfig_wlan.*?inet addr:\s*\d+')
      }
      $hasRouteFault = ($faultRaw -match '(?m)^wlan0\s+00000000\s')
      if ($hasIpFault -and -not $hasRouteFault) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wlan0_has_ip_but_no_default_route'
      } else {
        $ret.fault_observation_reason = 'no_missing_route_evidence'
      }
      $hasRoutePost = ($postRaw -match '(?m)^wlan0\s+00000000\s')
      if (-not $hasRoutePost) {
        $probePostPath = Join-Path $netDir $(if ($snapshots.ContainsKey('post2')) { 'probe_post2.txt' } else { 'probe_post.txt' })
        $probePostRaw  = if (Test-PathSafe $probePostPath) { [System.IO.File]::ReadAllText($probePostPath) } else { '' }
        $hasRoutePost  = ($probePostRaw -match '(?m)^wlan0\s+00000000\s')
      }
      $postProbe = if ($probes.ContainsKey('post2')) { $probes['post2'] } elseif ($probes.ContainsKey('post')) { $probes['post'] } else { $null }
      if ($hasRoutePost) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'default_route_restored'
      } elseif ($postProbe -and $postProbe.ping_ip_ok) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'ping_ip_ok_route_snapshot_empty'
      }
    }
    'net_no_ipv4_on_iface' {
      $fault = if ($snapshots.ContainsKey('fault')) { $snapshots['fault'] } else { $null }
      $post = if ($snapshots.ContainsKey('post2')) { $snapshots['post2'] } elseif ($snapshots.ContainsKey('post')) { $snapshots['post'] } else { $null }
      $faultPath = Join-Path $netDir 'net_fault.txt'
      $postPath = Join-Path $netDir $(if ($snapshots.ContainsKey('post2')) { 'net_post2.txt' } else { 'net_post.txt' })
      $faultRaw = if (Test-PathSafe $faultPath) { [System.IO.File]::ReadAllText($faultPath) } else { '' }
      $postRaw = if (Test-PathSafe $postPath) { [System.IO.File]::ReadAllText($postPath) } else { '' }
      $wlanBlock = [regex]::Match($faultRaw, '(?ms)### ifconfig wlan0.*?(?=### |\z)')
      if (-not $wlanBlock.Success) {
        $ifcaFault = [regex]::Match($faultRaw, '(?ms)### ifconfig -a.*?(?=### |\z)')
        if ($ifcaFault.Success) { $wlanBlock = [regex]::Match($ifcaFault.Value, '(?ms)^wlan0\b.*?(?=\r?\n\S|\z)') }
      }
      if (-not $wlanBlock.Success) {
        $probeFaultPath = Join-Path $netDir 'probe_fault.txt'
        $probeFaultRaw  = if (Test-PathSafe $probeFaultPath) { [System.IO.File]::ReadAllText($probeFaultPath) } else { '' }
        $wlanBlock = [regex]::Match($probeFaultRaw, '(?ms)### ifconfig_wlan.*?(?=### |\z)')
      }
      $wpaMatch = [regex]::Match($faultRaw, '(?ms)### wpa_cli status.*?wpa_state=(\S+)')
      $wpaStateFault = if ($wpaMatch.Success) { $wpaMatch.Groups[1].Value.Trim() } else { '' }
      $noIpFault = ($wlanBlock.Success -and $wlanBlock.Value -notmatch 'inet addr')
      $wpaUpFault = ($wpaStateFault -eq 'COMPLETED')
      if ($noIpFault -and $wpaUpFault) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wlan0_no_ip_while_wpa_still_completed'
      } elseif ($noIpFault) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wlan0_no_ip_wpa_state_unknown'
      } else {
        $ret.fault_observation_reason = 'no_missing_ip_evidence'
      }
      $wlanBlockPost = [regex]::Match($postRaw, '(?ms)### ifconfig wlan0.*?(?=### |\z)')
      if (-not $wlanBlockPost.Success) {
        $ifcaPost = [regex]::Match($postRaw, '(?ms)### ifconfig -a.*?(?=### |\z)')
        if ($ifcaPost.Success) { $wlanBlockPost = [regex]::Match($ifcaPost.Value, '(?ms)^wlan0\b.*?(?=\r?\n\S|\z)') }
      }
      if (-not $wlanBlockPost.Success) {
        $probePostPath = Join-Path $netDir $(if ($snapshots.ContainsKey('post2')) { 'probe_post2.txt' } else { 'probe_post.txt' })
        $probePostRaw  = if (Test-PathSafe $probePostPath) { [System.IO.File]::ReadAllText($probePostPath) } else { '' }
        $wlanBlockPost = [regex]::Match($probePostRaw, '(?ms)### ifconfig_wlan.*?(?=### |\z)')
      }
      if ($wlanBlockPost.Success -and $wlanBlockPost.Value -match 'inet addr') {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'wlan0_ip_restored'
      }
    }
    'net_wrong_default_route' {
      $ret.iface_used = if (-not [string]::IsNullOrWhiteSpace($WlanIface)) { $WlanIface } else { $IfaceUsed }
      $wdrIface = if (-not [string]::IsNullOrWhiteSpace($ret.iface_used)) { [string]$ret.iface_used } else { "wlan0" }
      $wdrDefaultRoutePattern = ("(?m)^{0}\s+00000000\s" -f [regex]::Escape($wdrIface))
      $faultPath = Join-Path $netDir 'net_fault.txt'
      $postPath  = Join-Path $netDir $(if ($snapshots.ContainsKey('post2')) { 'net_post2.txt' } else { 'net_post.txt' })
      $faultRaw  = if (Test-PathSafe $faultPath) { [System.IO.File]::ReadAllText($faultPath) } else { '' }
      $postRaw   = if (Test-PathSafe $postPath)  { [System.IO.File]::ReadAllText($postPath) }  else { '' }
      $faultProbe = if ($probes.ContainsKey('fault')) { $probes['fault'] } else { $null }
      $hasRouteFault = ($faultRaw -match $wdrDefaultRoutePattern)
      $faultGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $faultRaw -Iface $wdrIface)
      $faultGwHex = if ($faultGwHexes.Count -gt 0) { $faultGwHexes[0] } else { "" }
      # hasRouteFault fallback: probe_fault ### proc_net_route (more reliable when net_fault.txt section is empty)
      if (-not $hasRouteFault) {
        $probeFaultPath = Join-Path $netDir 'probe_fault.txt'
        $probeFaultRaw  = if (Test-PathSafe $probeFaultPath) { [System.IO.File]::ReadAllText($probeFaultPath) } else { '' }
        $hasRouteFault  = ($probeFaultRaw -match $wdrDefaultRoutePattern)
        $faultGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $probeFaultRaw -Iface $wdrIface)
        $faultGwHex = if ($faultGwHexes.Count -gt 0) { $faultGwHexes[0] } else { "" }
      }
      $preRaw = ''
      if ($snapshots.ContainsKey('pre')) {
        $prePath = Join-Path $netDir 'net_pre.txt'
        if (Test-PathSafe $prePath) { $preRaw = [System.IO.File]::ReadAllText($prePath) }
      }
      $preGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $preRaw -Iface $wdrIface)
      $preGwHex = if ($preGwHexes.Count -gt 0) { $preGwHexes[0] } else { "" }
      if (-not $preGwHex) {
        $probePrePath = Join-Path $netDir 'probe_pre.txt'
        $probePreRaw  = if (Test-PathSafe $probePrePath) { [System.IO.File]::ReadAllText($probePrePath) } else { '' }
        $preGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $probePreRaw -Iface $wdrIface)
        $preGwHex = if ($preGwHexes.Count -gt 0) { $preGwHexes[0] } else { "" }
      }
      $expectedFakeGwHexes = @(Get-WrongRouteExpectedFakeGatewayHexes -RunDir $RunDir)
      $hasExpectedFakeGwEvidence = ($expectedFakeGwHexes.Count -gt 0)
      $hasWrongGwFault = $hasExpectedFakeGwEvidence -and $faultGwHex -and ($expectedFakeGwHexes -contains $faultGwHex)
      # hasIpFault: 1) dedicated ### ifconfig wlan0 section, 2) wlan0 in ### ifconfig -a, 3) probe_fault ### ifconfig_wlan
      $hasIpFault = ($faultRaw -match '(?ms)### ifconfig wlan0.*?inet addr:\s*\d+')
      if (-not $hasIpFault) { $hasIpFault = ($faultRaw -match '(?ms)### ifconfig -a.*?^wlan0\b.*?inet addr:\s*\d+') }
      if (-not $hasIpFault) {
        $probeFaultPath = Join-Path $netDir 'probe_fault.txt'
        $probeFaultRaw  = if (Test-PathSafe $probeFaultPath) { [System.IO.File]::ReadAllText($probeFaultPath) } else { '' }
        $hasIpFault = ($probeFaultRaw -match '(?ms)### ifconfig_wlan.*?inet addr:\s*\d+')
      }
      if ($hasRouteFault -and $hasIpFault -and $hasWrongGwFault -and $faultProbe -and $faultProbe.ping_ip_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wrong_gateway_route_present_ip_present_but_connectivity_lost'
      } elseif ($hasRouteFault -and $hasIpFault -and $faultProbe -and $faultProbe.ping_ip_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'route_present_ip_present_but_connectivity_lost'
      } elseif ($hasRouteFault -and $hasIpFault -and $hasWrongGwFault) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'wrong_gateway_route_present_ip_present_ping_state_unknown'
      } else {
        $ret.fault_observation_reason = 'no_wrong_route_evidence'
      }
      $postGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $postRaw -Iface $wdrIface)
      $hasRoutePost = ($postGwHexes.Count -gt 0)
      $postGwHex = if ($postGwHexes.Count -gt 0) { $postGwHexes[0] } else { "" }
      $fakeGwHex = if ($hasWrongGwFault) { $faultGwHex } else { "" }
      foreach ($netPostName in @('net_post2.txt', 'net_post.txt')) {
        if ($preGwHex -and ($postGwHexes -contains $preGwHex) -and ((-not $fakeGwHex) -or ($postGwHexes -notcontains $fakeGwHex))) { break }
        $netPostPath = Join-Path $netDir $netPostName
        $netPostRaw  = if (Test-PathSafe $netPostPath) { [System.IO.File]::ReadAllText($netPostPath) } else { '' }
        $netPostGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $netPostRaw -Iface $wdrIface)
        if ($netPostGwHexes.Count -gt 0) {
          $hasRoutePost = $true
          $postGwHexes = $netPostGwHexes
          $postGwHex = $netPostGwHexes[0]
        }
      }
      foreach ($probePostName in @('probe_post2.txt', 'probe_post.txt')) {
        if ($preGwHex -and ($postGwHexes -contains $preGwHex) -and ((-not $fakeGwHex) -or ($postGwHexes -notcontains $fakeGwHex))) { break }
        $probePostPath = Join-Path $netDir $probePostName
        $probePostRaw  = if (Test-PathSafe $probePostPath) { [System.IO.File]::ReadAllText($probePostPath) } else { '' }
        $probePostGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $probePostRaw -Iface $wdrIface)
        if ($probePostGwHexes.Count -gt 0) {
          $hasRoutePost = $true
          $postGwHexes = $probePostGwHexes
          $postGwHex = $probePostGwHexes[0]
        }
      }
      $postProbe    = if ($probes.ContainsKey('post2')) { $probes['post2'] } elseif ($probes.ContainsKey('post')) { $probes['post'] } else { $null }
      $hasRealRoutePost = ($preGwHex -and ($postGwHexes -contains $preGwHex))
      $fakeRouteRemoved = ((-not $fakeGwHex) -or ($postGwHexes -notcontains $fakeGwHex))
      $postConnectivityRecovered = ($postProbe -and $postProbe.ping_ip_ok -and (-not $postProbe.ping_ip_fail) -and $postProbe.dns_host_ok -and (-not $postProbe.dns_host_fail) -and $postProbe.gateway_ping_ok -and (-not $postProbe.gateway_ping_fail))
      $hasParsableRoutePost = -not [string]::IsNullOrWhiteSpace($postGwHex)
      if ($hasRealRoutePost -and $fakeRouteRemoved -and $postConnectivityRecovered) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'real_default_route_restored_matches_pre_gateway'
      } elseif ($hasRealRoutePost -and $fakeRouteRemoved) {
        $ret.recovery_observation_reason = 'route_restored_but_connectivity_probe_incomplete'
      } elseif ($hasRealRoutePost) {
        $ret.recovery_observation_reason = 'route_restored_but_fake_gateway_still_present'
      } elseif ((-not $hasParsableRoutePost) -and $postProbe -and $postProbe.ping_ip_ok) {
        $ret.recovery_observation_reason = 'connectivity_restored_but_route_not_confirmed_matches_pre_gateway'
      }
    }
    'net_gateway_unreachable' {
      $ret.iface_used = if (-not [string]::IsNullOrWhiteSpace($WlanIface)) { $WlanIface } else { $IfaceUsed }
      $gwIface = if (-not [string]::IsNullOrWhiteSpace($ret.iface_used)) { [string]$ret.iface_used } else { "wlan0" }
      $faultPath = Join-Path $netDir 'net_fault.txt'
      $faultRaw = if (Test-PathSafe $faultPath) { [System.IO.File]::ReadAllText($faultPath) } else { '' }
      $probeFaultPath = Join-Path $netDir 'probe_fault.txt'
      $probeFaultRaw = if (Test-PathSafe $probeFaultPath) { [System.IO.File]::ReadAllText($probeFaultPath) } else { '' }
      $faultProbe = if ($probes.ContainsKey('fault')) { $probes['fault'] } else { $null }
      $hasRouteFault = (($faultRaw + "`n" + $probeFaultRaw) -match ("(?m)^{0}\s+00000000\s" -f [regex]::Escape($gwIface)))
      $hasIpFault = ($faultRaw -match '(?ms)### ifconfig wlan0.*?inet addr:\s*\d+') -or
                    ($faultRaw -match '(?ms)### ifconfig -a.*?^wlan0\b.*?inet addr:\s*\d+') -or
                    ($probeFaultRaw -match '(?ms)### ifconfig_wlan.*?inet addr:\s*\d+')
      if ($hasRouteFault -and $hasIpFault -and $faultProbe -and $faultProbe.gateway_ping_fail -and $faultProbe.ping_ip_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'default_gateway_unreachable_while_wlan_ip_and_route_present'
      } elseif ($hasRouteFault -and $hasIpFault -and $faultProbe -and $faultProbe.gateway_ping_fail) {
        $ret.fault_observed = $true
        $ret.fault_observation_reason = 'default_gateway_unreachable_public_probe_unknown'
      } else {
        $ret.fault_observation_reason = 'no_gateway_unreachable_evidence'
      }

      $preRaw = if (Test-PathSafe (Join-Path $netDir 'net_pre.txt')) { [System.IO.File]::ReadAllText((Join-Path $netDir 'net_pre.txt')) } else { '' }
      $preGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $preRaw -Iface $gwIface)
      if ($preGwHexes.Count -eq 0) {
        $probePreRaw = if (Test-PathSafe (Join-Path $netDir 'probe_pre.txt')) { [System.IO.File]::ReadAllText((Join-Path $netDir 'probe_pre.txt')) } else { '' }
        $preGwHexes = @(Get-WlanDefaultRouteGatewayHexes -Text $probePreRaw -Iface $gwIface)
      }
      $preGwHex = if ($preGwHexes.Count -gt 0) { $preGwHexes[0] } else { "" }
      $postGwHexes = @()
      foreach ($postName in @('net_post2.txt','net_post.txt','probe_post2.txt','probe_post.txt')) {
        $postPath = Join-Path $netDir $postName
        $postRaw = if (Test-PathSafe $postPath) { [System.IO.File]::ReadAllText($postPath) } else { '' }
        foreach ($gwHex in @(Get-WlanDefaultRouteGatewayHexes -Text $postRaw -Iface $gwIface)) {
          if ($postGwHexes -notcontains $gwHex) { $postGwHexes += $gwHex }
        }
      }
      $postProbe = if ($probes.ContainsKey('post2')) { $probes['post2'] } elseif ($probes.ContainsKey('post')) { $probes['post'] } else { $null }
      $hasRealRoutePost = ($preGwHex -and ($postGwHexes -contains $preGwHex))
      $postConnectivityRecovered = ($postProbe -and $postProbe.gateway_ping_ok -and (-not $postProbe.gateway_ping_fail) -and $postProbe.ping_ip_ok -and (-not $postProbe.ping_ip_fail) -and $postProbe.dns_host_ok -and (-not $postProbe.dns_host_fail))
      if ($hasRealRoutePost -and $postConnectivityRecovered) {
        $ret.recovery_observed = $true
        $ret.recovery_observation_reason = 'gateway_route_and_connectivity_recovered'
      } elseif ($hasRealRoutePost) {
        $ret.recovery_observation_reason = 'route_restored_but_gateway_connectivity_incomplete'
      }
    }
  }

  return [pscustomobject]$ret
}

function Save-NetOutcomeMetadata {
  param(
    [string]$RunDir,
    $Outcome
  )

  if ($null -eq $Outcome) { return }
  $jsonPath = Join-Path $RunDir "_net_outcome.json"
  $json = ConvertTo-Json -InputObject $Outcome -Depth 6
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($jsonPath, $json, $utf8NoBom)
}



# =========[ HiLog assembly from .gz segments ]=========
function Build-HilogFromGz {
  param([string]$BinDir,[string]$OutAll,[string]$OutIndex)
  if (-not (Test-Path $BinDir)) { return $false }

  $rx = [regex]'^hilog\.(\d{3})\.(\d{8})-(\d{6})\.gz$'
  $files = Get-ChildItem $BinDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $rx.IsMatch($_.Name)
  }

  if (-not $files -or $files.Count -eq 0) { return $false }

  $segments = @()
  foreach ($f in $files) {
    $m = $rx.Match($f.Name)
    $seq = [int]$m.Groups[1].Value
    $ts  = $m.Groups[2].Value + $m.Groups[3].Value
    $start = $null
    try { $start = [datetime]::ParseExact($ts,'yyyyMMddHHmmss',$null) } catch {}
    $segments += [pscustomobject]@{ Seq=$seq; Start=$start; Path=$f.FullName; Name=$f.Name }
  }
  $segments = $segments | Sort-Object Seq, Start

  $outDir = Split-Path $OutAll -Parent
  Ensure-Dir $outDir
  if (Test-Path $OutAll) { Remove-Item $OutAll -Force }
  if (Test-Path $OutIndex) { Remove-Item $OutIndex -Force }

  # open output stream for performance and accurate offsets
  $fsOut = [System.IO.File]::Open($OutAll,[System.IO.FileMode]::Append,[System.IO.FileAccess]::Write,[System.IO.FileShare]::Read)
  $sw = New-Object System.IO.StreamWriter($fsOut, [System.Text.Encoding]::UTF8)
  $idx = New-Object System.Collections.Generic.List[string]

  foreach ($s in $segments) {
    $hdr = ("##### BEGIN {0} | start={1:yyyy-MM-dd HH:mm:ss}" -f $s.Name, $s.Start)
    $sw.WriteLine($hdr)
    $sw.Flush()
    $offsetStart = $fsOut.Position  # first byte of segment content after header

    # decompress .gz and stream to output
    try {
      $fsIn = [System.IO.File]::OpenRead($s.Path)
      $gz = New-Object System.IO.Compression.GzipStream($fsIn, [System.IO.Compression.CompressionMode]::Decompress)
      $sr = New-Object System.IO.StreamReader($gz, [System.Text.Encoding]::UTF8, $true)
      while (($line = $sr.ReadLine()) -ne $null) {
        $sw.WriteLine($line)
      }
      $sr.Close(); $gz.Close(); $fsIn.Close()
    } catch {
      $sw.WriteLine("##### WARN failed to read {0} : {1}" -f $s.Name, $_.Exception.Message)
    }
    $sw.Flush()
    $offsetEnd = $fsOut.Position

    $idx.Add(("{0},{1:yyyy-MM-dd HH:mm:ss},{2},{3},{4}" -f $s.Seq, $s.Start, $offsetStart, ($offsetEnd-$offsetStart), $s.Name))
  }

  $sw.Close(); $fsOut.Close()

  # write index csv
  $idxDir = Split-Path $OutIndex -Parent
  Ensure-Dir $idxDir
  @("seq,start,offset,length,filename") + $idx | Out-File -FilePath $OutIndex -Encoding ASCII -Force
  return $true
}

# ----- textual hilog fallback (rare) -----
function Merge-HilogTextFallback {
  param([string]$BinDir,[string]$OutAll,[string]$OutIndex)
  if (-not (Test-Path $BinDir)) { return $false }
  $cand = Get-ChildItem $BinDir -Recurse -File | Where-Object {
    $_.Name -match '^hilog\.' -and ($_.Extension -ne '.gz')
  }
  if (-not $cand) { return $false }

  if (Test-Path $OutAll) { Remove-Item $OutAll -Force }
  New-Item -ItemType File -Force -Path $OutAll | Out-Null
  foreach ($f in $cand | Sort-Object FullName) {
    Add-Content -Path $OutAll -Value ("`n##### BEGIN {0}" -f $f.FullName)
    try { Get-Content $f.FullName -ErrorAction SilentlyContinue | Add-Content -Path $OutAll } catch {}
  }
  # simple index (no offsets)
  @("seq,start,offset,length,filename") | Out-File -FilePath $OutIndex -Encoding ASCII -Force
  return $true
}
# ----- fault injection helpers (start/stop remote demos) -----
function Quote-RemoteShellEnvValue {
  param([string]$Value)
  if ($null -eq $Value) { return "''" }
  if ($Value -match "[`r`n]") {
    throw "remote shell env value contains a newline"
  }
  $sq = [char]39
  $dq = [char]34
  $escaped = $Value.Replace([string]$sq, ([string]$sq + [string]$dq + [string]$sq + [string]$dq + [string]$sq))
  return ([string]$sq + $escaped + [string]$sq)
}

function Get-CurrentWlanSsid {
  param(
    [string]$SN,
    [string]$Iface,
    [string]$WpaCtrl = "/data/local/tmp/wpa_ctrl"
  )

  $status = (@(hdc -t $SN shell ("wpa_cli -p {0} -i {1} status 2>/dev/null || true" -f $WpaCtrl, $Iface) 2>$null) -join "`n")
  $m = [regex]::Match($status, '(?m)^ssid=(.+)$')
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  return ""
}

function Start-FaultInject {
  param(
    [string]$Type,
    [string]$SN,
    [string]$BinDir
  )

  $envPrefix = ""
  $remoteBin = ""
  $script:FaultInjectMinRunSec = 0   # 婵絽绻戦?run 闂佹彃绉堕悿?
  switch ($Type) {

    # ===== CPU 闁革妇鍎ゅ▍娆撴晬濮樿京鍩犲☉鎾亾濞达綀娉曢弫?cpu_stress_demo =====
    "cpu" {   # 濮掓稒顭堥鑽ゆ導?baseline
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=baseline CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=600 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=1000 CPU_STRESS_SLEEP_US=20000 " +
                   "CPU_STRESS_TEMP_LIMIT_C=85 CPU_STRESS_MONITOR_MS=2000"
    }
    "cpu_baseline" {
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=baseline CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=600 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=1000 CPU_STRESS_SLEEP_US=20000 " +
                   "CPU_STRESS_TEMP_LIMIT_C=85 CPU_STRESS_MONITOR_MS=2000"
    }
    "cpu_busy_loop" {
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=busy_loop CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=600 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=800000 CPU_STRESS_SLEEP_US=0 " +
                   "CPU_STRESS_TEMP_LIMIT_C=85 CPU_STRESS_MONITOR_MS=2000"
    }
    "cpu_busy_yield" {
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=busy_yield CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=600 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=5000 CPU_STRESS_SLEEP_US=1000 " +
                   "CPU_STRESS_YIELD_EVERY=1000 CPU_STRESS_TEMP_LIMIT_C=85 " +
                   "CPU_STRESS_MONITOR_MS=2000"
    }
    "cpu_multicore" {
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=multicore CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=600 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=5000 CPU_STRESS_SLEEP_US=0 " +
                   "CPU_STRESS_TEMP_LIMIT_C=85 CPU_STRESS_MONITOR_MS=2000"
    }
    "cpu_oversub" {
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=oversub CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=600 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=5000 CPU_STRESS_SLEEP_US=0 " +
                   "CPU_STRESS_OVERSUB=2.0 CPU_STRESS_TEMP_LIMIT_C=85 " +
                   "CPU_STRESS_MONITOR_MS=2000"
    }
    "cpu_thread_leak" {
      $remoteBin = "$BinDir/cpu_stress_demo"
      $envPrefix = "CPU_STRESS_MODE=thread_leak CPU_STRESS_INITIAL_DELAY=60 " +
                   "CPU_STRESS_DURATION_SEC=900 CPU_STRESS_LOG_INTERVAL=5 " +
                   "CPU_STRESS_BUSY_US=5000 CPU_STRESS_SLEEP_US=0 " +
                   "CPU_STRESS_THREAD_MS=5000 CPU_STRESS_TEMP_LIMIT_C=85 " +
                   "CPU_STRESS_MONITOR_MS=2000"
    }

    # ===== Mem 闁革妇鍎ゅ▍娆撴晬濮樿京鍩犲☉鎾亾闁?memory_leak_demo闁挎稑鏈涵鐘绘闊厾鐟愰梻鍕姍閸?>0 =====
    # 闁稿繑婀归懙?mem / mem_oomsafe 濞达綀娉曢弫銈嗘媴閻樿崵宕ｉ悹鍥︽祰缁诲啴鎯冮崟顖氫簼缂備礁瀚顒勫极?
    "mem" {   # 濮掓稒顭堥鑽ゆ導妫颁胶绋戦悹瀣暙閵堜粙鎯?OOM-safe 婵?
      $remoteBin = "$BinDir/memory_leak_demo"
      $envPrefix = "LEAK_DELAY_SEC=5 LEAK_ALLOC_KB=4096 LEAK_INTERVAL_MS=500 " +
                   "LEAK_LOG_EVERY=20 LEAK_MAX_MB=640"
    }
     "mem_mild" {
      $remoteBin = "$BinDir/memory_leak_demo"

      # 闁烩晩鍠楅悥锝夋晬濮樺崬顔?64MB闁挎稑鏈埀顒冾唺缂嶅袙閺冨洨绐涢柍銉︾矋娣囶垶宕仦绯曞亾濠靛牊鐣遍弶鐐额嚙娴滄洖鈻旈崟顖涜嫙
      # 闂侇偆鍠撳濂告晬?MB / 0.5s = 8MB/s闁挎稑鑻崹顖滅棯?64MB 闂傚洠鍋撻悷?8s闁挎稑鐭傞崢銈夊触閸喐绾梻鈧捄銊︾暠 run_window 闁活亜顑堥幑锝夊级閵夈倗绐楁慨锝嗘缁舵繈鐛搹顐ゆ嫧
      $leakDelaySec   = 5
      $leakAllocKB    = 1024      # 1MB/婵?
      $leakIntervalMs = 500       # 0.5s/婵?
      $leakMaxMB      = 128        # 闁哄牃鍋撳鍫嗗嫮顢ラ梻鍥皺鐎?64MB
      $leakLogEvery   = 20

      $envPrefix = ("LEAK_DELAY_SEC={0} LEAK_ALLOC_KB={1} LEAK_INTERVAL_MS={2} LEAK_LOG_EVERY={3} LEAK_MAX_MB={4}" -f `
                    $leakDelaySec, $leakAllocKB, $leakIntervalMs, $leakLogEvery, $leakMaxMB)

      # 闁哄秷顫夊畵渚€宕ｉ崒娑欐濞村吋澹嗛悾璇测枖閸曨垱鑻熼柟闀愯兌閻㈠寮崼鏇燂紵闁挎稑鑻懟鐔虹磼濞嗗海顏遍柣?buffer闁挎稑濂旂换姘辨嫚娓氣偓閸ｄ即姊块崱娆戝炊闁告瑱缍€閸忔﹢鎯囩€ｎ亜鐓傞柍銉︾矊閻ｎ剟寮弶鎴犳瘓闁秆€妾ч埀?
      $allocCount      = [math]::Ceiling(($leakMaxMB * 1024.0) / $leakAllocKB)
      $leakDurationSec = ($allocCount * $leakIntervalMs) / 1000.0
      $totalLeakSec    = $leakDelaySec + $leakDurationSec   # 闁荤偛妫滈鎴濃枖閸曨垱鑻熺紓浣规尰濞碱偊寮崼鏇燂紵

      # mild 缂?20s 濞达絾鐟╅崳娲础閸愭彃璁查柨娑橆啈un_window 濠㈠爢鍕垫搐闁?40~60s 鐎归潻绠戣ぐ?
      $target = [int][math]::Ceiling($totalLeakSec + 25.0)
      if ($target -gt $script:FaultInjectMinRunSec) {
        $script:FaultInjectMinRunSec = $target
      }
    }
     "mem_moderate" {
      $remoteBin = "$BinDir/memory_leak_demo"

      # 闁烩晩鍠楅悥锝夋晬濮樺崬顔?192MB闁挎稑鏈Σ鎴﹀及閻愵剛妲?mild 濠㈠爢鍌滎伇婵℃缍囩槐婵囨媴閸℃洜鐭濋柣鎺撴构缁楀瀵煎鍨涘亾閼规壆绠?OOM
      # 闂侇偆鍠撳濂告晬?MB / 0.5s = 4MB/s闁挎稑鑻妵鍥╃棯?48s 婵炲瀚悾?
      $leakDelaySec   = 5
      $leakAllocKB    = 2048      # 2MB/婵?
      $leakIntervalMs = 500       # 0.5s/婵?
      $leakMaxMB      = 192       # 闁哄牃鍋撳鍫嗗嫮顢ラ梻鍥皺鐎?192MB
      $leakLogEvery   = 20

      $envPrefix = ("LEAK_DELAY_SEC={0} LEAK_ALLOC_KB={1} LEAK_INTERVAL_MS={2} LEAK_LOG_EVERY={3} LEAK_MAX_MB={4}" -f `
                    $leakDelaySec, $leakAllocKB, $leakIntervalMs, $leakLogEvery, $leakMaxMB)

      $allocCount      = [math]::Ceiling(($leakMaxMB * 1024.0) / $leakAllocKB)
      $leakDurationSec = ($allocCount * $leakIntervalMs) / 1000.0
      $totalLeakSec    = $leakDelaySec + $leakDurationSec

      # moderate 缂?25s 濞达絾鐟╅崳娲晬瀹€鍐惧敤 metrics 闁煎磭鏅﹢鍛村礆閹垫枼鍋撳鈧粭鍛村锤?+ 闂侇喓鍔岄崹搴ㄥ炊閻愭彃纾抽柍?
      $target = [int][math]::Ceiling($totalLeakSec + 25.0)   # 濠㈠爢鍛唺 80s 缂?run_window
      if ($target -gt $script:FaultInjectMinRunSec) {
        $script:FaultInjectMinRunSec = $target
      }
    }
      "mem_severe" {
      $remoteBin = "$BinDir/memory_leak_demo"

      # 闁烩晩鍠楅悥锝夋晬濮樺崬顔?384MB闁挎稑鑻鍗炩枖閸曨垱鑻熼柨娑樼焸閳ь剝澹堢换?OOM-safe 濞达絽妫涢弳鎰媴鎼存繄顏辨俊?
      # 闂侇偆鍠撳濂告晬?MB / 0.5s = 8MB/s闁挎稑鑻妵鍥╃棯?48s 婵炲瀚悾?
      $leakDelaySec   = 5
      $leakAllocKB    = 4096      # 4MB/婵?
      $leakIntervalMs = 500       # 0.5s/婵?
      $leakMaxMB      = 384       # 闁哄牃鍋撳鍫嗗嫮顢ラ梻鍥皺鐎?384MB
      $leakLogEvery   = 20

      $envPrefix = ("LEAK_DELAY_SEC={0} LEAK_ALLOC_KB={1} LEAK_INTERVAL_MS={2} LEAK_LOG_EVERY={3} LEAK_MAX_MB={4}" -f `
                    $leakDelaySec, $leakAllocKB, $leakIntervalMs, $leakLogEvery, $leakMaxMB)

      $allocCount      = [math]::Ceiling(($leakMaxMB * 1024.0) / $leakAllocKB)
      $leakDurationSec = ($allocCount * $leakIntervalMs) / 1000.0
      $totalLeakSec    = $leakDelaySec + $leakDurationSec

      # severe 缂?30s 濞达絾鐟╅崳娲晬鐎圭皫n_window 濞村吋鑹惧﹢?80~90s 鐎归潻绠戣ぐ?
      $target = [int][math]::Ceiling($totalLeakSec + 30.0)
      if ($target -gt $script:FaultInjectMinRunSec) {
        $script:FaultInjectMinRunSec = $target
      }
    }
    "mem_oomsafe" {
      $remoteBin = "$BinDir/memory_leak_demo"

      # OOM-safe 闁哄牃鍋撳Δ鍌浬戦妴鍌炴晬濮橆剙妫橀柡渚€顣︾粭灞剧▕鐎ｎ亜顤呭ǎ鍥ㄧ箖鐎垫梹绋夐埀顒勬嚊鏉堝墽绀夊ù锝呮椤ゅ倹寰勯弽銊у闁硅鍠楃涵鐘绘閺屻儮鍋撻悢鍝勮姵濞村吋澹嗛悾鑽も偓鐟版湰閺嗭絽鈻旈崟顖涜嫙闁哄啫鐖奸弳閬嶆晬?
      # 闁活潿鍔嬬花顒勫箰閸パ屽殼闂佹彃娲▔锔剧玻濡も偓瑜版盯鎳涢崘鑼瘜閻熸洖妫涘ú濠囧灳濠婂嫮顢ラ梻?+ 濞戞挴鍋撴繛鍫濈仛娴狀喗寰勫浣插亾濠靛牊鐣遍柛蹇嬪姀缁诲啰绮欑€ｃ劉鍋?
      $leakDelaySec   = 5
      $leakAllocKB    = 4096
      $leakIntervalMs = 500
      $leakMaxMB      = 640
      $leakLogEvery   = 20

      $envPrefix = ("LEAK_DELAY_SEC={0} LEAK_ALLOC_KB={1} LEAK_INTERVAL_MS={2} LEAK_LOG_EVERY={3} LEAK_MAX_MB={4}" -f `
                    $leakDelaySec, $leakAllocKB, $leakIntervalMs, $leakLogEvery, $leakMaxMB)

      # 濞村吋澹嗛悾璇测枖閸曨垱鑻熼柤鐗堫殕濡炲倿鏁嶅鐎峫ay + (MaxMB * 1024 / AllocKB) * (IntervalMs / 1000)
      $allocCount      = [math]::Ceiling( ($leakMaxMB * 1024.0) / $leakAllocKB )
      $leakDurationSec = ($allocCount * $leakIntervalMs) / 1000.0
      $totalLeakSec    = $leakDelaySec + $leakDurationSec

      # 闁告劕绉存慨?30s 濞达絾鐟╅崳娲晬鐏炶偐绠介悹?faultmon metrics 闁煎疇妫勯悾顒勫极鐎靛憡绠欓柛鎺撳閳ь剚绔緍ee 濞戞挸顑夊鐑藉焼閹哄秶绉靛ù锝呯Т娴犵娀鎮?闂侇喓鍔岄崹搴ㄥ箒閵忕媭妲婚柍銉︾箘濞堟垵鈻旈姀鐘哄煂
      $script:FaultInjectMinRunSec = [int][math]::Ceiling($totalLeakSec + 30.0)
    }


    # ===== 闁绘粎澧楀﹢渚€鎯?deadlock / segv 濞ｅ洦绻冪€垫梹绋夊鍛秮 =====
    "deadlock" {
      $remoteBin = "$BinDir/deadlock_demo"
      $envPrefix = "DEADLOCK_DELAY_SEC=5 DEADLOCK_SLEEP_MS=1000 DEADLOCK_MONITOR_MS=2000 DEADLOCK_ENABLE_MONITOR=1"
    }
    "segv" {
      $remoteBin = "$BinDir/crash_seg_demo"
      $envPrefix = "CRASH_DELAY_SEC=5"
    }
    # ===== Network 闁革妇鍎ゅ▍娆撴晬濮樿鲸鏆?net_fault.sh闁挎稑鐗呯粭澶嬬瑹濠靛﹦顩?iptables/tc闁?====
    "net_dns_fail" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=dns_fail NET_IFACE=$NET_IFACE NET_BAD_DNS=$NET_BAD_DNS NET_DNS_PROOF_BASE_IP=$NET_DNS_PROOF_BASE_IP NET_PING_IP=$NET_PING_IP NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 15)
    }
    "net_link_down" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=link_down NET_IFACE=$NET_IFACE NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_link_flap" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=link_flap NET_IFACE=$NET_IFACE NET_FLAP_COUNT=$NET_FLAP_COUNT NET_FLAP_DOWN_SEC=$NET_FLAP_DOWN_SEC NET_FLAP_UP_SEC=$NET_FLAP_UP_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(45, (($NET_FLAP_DOWN_SEC + $NET_FLAP_UP_SEC) * $NET_FLAP_COUNT) + 12)
    }
    "net_wlan_disconnect" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=wlan_disconnect NET_WLAN_IFACE=$NET_WLAN_IFACE NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_wifi_disconnect" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=net_wifi_disconnect NET_WLAN_IFACE=$NET_WLAN_IFACE NET_WPA_CTRL=/data/local/tmp/wpa_ctrl NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_wifi_auth_fail_wrong_psk" {
      $remoteBin = $NetFaultScriptRemote
      $authSsid = Get-CurrentWlanSsid -SN $SN -Iface $NET_WLAN_IFACE -WpaCtrl "/data/local/tmp/wpa_ctrl"
      if ([string]::IsNullOrWhiteSpace($authSsid)) {
        throw "WIFI_AUTH_RECOVERY_NEEDS_USER_INPUT: net_wifi_auth_fail_wrong_psk could not read the current saved SSID from wpa_cli"
      }
      $authSsidEnv = Quote-RemoteShellEnvValue $authSsid
      $envPrefix = "NET_MODE=net_wifi_auth_fail_wrong_psk NET_WLAN_IFACE=$NET_WLAN_IFACE NET_WPA_CTRL=/data/local/tmp/wpa_ctrl NET_WPA_CONF=/data/local/tmp/wpa.conf NET_WLAN_SSID=$authSsidEnv NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(40, $NET_LINK_DOWN_HOLD_SEC + 15)
    }
    "net_no_default_route" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=net_no_default_route NET_WLAN_IFACE=$NET_WLAN_IFACE NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_no_ipv4_on_iface" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=net_no_ipv4_on_iface NET_WLAN_IFACE=$NET_WLAN_IFACE NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_wrong_default_route" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=net_wrong_default_route NET_WLAN_IFACE=$NET_WLAN_IFACE NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_gateway_unreachable" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=net_gateway_unreachable NET_WLAN_IFACE=$NET_WLAN_IFACE NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    "net_public_ip_unreachable" {
      $remoteBin = $NetFaultScriptRemote
      $envPrefix = "NET_MODE=net_public_ip_unreachable NET_WLAN_IFACE=$NET_WLAN_IFACE NET_TARGET_IP=$NET_TARGET_IP NET_HOLD_SEC=$NET_LINK_DOWN_HOLD_SEC"
      $script:FaultInjectMinRunSec = [int][math]::Max(30, $NET_LINK_DOWN_HOLD_SEC + 10)
    }
    default {
      Write-Host ("[FAULT] Unknown fault injection type: {0}" -f $Type) -ForegroundColor Yellow
      return 0
    }
  }

  if (-not $remoteBin -or $remoteBin -eq "") {
    Write-Host "[FAULT] remote binary path is empty" -ForegroundColor Yellow
    return 0
  }

    # 闁告艾楠歌ぐ鎾触椤栨艾袟 + echo $! 闁?PID
  $remoteCmd = ("{0} {1} >/data/local/tmp/fault_{2}.log 2>&1 & echo `$!" -f $envPrefix, $remoteBin, $Type)
  $HdcArgs = @("-t", $SN, "shell", $remoteCmd)

  Write-Host ("[FAULT] Start {0}: hdc {1}" -f $Type, ($HdcArgs -join " "))
  $out = & hdc @HdcArgs

  # 婵炲鍔嶉崜浼存晬濮橆偆鐟濋悷鏇氳兌閺?$procId闁挎稑鐭傛导鈺呭礂瀹ュ懏瀚?PowerShell 闁告劕鎳愰悿?$procId 闁告劘灏欓悰?
  $faultPidLocal = 0
  foreach ($line in $out) {
    $trim = $line.Trim()
    if ($trim -match '^\d+$') {
      [void][int]::TryParse($trim, [ref]$faultPidLocal)
      break
    }
  }

  if ($faultPidLocal -gt 0) {
    Write-Host ("[FAULT] Started type={0}, pid={1}" -f $Type, $faultPidLocal)
  } else {
    Write-Host "[FAULT] WARN: failed to get remote PID, process may not have started" -ForegroundColor Yellow
  }

  return $faultPidLocal
}


function Stop-FaultInject {
  param(
    [int]$faultPidLocal,
    [string]$SN,
    [string]$Type = ""
  )

  # net_wifi_disconnect: read PID file first; use it if faultPidLocal was not captured
  if ($Type -eq "net_wifi_disconnect") {
    $pidFileRaw = (@(hdc -t $SN shell "cat /data/local/tmp/net_fault_state/injector_net_wifi_disconnect.pid 2>/dev/null || true" 2>$null) -join '').Trim()
    $pidFromFile = 0
    if ($pidFileRaw -match '^\d+$') { $pidFromFile = [int]$pidFileRaw }
    if ($pidFromFile -gt 0) {
      if ($faultPidLocal -le 0) {
        Write-Host ("[FAULT][wifi_disconnect] faultPidLocal=0; using PID from file: {0}" -f $pidFromFile) -ForegroundColor Cyan
        $faultPidLocal = $pidFromFile
      } else {
        Write-Host ("[FAULT][wifi_disconnect] TERM injector from PID file: pid={0}" -f $pidFromFile) -ForegroundColor Cyan
        hdc -t $SN shell ("kill -TERM {0} >/dev/null 2>&1 || true" -f $pidFromFile) 2>$null | Out-Null
      }
    }
  }

  # net_wifi_auth_fail_wrong_psk: read PID file first; use it if faultPidLocal was not captured
  if ($Type -eq "net_wifi_auth_fail_wrong_psk") {
    $pidFileRaw = (@(hdc -t $SN shell "cat /data/local/tmp/net_fault_state/injector_net_wifi_auth_fail_wrong_psk.pid 2>/dev/null || true" 2>$null) -join '').Trim()
    $pidFromFile = 0
    if ($pidFileRaw -match '^\d+$') { $pidFromFile = [int]$pidFileRaw }
    if ($pidFromFile -gt 0) {
      if ($faultPidLocal -le 0) {
        Write-Host ("[FAULT][auth_fail] faultPidLocal=0; using PID from file: {0}" -f $pidFromFile) -ForegroundColor Cyan
        $faultPidLocal = $pidFromFile
      } else {
        Write-Host ("[FAULT][auth_fail] TERM injector from PID file: pid={0}" -f $pidFromFile) -ForegroundColor Cyan
        hdc -t $SN shell ("kill -TERM {0} >/dev/null 2>&1 || true" -f $pidFromFile) 2>$null | Out-Null
      }
    }
  }

  if ($faultPidLocal -le 0) {
    Write-Host "[FAULT] No valid PID to stop (maybe process already exited)"
    # For net_wifi_disconnect / net_wifi_auth_fail_wrong_psk, still run ps-based cleanup
    if ($Type -eq "net_wifi_disconnect" -or $Type -eq "net_wifi_auth_fail_wrong_psk") {
      $lines2 = @(hdc -t $SN shell "ps -A 2>/dev/null | grep net_fault.sh 2>/dev/null || true" 2>$null)
      foreach ($ln2 in $lines2) {
        $t2 = ([string]$ln2).Trim(); if ($t2 -eq "") { continue }
        $c2 = $t2 -split "\s+"
        if ($c2.Count -ge 1 -and $c2[0] -match '^\d+$') {
          Write-Host ("[FAULT][{0}] fallback TERM residual pid={1}" -f $Type, $c2[0]) -ForegroundColor Yellow
          hdc -t $SN shell ("kill -TERM {0} >/dev/null 2>&1 || true" -f $c2[0]) 2>$null | Out-Null
        }
      }
      Start-Sleep -Seconds 3
      $res2 = (@(hdc -t $SN shell "ps -A 2>/dev/null | grep net_fault.sh 2>/dev/null || true" 2>$null) -join '').Trim()
      $script:WifiInjectorStopOk = ($res2 -eq '' -or $res2 -notmatch '\S')
      Write-Host ("[{0}] injector_stop_ok={1} (fallback path)" -f $Type, $script:WifiInjectorStopOk) -ForegroundColor Cyan
    }
    return
  }

  Write-Host ("[FAULT] Stop pid={0} (type={1})" -f $faultPidLocal, $Type)

  # 1) TERM first
  hdc -t $SN shell ("kill -TERM {0} >/dev/null 2>&1 || true" -f $faultPidLocal) 2>$null | Out-Null

  # 2) wait for exit; net_wifi_disconnect gets 25s, net_wifi_auth_fail_wrong_psk gets 60s (restore_all wpa wait up to 45s + ~10s overhead)
  $maxWaitSec = if ($Type -eq "net_wifi_disconnect") { 25 } elseif ($Type -eq "net_wifi_auth_fail_wrong_psk") { 60 } else { 8 }
  $alive = $true
  for ($i = 0; $i -lt $maxWaitSec; $i++) {
    $chkCmd = ('ps -A 2>/dev/null | grep -m 1 "^[[:space:]]*{0}[[:space:]]" 2>/dev/null || true' -f $faultPidLocal)
    $chk = @(hdc -t $SN shell $chkCmd 2>$null) | Select-Object -First 1
    if (-not $chk -or $chk.Trim() -eq "") { $alive = $false; break }
    Start-Sleep -Seconds 1
  }

  # 3) still alive -> KILL
  if ($alive) {
    Write-Host ("[FAULT] pid still alive after TERM, send KILL: {0}" -f $faultPidLocal) -ForegroundColor Yellow
    hdc -t $SN shell ("kill -KILL {0} >/dev/null 2>&1 || true" -f $faultPidLocal) 2>$null | Out-Null
    Start-Sleep -Seconds 1
  }

  # 4) net_* extra cleanup: avoid residual net_fault.sh after run
  if ($Type -like "net_*") {
    $lines = @(hdc -t $SN shell "ps -A 2>/dev/null | grep net_fault.sh 2>/dev/null || true" 2>$null)
    $pids = @()
    foreach ($ln in $lines) {
      $t = [string]$ln
      if (-not $t) { continue }
      $t = $t.Trim()
      if ($t -eq "") { continue }
      $cols = $t -split "\s+"
      if ($cols.Count -ge 1 -and $cols[0] -match '^\d+$') { $pids += [int]$cols[0] }
    }
    $pids = $pids | Sort-Object -Unique
    foreach ($p in $pids) {
      Write-Host ("[FAULT] extra TERM net_fault.sh pid={0}" -f $p) -ForegroundColor DarkYellow
      hdc -t $SN shell ("kill -TERM {0} >/dev/null 2>&1 || true" -f $p) 2>$null | Out-Null
    }
$still = @($pids)
for ($i = 0; $i -lt 12 -and $still.Count -gt 0; $i++) {
  Start-Sleep -Seconds 1
  $aliveNow = @()
  foreach ($p in $still) {
    $chkCmd = ('ps -A 2>/dev/null | grep -m 1 "^[[:space:]]*{0}[[:space:]]" 2>/dev/null || true' -f $p)
    $chk = @(hdc -t $SN shell $chkCmd 2>$null) | Select-Object -First 1
    if ($chk -and $chk.Trim() -ne "") { $aliveNow += $p }
  }
  $still = $aliveNow
}
foreach ($p in $still) {
  hdc -t $SN shell ("kill -KILL {0} >/dev/null 2>&1 || true" -f $p) 2>$null | Out-Null
}

    # net_wifi_disconnect / net_wifi_auth_fail_wrong_psk: final residual check and injector_stop_ok tracking
    if ($Type -eq "net_wifi_disconnect" -or $Type -eq "net_wifi_auth_fail_wrong_psk") {
      Start-Sleep -Seconds 1
      # Primary: subtype-specific PID file. If the file is gone, restore_all completed and the injector is no longer alive.
      $pidFileRaw2 = (@(hdc -t $SN shell ("cat /data/local/tmp/net_fault_state/injector_{0}.pid 2>/dev/null || true" -f $Type) 2>$null) -join '').Trim()
      $pidFromFile2 = if ($pidFileRaw2 -match '^\d+$') { [int]$pidFileRaw2 } else { 0 }
      $injectorAlive = $false
      if ($pidFromFile2 -gt 0) {
        $aliveChk = (@(hdc -t $SN shell ("ps -A 2>/dev/null | grep -m 1 '^[[:space:]]*{0}[[:space:]]' 2>/dev/null || true" -f $pidFromFile2) 2>$null) -join '').Trim()
        $injectorAlive = ($aliveChk -ne '')
      }
      # Secondary: full-path grep, re-confirmed after 2s to skip transient fork-before-exec match.
      $residualOut = (@(hdc -t $SN shell "ps -A 2>/dev/null | grep '/data/local/tmp/net_fault.sh' 2>/dev/null || true" 2>$null) -join '').Trim()
      if ($residualOut -ne '') {
        Start-Sleep -Seconds 2
        $residualOut = (@(hdc -t $SN shell "ps -A 2>/dev/null | grep '/data/local/tmp/net_fault.sh' 2>/dev/null || true" 2>$null) -join '').Trim()
      }
      $noResidual = ($residualOut -eq '' -or $residualOut -notmatch '\S')
      $script:WifiInjectorStopOk = ((-not $injectorAlive) -and $noResidual)
      Write-Host ("[{0}] injector_stop_ok={1} pid_file={2} pid_alive={3} residual='{4}'" -f $Type, $script:WifiInjectorStopOk, $pidFromFile2, $injectorAlive, $residualOut) -ForegroundColor Cyan
    }
  }
}


# ----- net_wifi_disconnect recovery gate -----
# Polls until wpa=COMPLETED + IPv4 + default route + main public probe (max TimeoutSec).
# Returns pscustomobject { ok; reason }.
function Wait-WifiRecovery {
  param(
    [string]$SN,
    [string]$Iface = "wlan0",
    [string]$WpaCtrl = "/data/local/tmp/wpa_ctrl",
    [int]$TimeoutSec = 30,
    [switch]$CheckDns
  )
  $gatePingIp = if ($script:NET_PING_IP -and $script:NET_PING_IP -ne "") {
    $script:NET_PING_IP
  } elseif ($env:WK_NET_PING_IP -and $env:WK_NET_PING_IP -ne "") {
    $env:WK_NET_PING_IP
  } elseif ($env:NET_PING_IP -and $env:NET_PING_IP -ne "") {
    $env:NET_PING_IP
  } else {
    "8.8.8.8"
  }
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $reason = "timeout"
  while ((Get-Date) -lt $deadline) {
    # Must use -p $WpaCtrl — default socket path not present on this device
    $wpaOut  = (@(hdc -t $SN shell ("wpa_cli -p {0} -i {1} status 2>/dev/null || true" -f $WpaCtrl, $Iface) 2>$null) -join "`n").Trim()
    $ipOut   = (@(hdc -t $SN shell ("ifconfig {0} 2>/dev/null | grep 'inet' || true" -f $Iface) 2>$null) -join '').Trim()
    $rtOut   = (@(hdc -t $SN shell ("grep -c '^{0}[[:space:]]*00000000' /proc/net/route 2>/dev/null || echo 0" -f $Iface) 2>$null) -join '').Trim()
    $pingOut = (@(hdc -t $SN shell ("ping -c 1 -W 3 {0} 2>/dev/null | grep '1 received' || true" -f $gatePingIp) 2>$null) -join '').Trim()
    $dnsOut  = if ($CheckDns) { (@(hdc -t $SN shell "ping -c 1 -W 5 www.baidu.com 2>/dev/null | grep '1 received' || true" 2>$null) -join '').Trim() } else { "skip" }

    Write-Host ("[wifi_gate] gate_check: wpa={0} ip={1} rt={2} ping={3} ping_ip={4} dns={5}" -f
      (([regex]::Match($wpaOut, '(?m)^wpa_state=(\S+)')).Groups[1].Value), (($ipOut -match 'inet\s+(addr:)?\d+\.\d+\.\d+\.\d+\b') -or ($wpaOut -match '(?m)^ip_address=\d+\.\d+\.\d+\.\d+\s*$')), ($rtOut -match '^[1-9]'), ($pingOut -ne ''), $gatePingIp, ($dnsOut -ne '')) -ForegroundColor DarkCyan

    if ($wpaOut -notmatch 'wpa_state=COMPLETED') { $reason = "wpa_not_completed"; Start-Sleep -Seconds 3; continue }
    if (($ipOut -notmatch 'inet\s+(addr:)?\d+\.\d+\.\d+\.\d+\b') -and ($wpaOut -notmatch '(?m)^ip_address=\d+\.\d+\.\d+\.\d+\s*$')) { $reason = "no_ipv4"; Start-Sleep -Seconds 3; continue }
    if ($rtOut -notmatch '^[1-9]')                { $reason = "no_default_route";  Start-Sleep -Seconds 3; continue }
    if ($pingOut -eq '')                           { $reason = "ping_ip_fail";      Start-Sleep -Seconds 3; continue }
    if ($CheckDns -and $dnsOut -eq '')             { $reason = "ping_dns_fail";     Start-Sleep -Seconds 3; continue }

    Write-Host ("[wifi_gate] recovery_gate PASS") -ForegroundColor Green
    return [pscustomobject]@{ ok = $true; reason = "all_conditions_met" }
  }
  Write-Host ("[wifi_gate] recovery_gate TIMEOUT after ${TimeoutSec}s last_reason=${reason}") -ForegroundColor Yellow
  return [pscustomobject]@{ ok = $false; reason = $reason }
}

function Get-NoIpv4RecoveryStuckState {
  param(
    [string]$SN,
    [string]$Iface = "wlan0",
    [string]$Gateway = "",
    [string]$ExpectedIp = "",
    [string]$WpaCtrl = "/data/local/tmp/wpa_ctrl"
  )

  $wpaOut = (@(hdc -t $SN shell ("wpa_cli -p {0} -i {1} status 2>/dev/null || true" -f $WpaCtrl, $Iface) 2>$null) -join "`n").Trim()
  $ipOut  = (@(hdc -t $SN shell ("ifconfig {0} 2>/dev/null | grep 'inet' || true" -f $Iface) 2>$null) -join '').Trim()
  $rtOut  = (@(hdc -t $SN shell ("grep -c '^{0}[[:space:]]*00000000' /proc/net/route 2>/dev/null || echo 0" -f $Iface) 2>$null) -join '').Trim()
  $arpOut = if ([string]::IsNullOrWhiteSpace($Gateway)) { "" } else { (@(hdc -t $SN shell ("cat /proc/net/arp 2>/dev/null | grep '^{0}[[:space:]]' || true" -f $Gateway) 2>$null) -join "`n").Trim() }
  $gwPing = if ([string]::IsNullOrWhiteSpace($Gateway)) { "" } else { (@(hdc -t $SN shell ("ping -c 1 -W 3 {0} 2>/dev/null | grep '1 received' || true" -f $Gateway) 2>$null) -join '').Trim() }

  $wpaCompleted = ($wpaOut -match 'wpa_state=COMPLETED')
  $hasExpectedIp = (-not [string]::IsNullOrWhiteSpace($ExpectedIp)) -and (($ipOut -match [regex]::Escape($ExpectedIp)) -or ($wpaOut -match ("(?m)^ip_address={0}\s*$" -f [regex]::Escape($ExpectedIp))))
  $hasRoute = ($rtOut -match '^[1-9]')
  $validArp = (-not [string]::IsNullOrWhiteSpace($Gateway)) -and ($arpOut -match [regex]::Escape($Gateway) -and $arpOut -notmatch '00:00:00:00:00:00' -and $arpOut -notmatch '(?i)incomplete')
  $gatewayPingFail = ($gwPing -eq '')
  $stuck = ($wpaCompleted -and $hasExpectedIp -and $hasRoute -and $validArp -and $gatewayPingFail)

  return [pscustomobject]@{
    stuck = $stuck
    wpa_completed = $wpaCompleted
    has_expected_ip = $hasExpectedIp
    has_default_route = $hasRoute
    arp_valid = $validArp
    gateway_ping_fail = $gatewayPingFail
    arp = $arpOut
  }
}

function Get-NoIpv4RecoveryContext {
  param(
    [string]$SN,
    [string]$Iface = "wlan0"
  )

  $routeBak = @(hdc -t $SN shell "cat /data/local/tmp/net_fault_state/route.bak 2>/dev/null || true" 2>$null)
  $ipBak = @(hdc -t $SN shell "cat /data/local/tmp/net_fault_state/wlan_ip.bak 2>/dev/null || true" 2>$null)
  $gwLine = $routeBak | Where-Object { $_ -match '^GW=' } | Select-Object -First 1
  $ipLine = $ipBak | Where-Object { $_ -match '^IP=' } | Select-Object -First 1
  $maskLine = $ipBak | Where-Object { $_ -match '^MASK=' } | Select-Object -First 1

  $gw = if ($gwLine) { ($gwLine -replace '^GW=','').Trim() } else { "" }
  $ip = if ($ipLine) { ($ipLine -replace '^IP=','').Trim() } else { "" }
  $mask = if ($maskLine) { ($maskLine -replace '^MASK=','').Trim() } else { "" }

  if ([string]::IsNullOrWhiteSpace($gw) -or $gw -eq "0.0.0.0") {
    $routeText = (@(hdc -t $SN shell "cat /proc/net/route 2>/dev/null || true" 2>$null) -join "`n")
    foreach ($line in ($routeText -split "`r?`n")) {
      if ($line -notmatch ("^{0}\s+00000000\s+" -f [regex]::Escape($Iface))) { continue }
      $parts = $line -split '\s+'
      if ($parts.Count -lt 3 -or $parts[2] -notmatch '^[0-9A-Fa-f]{8}$') { continue }
      $hex = $parts[2]
      $gw = ("{0}.{1}.{2}.{3}" -f ([Convert]::ToInt32($hex.Substring(6,2),16)),([Convert]::ToInt32($hex.Substring(4,2),16)),([Convert]::ToInt32($hex.Substring(2,2),16)),([Convert]::ToInt32($hex.Substring(0,2),16)))
      break
    }
  }

  return [pscustomobject]@{
    gateway = $gw
    ip = $ip
    mask = $mask
    route_bak = ($routeBak -join "`n")
    ip_bak = ($ipBak -join "`n")
  }
}

function Invoke-NoIpv4IpRefresh {
  param(
    [string]$SN,
    [string]$Iface = "wlan0",
    [string]$PrimaryIp,
    [string]$TempIp,
    [string]$Gateway
  )

  if ([string]::IsNullOrWhiteSpace($PrimaryIp) -or [string]::IsNullOrWhiteSpace($TempIp) -or [string]::IsNullOrWhiteSpace($Gateway)) {
    return [pscustomobject]@{ ok = $false; reason = "ip_refresh_missing_dynamic_context"; final_gate = $null }
  }

  $routeAdd = "if [ -x /data/local/tmp/busybox ]; then /data/local/tmp/busybox route add default gw {0} dev {1} 2>/dev/null || true; elif [ -x /data/busybox ]; then /data/busybox route add default gw {0} dev {1} 2>/dev/null || true; else route add default gw {0} dev {1} 2>/dev/null || true; fi"
  Write-Host ("[no_ipv4] IP refresh: switch {0} -> {1}" -f $PrimaryIp, $TempIp) -ForegroundColor Cyan
  hdc -t $SN shell ("ifconfig {0} {1} netmask 255.255.255.0 up 2>/dev/null || true" -f $Iface, $TempIp) 2>$null | Out-Null
  hdc -t $SN shell ($routeAdd -f $Gateway, $Iface) 2>$null | Out-Null
  Start-Sleep -Seconds 2
  $tempGate = Wait-WifiRecovery -SN $SN -Iface $Iface -TimeoutSec 18 -CheckDns

  Write-Host ("[no_ipv4] IP refresh: switch {0} -> {1}" -f $TempIp, $PrimaryIp) -ForegroundColor Cyan
  hdc -t $SN shell ("ifconfig {0} {1} netmask 255.255.255.0 up 2>/dev/null || true" -f $Iface, $PrimaryIp) 2>$null | Out-Null
  hdc -t $SN shell ($routeAdd -f $Gateway, $Iface) 2>$null | Out-Null
  Start-Sleep -Seconds 2
  $finalGate = Wait-WifiRecovery -SN $SN -Iface $Iface -TimeoutSec 24 -CheckDns

  $reason = if ($tempGate.ok -and $finalGate.ok) {
    "ip_refresh_restored_guest_forwarding"
  } elseif (-not $tempGate.ok) {
    "temp_ip_gate_failed:" + $tempGate.reason
  } else {
    "primary_ip_gate_failed:" + $finalGate.reason
  }
  return [pscustomobject]@{ ok = ($tempGate.ok -and $finalGate.ok); reason = $reason; final_gate = $finalGate }
}

function Ensure-NoIpv4DefaultRoute {
  param(
    [string]$SN,
    [string]$Iface = "wlan0",
    [string]$Gateway = "",
    [string]$Tag = "no_ipv4"
  )

  $routeText = (@(hdc -t $SN shell "cat /proc/net/route 2>/dev/null || true" 2>$null) -join "`n")
  if ($routeText -match ("(?m)^{0}\s+00000000\s+" -f [regex]::Escape($Iface))) {
    return [pscustomobject]@{ ok = $true; action = "already_present"; tag = $Tag }
  }

  if ([string]::IsNullOrWhiteSpace($Gateway) -or $Gateway -eq "0.0.0.0") {
    $ctx = Get-NoIpv4RecoveryContext -SN $SN -Iface $Iface
    $Gateway = $ctx.gateway
  }
  if ([string]::IsNullOrWhiteSpace($Gateway) -or $Gateway -eq "0.0.0.0") {
    Write-Host ("[no_ipv4] default route missing at {0}; no dynamic gateway available for {1}" -f $Tag, $Iface) -ForegroundColor Yellow
    return [pscustomobject]@{ ok = $false; action = "missing_dynamic_gateway"; tag = $Tag }
  }

  Write-Host ("[no_ipv4] default route missing at {0}; re-add gw={1} dev={2}" -f $Tag, $Gateway, $Iface) -ForegroundColor Yellow
  $routeAdd = "if [ -x /data/local/tmp/busybox ]; then /data/local/tmp/busybox route add default gw {0} dev {1} 2>/dev/null || true; elif [ -x /data/busybox ]; then /data/busybox route add default gw {0} dev {1} 2>/dev/null || true; else route add default gw {0} dev {1} 2>/dev/null || true; fi" -f $Gateway, $Iface
  hdc -t $SN shell $routeAdd 2>$null | Out-Null
  Start-Sleep -Seconds 2

  $routeText2 = (@(hdc -t $SN shell "cat /proc/net/route 2>/dev/null || true" 2>$null) -join "`n")
  $ok = ($routeText2 -match ("(?m)^{0}\s+00000000\s+" -f [regex]::Escape($Iface)))
  Write-Host ("[no_ipv4] default_route_restore tag={0} ok={1}" -f $Tag, $ok) -ForegroundColor Cyan
  return [pscustomobject]@{ ok = $ok; action = "readd"; tag = $Tag }
}

function Convert-RouteGatewayHexToIpv4 {
  param([string]$Hex)
  if ([string]::IsNullOrWhiteSpace($Hex) -or $Hex -notmatch '^[0-9A-Fa-f]{8}$') { return "" }
  return ("{0}.{1}.{2}.{3}" -f
    ([Convert]::ToInt32($Hex.Substring(6,2),16)),
    ([Convert]::ToInt32($Hex.Substring(4,2),16)),
    ([Convert]::ToInt32($Hex.Substring(2,2),16)),
    ([Convert]::ToInt32($Hex.Substring(0,2),16)))
}

function Get-DefaultRouteEntriesFromText {
  param(
    [string]$Text,
    [string]$Iface = "wlan0"
  )

  $ret = New-Object System.Collections.Generic.List[object]
  if ([string]::IsNullOrWhiteSpace($Text) -or [string]::IsNullOrWhiteSpace($Iface)) { return @() }
  foreach ($line in ($Text -split "`r?`n")) {
    $t = ([string]$line).Trim()
    if ([string]::IsNullOrWhiteSpace($t) -or $t -match '^Iface\s+') { continue }
    $parts = $t -split '\s+'
    if ($parts.Count -lt 8) { continue }
    if ($parts[0] -ne $Iface -or $parts[1] -ne "00000000" -or $parts[2] -notmatch '^[0-9A-Fa-f]{8}$') { continue }
    [void]$ret.Add([pscustomobject]@{
      iface = $parts[0]
      gateway_hex = $parts[2].ToUpperInvariant()
      gateway = Convert-RouteGatewayHexToIpv4 $parts[2]
      metric = $parts[6]
      raw = $line
    })
  }
  return [object[]]$ret.ToArray()
}

function Get-HostDefaultRouteContext {
  param(
    [string]$SN,
    [string]$Iface = "wlan0",
    [string]$Tag = "pre"
  )

  $routeText = (@(hdc -t $SN shell "cat /proc/net/route 2>/dev/null || true" 2>$null) -join "`n")
  $entries = @(Get-DefaultRouteEntriesFromText -Text $routeText -Iface $Iface)
  $entry = if ($entries.Count -gt 0) { $entries[0] } else { $null }
  if ($null -eq $entry -or [string]::IsNullOrWhiteSpace([string]$entry.gateway) -or [string]$entry.gateway -eq "0.0.0.0") {
    return [pscustomobject]@{
      ok = $false
      reason = "pre_default_route_missing"
      tag = $Tag
      iface = $Iface
      gateway = ""
      gateway_hex = ""
      metric = ""
      raw_route = $routeText
    }
  }
  return [pscustomobject]@{
    ok = $true
    reason = "pre_default_route_captured"
    tag = $Tag
    iface = [string]$entry.iface
    gateway = [string]$entry.gateway
    gateway_hex = [string]$entry.gateway_hex
    metric = [string]$entry.metric
    raw_route = $routeText
  }
}

function Invoke-RouteFaultRestoreCommand {
  param(
    [string]$SN,
    [string]$RouteArgs,
    [string]$OutPath,
    [string]$Tag
  )

  if ([string]::IsNullOrWhiteSpace($RouteArgs)) { return }
  $cmd = "if [ -x /data/local/tmp/busybox ]; then /data/local/tmp/busybox route {0} 2>&1 || true; elif [ -x /data/busybox ]; then /data/busybox route {0} 2>&1 || true; else route {0} 2>&1 || true; fi" -f $RouteArgs
  Add-Content -Path $OutPath -Value ("### ROUTE_RESTORE_CMD tag={0} args={1}" -f $Tag, $RouteArgs) -Encoding UTF8
  Append-RemoteOut $SN $cmd $OutPath ("route.restore.{0}" -f $Tag)
}

function Ensure-RouteFaultDefaultRouteRestore {
  param(
    [string]$SN,
    [string]$RunDir,
    [string]$Phase,
    $PreRoute
  )

  $outDir = Join-Path $RunDir "net"
  Ensure-Dir $outDir
  $outPath = Join-Path $outDir ("route_restore_{0}.txt" -f $Phase)
  "" | Out-File -FilePath $outPath -Encoding UTF8 -Force
  Add-Content -Path $outPath -Value ("### ROUTE_RESTORE_VERIFY_BEGIN phase={0}" -f $Phase) -Encoding UTF8
  Add-Content -Path $outPath -Value ((Get-Date).ToString("yyyy-MM-dd HH:mm:ss")) -Encoding UTF8

  if ($null -eq $PreRoute -or -not [bool]$PreRoute.ok) {
    $reason = if ($null -ne $PreRoute -and $PreRoute.reason) { [string]$PreRoute.reason } else { "pre_route_context_missing" }
    Add-Content -Path $outPath -Value ("ROUTE_RESTORE_RESULT phase={0} ok=0 reason={1}" -f $Phase, $reason) -Encoding UTF8
    Add-Content -Path $outPath -Value ("### ROUTE_RESTORE_VERIFY_END phase={0}" -f $Phase) -Encoding UTF8
    return [pscustomobject]@{ phase = $Phase; ok = $false; reason = $reason; iface = ""; gateway = ""; gateway_hex = ""; metric = "" }
  }

  $iface = [string]$PreRoute.iface
  $gw = [string]$PreRoute.gateway
  $gwHex = [string]$PreRoute.gateway_hex
  $metric = [string]$PreRoute.metric
  Add-Content -Path $outPath -Value ("PRE_ROUTE iface={0} gateway={1} gateway_hex={2} metric={3}" -f $iface, $gw, $gwHex, $metric) -Encoding UTF8

  $ok = $false
  $reason = "not_verified"
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    Add-Content -Path $outPath -Value ("### ROUTE_RESTORE_ATTEMPT phase={0} attempt={1}" -f $Phase, $attempt) -Encoding UTF8
    $routeBefore = (@(hdc -t $SN shell "cat /proc/net/route 2>/dev/null || true" 2>$null) -join "`n")
    Add-Content -Path $outPath -Value "### /proc/net/route before" -Encoding UTF8
    Add-Content -Path $outPath -Value $routeBefore -Encoding UTF8
    $entriesBefore = @(Get-DefaultRouteEntriesFromText -Text $routeBefore -Iface $iface)

    foreach ($entry in $entriesBefore) {
      if ([string]$entry.gateway_hex -eq $gwHex) { continue }
      if (-not [string]::IsNullOrWhiteSpace([string]$entry.gateway) -and [string]$entry.gateway -ne "0.0.0.0") {
        Invoke-RouteFaultRestoreCommand -SN $SN -RouteArgs ("del default gw {0} dev {1}" -f ([string]$entry.gateway), $iface) -OutPath $outPath -Tag ("{0}.del_bad.{1}" -f $Phase, $attempt)
      }
    }

    $hasPreRoute = $false
    foreach ($entry in $entriesBefore) {
      if ([string]$entry.gateway_hex -eq $gwHex) { $hasPreRoute = $true; break }
    }
    if (-not $hasPreRoute) {
      Invoke-RouteFaultRestoreCommand -SN $SN -RouteArgs ("add default gw {0} dev {1}" -f $gw, $iface) -OutPath $outPath -Tag ("{0}.add_pre.{1}" -f $Phase, $attempt)
    }

    Start-Sleep -Seconds 2
    $routeAfter = (@(hdc -t $SN shell "cat /proc/net/route 2>/dev/null || true" 2>$null) -join "`n")
    Add-Content -Path $outPath -Value "### /proc/net/route after" -Encoding UTF8
    Add-Content -Path $outPath -Value $routeAfter -Encoding UTF8
    $entriesAfter = @(Get-DefaultRouteEntriesFromText -Text $routeAfter -Iface $iface)
    $hasPreAfter = $false
    $badAfter = @()
    foreach ($entry in $entriesAfter) {
      if ([string]$entry.gateway_hex -eq $gwHex) { $hasPreAfter = $true } else { $badAfter += [string]$entry.gateway }
    }
    if ($hasPreAfter -and $badAfter.Count -eq 0) {
      $ok = $true
      $reason = "pre_gateway_restored"
      break
    }
    $reason = if (-not $hasPreAfter) { "pre_gateway_missing_after_restore" } else { "bad_default_route_still_present:" + ($badAfter -join ",") }
  }

  Add-Content -Path $outPath -Value ("ROUTE_RESTORE_RESULT phase={0} ok={1} reason={2} iface={3} gateway={4} gateway_hex={5}" -f $Phase, [int]$ok, $reason, $iface, $gw, $gwHex) -Encoding UTF8
  Add-Content -Path $outPath -Value ("### ROUTE_RESTORE_VERIFY_END phase={0}" -f $Phase) -Encoding UTF8
  Write-Host ("[route_restore] phase={0} ok={1} reason={2} iface={3} gw={4}" -f $Phase, $ok, $reason, $iface, $gw) -ForegroundColor Cyan
  return [pscustomobject]@{ phase = $Phase; ok = $ok; reason = $reason; iface = $iface; gateway = $gw; gateway_hex = $gwHex; metric = $metric }
}

# ----- remote faultlog listing & diff (robust) -----
function Get-RemoteFileList {
  param([string]$SN,[string]$Root)

  if ([string]::IsNullOrWhiteSpace($Root)) { return @() }
  if ($Root.EndsWith("/")) { $Root = $Root.TrimEnd("/") }

  $out = hdc -t $SN shell ("ls -1R " + $Root + " 2>/dev/null")

  $list = @()
  $cur  = $null
  foreach ($ln in $out) {
    if ([string]::IsNullOrWhiteSpace($ln)) { continue }
    $t = $ln.Trim()
    if ($t -like "total *") { continue }
    if ($t -match '^(ls:|cannot access|No such file)') { continue }
    if ($t -eq "." -or $t -eq "..") { continue }

    # 闁烩晩鍠栫紞宥嗗緞鏉堝墽绀勭憸鑸灩椤?/path/to/dir:闁?
    if ($t.EndsWith(":")) {
      $cur = $t.TrimEnd(":")
      continue
    }

    # 閻犱緤绱曢悾濠氭儎缁嬫鍤犻悹渚灠缁剁偤鏁嶉崼婵堢Ъ $cur 濞戞捁娅ｉ埞鏍籍鐠佸湱绀夐柣鈺佺摠鐢挳鎮介妸锔界€ù鐘烘硾閹洟鏁?
    $rel = ""
    if ($cur) {
      $tmp = $cur
      if ($tmp.ToLower().StartsWith($Root.ToLower())) {
        $tmp = $tmp.Substring($Root.Length).TrimStart("/")
      }
      $rel = $tmp
    }

    $relPath = $t
    if (-not [string]::IsNullOrEmpty($rel)) {
      try {
        $relPath = (Join-Path $rel $t)
      } catch {
        # 闂侇剙鐏濋崢?Join-Path 闁?Path 濞戞捁娅ｉ埞鏍籍閼搁潧袚闂佹寧鐟辩槐閬嶆焻閳ь剟宕犻弽锕佺闁瑰嘲鍚嬬敮?
        $relPath = ($rel.TrimEnd("/") + "/" + $t)
      }
    }

    $list += $relPath.Replace("\","/")
  }

  return $list
}
function Normalize-AppList {
  param([object]$List)

  $out = New-Object System.Collections.Generic.List[string]
  if ($null -eq $List) { return @() }

  foreach ($x in $List) {
    if ($null -eq $x) { continue }
    $s = ([string]$x).Trim()
    if ($s -eq "") { continue }

    # 闁稿繋娴囬蹇旀綇閹惧啿寮抽梺鎻掓湰濠€浣圭閸濆嫬鏅搁柟?"a,b" / "a; b" / "a b"
    foreach ($t in ($s -split '[,; ]+')) {
      $v = $t.Trim()
      if ($v -ne "") { [void]$out.Add($v) }
    }
  }

  # 闁告ê顭烽崳鎼佹晬閸粎绠介幖鏉戦獜缁?
  $seen = @{}
  $uniq = @()
  foreach ($v in $out) {
    if (-not $seen.ContainsKey($v)) { $seen[$v] = $true; $uniq += $v }
  }
  return $uniq
}


# ----- quick pattern counting -----
function Count-Pattern {
  param([string]$Path,[string]$Regex)
  if (-not (Test-Path $Path)) { return 0 }
  $cnt = (Select-String -Path $Path -Pattern $Regex -AllMatches -Encoding UTF8 -ErrorAction SilentlyContinue | Measure-Object).Count
  return $cnt
}

function Test-HilogIndex {
  param(
    [string]$IndexPath,
    [string]$AllPath
  )

  if (-not (Test-Path $IndexPath)) { return "missing" }

  $idx = $null
  try {
    $idx = Import-Csv -Path $IndexPath -ErrorAction Stop
  } catch {
    return "bad_csv"
  }

  if (-not $idx -or $idx.Count -eq 0) { return "empty" }

  # check seq continuity
  $seqs = $idx | ForEach-Object { [int]$_.seq }
  $min  = ($seqs | Measure-Object -Minimum).Minimum
  $max  = ($seqs | Measure-Object -Maximum).Maximum
  $uniq = ($seqs | Sort-Object -Unique).Count
  $cnt  = $idx.Count
  $seqOk = ($uniq -eq $cnt) -and ($uniq -eq ($max - $min + 1))

  # check offset + length within hilog_text_full.log
  $offsetOk = $true
  if (Test-Path $AllPath) {
    $size = (Get-Item $AllPath).Length
    foreach ($row in $idx) {
      $off = [int64]$row.offset
      $len = [int64]$row.length
      if ($off -lt 0 -or $len -lt 0 -or ($off + $len) -gt $size) {
        $offsetOk = $false
        break
      }
    }
  }

  if ($seqOk -and $offsetOk) {
    return "ok"
  } elseif (-not $seqOk -and -not $offsetOk) {
    return "bad_seq+offset"
  } elseif (-not $seqOk) {
    return "bad_seq"
  } else {
    return "bad_offset"
  }
}


# ----- prune device hilog segments -----
function Prune-DeviceHilogSegments {
  param([string]$SN,[int]$Keep=10)
  $list = hdc -t $SN shell 'ls -1 /data/log/hilog 2>/dev/null'
  $lines = @()
  foreach ($ln in $list) { if ($ln) { $lines += $ln.Trim() } }
  $cands = @()
  foreach ($n in ($lines | Sort-Object)) {
    if ($n -match '^hilog\.\d{3}\.\d{8}-\d{6}(\.gz)?$') { $cands += $n }
  }
  if ($cands.Count -gt $Keep) {
    $del = $cands | Select-Object -First ($cands.Count - $Keep)
    foreach ($f in $del) { hdc -t $SN shell ("rm -f /data/log/hilog/" + $f) | Out-Null }
  }
}

#endregion Functions

#region Main
# =====================[ Start Run ]==================
$RunStart = Get-Date
Ensure-Dir $Runs
$RunId  = (Get-Date -Format "yyyyMMdd_HHmmss")
$RunDir = Join-Path $Runs $RunId
$FaultInjectDir = Join-Path $RunDir "fault_inject"
Ensure-Dir $FaultInjectDir
if (-not $FAULT_INJECT_TYPE -or $FAULT_INJECT_TYPE -eq "" -or $FAULT_INJECT_TYPE -eq "none") {
  "fault_type=none (no injection)" | Out-File -FilePath (Join-Path $FaultInjectDir "fault_none.txt") -Encoding UTF8 -Force
}

if ($SET_DEVICE_TIME) {
  if (-not $DEVICE_TIME -or $DEVICE_TIME -eq "") {
    $DEVICE_TIME = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  }
  Device-SetTime -SN $SN -Ts $DEVICE_TIME
}

# 閻犱焦婢樼紞宥嗙▔缂佹ɑ绨氬☉鎾冲濠㈡绮╅婊勭暠闁哄啫鐖煎Λ鍧楀箣缁涘湱绀夐柣顫妺缁剟宕ユ惔锝囨暰 PyRCA / LLM 闁哄啫鐖煎Λ璺ㄢ偓闈涚秺缂?

$HostEpochMsAtStart  = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$BoardEpochMsAtStart = Get-BoardEpochMs -SN $SN
if ($null -ne $BoardEpochMsAtStart) {
  $TimeSkewMs = $HostEpochMsAtStart - $BoardEpochMsAtStart
  Write-Host ("[time] host-board skew: {0} ms" -f $TimeSkewMs)
} else {
  $TimeSkewMs = $null
}
# net: auto-detect active iface (unless user forces WK_NET_IFACE)
if (($ENABLE_NET_SNAPSHOT) -or ($FAULT_INJECT_TYPE -like "net_*")) {
  if (-not ($env:WK_NET_IFACE -and $env:WK_NET_IFACE.Trim() -ne "")) {
    $NET_IFACE = Detect-ActiveNetIface -SN $SN -Fallback $NET_IFACE
  }
  Write-Host ("[net] iface={0}, wlan={1}" -f $NET_IFACE, $NET_WLAN_IFACE) -ForegroundColor Cyan
}

Device-Clean -SN $SN
# net: ensure injector script & pre snapshot
if ($FAULT_INJECT_TYPE -like "net_*") {
  Ensure-NetFaultScript -SN $SN
}
Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "pre" -Iface $NET_IFACE
$script:RouteFaultPreDefaultRoute = $null
$script:RouteFaultRestoreResults = @()
if ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable") -and -not [string]::IsNullOrWhiteSpace($NET_WLAN_IFACE)) {
  $script:RouteFaultPreDefaultRoute = Get-HostDefaultRouteContext -SN $SN -Iface $NET_WLAN_IFACE -Tag "pre_before_injection"
  $preRoutePath = Join-Path (Join-Path $RunDir "net") "route_restore_pre.txt"
  Ensure-Dir (Split-Path $preRoutePath -Parent)
  @(
    "### ROUTE_RESTORE_PRE_CAPTURE",
    ("PRE_ROUTE_CAPTURE ok={0} reason={1} iface={2} gateway={3} gateway_hex={4} metric={5}" -f [int][bool]$script:RouteFaultPreDefaultRoute.ok, $script:RouteFaultPreDefaultRoute.reason, $script:RouteFaultPreDefaultRoute.iface, $script:RouteFaultPreDefaultRoute.gateway, $script:RouteFaultPreDefaultRoute.gateway_hex, $script:RouteFaultPreDefaultRoute.metric),
    "### /proc/net/route pre",
    [string]$script:RouteFaultPreDefaultRoute.raw_route
  ) | Out-File -FilePath $preRoutePath -Encoding UTF8 -Force
  Write-Host ("[route_restore] pre_capture ok={0} iface={1} gw={2} hex={3} metric={4}" -f $script:RouteFaultPreDefaultRoute.ok, $script:RouteFaultPreDefaultRoute.iface, $script:RouteFaultPreDefaultRoute.gateway, $script:RouteFaultPreDefaultRoute.gateway_hex, $script:RouteFaultPreDefaultRoute.metric) -ForegroundColor Cyan
}

# Pre faultlog list (for diff)
$FaultRootRemote = "/data/log/faultlog"
$PreFaultList = Get-RemoteFileList -SN $SN -Root $FaultRootRemote

# Hilog capture
$HilogRaw  = Join-Path $RunDir "hilog_raw.log"
Start-HilogPersistent -SN $SN
Start-HilogLiveCapture -SN $SN -OutPath $HilogRaw
Start-FaultmonIfNeeded -SN $SN



# net_*: start run window before fault snapshot to keep probes in window
if ($FAULT_INJECT_TYPE -like "net_*") {
  $RunWindowStart = Get-Date
  $RunWindowHostEpochMsStart = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  Faultmon-Poke -SN $SN -Tag ("run_begin:{0}:{1}:{2}" -f $RunId, $SCENARIO_TAG, $FAULT_INJECT_TYPE)
}
# dmesg before (raw + utf8)
$DmesgBeforeRaw = Join-Path $RunDir "dmesg_before.log"
Save-Dmesg -SN $SN -OutHost $DmesgBeforeRaw -RunDir $RunDir -Tag "before"
$DmesgBeforeUtf8 = Join-Path $RunDir "dmesg_before.utf8.log"
Convert-FileToUtf8 -Src $DmesgBeforeRaw -Dst $DmesgBeforeUtf8
# ----- optional fault injection start -----
$faultPid = 0
# --- [ADD] net_dns_fail: clear marker BEFORE starting injector (avoid deleting a freshly-created marker) ---
if ($FAULT_INJECT_TYPE -eq "net_dns_fail") {
  hdc -t $SN shell "rm -f /data/local/tmp/net_fault_state/iptables_dns_block.applied 2>/dev/null || true" 2>&1 | Out-Null
}
if ($ENABLE_FAULT_INJECT -and $FAULT_INJECT_TYPE -and $FAULT_INJECT_TYPE -notin @("none","net_bg")) {
  $faultPid = Start-FaultInject -Type $FAULT_INJECT_TYPE -SN $SN -BinDir $FAULT_BIN_DIR
}
# net: snapshot right after injection applied
# NOTE: net_link_flap 闂傚洠鍋撻悷鏇氱窔椤ゅ倹寰勯弽顓炴珰濞戞挴鍋撴繛鍡忔缁辨繄浜告禒瀣闁告艾鏈鍌炲箮閹惧啿鐓?down/up 濞戞挶鍊楅～鎺楁偐閼哥鍋撴笟濠勭濞撴艾銇樼花顒勫冀閻熺増绀堥悗瑙勭煯缂嶅懘鏁嶉崼銉︽嚑閻犱警鍨辨慨鍫ュ礉椤帞绀?
if ($FAULT_INJECT_TYPE -like "net_*") {

  if ($FAULT_INJECT_TYPE -eq "net_dns_fail") {
  # 缂佹稑顦欢鐔兼晬濮濓箲tables marker + netmanager resolv.conf 缂佸鍟块悾楣冨礃濞嗗繐寮抽柨娑樼墛濞撳爼姊圭捄銊ф惣 15s闁?
  $ready = $false
  for ($i = 0; $i -lt 15; $i++) {
    $cmd = ("if test -f /data/local/tmp/net_fault_state/iptables_dns_block.applied " +
            "&& test -s /data/service/el1/public/netmanager/resolv.conf " +
            "&& grep -F {0} /data/service/el1/public/netmanager/resolv.conf >/dev/null 2>&1; " +
            "then echo READY; else echo NO; fi") -f $NET_BAD_DNS

    $r = hdc -t $SN shell $cmd 2>&1
    if ($r -match "READY") { $ready = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) {
    Write-Host "[WARN] net_dns_fail not stable within 15s (marker/resolv not ready), snapshot anyway."
  }

  # 闂侇剙鐏濋崢銈嗙▔?netmanager / 闁告劖鐟ラ崣鍡涘籍鐠哄搫鐓濋柛姘灱椤绮╅悙绮瑰亾?
  Start-Sleep -Seconds 1
  Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "fault" -Iface $NET_IFACE
} elseif ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable")) {
    # Wait for ready marker: injector writes it only after fake route is verified
    $routeReadyFile = if ($FAULT_INJECT_TYPE -eq "net_gateway_unreachable") { "/data/local/tmp/net_fault_state/gateway_unreachable.ready" } else { "/data/local/tmp/net_fault_state/wrong_route.ready" }
    $routeMarkerPrefix = if ($FAULT_INJECT_TYPE -eq "net_gateway_unreachable") { "GATEWAY_UNREACHABLE" } else { "WRONG_ROUTE" }
    $routeTimeoutMarker = if ($FAULT_INJECT_TYPE -eq "net_gateway_unreachable") { "gateway_unreachable_timeout.marker" } else { "wrong_route_timeout.marker" }
    $wdrReady = $false
    Write-Host ("[{0}] waiting for ready marker (max 15s)..." -f $FAULT_INJECT_TYPE) -ForegroundColor Cyan
    for ($i = 0; $i -lt 15; $i++) {
      $r = hdc -t $SN shell ("if test -f {0}; then echo READY; else echo NO; fi" -f $routeReadyFile) 2>&1
      if (([string]($r -join '')) -match "READY") { $wdrReady = $true; break }
      Start-Sleep -Seconds 1
    }
    if (-not $wdrReady) {
      Write-Host ("[WARN] {0}: ready marker not seen within 15s; pulling injector log for diagnosis" -f $FAULT_INJECT_TYPE) -ForegroundColor Yellow
      Collect-RemoteFaultLog -SN $SN -RunDir $RunDir -FaultType $FAULT_INJECT_TYPE
      ("{0}_READY_TIMEOUT" -f $routeMarkerPrefix) | Out-File -FilePath (Join-Path $FaultInjectDir $routeTimeoutMarker) -Encoding UTF8 -Force
    } else {
      Write-Host ("[{0}] ready marker confirmed (route fault stable), collecting fault snapshot" -f $FAULT_INJECT_TYPE) -ForegroundColor Green
    }
    if ($wdrReady) {
      Collect-WrongDefaultRouteLiveEvidence -SN $SN -RunDir $RunDir -Phase "fault" -Iface $NET_WLAN_IFACE -When "after_ready_before_snapshot" -ReadyFile $routeReadyFile -MarkerPrefix $routeMarkerPrefix
    }
    Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "fault" -Iface $NET_IFACE
  } else {

    Start-Sleep -Seconds 2
    Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "fault" -Iface $NET_IFACE

    if ($FAULT_INJECT_TYPE -eq "net_link_flap") {
      Start-Sleep -Seconds ([int][math]::Max(4, $NET_FLAP_DOWN_SEC + 1))
      Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "fault2" -Iface $NET_IFACE
    }
  }
}




# 闁哄秴娲╅鍥嫉椤掍緡鍋?run window 闁汇劌瀚幑锝夋倷閻у摜绀勯柣顫妺缁剟妫?wukong 闁革妇鍎ゅ▍娆愮┍濠靛﹦妲堥柡鍫氬亾閻忓繑鍨块崳浼村冀闁垮顦ч梻鍌濇彧缁?
if ($null -eq $RunWindowStart) {
  $RunWindowStart = Get-Date
  $RunWindowHostEpochMsStart = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  Faultmon-Poke -SN $SN -Tag ("run_begin:{0}:{1}:{2}" -f $RunId, $SCENARIO_TAG, $FAULT_INJECT_TYPE)
}

# ----- WuKong auto-tuning: 闁哄秷顫夊畵渚€寮崨瀛橆唶缂侇偉顕ч悗?/ 闁革妇鍎ゅ▍娆撳箯閸℃鐎婚柍銉︾矎閸庢寮查妞诲亾濠靛棙瀚查柍銉︾矊濞呮梹绔熼幍鏂ュ亾?-----
$envWukExec    = Convert-EnvToBoolOrNull $env:WK_ENABLE_WUKONG_EXEC

$envWukSpecial = Convert-EnvToBoolOrNull $env:WK_ENABLE_SPECIAL_SWEEP

# 1) 闁哄嫭鍎崇槐锛勬媼閸撗呮瀭濞村吋锚閸樻盯鏁嶅顒夋搐闁哄绮庨獮鍡樻櫠閸愩劌缍侀梺鎻掔箳缁増绂嶉崱妞诲亾绾绀夐悘蹇氱簿椤╊偊鎯勯弽顒€澹栭柡鍫墴缁垳鎷?
if ($envWukExec -ne $null) {
  $ENABLE_WUKONG_EXEC = $envWukExec
}
if ($envWukSpecial -ne $null) {
  $ENABLE_SPECIAL_SWEEP = $envWukSpecial
}

# 2) 婵炲备鍓濆﹢渚€寮伴幆褏纭€閻熸洖妫涘ú濠囧籍鐠佸湱绀夐柟绋款槹閺呯娀姊惧鍛邦潶闁?/ 闁革妇鍎ゅ▍娆撴嚊椤忓嫬袟闂侇偄顦扮€?
if ($envWukExec -eq $null -and $envWukSpecial -eq $null) {
  if ($FAULT_INJECT_TYPE) {
    switch -regex ($FAULT_INJECT_TYPE) {
      '^cpu' {
        # CPU 闁告ê顑呮慨蹇涘捶閻戞ɑ鐝柨娑欑煯缁绘岸鎮?wukong闁挎稑鐬奸弫銈嗙鎼粹挍渚€骞忛悢宄邦枀闁告瑩顣﹀锔界?
        $ENABLE_WUKONG_EXEC   = $true
        $ENABLE_SPECIAL_SWEEP = $true
        break
      }
      '^mem' {
        # 闁告劕鎳庨悺銊х尵鐠囧弶绨氶柡鍜佸灲缁辩増顪€濡鍚囬柛蹇氭珪鐢偓 wukong闁挎稑鐭傛导鈺呭礂?CPU 閻炴凹鍋勯崗閬嶅箥?
        $ENABLE_WUKONG_EXEC   = $false
        $ENABLE_SPECIAL_SWEEP = $false
        break
      }
      '^deadlock' {
        $ENABLE_WUKONG_EXEC   = $false
        $ENABLE_SPECIAL_SWEEP = $false
        break
      }
      '^segv' {
        $ENABLE_WUKONG_EXEC   = $false
        $ENABLE_SPECIAL_SWEEP = $false
        break
      }
      '^net' {
        $ENABLE_WUKONG_EXEC   = $false
        $ENABLE_SPECIAL_SWEEP = $false
        break
      }

    }
  }

  # 缂佺虎鍨甸崕妤呭疾?/ 闁革綆浜滈敍鎰板冀闁垮鎷遍柨娑樻构K_FAULT_TYPE=none 闁哄啳顔愮槐?
  if ($FAULT_INJECT_TYPE -eq "none") {
    if ($SCENARIO_TAG -eq "bg_idle_pure" -or $SCENARIO_TAG -eq "bg_idle") {
      # 缂佺虎鍨甸崕妤呭疾椤栥倗绐楀☉鎾崇У閺佺偤宕楅妷锝傚亾娴ｉ鐟濋悹?wukong
      $ENABLE_WUKONG_EXEC   = $false
      $ENABLE_SPECIAL_SWEEP = $false
    } elseif ($SCENARIO_TAG -eq "bg_idle_noise") {
      # 闁革綆浜滈敍鎰版嚄鐏炵偓鐝柨娑欑煯缁楀鈻旈妸銉ュ汲闁挎稑鐭佹禍銈夋煂?wukong闁挎稑娼抶ec 鐎殿喒鍋撻柛姘煎灲缁辨嫉pecial sweep 闁稿繑濞婂Λ鎾晬?
      $ENABLE_WUKONG_EXEC   = $true
      $ENABLE_SPECIAL_SWEEP = $false
    } elseif ($SCENARIO_TAG -eq "noise_wukong_only") {
      # 闁稿繒鍘ч鎰板籍瑜庨悥锝囩驳閹惧懐绐楃紒缁㈠灠濞呮梹绔熺敮顔剧exec + special sweep闁?
      $ENABLE_WUKONG_EXEC   = $true
      $ENABLE_SPECIAL_SWEEP = $true
    }
  }
}

# =====================[ WuKong ]=====================
if ($ENABLE_SPECIAL_SWEEP -and $AUTO_APP_LIST) {
  Write-Host "Fetch app list from wukong appinfo"
  $appinfo = hdc -t $SN shell "wukong appinfo"
  $parsed = @()
  foreach ($ln in $appinfo) {
    $m = [regex]::Match($ln, '(?i)bundleName\s*:\s*([^\s"]+)')
    if ($m.Success) { $parsed += $m.Groups[1].Value }
  }
  if ($parsed.Count -gt 0) {
    $APP_LIST = $parsed | Sort-Object -Unique
    Write-Host ("App list parsed: " + (($APP_LIST | Select-Object -First 10) -join ", ") + " ...")
  } else {
    Write-Host "No app parsed; fall back to static APP_LIST"
  }
}

if ($ENABLE_WUKONG_EXEC) {
$ap = Normalize-WukongPct $EXEC_APPSWITCH_PCT
$tp = Normalize-WukongPct $EXEC_TOUCH_PCT

# 濠碘€冲€归悘澶娦掗弬鍓т紣闁告梻濮鹃幑锝夊级?> 1闁挎稑鐭侀崵婊堝礉閵娿儳绉哄☉鎾亾闁告牗鐗槐娆撴焼閸喖甯?wukong 闁告劕绉垫慨銈夋煥濞嗘瑧绀?
$sum = $ap + $tp
if ($sum -gt 1.000001) {
  $ap = $ap / $sum
  $tp = $tp / $sum
}

$HdcArgs = @("exec","-s",$EXEC_SEED,"-i",$EXEC_INTERVAL_MS,"-a",$ap,"-t",$tp,"-c",$EXEC_COUNT)

  # 閻忓繐妫濆▓銏ゅ嫉閻戞銈撮悹鍥ㄦ礋濡炬椽宕氱捄鐑樿含闁圭娲ら悾?APP_LIST 闁告劕鎷戠槐婵囩閵壯呯礆闁活収鍘藉鍌炴⒒閺夋垼瀚欓柛鎴濈箰閻垶寮悩鎻掑綘 app 妤犵偛寮舵竟鍫ュΥ?
  # 濠碘€冲€归悘?APP_LIST 濞戞捁娅ｉ埞鏍晬鐏炶棄鐏熷☉鎾崇Т婵?-b闁挎稑鐦島kong 濞村吋宀搁埀顑藉亾闁搞儳鍋涢崺宀勫灳濠婂啫寮?app闁炽儲绻冭啯鐎殿喖绻堥埀?
  $apps = Normalize-AppList $APP_LIST

if ($apps -and $apps.Count -gt 0) {
  $b0 = $apps[0]
  if ($b0) { $HdcArgs += @("-b", $b0) }
}
    $WukongCmdline = "wukong " + ($HdcArgs -join " ")
Write-Host ("Run: " + $WukongCmdline)

$out = (hdc -t $SN shell $WukongCmdline 2>&1 | Out-String)
Write-Host $out

# 濠碘€冲€归悘?-b 閻庝絻澹堥崵?invalid闁挎稑鐭侀崵婊堝礉閵娾晛娅㈤悹鍥ㄦ穿缁辨瑩寮?-b闁?
if ($out -match "Command arguments is invalid") {
  Write-Host "[WARN] wukong exec invalid HdcArgs; retry without -b..."

  # 闂佹彃绉甸弻濠勭磼閸曨亞顏卞ù鐘冲灊缁楀鏁?-b 闁汇劌瀚顒勫极?
  $HdcArgs2 = @("exec","-s",$EXEC_SEED,"-i",$EXEC_INTERVAL_MS,"-a",$EXEC_APPSWITCH_PCT,"-t",$EXEC_TOUCH_PCT,"-c",$EXEC_COUNT)
  $WukongCmdline2 = "wukong " + ($HdcArgs2 -join " ")
  Write-Host ("Retry: " + $WukongCmdline2)

  $out2 = (hdc -t $SN shell $WukongCmdline2 2>&1 | Out-String)
  Write-Host $out2
}

}



if ($ENABLE_SPECIAL_SWEEP) {
  $apps = Normalize-AppList $APP_LIST
foreach ($a in $apps) {
    Write-Host ("== SPECIAL on {0} ==" -f $a)
    $out = hdc -t $SN shell ("wukong special -C '" + $a + "' -p -c " + $SPECIAL_COUNT)
    Write-Host $out
    if ($out -match "not be included in all bundles") {
      Write-Host ("Fallback focus on {0}" -f $a)
      hdc -t $SN shell ("wukong focus -b '" + $a + "' -f button -n 5 -c " + $SPECIAL_COUNT) | Out-Null
    }
    Start-Sleep -Milliseconds 500
  }
}
# ----- run window alignment for non-wukong scenarios -----
# 閻庣敻鈧稓鑹?mem_oomsafe 閺夆晜鐟х悮顐﹀礃閸涱厾鎽犳繛澶婂濠€鍫曞捶閻戞ɑ鐝柨娑橆劔tart-FaultInject 濞村吋鑹惧﹢?$script:FaultInjectMinRunSec 濞戞搩鍘剧划浼村礄閻戞ê鑵归柤鑺ュ姍閸ｄ即姊块崱妯活槯闂傗偓閸栵紕绀?
# 閺夆晜鐟╅崳鐑藉矗?max(RUN_WINDOW_SEC, FaultInjectMinRunSec) 濞达絾绮堢拹鐔兼儑閻斿壊鍔€闁汇劌瀚ú浼村冀閸モ晝宕堕柛娆欑祷閳?
$targetWindowSec = $RUN_WINDOW_SEC
if ($script:FaultInjectMinRunSec -gt $targetWindowSec) {
  $targetWindowSec = [int][math]::Ceiling($script:FaultInjectMinRunSec)
}
if ($BASELINE_SEC -gt $targetWindowSec) {
  $targetWindowSec = [int]$BASELINE_SEC
}

# 濮掓稒顭堥濠氬矗椤忓嫭韬柍銉︾矒濞?wukong 闁革妇鍎ゅ▍娆撳灳濠靛懐鐟撳ǎ鍥ㄧ箚閻﹀寮甸埀顒備焊?run window闁?
# 濞达絽妫滅€氥垽寮伴幆褏纭€閻犱礁澧介悿鍡樼?WK_BASELINE_SEC闁挎稑鑻崹顖炲籍閻樹警鍟堥柡鍕靛灠閹胶鎹?wukong 闂侇喛妫勫閬嶅礆鐠轰警鍤犲缁樺姍閸ｄ即寮芥搴ｅ炊闁告瑱缍囩槐娆撴偨閵娿倗鑹?bg_idle_noise 缂佹稑顧€缁辨岸濡?
$forceRunWindow = ($BASELINE_SEC -gt 0) -or (-not $ENABLE_WUKONG_EXEC -and -not $ENABLE_SPECIAL_SWEEP)

if ($forceRunWindow -and $targetWindowSec -gt 0) {
  $now = Get-Date
  $elapsedSec = ($now - $RunWindowStart).TotalSeconds
  $remain = [int][Math]::Ceiling($targetWindowSec - $elapsedSec)
  if ($remain -gt 0) {
    $reason = "non-wukong"
    if ($BASELINE_SEC -gt 0) { $reason = "baseline" }
    Write-Host ("[run_window] {0}, sleep {1}s to reach {2}s (elapsed ~{3:N1}s)" -f $reason, $remain, $targetWindowSec, $elapsedSec)
    Start-Sleep -Seconds $remain
  }
}
# 闁哄秴娲╅鍥嫉椤掍緡鍋?run window 闁汇劌瀚划鎾绘倷閻у摜绀勯柣顫妺缁剛鎲楁担绋款梾 events/metrics闁?
$DeferRunWindowEndForRecovery = ($FAULT_INJECT_TYPE -eq "net_dns_fail")
if (-not $DeferRunWindowEndForRecovery) {
  $RunWindowEnd = Get-Date
  $RunWindowHostEpochMsEnd = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  Faultmon-Poke -SN $SN -Tag ("run_end:{0}:{1}:{2}" -f $RunId, $SCENARIO_TAG, $FAULT_INJECT_TYPE)
}


# ----- optional fault injection stop -----
# net_wifi_disconnect / net_wifi_auth_fail_wrong_psk: always call Stop-FaultInject (PID file recovery handles faultPid=0 case)
$script:WifiRecoveryGate = $null
$script:WifiInjectorStopOk = $null
$script:NoIpv4IpRefreshUsed = $false
$script:NoIpv4IpRefreshReason = "not_needed"
if ($faultPid -gt 0 -or $FAULT_INJECT_TYPE -eq "net_wifi_disconnect" -or $FAULT_INJECT_TYPE -eq "net_wifi_auth_fail_wrong_psk") {
  Stop-FaultInject -faultPidLocal $faultPid -SN $SN -Type $FAULT_INJECT_TYPE
}

# net_*: best-effort restore & wait before post snapshot (avoid "post too early")
if ($FAULT_INJECT_TYPE -like "net_*") {
  if ($FAULT_INJECT_TYPE -eq "net_wifi_disconnect") {
    # recovery gate: wait until wpa_state=COMPLETED + IPv4 + default route + ping (max 30s)
    $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 30
    Write-Host ("[wifi_disconnect] recovery_gate_ok={0} reason={1}" -f $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason) -ForegroundColor Cyan
  } elseif ($FAULT_INJECT_TYPE -eq "net_wifi_auth_fail_wrong_psk") {
    # ConnectivityService doesn't auto-restore default route after wpa_supplicant replacement; re-add from backup
    $afRouteBak = @(hdc -t $SN shell "cat /data/local/tmp/net_fault_state/route.bak 2>/dev/null || true" 2>$null)
    $afGwLine = $afRouteBak | Where-Object { $_ -match '^GW=' } | Select-Object -First 1
    $afGw = if ($afGwLine) { ($afGwLine -replace '^GW=','').Trim() } else { '' }
    if (-not [string]::IsNullOrWhiteSpace($afGw) -and $afGw -ne '0.0.0.0') {
      hdc -t $SN shell ("/data/busybox route add default gw {0} dev {1} 2>/dev/null || true" -f $afGw, $NET_WLAN_IFACE) 2>$null | Out-Null
      Write-Host ("[auth_fail] re-added default route gw={0} dev={1} before gate" -f $afGw, $NET_WLAN_IFACE) -ForegroundColor Cyan
    }
    Start-Sleep -Seconds 2
    # recovery gate: wpa_state=COMPLETED + IPv4 + default route + main public probe + ping baidu (max 60s)
    $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 60 -CheckDns
    if (-not $script:WifiRecoveryGate.ok) {
      Write-Host ("[auth_fail] gate TIMEOUT reason={0}; running PS-side hard wifi recovery" -f $script:WifiRecoveryGate.reason) -ForegroundColor Yellow
      hdc -t $SN shell "pid=`$(cat /data/local/tmp/wpa.pid 2>/dev/null); [ -n `"`$pid`" ] && kill -TERM `$pid 2>/dev/null; killall wpa_supplicant 2>/dev/null; true" 2>$null | Out-Null
      Start-Sleep -Seconds 2
      hdc -t $SN shell ("ifconfig {0} down 2>/dev/null; true" -f $NET_WLAN_IFACE) 2>$null | Out-Null
      Start-Sleep -Seconds 2
      hdc -t $SN shell ("ifconfig {0} up 2>/dev/null; true" -f $NET_WLAN_IFACE) 2>$null | Out-Null
      Start-Sleep -Seconds 1
      hdc -t $SN shell "rm -rf /data/local/tmp/wpa_ctrl /data/local/tmp/wpa.pid 2>/dev/null; mkdir -p /data/local/tmp/wpa_ctrl; chmod 777 /data/local/tmp/wpa_ctrl; true" 2>$null | Out-Null
      hdc -t $SN shell ("/system/bin/wpa_supplicant -B -D nl80211 -i {0} -c /data/local/tmp/wpa.conf -P /data/local/tmp/wpa.pid 2>/dev/null; true" -f $NET_WLAN_IFACE) 2>$null | Out-Null
      Start-Sleep -Seconds 3
      hdc -t $SN shell ("wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} reconfigure >/dev/null 2>&1; wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} select_network 0 >/dev/null 2>&1; wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} reconnect >/dev/null 2>&1; true" -f $NET_WLAN_IFACE) 2>$null | Out-Null
      if (-not [string]::IsNullOrWhiteSpace($afGw) -and $afGw -ne '0.0.0.0') {
        hdc -t $SN shell ("/data/busybox route add default gw {0} dev {1} 2>/dev/null || true" -f $afGw, $NET_WLAN_IFACE) 2>$null | Out-Null
      }
      $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 30 -CheckDns
      Write-Host ("[auth_fail] hard_recovery gate_ok={0} reason={1}" -f $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason) -ForegroundColor Cyan
    }
    Write-Host ("[auth_fail] recovery_gate_ok={0} reason={1}" -f $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason) -ForegroundColor Cyan
  } elseif ($FAULT_INJECT_TYPE -eq "net_no_default_route") {
    # restore_all in net_fault.sh re-adds default route; OS may flush it briefly. Re-add from backup as belt-and-suspenders.
    $ndrRouteBak = @(hdc -t $SN shell "cat /data/local/tmp/net_fault_state/route.bak 2>/dev/null || true" 2>$null)
    $ndrGwLine = $ndrRouteBak | Where-Object { $_ -match '^GW=' } | Select-Object -First 1
    $ndrGw = if ($ndrGwLine) { ($ndrGwLine -replace '^GW=','').Trim() } else { '' }
    if (-not [string]::IsNullOrWhiteSpace($ndrGw) -and $ndrGw -ne '0.0.0.0') {
      hdc -t $SN shell ("/data/busybox route add default gw {0} dev {1} 2>/dev/null || true" -f $ndrGw, $NET_WLAN_IFACE) 2>$null | Out-Null
      Write-Host ("[no_default_route] re-added default route gw={0} dev={1} before gate" -f $ndrGw, $NET_WLAN_IFACE) -ForegroundColor Cyan
    }
    # recovery gate: wpa_state=COMPLETED + IPv4 + default route + main public probe + ping baidu (max 30s)
    $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 30 -CheckDns
    Write-Host ("[no_default_route] recovery_gate_ok={0} reason={1}" -f $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason) -ForegroundColor Cyan
  } elseif ($FAULT_INJECT_TYPE -eq "net_no_ipv4_on_iface") {
    # restore_all may put IPv4/route back while Guest/AP forwarding remains stale. Gate first, then
    # only for that stale-forwarding signature do the controlled .225 -> .226 -> .225 refresh.
    Start-Sleep -Seconds 2
    $script:NoIpv4RecoveryContext = Get-NoIpv4RecoveryContext -SN $SN -Iface $NET_WLAN_IFACE
    $noIpv4Gw = [string]$script:NoIpv4RecoveryContext.gateway
    $noIpv4PrimaryIp = [string]$script:NoIpv4RecoveryContext.ip
    Write-Host ("[no_ipv4] dynamic recovery context: ip={0} gw={1}" -f $noIpv4PrimaryIp, $noIpv4Gw) -ForegroundColor Cyan
    [void](Ensure-NoIpv4DefaultRoute -SN $SN -Iface $NET_WLAN_IFACE -Gateway $noIpv4Gw -Tag "before_gate")
    $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 24 -CheckDns
    if ((-not $script:WifiRecoveryGate.ok) -and $script:WifiRecoveryGate.reason -eq "no_default_route") {
      [void](Ensure-NoIpv4DefaultRoute -SN $SN -Iface $NET_WLAN_IFACE -Gateway $noIpv4Gw -Tag "after_no_default_route_gate")
      $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 24 -CheckDns
    }
    if (-not $script:WifiRecoveryGate.ok) {
      $stuck = Get-NoIpv4RecoveryStuckState -SN $SN -Iface $NET_WLAN_IFACE -Gateway $noIpv4Gw -ExpectedIp $noIpv4PrimaryIp
      Write-Host ("[no_ipv4] stuck_check: wpa={0} expected_ip={1} route={2} arp={3} gw_ping_fail={4} gw={5}" -f $stuck.wpa_completed, $stuck.has_expected_ip, $stuck.has_default_route, $stuck.arp_valid, $stuck.gateway_ping_fail, $noIpv4Gw) -ForegroundColor Cyan
      if ($stuck.stuck) {
        if ($noIpv4PrimaryIp -eq "172.70.2.225" -and $noIpv4Gw -eq "172.70.2.1") {
          $script:NoIpv4IpRefreshUsed = $true
          $refresh = Invoke-NoIpv4IpRefresh -SN $SN -Iface $NET_WLAN_IFACE -PrimaryIp "172.70.2.225" -TempIp "172.70.2.226" -Gateway "172.70.2.1"
          $script:NoIpv4IpRefreshReason = $refresh.reason
          if ($refresh.ok) {
            $script:WifiRecoveryGate = $refresh.final_gate
          } else {
            $script:WifiRecoveryGate = [pscustomobject]@{ ok = $false; reason = $refresh.reason }
          }
        } else {
          $script:NoIpv4IpRefreshReason = "ip_refresh_skipped_non_guest_dynamic_context:ip=" + $noIpv4PrimaryIp + ":gw=" + $noIpv4Gw
        }
      } else {
        $script:NoIpv4IpRefreshReason = "gate_failed_without_stale_guest_forwarding_signature:" + $script:WifiRecoveryGate.reason
      }
    }
    Write-Host ("[no_ipv4] recovery_gate_ok={0} reason={1} ip_refresh_used={2} ip_refresh_reason={3}" -f $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason, $script:NoIpv4IpRefreshUsed, $script:NoIpv4IpRefreshReason) -ForegroundColor Cyan
  } else {
    Start-Sleep -Seconds 2

    if ($NET_IFACE -and $NET_IFACE.Trim() -ne "") {
      # ensure iface up
      hdc -t $SN shell ("ifconfig {0} up 2>/dev/null || true" -f $NET_IFACE) 2>$null | Out-Null

      # wait carrier=1 up to 12s
      $carrier = ""
      for ($i = 0; $i -lt 12; $i++) {
        $carrier = @(hdc -t $SN shell ("cat /sys/class/net/{0}/carrier 2>/dev/null || echo 0" -f $NET_IFACE) 2>$null) | Select-Object -First 1
        if ($carrier -and $carrier.Trim() -eq "1") { break }
        Start-Sleep -Seconds 1
      }

      # still not up -> bounce once
      if (-not $carrier -or $carrier.Trim() -ne "1") {
        hdc -t $SN shell ("ifconfig {0} down 2>/dev/null || true; sleep 2; ifconfig {0} up 2>/dev/null || true" -f $NET_IFACE) 2>$null | Out-Null
        Start-Sleep -Seconds 2
      }
    }
  }
}

# pull injector log & post snapshot
Collect-RemoteFaultLog -SN $SN -RunDir $RunDir -FaultType $FAULT_INJECT_TYPE
if ($FAULT_INJECT_TYPE -eq "net_no_ipv4_on_iface" -and -not [string]::IsNullOrWhiteSpace($NET_WLAN_IFACE)) {
  $noIpv4PostCtx = Get-NoIpv4RecoveryContext -SN $SN -Iface $NET_WLAN_IFACE
  [void](Ensure-NoIpv4DefaultRoute -SN $SN -Iface $NET_WLAN_IFACE -Gateway ([string]$noIpv4PostCtx.gateway) -Tag "before_post_snapshot")
}
if ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable") -and -not [string]::IsNullOrWhiteSpace($NET_WLAN_IFACE)) {
  $script:RouteFaultRestoreResults += Ensure-RouteFaultDefaultRouteRestore -SN $SN -RunDir $RunDir -Phase "post" -PreRoute $script:RouteFaultPreDefaultRoute
}
Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "post" -Iface $NET_IFACE

# net_dns_fail / net_link_down / net_link_flap / new wlan types: add post2 (capture recovered state reliably)
# net_wifi_disconnect uses 8s sleep (ConnectivityService needs extra time after wpa reconnect)
if ($FAULT_INJECT_TYPE -in @("net_dns_fail","net_link_down","net_link_flap","net_wifi_disconnect","net_wifi_auth_fail_wrong_psk","net_no_default_route","net_no_ipv4_on_iface","net_wrong_default_route","net_gateway_unreachable","net_public_ip_unreachable")) {
  $post2Sleep = if ($FAULT_INJECT_TYPE -in @("net_wifi_disconnect","net_wifi_auth_fail_wrong_psk","net_wrong_default_route","net_gateway_unreachable")) { 8 } else { 3 }
  Start-Sleep -Seconds $post2Sleep
  if ($FAULT_INJECT_TYPE -eq "net_no_ipv4_on_iface" -and -not [string]::IsNullOrWhiteSpace($NET_WLAN_IFACE)) {
    $noIpv4Post2Ctx = Get-NoIpv4RecoveryContext -SN $SN -Iface $NET_WLAN_IFACE
    [void](Ensure-NoIpv4DefaultRoute -SN $SN -Iface $NET_WLAN_IFACE -Gateway ([string]$noIpv4Post2Ctx.gateway) -Tag "before_post2_snapshot")
  }
  if ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable") -and -not [string]::IsNullOrWhiteSpace($NET_WLAN_IFACE)) {
    $script:RouteFaultRestoreResults += Ensure-RouteFaultDefaultRouteRestore -SN $SN -RunDir $RunDir -Phase "post2" -PreRoute $script:RouteFaultPreDefaultRoute
  }
  Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "post2" -Iface $NET_IFACE
}

if ($DeferRunWindowEndForRecovery) {
  $RunWindowEnd = Get-Date
  $RunWindowHostEpochMsEnd = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  Faultmon-Poke -SN $SN -Tag ("run_end:{0}:{1}:{2}" -f $RunId, $SCENARIO_TAG, $FAULT_INJECT_TYPE)
}

if ($FAULT_INJECT_TYPE -eq "net_no_ipv4_on_iface") {
  $noIpv4FinalCtx = Get-NoIpv4RecoveryContext -SN $SN -Iface $NET_WLAN_IFACE
  $noIpv4FinalGw = [string]$noIpv4FinalCtx.gateway
  $noIpv4FinalIp = [string]$noIpv4FinalCtx.ip
  [void](Ensure-NoIpv4DefaultRoute -SN $SN -Iface $NET_WLAN_IFACE -Gateway $noIpv4FinalGw -Tag "before_final_gate")
  $finalGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 12 -CheckDns
  if ($finalGate.ok) {
    $script:WifiRecoveryGate = $finalGate
  } else {
    $stuckFinal = Get-NoIpv4RecoveryStuckState -SN $SN -Iface $NET_WLAN_IFACE -Gateway $noIpv4FinalGw -ExpectedIp $noIpv4FinalIp
    Write-Host ("[no_ipv4] final_stuck_check: wpa={0} expected_ip={1} route={2} arp={3} gw_ping_fail={4} gw={5}" -f $stuckFinal.wpa_completed, $stuckFinal.has_expected_ip, $stuckFinal.has_default_route, $stuckFinal.arp_valid, $stuckFinal.gateway_ping_fail, $noIpv4FinalGw) -ForegroundColor Cyan
    if ($stuckFinal.stuck -and -not $script:NoIpv4IpRefreshUsed) {
      if ($noIpv4FinalIp -eq "172.70.2.225" -and $noIpv4FinalGw -eq "172.70.2.1") {
        $script:NoIpv4IpRefreshUsed = $true
        $refreshFinal = Invoke-NoIpv4IpRefresh -SN $SN -Iface $NET_WLAN_IFACE -PrimaryIp "172.70.2.225" -TempIp "172.70.2.226" -Gateway "172.70.2.1"
        $script:NoIpv4IpRefreshReason = "post2_final_gate:" + $refreshFinal.reason
        if ($refreshFinal.ok) {
          $script:WifiRecoveryGate = $refreshFinal.final_gate
          Collect-NetSnapshot -SN $SN -RunDir $RunDir -Phase "post2" -Iface $NET_IFACE
        } else {
          $script:WifiRecoveryGate = [pscustomobject]@{ ok = $false; reason = $refreshFinal.reason }
        }
      } else {
        $script:NoIpv4IpRefreshReason = "post2_final_gate:ip_refresh_skipped_non_guest_dynamic_context:ip=" + $noIpv4FinalIp + ":gw=" + $noIpv4FinalGw
        $script:WifiRecoveryGate = $finalGate
      }
    } else {
      $script:WifiRecoveryGate = $finalGate
      if ($script:NoIpv4IpRefreshUsed) {
        $script:NoIpv4IpRefreshReason = $script:NoIpv4IpRefreshReason + ";post2_final_gate_failed:" + $finalGate.reason
      } else {
        $script:NoIpv4IpRefreshReason = "post2_final_gate_failed_without_stale_guest_forwarding_signature:" + $finalGate.reason
      }
    }
  }
  Write-Host ("[no_ipv4] final_recovery_gate_ok={0} reason={1} ip_refresh_used={2} ip_refresh_reason={3}" -f $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason, $script:NoIpv4IpRefreshUsed, $script:NoIpv4IpRefreshReason) -ForegroundColor Cyan
} elseif ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable","net_public_ip_unreachable") -and -not [string]::IsNullOrWhiteSpace($NET_WLAN_IFACE)) {
  $script:WifiRecoveryGate = Wait-WifiRecovery -SN $SN -Iface $NET_WLAN_IFACE -TimeoutSec 12 -CheckDns
  Write-Host ("[{0}] final_recovery_gate_ok={1} reason={2}" -f $FAULT_INJECT_TYPE, $script:WifiRecoveryGate.ok, $script:WifiRecoveryGate.reason) -ForegroundColor Cyan
}

$NetOutcome = $null
if ($FAULT_INJECT_TYPE -like "net_*") {
  try {
    $NetOutcome = Get-NetOutcomeMetadata -RunDir $RunDir -FaultType $FAULT_INJECT_TYPE -IfaceUsed $NET_IFACE -WlanIface $NET_WLAN_IFACE -InjectorStarted ($faultPid -gt 0)
    Save-NetOutcomeMetadata -RunDir $RunDir -Outcome $NetOutcome
  } catch {
    Write-Host ("WARN: Get/Save-NetOutcomeMetadata failed: " + $_.Exception.Message) -ForegroundColor Yellow
  }
}

# net_wifi_disconnect / net_wifi_auth_fail_wrong_psk / net_no_default_route / net_no_ipv4_on_iface / net_wrong_default_route:
# inject recovery gate fields into _net_outcome.json for acceptance auditing.
if ($FAULT_INJECT_TYPE -eq "net_wifi_disconnect" -or $FAULT_INJECT_TYPE -eq "net_wifi_auth_fail_wrong_psk" -or $FAULT_INJECT_TYPE -eq "net_no_default_route" -or $FAULT_INJECT_TYPE -eq "net_no_ipv4_on_iface" -or $FAULT_INJECT_TYPE -eq "net_wrong_default_route" -or $FAULT_INJECT_TYPE -eq "net_gateway_unreachable" -or $FAULT_INJECT_TYPE -eq "net_public_ip_unreachable") {
  $outPath = Join-Path $RunDir "_net_outcome.json"
  if (Test-Path $outPath) {
    try {
      $obj = Get-Content $outPath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($null -ne $script:WifiRecoveryGate) {
        $gateOk = [bool]$script:WifiRecoveryGate.ok
        $gateReason = [string]$script:WifiRecoveryGate.reason
        if ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable")) {
          $outcomeNames = @($obj.PSObject.Properties.Name)
          $strictRecoveryObserved = ($outcomeNames -contains "recovery_observed") -and ([bool]$obj.recovery_observed)
          if (-not $strictRecoveryObserved) {
            $gateOk = $false
            $outcomeReason = if ($outcomeNames -contains "recovery_observation_reason") { [string]$obj.recovery_observation_reason } else { "" }
            $gateReason = if ([string]::IsNullOrWhiteSpace($outcomeReason)) { "strict_recovery_not_observed" } else { "strict_recovery_not_observed:$outcomeReason" }
          }
        }
        $obj | Add-Member -NotePropertyName 'recovery_gate_ok'     -NotePropertyValue $gateOk     -Force
        $obj | Add-Member -NotePropertyName 'recovery_gate_reason' -NotePropertyValue $gateReason -Force
      }
      if ($FAULT_INJECT_TYPE -eq "net_no_ipv4_on_iface") {
        $obj | Add-Member -NotePropertyName 'recovery_ip_refresh_used'   -NotePropertyValue $script:NoIpv4IpRefreshUsed   -Force
        $obj | Add-Member -NotePropertyName 'recovery_ip_refresh_reason' -NotePropertyValue $script:NoIpv4IpRefreshReason -Force
      }
      if ($null -ne $script:WifiInjectorStopOk) {
        $obj | Add-Member -NotePropertyName 'injector_stop_ok' -NotePropertyValue $script:WifiInjectorStopOk -Force
      }
      if ($FAULT_INJECT_TYPE -in @("net_wrong_default_route","net_gateway_unreachable")) {
        if ($null -ne $script:RouteFaultPreDefaultRoute) {
          $obj | Add-Member -NotePropertyName 'route_restore_source' -NotePropertyValue 'host_pre_route_snapshot' -Force
          $obj | Add-Member -NotePropertyName 'route_restore_pre_iface' -NotePropertyValue ([string]$script:RouteFaultPreDefaultRoute.iface) -Force
          $obj | Add-Member -NotePropertyName 'route_restore_pre_gateway' -NotePropertyValue ([string]$script:RouteFaultPreDefaultRoute.gateway) -Force
          $obj | Add-Member -NotePropertyName 'route_restore_pre_gateway_hex' -NotePropertyValue ([string]$script:RouteFaultPreDefaultRoute.gateway_hex) -Force
          $obj | Add-Member -NotePropertyName 'route_restore_pre_metric' -NotePropertyValue ([string]$script:RouteFaultPreDefaultRoute.metric) -Force
        }
        foreach ($rr in @($script:RouteFaultRestoreResults)) {
          if ($null -eq $rr -or [string]::IsNullOrWhiteSpace([string]$rr.phase)) { continue }
          $phaseName = ([string]$rr.phase).ToLowerInvariant()
          $obj | Add-Member -NotePropertyName ("route_restore_{0}_ok" -f $phaseName) -NotePropertyValue ([bool]$rr.ok) -Force
          $obj | Add-Member -NotePropertyName ("route_restore_{0}_reason" -f $phaseName) -NotePropertyValue ([string]$rr.reason) -Force
        }
      }
      $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
      [System.IO.File]::WriteAllText($outPath, (ConvertTo-Json -InputObject $obj -Depth 6), $utf8NoBom)
      Write-Host ("[{0}] _net_outcome.json: recovery_gate_ok={1} injector_stop_ok={2} ip_refresh_used={3}" -f $FAULT_INJECT_TYPE, $obj.recovery_gate_ok, $script:WifiInjectorStopOk, $script:NoIpv4IpRefreshUsed) -ForegroundColor Cyan
    } catch {
      Write-Host ("WARN: failed to inject wifi gate fields into _net_outcome.json: " + $_.Exception.Message) -ForegroundColor Yellow
    }
  }
}



# =====================[ Stop & Collect ]=============
Stop-HilogLiveCapture
Stop-HilogPersistent -SN $SN

# dmesg after (raw + utf8)
$DmesgAfterRaw = Join-Path $RunDir "dmesg_after.log"
Save-Dmesg -SN $SN -OutHost $DmesgAfterRaw -RunDir $RunDir -Tag "after"
$DmesgAfterUtf8 = Join-Path $RunDir "dmesg_after.utf8.log"
Convert-FileToUtf8 -Src $DmesgAfterRaw -Dst $DmesgAfterUtf8

# Pull wukong report
$WkLocalRoot = Join-Path $RunDir "wukong_report"
if ($ENABLE_WUKONG_EXEC -or $ENABLE_SPECIAL_SWEEP) {
  $ReportRootRemote = "/data/local/tmp/wukong/report"

  # hdc output may be $null; never call .Trim() on $null
  $repLines = @(hdc -t $SN shell ("ls -1 " + $ReportRootRemote + " 2>/dev/null") 2>$null)

  $Rep = ""
  if ($repLines -and $repLines.Count -gt 0) {
    $Rep = ($repLines | ForEach-Object { [string]$_ } |
              Where-Object { $_ -and $_.Trim().Length -gt 0 } |
              Select-Object -Last 1)
    if ($Rep) { $Rep = $Rep.Trim() } else { $Rep = "" }
  }

  Ensure-Dir $WkLocalRoot
  if ($Rep) {
    Write-Host "Pull wukong report: $Rep"
    hdc -t $SN file recv ("$ReportRootRemote/$Rep") $WkLocalRoot | Out-Null
  } else {
    Write-Host "[wukong] No report directory found under $ReportRootRemote" -ForegroundColor DarkGray
  }
} else {
  Write-Host "[wukong] disabled in this run; skip pulling report." -ForegroundColor DarkGray
}


# Pull faultlog + hilog_bin
$FaultLocal = Join-Path $RunDir "faultlog"
hdc -t $SN file recv $FaultRootRemote $FaultLocal | Out-Null

$HilogBinLocal = Join-Path $RunDir "hilog_bin"
hdc -t $SN file recv "/data/log/hilog" $HilogBinLocal | Out-Null

# Assemble hilog_text_full.log + hilog_index.txt from .gz segments
$HilogAll   = Join-Path $RunDir "hilog_text_full.log"
$HilogIndex = Join-Path $RunDir "hilog_index.txt"
$ok = Build-HilogFromGz -BinDir $HilogBinLocal -OutAll $HilogAll -OutIndex $HilogIndex
if (-not $ok) {
  Write-Host "No .gz segments detected; try textual fallback"
  Merge-HilogTextFallback -BinDir $HilogBinLocal -OutAll $HilogAll -OutIndex $HilogIndex | Out-Null
}

# Optional: prune device hilog segments
if ($ENABLE_PRUNE_DEVICE_HILOG) {
  Prune-DeviceHilogSegments -SN $SN -Keep $PRUNE_KEEP_SEGMENTS
}
Collect-FaultmonFiles -SN $SN -RunDir $RunDir
# Post faultlog diff (new-only copy)
$PostFaultList = Get-RemoteFileList -SN $SN -Root $FaultRootRemote
$FaultNew = Join-Path $RunDir "faultlog_new"
Ensure-Dir $FaultNew
$NewFaultRel = @()
foreach ($p in $PostFaultList) { if ($PreFaultList -notcontains $p) { $NewFaultRel += $p } }
foreach ($rel in $NewFaultRel) {
  $src = Join-Path $FaultLocal $rel
  if (Test-Path $src) {
    $dst = Join-Path $FaultNew $rel
    Ensure-Dir (Split-Path $dst -Parent)
    Copy-Item $src $dst -Force
  }
}

# =====================[ Quick Analysis ]=============
Write-Host ""
Write-Host "== Presence check =="
"{0,-22}{1}" -f "hilog_text_full:", (Test-Path $HilogAll)
"{0,-22}{1}" -f "hilog_index.txt:", (Test-Path $HilogIndex)
"{0,-22}{1}" -f "hilog_raw.log:",   (Test-Path $HilogRaw)
"{0,-22}{1}" -f "dmesg_before:",    (Test-Path $DmesgBeforeRaw)
"{0,-22}{1}" -f "dmesg_after:",     (Test-Path $DmesgAfterRaw)
"{0,-22}{1}" -f "dmesg_before.u8:", (Test-Path $DmesgBeforeUtf8)
"{0,-22}{1}" -f "dmesg_after.u8:",  (Test-Path $DmesgAfterUtf8)
"{0,-22}{1}" -f "faultlog/:",       (Test-Path $FaultLocal)
"{0,-22}{1}" -f "wukong_report/:",  ($WkLocalRoot -and (Test-Path $WkLocalRoot))

# quick hilog_index self-check
$HilogIndexStatus = Test-HilogIndex -IndexPath $HilogIndex -AllPath $HilogAll
Write-Host ("hilog_index check: " + $HilogIndexStatus)

# show latest wukong report heads
$LatestRepDir = $null
$WkLog = $null
$WkCsv = $null
$WukongTaskTotal   = $null
$WukongTaskSuccess = $null
$WukongTaskFail    = $null
$WukongTaskOther   = $null

if ($WkLocalRoot -and (Test-Path -LiteralPath $WkLocalRoot)) {
  $LatestRepDir = Get-ChildItem $WkLocalRoot -Directory | Sort-Object Name | Select-Object -Last 1
  if ($LatestRepDir) {
    $WkLog = Join-Path $LatestRepDir.FullName "wukong.log"
    $WkCsv = Join-Path $LatestRepDir.FullName "wukong_report.csv"
  }
}

if ($LatestRepDir -and $WkCsv -and (Test-Path -LiteralPath $WkCsv)){
  Write-Host ""
  Write-Host "== wukong_report.csv (first 20 rows) =="

  # simple summary: count tasks and status distribution
  try {
    $wkRows = Import-Csv -Path $WkCsv -ErrorAction Stop
    if ($wkRows) {
      $WukongTaskTotal = $wkRows.Count
      $statusCol = $null
      $first = $wkRows[0]
      if ($first) {
        $cols = $first.PSObject.Properties.Name
        foreach ($c in $cols) {
          if ($c -match '(?i)status') { $statusCol = $c; break }
        }
      }
      if ($statusCol) {
        $WukongTaskSuccess = ($wkRows | Where-Object { $_.$statusCol -match '(?i)success|pass' }).Count
        $WukongTaskFail    = ($wkRows | Where-Object { $_.$statusCol -match '(?i)fail|error|timeout' }).Count
        $WukongTaskOther   = $WukongTaskTotal - $WukongTaskSuccess - $WukongTaskFail
        Write-Host ("wukong_report summary: total={0}, success={1}, fail={2}, other={3}" -f $WukongTaskTotal, $WukongTaskSuccess, $WukongTaskFail, $WukongTaskOther)
      } else {
        Write-Host ("wukong_report rows: {0} (no 'status' column found)" -f $WukongTaskTotal)
      }
    }
  } catch {
    Write-Host ("wukong_report parse failed: " + $_.Exception.Message)
  }
}

# faultlog sample (prefer new-only)
$SampleRoot = $FaultLocal
$__newDir = Join-Path $RunDir "faultlog_new"
$__newFiles = Get-ChildItem $__newDir -Recurse -File -ErrorAction SilentlyContinue
if ($__newFiles -and $__newFiles.Count -gt 0) {
  $SampleRoot = $__newDir
}
if (($FAULTLOG_SAMPLE_N -gt 0) -and (Test-Path $SampleRoot)) {
  $FaultFiles = Get-ChildItem $SampleRoot -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First $FAULTLOG_SAMPLE_N
  Write-Host ""
  Write-Host ("== faultlog sample ({0}) ==" -f $FAULTLOG_SAMPLE_N)
  foreach ($f in $FaultFiles) {
    Write-Host ("--- " + $f.FullName)
    Get-Content $f.FullName -TotalCount 40 | ForEach-Object { $_ }
    Write-Host ""
  }
}

# quick counters on UTF-8 dmesg
$BinderB = Count-Pattern -Path $DmesgBeforeUtf8 -Regex 'binder:\s+\d+:\d+\s+transaction failed'
$BinderA = Count-Pattern -Path $DmesgAfterUtf8  -Regex 'binder:\s+\d+:\d+\s+transaction failed'
$AvcB    = Count-Pattern -Path $DmesgBeforeUtf8 -Regex '\bavc:\s+denied\b'
$AvcA    = Count-Pattern -Path $DmesgAfterUtf8  -Regex '\bavc:\s+denied\b'
$HungB   = Count-Pattern -Path $DmesgBeforeUtf8 -Regex 'hungtask_user process'
$HungA   = Count-Pattern -Path $DmesgAfterUtf8  -Regex 'hungtask_user process'

$BinderD = $BinderA - $BinderB
$AvcD    = $AvcA    - $AvcB
$HungD   = $HungA   - $HungB

$neg = ($BinderD -lt 0) -or ($AvcD -lt 0) -or ($HungD -lt 0)
if ($BinderD -lt 0) { $BinderD = 0 }
if ($AvcD    -lt 0) { $AvcD    = 0 }
if ($HungD   -lt 0) { $HungD   = 0 }

Write-Host ""
Write-Host "== dmesg anomaly counters (UTF-8) =="
"{0,-22}{1,8} => {2,8} (delta {3,6})" -f "binder failed:", $BinderB, $BinderA, $BinderD
"{0,-22}{1,8} => {2,8} (delta {3,6})" -f "avc denied:",    $AvcB,    $AvcA,    $AvcD
"{0,-22}{1,8} => {2,8} (delta {3,6})" -f "hungtask:",      $HungB,   $HungA,   $HungD

if ($neg) {
  Write-Host "WARN: negative dmesg delta detected (likely ring buffer truncation). Clamped to 0."
}

$RunEnd = Get-Date

# summary
$SumTxt = Join-Path $RunDir "_summary.txt"
$LatestRepPath = if ($LatestRepDir) { $LatestRepDir.FullName } else { "(none)" }
$WkCsvExists = (-not [string]::IsNullOrWhiteSpace($WkCsv)) -and (Test-Path -LiteralPath $WkCsv)
$WkLogExists = (-not [string]::IsNullOrWhiteSpace($WkLog)) -and (Test-Path -LiteralPath $WkLog)
$NewFaultCount = (Get-ChildItem (Join-Path $RunDir "faultlog_new") -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
$NetFaultTypeSummary = ""
$NetIfaceUsedSummary = ""
$NetInjectOkSummary = ""
$NetFaultObservedSummary = ""
$NetRecoveryObservedSummary = ""
$NetFaultReasonSummary = ""
$NetRecoveryReasonSummary = ""
if ($NetOutcome) {
  $NetFaultTypeSummary = [string]$NetOutcome.net_fault_type
  $NetIfaceUsedSummary = [string]$NetOutcome.iface_used
  $NetInjectOkSummary = [string]$NetOutcome.inject_ok
  $NetFaultObservedSummary = [string]$NetOutcome.fault_observed
  $NetRecoveryObservedSummary = [string]$NetOutcome.recovery_observed
  $NetFaultReasonSummary = [string]$NetOutcome.fault_observation_reason
  $NetRecoveryReasonSummary = [string]$NetOutcome.recovery_observation_reason
}

@(
  "run_dir: $RunDir"
  "run_id: " + (Split-Path $RunDir -Leaf)
  "script_version: $SCRIPT_VERSION"
  "scenario_tag: $SCENARIO_TAG"
  "device_sn: $SN"
  "run_start: " + $RunStart.ToString("yyyy-MM-dd HH:mm:ss")
  "run_end: " + $RunEnd.ToString("yyyy-MM-dd HH:mm:ss")
  "hilog_text_full: " + (Test-Path $HilogAll)
  "hilog_index: " + (Test-Path $HilogIndex)
  "hilog_index_check: " + $HilogIndexStatus
  "hilog_raw: " + (Test-Path $HilogRaw)
  "dmesg_before_utf8: " + (Test-Path $DmesgBeforeUtf8)
  "dmesg_after_utf8: " + (Test-Path $DmesgAfterUtf8)
  "wukong_report: " + ($WkLocalRoot -and (Test-Path -LiteralPath $WkLocalRoot))
  "latest_report_dir: $LatestRepPath"
  "wukong_log: $WkLogExists"
  "wukong_csv: $WkCsvExists"
  "wukong_cmdline: $WukongCmdline"
  ("wukong_task_total: " + $WukongTaskTotal)
  ("wukong_task_success: " + $WukongTaskSuccess)
  ("wukong_task_fail: " + $WukongTaskFail)
  ("wukong_task_other: " + $WukongTaskOther)
  ("faultlog_all: " + (Test-Path $FaultLocal))
  ("faultlog_new_count: " + $NewFaultCount)
  ("binder_failed_before: " + $BinderB)
  ("binder_failed_after: " + $BinderA)
  ("binder_failed_delta: " + ($BinderA-$BinderB))
  ("avc_denied_before: " + $AvcB)
  ("avc_denied_after: " + $AvcA)
  ("avc_denied_delta: " + ($AvcA-$AvcB))
  ("hungtask_before: " + $HungB)
  ("hungtask_after: " + $HungA)
  ("hungtask_delta: " + ($HungA-$HungB))
  ("net_fault_type: " + $NetFaultTypeSummary)
  ("iface_used: " + $NetIfaceUsedSummary)
  ("inject_ok: " + $NetInjectOkSummary)
  ("fault_observed: " + $NetFaultObservedSummary)
  ("recovery_observed: " + $NetRecoveryObservedSummary)
  ("fault_observation_reason: " + $NetFaultReasonSummary)
  ("recovery_observation_reason: " + $NetRecoveryReasonSummary)
) | Out-File -FilePath $SumTxt -Encoding ASCII -Force
# run-level JSON metadata for downstream LLM pipeline
try {
  $RunMetaPath = Join-Path $RunDir "_run_meta.json"
  $gt = Infer-GTLabels -ScenarioTag $SCENARIO_TAG -FaultType $FAULT_INJECT_TYPE -EnableFaultInject $ENABLE_FAULT_INJECT
  $wukProfile = Get-WukongProfile -Exec $ENABLE_WUKONG_EXEC -Special $ENABLE_SPECIAL_SWEEP

  # 閻犱緤绱曢悾濠氬嫉椤掍緡鍋?run window 闁革负鍔夐埀顒佺矋濠㈡绮?epoch(ms)闁炽儲绻€缁楀倿鎯冮崟顓犲炊闁告瑱缍囩槐?
  # 1) 闁稿繐鐗忛弫?host->board 闁哄嫮濮撮惃鐘差嚗濡も偓閸╁瞼鍒掑Δ鍐╂缂佹劖顨呰ぐ?
  # 2) 闁告劕绉崇槐顓㈠礂閸垺鏆?faultmon 闁?poke(run_begin/run_end) 闁煎浜滄慨鈺冩啑娴ｇ顥呴柨娑樼焸娴尖晠宕楀鍫熺《 run 濞戞挻褰冨┃鈧?
  $winStartBoardMs = 0
  $winEndBoardMs   = 0
  $winSource       = "host_map"

  if ($null -ne $BoardEpochMsAtStart -and $null -ne $HostEpochMsAtStart) {
    $winStartBoardMs = [Int64]($BoardEpochMsAtStart + ($RunWindowHostEpochMsStart - $HostEpochMsAtStart))
    $winEndBoardMs   = [Int64]($BoardEpochMsAtStart + ($RunWindowHostEpochMsEnd   - $HostEpochMsAtStart))
  } else {
    $winSource = "host_elapsed"
    if ($null -ne $RunWindowStart -and $null -ne $RunWindowEnd -and $null -ne $BoardEpochMsAtStart) {
      $hostWinElapsedMs = [Int64]((New-TimeSpan -Start $RunWindowStart -End $RunWindowEnd).TotalMilliseconds)
      $winStartBoardMs  = [Int64]$BoardEpochMsAtStart
      $winEndBoardMs    = $winStartBoardMs + $hostWinElapsedMs
    }
  }

  $evWin = $null
  if (Test-PathSafe $script:FaultmonEventsLocal) {
    $evWin = Summarize-FaultmonEventsForRun -EventsPath $script:FaultmonEventsLocal -RunId $RunId -FallbackStartMs $winStartBoardMs -FallbackEndMs $winEndBoardMs
    if ($null -ne $evWin.window_start_ms -and $null -ne $evWin.window_end_ms -and $evWin.window_end_ms -ge $evWin.window_start_ms) {
      $winStartBoardMs = [Int64]$evWin.window_start_ms
      $winEndBoardMs   = [Int64]$evWin.window_end_ms
      $winSource       = [string]$evWin.window_source
    }
  }

  $evSum = Summarize-FaultmonEventsInWindow -EventsPath $script:FaultmonEventsLocal -StartMs $winStartBoardMs -EndMs $winEndBoardMs
  # metrics summary in the same board-ms window
  $metSum = Read-MetricsSummaryInWindow -CsvPath $script:FaultmonMetricsLocal -StartMs $winStartBoardMs -EndMs $winEndBoardMs

  # richer OBS multi-labels (scores/primary/secondary/confounders)
  $obs = Compute-ObsMultiLabels `
    -GTFamily $gt.gt_family `
    -WukongProfile $wukProfile `
    -EventSummary $evSum `
    -MetricsSummary $metSum `
    -NewFaultCount $NewFaultCount `
    -BinderDelta ($BinderA - $BinderB) `
    -AvcDelta ($AvcA - $AvcB) `
    -HungDelta ($HungA - $HungB)

  # labels: auto (gt+obs) + user provided (WK_LABELS)
  $labels_user = Split-RunLabels $env:WK_LABELS
  $isAnomalyStr = "0"
  if ($gt.gt_is_anomaly) { $isAnomalyStr = "1" }
  $labels_gt = @(
    ("scenario=" + $SCENARIO_TAG),
    ("fault_type=" + $FAULT_INJECT_TYPE),
    ("family=" + $gt.gt_family),
    ("severity=" + $gt.gt_severity),
    ("run_kind=" + $gt.gt_run_kind),
    ("is_anomaly=" + $isAnomalyStr)
  )

  $labels_obs = @(
    ("wukong=" + $wukProfile),
    ("obs_state=" + $obs.obs_fault_state),
    ("obs_primary=" + $obs.obs_primary_family)
  )

  foreach ($f in @($obs.obs_families))      { $labels_obs += ("obs_fault=" + $f) }
  foreach ($f in @($obs.obs_families_warn)) { $labels_obs += ("obs_warn=" + $f) }
  foreach ($c in @($obs.obs_confounders))   { $labels_obs += ("confounder=" + $c) }

  $labels = Merge-RunLabels (Merge-RunLabels $labels_gt $labels_obs) $labels_user

  $obsHasRule = ($evSum.cpu_hotspot -gt 0) -or ($evSum.mem_pressure -gt 0) -or ($evSum.io_pressure -gt 0)
  $obsHasFaultlog = ($NewFaultCount -gt 0)
  $obsHasDmesg = (($BinderA-$BinderB) -gt 0) -or (($AvcA-$AvcB) -gt 0) -or (($HungA-$HungB) -gt 0)

  $obsFaultState = "normal"
  if ($obsHasFaultlog -or $obsHasRule) { $obsFaultState = "fault" }
  elseif ($obsHasDmesg) { $obsFaultState = "warning" }

  $meta = [ordered]@{
    run_dir            = $RunDir
    run_id             = (Split-Path $RunDir -Leaf)
    script_version     = $SCRIPT_VERSION
    scenario_tag       = $SCENARIO_TAG
    fault_type         = $FAULT_INJECT_TYPE
    device_sn          = $SN
    run_start          = $RunStart
    run_end            = $RunEnd
    run_window_start   = $RunWindowStart
    run_window_end     = $RunWindowEnd
    run_window_host_epoch_ms_start = $RunWindowHostEpochMsStart
    run_window_host_epoch_ms_end   = $RunWindowHostEpochMsEnd
    run_window_board_ms_start      = $winStartBoardMs
    run_window_board_ms_end        = $winEndBoardMs
    run_window_source              = $winSource
    hilog_text_full    = (Test-Path $HilogAll)
    hilog_index        = (Test-Path $HilogIndex)
    hilog_index_check  = $HilogIndexStatus
    dmesg_before_utf8  = (Test-Path $DmesgBeforeUtf8)
    dmesg_after_utf8   = (Test-Path $DmesgAfterUtf8)
    wukong_report        = ($WkLocalRoot -and (Test-Path -LiteralPath $WkLocalRoot))
    wukong_cmdline       = $WukongCmdline
    wukong_enabled       = $ENABLE_WUKONG_EXEC
    wukong_special_sweep = $ENABLE_SPECIAL_SWEEP
    wukong_task_total    = $WukongTaskTotal
    wukong_task_success  = $WukongTaskSuccess
    wukong_task_fail     = $WukongTaskFail
    wukong_task_other    = $WukongTaskOther
    binder_failed_before = $BinderB
    binder_failed_after  = $BinderA
    avc_denied_before    = $AvcB
    avc_denied_after     = $AvcA
    hungtask_before      = $HungB
    hungtask_after       = $HungA
    faultlog_all          = (Test-Path $FaultLocal)
    faultlog_new_count    = $NewFaultCount
    # faultmon + PyRCA metadata
    faultmon_enabled      = $EnableFaultmon
    faultmon_board_date   = $script:FaultmonBoardDate
    faultmon_metrics_csv  = $script:FaultmonMetricsLocal
    faultmon_events_jsonl = $script:FaultmonEventsLocal
    faultmon_has_metrics  = $script:FaultmonHasMetrics
    faultmon_has_events   = $script:FaultmonHasEvents
    # host / board time alignment
    host_epoch_ms_start   = $HostEpochMsAtStart
    board_epoch_ms_start  = $BoardEpochMsAtStart
    time_skew_ms          = $TimeSkewMs
    baseline_sec         = $BASELINE_SEC
    run_window_target_sec = $targetWindowSec
    probe_profile_id     = $NET_PROBE_PROFILE_ID
    active_ping_ip       = $NET_ACTIVE_PING_IP
    active_dns_host      = $NET_ACTIVE_DNS_HOST
    dns_proof_base_ip    = $NET_DNS_PROOF_BASE_IP
    net_target_ip        = $NET_TARGET_IP
    target_ip            = if ($FAULT_INJECT_TYPE -eq "net_public_ip_unreachable") { $NET_TARGET_IP } else { "" }
    diagnostic_probe_targets = $NET_DIAGNOSTIC_PROBE_TARGETS
    baseline_policy      = $NET_BASELINE_POLICY
    baseline_max_attempts = $NET_BASELINE_MAX_ATTEMPTS
    baseline_consecutive_required = $NET_BASELINE_CONSECUTIVE_REQUIRED
    baseline_sleep_sec   = $NET_BASELINE_SLEEP_SEC
    probe_profile_reason = $NET_PROBE_PROFILE_REASON
    ap_or_network_profile = $NET_AP_OR_NETWORK_PROFILE
    profile_effective_from = $NET_PROFILE_EFFECTIVE_FROM
    profile_approved_by = $NET_PROFILE_APPROVED_BY
    profile_approval_time = $NET_PROFILE_APPROVAL_TIME
    label_version      = "v3_gt_obs_labels"
    labels             = $labels
    labels_gt          = $labels_gt
    labels_obs         = $labels_obs
    labels_user        = $labels_user

    metrics_summary    = $metSum
    obs_multi          = $obs

    gt_run_kind        = $gt.gt_run_kind
    gt_family          = $gt.gt_family
    gt_severity        = $gt.gt_severity
    gt_is_anomaly      = $gt.gt_is_anomaly

    wukong_profile     = $wukProfile

    events_win_start_ms = $winStartBoardMs
    events_win_end_ms   = $winEndBoardMs
    obs_event_total     = $evSum.total
    obs_cpu_hotspot     = $evSum.cpu_hotspot
    obs_mem_pressure    = $evSum.mem_pressure
    obs_io_pressure     = $evSum.io_pressure
    obs_poke_begin      = $evSum.poke_begin
    obs_poke_end        = $evSum.poke_end
    obs_fault_state     = $obsFaultState
    net_fault_type      = $NetFaultTypeSummary
    iface_used          = $NetIfaceUsedSummary
    inject_ok           = $NetOutcome.inject_ok
    fault_observed      = $NetOutcome.fault_observed
    recovery_observed   = $NetOutcome.recovery_observed
    fault_observation_reason = $NetFaultReasonSummary
    recovery_observation_reason = $NetRecoveryReasonSummary
  }
  $metaJson = ConvertTo-Json -InputObject ([pscustomobject]$meta) -Depth 10
  $metaDir = Split-Path $RunMetaPath -Parent
  Ensure-Dir $metaDir
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($RunMetaPath, $metaJson, $utf8NoBom)
} catch {
  Write-Host ("WARN: failed to write _run_meta.json: " + $_.Exception.Message)
}

if ($ENABLE_QWEN3_CLOSED_LOOP) {
  Write-Host ""
  Write-Host "== Qwen3 closed-loop: upload -> infer -> download =="
  $r = Invoke-Qwen3ClosedLoopForRun -RunDir $RunDir

if ($r -and $r.ok) {
  $localOut = Join-Path $RunDir "_server_out"
  $actionsPath = Join-Path $localOut "actions.json"

  # 等 actions.json 出现（最长 120 秒）
  for ($i=0; $i -lt 120; $i++) {
    if (Test-Path -LiteralPath $actionsPath) { break }
    Start-Sleep -Seconds 1
  }

  if (Test-Path -LiteralPath $actionsPath) {
    # Apply actions only when explicitly enabled and DeviceTarget is valid
if ($env:WK_APPLY_ACTIONS -and $env:WK_APPLY_ACTIONS -ne "0") {
  if ([string]::IsNullOrWhiteSpace($TARGET)) {
    Write-Host "[actions] DeviceTarget empty; skip execution."
  } else {
    Invoke-WkApplyActionsFromServerOut -RunDir $RunDir -DeviceTarget $TARGET
  }
} else {
  Write-Host "[actions] WK_APPLY_ACTIONS=0; skip execution."
}

  } else {
    Write-Host "[actions] actions.json still missing after wait; skip execution."
  }
} else {
  Write-Host "[actions] closed-loop not ok; skip execution."
}
  if (-not $r.ok) {
    Write-Host ("WARN: closed-loop failed at step=" + $r.step + " exit=" + $r.exit)
    if ($r.msg) { Write-Host ("WARN: " + $r.msg) }
  } else {
    $diag = Join-Path (Join-Path $RunDir "_server_out") "diagnosis.json"
    if (Test-Path -LiteralPath $diag) {
      Write-Host "== diagnosis.json (first 40 lines) =="
      Get-Content -LiteralPath $diag -Encoding UTF8 -TotalCount 40 | ForEach-Object { $_ }
    } else {
      Write-Host "WARN: _server_out downloaded but diagnosis.json not found"
    }
  }
}


Write-Host ""
Write-Host "== Done =="
Write-Host ("Artifacts under: " + $RunDir)
#endregion Main
#$env:WK_ENABLE_QWEN3_CLOSED_LOOP="1"
#$env:QWEN3_SERVER_HOST="183.56.183.131"
#$env:QWEN3_SSH_HOST="183.56.183.131"
#$env:WK_QWEN3_SERVER="xrh@$env:QWEN3_SSH_HOST"   # legacy compatibility
#$env:WK_QWEN3_SSH_KEY="$env:USERPROFILE\.ssh\qwen3_server_ed25519"
## 可选：如果你 server repo 不在默认位置
## $env:WK_QWEN3_REMOTE_ROOT="/home/xrh/qwen3_os_fault"
