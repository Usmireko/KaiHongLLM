param(
  [Parameter(Mandatory=$false)][string]$SN = "7001005458323933328a017ce1c43800",
  [Parameter(Mandatory=$false)][string[]]$Subtypes,
  [Parameter(Mandatory=$false)][int]$AcceptedPerSubtype = 1,
  [Parameter(Mandatory=$false)][string]$PingIP = "8.8.8.8",
  [Parameter(Mandatory=$false)][string]$Iface = "wlan0",
  [Parameter(Mandatory=$false)][string]$WlanIface = "wlan0",
  [Parameter(Mandatory=$false)][string]$DnsHost = "www.baidu.com",
  [Parameter(Mandatory=$false)][string]$ProbeProfileId = "",
  [Parameter(Mandatory=$false)][string]$ProbeProfilePath = "",
  [Parameter(Mandatory=$false)][int]$NetLinkDownHoldSec = 15,
  [Parameter(Mandatory=$false)][int]$NetFlapCount = 2,
  [Parameter(Mandatory=$false)][int]$NetFlapDownSec = 3,
  [Parameter(Mandatory=$false)][int]$NetFlapUpSec = 3,
  [Parameter(Mandatory=$false)][string]$NetBadDns = "127.0.0.2",
  [Parameter(Mandatory=$false)][string]$NetTargetIP = "8.8.4.4",
  [Parameter(Mandatory=$false)][string]$OutDir,
  [Parameter(Mandatory=$false)][int]$MaxRetryPerSubtype = 1,
  [Parameter(Mandatory=$false)][switch]$StopOnBlocker,
  [Parameter(Mandatory=$false)][switch]$Smoke,
  [Parameter(Mandatory=$false)][switch]$DryRun,
  [Parameter(Mandatory=$false)][switch]$FixcheckOnly,
  [Parameter(Mandatory=$false)][switch]$CandidateOnly,
  [Parameter(Mandatory=$false)][switch]$NoL1L2,
  [Parameter(Mandatory=$false)][int]$PreBaselineMaxAttempts = 8,
  [Parameter(Mandatory=$false)][int]$PreBaselineConsecutivePasses = 3,
  [Parameter(Mandatory=$false)][int]$PreBaselineSleepSec = 5,
  [Parameter(Mandatory=$false)][int]$SleepBetweenRunsSec = 30
)

# run_wukong_weekend.ps1 - NET batch runner for KaiHongOS / OpenHarmony
#
# Purpose:
#   - Loop over user-selected NET fault subtypes
#   - For each subtype, drive run_wukong_collect_refactor.ps1 until -AcceptedPerSubtype
#     accepted runs are gathered (or -MaxRetryPerSubtype is exhausted)
#   - Pre-baseline + post-baseline gate; validator integration; jsonl ledgers
#   - Stop-on-blocker safety; never promote dry-run / fixcheck / failed runs
#
# Hard rules (do not relax):
#   - run_id MUST come from collector stdout "Artifacts under: <path>" (NOT
#     from Get-ChildItem newest)
#   - 1.1.1.1 must NEVER be the active baseline / recovery probe
#   - validator semantics are NOT modified here; we just call wk_validate_run_net.ps1
#   - frozen first batch (dataset_batches\net_formal_batch_70_20260429) is NEVER
#     written to from this script
#   - L1/L2 export is NOT triggered from here even if -NoL1L2 is omitted; the
#     flag exists to make the no-L1L2 contract explicit
#   - -FixcheckOnly must never append to accepted ledger
#   - -CandidateOnly writes candidate_runs.jsonl, never accepted_runs.jsonl

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
try { chcp 65001 | Out-Null } catch {}
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch {}

# Project root = directory of this script
if ($PSScriptRoot) { $ScriptDir = $PSScriptRoot }
else { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

$CollectScript = Join-Path $ScriptDir "run_wukong_collect_refactor.ps1"
$ValidatorScript = Join-Path $ScriptDir "wk_validate_run_net.ps1"

if (-not (Test-Path -LiteralPath $CollectScript)) {
  throw ("collector script missing: {0}" -f $CollectScript)
}
if (-not (Test-Path -LiteralPath $ValidatorScript)) {
  throw ("validator script missing: {0}" -f $ValidatorScript)
}

# Default subtype order (batch2)
$DefaultSubtypes = @(
  "net_dns_fail",
  "net_public_ip_unreachable",
  "net_no_default_route",
  "net_wrong_default_route",
  "net_no_ipv4_on_iface",
  "net_wifi_disconnect",
  "net_gateway_unreachable"
)

if (-not $Subtypes -or $Subtypes.Count -eq 0) {
  $Subtypes = $DefaultSubtypes
} else {
  $normalizedSubtypes = New-Object System.Collections.Generic.List[string]
  foreach ($item in $Subtypes) {
    if (-not $item) { continue }
    foreach ($part in ($item -split ",")) {
      $trimmed = $part.Trim()
      if ($trimmed -ne "") { [void]$normalizedSubtypes.Add($trimmed) }
    }
  }
  $Subtypes = [string[]]$normalizedSubtypes.ToArray()
  if ($Subtypes.Count -eq 0) {
    throw "no valid subtypes specified"
  }
}

if ($CandidateOnly -and $FixcheckOnly) {
  throw "-CandidateOnly cannot be combined with -FixcheckOnly"
}

# Validate that all requested subtypes are recognized; refuse unknown to avoid
# silent typos while allowing collector-supported legacy link subtypes.
$KnownSubtypes = @($DefaultSubtypes + @(
  "net_link_down",
  "net_link_flap"
))
foreach ($s in $Subtypes) {
  if (-not ($KnownSubtypes -contains $s)) {
    throw ("unknown subtype '{0}'. Allowed: {1}" -f $s, ($KnownSubtypes -join ","))
  }
}

# Refuse 1.1.1.1 as PingIP gate at parse time - hard guardrail
if ($PingIP -eq "1.1.1.1") {
  throw "PingIP=1.1.1.1 is forbidden as active baseline / recovery gate. Use the legacy default or an explicitly approved probe profile."
}

# OutDir resolution
if (-not $OutDir -or $OutDir -eq "") {
  if ($Smoke) {
    $OutDir = "_smoke_net_batch2_20260430"
  } else {
    $stampDate = Get-Date -Format "yyyyMMdd"
    $OutDir = ("dataset_batches\net_formal_batch2_raw_{0}" -f $stampDate)
  }
}
if (-not [System.IO.Path]::IsPathRooted($OutDir)) {
  $OutDir = Join-Path $ScriptDir $OutDir
}

$LogDir = Join-Path $OutDir "logs"
# Refuse to write into the frozen first batch directory
if ($OutDir -match 'net_formal_batch_70_20260429') {
  throw ("refusing to write into frozen first batch: {0}" -f $OutDir)
}

function Get-ProfileValue([object]$Obj, [string]$Name, $DefaultValue) {
  if ($null -eq $Obj) { return $DefaultValue }
  $prop = $Obj.PSObject.Properties[$Name]
  if ($null -eq $prop) { return $DefaultValue }
  if ($null -eq $prop.Value) { return $DefaultValue }
  if (($prop.Value -is [string]) -and $prop.Value.Trim() -eq "") { return $DefaultValue }
  return $prop.Value
}

$ProfileSelected = $false
$ProfileSourcePath = ""
$ProfileRaw = $null
$DnsProofBaseIP = $PingIP

if ($ProbeProfileId -and -not $ProbeProfilePath) {
  $ProbeProfilePath = Join-Path $OutDir ("probe_profile_{0}.json" -f $ProbeProfileId)
}

if ($ProbeProfilePath) {
  if (-not [System.IO.Path]::IsPathRooted($ProbeProfilePath)) {
    $ProbeProfilePath = Join-Path $ScriptDir $ProbeProfilePath
  }
  if (-not (Test-Path -LiteralPath $ProbeProfilePath)) {
    throw ("probe profile file missing: {0}" -f $ProbeProfilePath)
  }
  $ProfileRaw = Get-Content -LiteralPath $ProbeProfilePath -Raw -Encoding UTF8 | ConvertFrom-Json
  $ProfileSelected = $true
  $ProfileSourcePath = (Resolve-Path -LiteralPath $ProbeProfilePath).Path

  $fileProfileId = [string](Get-ProfileValue $ProfileRaw "probe_profile_id" "")
  if (-not $ProbeProfileId) { $ProbeProfileId = $fileProfileId }
  if ($fileProfileId -and $ProbeProfileId -and $fileProfileId -ne $ProbeProfileId) {
    throw ("probe profile id mismatch: param={0} file={1}" -f $ProbeProfileId, $fileProfileId)
  }

  $profilePingIP = [string](Get-ProfileValue $ProfileRaw "active_ping_ip" $PingIP)
  $profileDnsHost = [string](Get-ProfileValue $ProfileRaw "active_dns_host" $DnsHost)
  $profileDnsProofBaseIP = [string](Get-ProfileValue $ProfileRaw "dns_proof_base_ip" $profilePingIP)

  if ($PSBoundParameters.ContainsKey("PingIP") -and $PingIP -ne $profilePingIP) {
    throw ("PingIP override conflicts with probe profile {0}: param={1} profile={2}" -f $ProbeProfileId, $PingIP, $profilePingIP)
  }
  if ($PSBoundParameters.ContainsKey("DnsHost") -and $DnsHost -ne $profileDnsHost) {
    throw ("DnsHost override conflicts with probe profile {0}: param={1} profile={2}" -f $ProbeProfileId, $DnsHost, $profileDnsHost)
  }
  if ($profileDnsHost -ne "www.baidu.com") {
    throw ("active_dns_host must remain www.baidu.com in this task; profile requested {0}" -f $profileDnsHost)
  }

  $PingIP = $profilePingIP
  $DnsHost = $profileDnsHost
  $DnsProofBaseIP = $profileDnsProofBaseIP

  if ($ProfileRaw.PSObject.Properties["baseline_max_attempts"] -and -not $PSBoundParameters.ContainsKey("PreBaselineMaxAttempts")) {
    $PreBaselineMaxAttempts = [int]$ProfileRaw.baseline_max_attempts
  }
  if ($ProfileRaw.PSObject.Properties["baseline_consecutive_required"] -and -not $PSBoundParameters.ContainsKey("PreBaselineConsecutivePasses")) {
    $PreBaselineConsecutivePasses = [int]$ProfileRaw.baseline_consecutive_required
  }
  if ($ProfileRaw.PSObject.Properties["baseline_sleep_sec"] -and -not $PSBoundParameters.ContainsKey("PreBaselineSleepSec")) {
    $PreBaselineSleepSec = [int]$ProfileRaw.baseline_sleep_sec
  }
}

if (-not $ProbeProfileId) {
  $ProbeProfileId = if ($Smoke) { "legacy_batch2_default" } else { "runtime_default" }
}

if ($PingIP -eq "1.1.1.1" -or $DnsProofBaseIP -eq "1.1.1.1") {
  throw "1.1.1.1 is forbidden as active baseline / recovery / DNS-proof public probe."
}
if ($DnsHost -ne "www.baidu.com") {
  throw ("DnsHost must remain www.baidu.com in this task; active value is {0}" -f $DnsHost)
}

$diagnosticTargets = Get-ProfileValue $ProfileRaw "diagnostic_probe_targets" $null
$ProbeProfileRecord = [ordered]@{
  probe_profile_id = $ProbeProfileId
  active_ping_ip = $PingIP
  active_dns_host = $DnsHost
  dns_proof_base_ip = $DnsProofBaseIP
  diagnostic_probe_targets = $diagnosticTargets
  baseline_policy = [string](Get-ProfileValue $ProfileRaw "baseline_policy" "legacy_runner_baseline")
  baseline_max_attempts = $PreBaselineMaxAttempts
  baseline_consecutive_required = $PreBaselineConsecutivePasses
  baseline_sleep_sec = $PreBaselineSleepSec
  probe_profile_reason = [string](Get-ProfileValue $ProfileRaw "probe_profile_reason" "")
  ap_or_network_profile = [string](Get-ProfileValue $ProfileRaw "ap_or_network_profile" "")
  profile_effective_from = Get-ProfileValue $ProfileRaw "profile_effective_from" $null
  profile_approved_by = Get-ProfileValue $ProfileRaw "profile_approved_by" $null
  profile_approval_time = Get-ProfileValue $ProfileRaw "profile_approval_time" $null
  profile_selected = [bool]$ProfileSelected
  profile_path = $ProfileSourcePath
}

if (-not $DryRun) {
  if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
  }
  if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  }
}

# Ledger paths
if ($Smoke) {
  if ($CandidateOnly) {
    $AcceptedLedger = Join-Path $OutDir "smoke_candidate_runs.jsonl"
  } else {
    $AcceptedLedger = Join-Path $OutDir "smoke_accepted_runs.jsonl"
  }
  $RejectedLedger = Join-Path $OutDir "smoke_rejected_runs.jsonl"
  $FixcheckLedger = Join-Path $OutDir "smoke_fixcheck_runs.jsonl"
} else {
  if ($CandidateOnly) {
    $AcceptedLedger = Join-Path $OutDir "candidate_runs.jsonl"
  } else {
    $AcceptedLedger = Join-Path $OutDir "accepted_runs.jsonl"
  }
  $RejectedLedger = Join-Path $OutDir "rejected_runs.jsonl"
  $FixcheckLedger = Join-Path $OutDir "fixcheck_runs.jsonl"
}

$RunStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$SummaryPath = Join-Path $OutDir ("weekend_summary_{0}.md" -f $RunStamp)
$CommandsPath = Join-Path $OutDir ("weekend_commands_{0}.md" -f $RunStamp)

$CommandsRun = New-Object System.Collections.Generic.List[string]
$AcceptedRuns = New-Object System.Collections.Generic.List[object]
$RejectedRuns = New-Object System.Collections.Generic.List[object]
$Blockers = New-Object System.Collections.Generic.List[string]
$PreBaselineStabilizationRecords = New-Object System.Collections.Generic.List[object]
$CollectorStartedCount = 0
$ArtifactParsedCount = 0

function Add-CommandRun([string]$cmd) {
  if ($cmd) { [void]$CommandsRun.Add($cmd) }
}

function Invoke-HdcText([string]$RemoteCommand, [string]$Tag) {
  Add-CommandRun ("hdc -t {0} shell ""{1}""" -f $SN, $RemoteCommand)
  $out = (& hdc -t $SN shell $RemoteCommand 2>&1 | Out-String)
  return [pscustomobject]@{
    tag = $Tag
    command = $RemoteCommand
    text = $out
    exit_code = $LASTEXITCODE
  }
}

function Test-PingUsable([string]$txt) {
  if (-not $txt) { return $false }
  if ($txt -match '(?i)bytes from') { return $true }
  if ($txt -match '(?i)\b[1-9]\s+(packets\s+)?received\b') { return $true }
  if ($txt -match '(?i)\b0%[ ]+packet loss\b') { return $true }
  return $false
}

function Test-PingPass([string]$txt) {
  if (-not $txt) { return $false }
  if ($txt -match '(?i)\b0%[ ]+packet loss\b') { return $true }
  if ($txt -match '(?i)\b3 packets transmitted,\s*3 (packets )?received\b') { return $true }
  return $false
}

function Test-DefaultGwOnIface([string]$routeText, [string]$IfaceName) {
  if (-not $routeText) { return $false }
  $pattern = ('(?m)^{0}\s+00000000\s+[0-9A-F]{{8}}\s+0003\b' -f [regex]::Escape($IfaceName))
  return ($routeText -match $pattern)
}

function Test-NoNetFaultProcess([string]$psText) {
  if (-not $psText) { return $true }
  $lines = $psText -split "`r?`n"
  foreach ($line in $lines) {
    if ($line -match 'net_fault\.sh' -and $line -notmatch 'grep net_fault') {
      return $false
    }
  }
  return $true
}

function Test-NoActiveMarker([string]$lsText) {
  if (-not $lsText) { return $true }
  if ($lsText -match '\.applied\b|\.pid\b|active|injector_|iptables_|wrong_default_route|no_default_route|no_ipv4|wifi_auth|wifi_disconnect|dns_block') {
    return $false
  }
  return $true
}

function Test-NoRootBak([string]$lsText) {
  if (-not $lsText) { return $true }
  return -not ($lsText -match '\.bak\b')
}

function Test-IfaceIpv4([string]$ifconfigText, [string]$wpaText) {
  if ($ifconfigText -match 'inet\s+(addr:)?\d+\.\d+\.\d+\.\d+\b') { return $true }
  if ($wpaText -match '(?m)^ip_address=\d+\.\d+\.\d+\.\d+\s*$') { return $true }
  return $false
}

function Get-Baseline([string]$Phase, [switch]$RequireIpFullPass) {
  Write-Host ("[baseline:{0}] begin" -f $Phase)
  Add-CommandRun "hdc list targets -v"
  $targets = (& hdc list targets -v 2>&1 | Out-String)
  $hdcOk = Invoke-HdcText "echo HDC_OK" "hdc_ok"
  $ifconfig = Invoke-HdcText ("ifconfig {0}" -f $Iface) "ifconfig_iface"
  $route = Invoke-HdcText "cat /proc/net/route" "proc_net_route"
  $wpa = Invoke-HdcText ("wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} status" -f $WlanIface) "wpa_status"
  $wpaPid = Invoke-HdcText "cat /data/local/tmp/wpa.pid 2>/dev/null || true" "wpa_pid"
  $wpaPs = Invoke-HdcText "ps -ef | grep wpa" "wpa_ps"
  # Gateway is parsed from /proc/net/route. Format: hex little-endian.
  # We don't decode the gateway here - we simply check default-route presence,
  # then ping a stable router by re-using the pre-existing default-gw IP. As
  # gateway differs per network, fallback to PingIP itself if route absent.
  $gwIp = $null
  if ($route.text) {
    $m = [regex]::Match($route.text, ('(?m)^{0}\s+00000000\s+([0-9A-F]{{8}})\s' -f [regex]::Escape($Iface)))
    if ($m.Success) {
      $gwHex = $m.Groups[1].Value
      try {
        $b1 = [Convert]::ToInt32($gwHex.Substring(6,2),16)
        $b2 = [Convert]::ToInt32($gwHex.Substring(4,2),16)
        $b3 = [Convert]::ToInt32($gwHex.Substring(2,2),16)
        $b4 = [Convert]::ToInt32($gwHex.Substring(0,2),16)
        $gwIp = ("{0}.{1}.{2}.{3}" -f $b1,$b2,$b3,$b4)
      } catch { $gwIp = $null }
    }
  }
  $gwPing = if ($gwIp) {
    Invoke-HdcText ("ping -c 3 {0}" -f $gwIp) "ping_gateway"
  } else {
    [pscustomobject]@{ tag="ping_gateway"; command="(no_gateway)"; text=""; exit_code=1 }
  }
  $ipPing = Invoke-HdcText ("ping -c 3 {0}" -f $PingIP) "ping_ip"
  $dnsPing = Invoke-HdcText ("ping -c 3 {0}" -f $DnsHost) "ping_dns"
  $netFaultPs = Invoke-HdcText "ps -ef | grep net_fault" "net_fault_ps"
  $stateLs = Invoke-HdcText "ls -la /data/local/tmp/net_fault_state 2>/dev/null || true" "net_fault_state"
  $iptables1111 = Invoke-HdcText "iptables -L OUTPUT -n -v | grep 1.1.1.1 || true" "iptables_1111"
  $resolv = Invoke-HdcText "echo '### resolv_conf_candidates'; for p in /data/service/el1/public/netmanager/resolv.conf /etc/resolv.conf /system/etc/resolv.conf; do echo `"-- `$p`"; if [ -e `"`$p`" ]; then grep -n nameserver `"`$p`" 2>/dev/null || echo NO_NAMESERVER; else echo MISSING; fi; done" "resolv_conf"

  $ipPingOk = if ($RequireIpFullPass) { Test-PingPass $ipPing.text } else { Test-PingUsable $ipPing.text }
  $checks = [ordered]@{
    hdc_online = ($targets -match [regex]::Escape($SN) -and $targets -match 'Connected' -and $hdcOk.text -match 'HDC_OK')
    iface_ipv4 = (Test-IfaceIpv4 $ifconfig.text $wpa.text)
    default_gw_present = (Test-DefaultGwOnIface $route.text $Iface)
    wpa_completed = ($wpa.text -match '(?m)^wpa_state=COMPLETED\s*$')
    wpa_pid_live = (($wpaPid.text).Trim() -match '^\d+$' -and $wpaPs.text -match 'wpa_supplicant')
    gateway_ping_pass = (Test-PingPass $gwPing.text)
    ip_ping_usable = $ipPingOk
    dns_ping_pass = (Test-PingUsable $dnsPing.text)
    no_net_fault_process = (Test-NoNetFaultProcess $netFaultPs.text)
    no_active_marker = (Test-NoActiveMarker $stateLs.text)
    no_root_bak = (Test-NoRootBak $stateLs.text)
    no_active_1_1_1_1 = (-not ($iptables1111.text -match '1\.1\.1\.1'))
    no_bad_dns_resolver = (-not ($resolv.text -match ("nameserver\s+" + [regex]::Escape($NetBadDns) + "(\s|$)")))
  }
  $ok = $true
  foreach ($k in $checks.Keys) { if (-not $checks[$k]) { $ok = $false } }
  $obj = [pscustomobject]@{
    phase = $Phase
    ok = $ok
    checks = $checks
    probe_profile = $ProbeProfileRecord
    active_ping_ip = $PingIP
    active_dns_host = $DnsHost
    dns_proof_base_ip = $DnsProofBaseIP
    gateway_ip_resolved = $gwIp
    targets = $targets
    ifconfig = $ifconfig.text
    proc_net_route = $route.text
    wpa_status = $wpa.text
    wpa_pid = $wpaPid.text
    wpa_ps = $wpaPs.text
    ping_gateway = $gwPing.text
    ping_ip = $ipPing.text
    ping_dns = $dnsPing.text
    net_fault_ps = $netFaultPs.text
    net_fault_state = $stateLs.text
    iptables_1_1_1_1 = $iptables1111.text
    resolv_conf = $resolv.text
  }
  Write-Host ("[baseline:{0}] ok={1}" -f $Phase, $ok)
  return $obj
}

function Get-BaselineStable([string]$Phase, [int]$MaxAttempts = 3, [int]$SleepSec = 5, [switch]$RequireIpFullPass) {
  if ($MaxAttempts -lt 1) { $MaxAttempts = 1 }
  $last = $null
  for ($i = 1; $i -le $MaxAttempts; $i++) {
    $attemptPhase = if ($i -eq 1) { $Phase } else { ("{0}_retry{1}" -f $Phase, $i) }
    $last = Get-Baseline $attemptPhase -RequireIpFullPass:$RequireIpFullPass
    $last | Add-Member -NotePropertyName baseline_attempt -NotePropertyValue $i -Force
    $last | Add-Member -NotePropertyName baseline_max_attempts -NotePropertyValue $MaxAttempts -Force
    if ($last.ok) { return $last }
    if ($i -lt $MaxAttempts) {
      Write-Host ("[baseline:{0}] unstable; retry {1}/{2} after {3}s" -f $Phase, ($i + 1), $MaxAttempts, $SleepSec) -ForegroundColor DarkYellow
      Start-Sleep -Seconds $SleepSec
    }
  }
  return $last
}

function Get-FailedCheckNames([object]$Checks) {
  $failed = New-Object System.Collections.Generic.List[string]
  if (-not $Checks) { return [string[]]$failed.ToArray() }
  foreach ($k in $Checks.Keys) {
    if (-not $Checks[$k]) { [void]$failed.Add($k) }
  }
  return [string[]]$failed.ToArray()
}

function Test-DnsCleanupVerify([string]$RunDir) {
  $logPath = Join-Path $RunDir "fault_inject\fault_net_dns_fail.log"
  if (-not (Test-Path -LiteralPath $logPath)) {
    return [pscustomobject]@{ ok=$false; log=$logPath; reason="dns_cleanup_log_missing"; marker_found=$false }
  }
  $txt = Get-Content -Raw -LiteralPath $logPath
  $markerFound = ($txt -match 'DNS_CLEANUP_VERIFY_RESULT')
  $ok = ($txt -match 'DNS_CLEANUP_VERIFY_RESULT\s+status=pass')
  $reason = if ($ok) { "" } elseif ($markerFound) { "dns_cleanup_verify_not_pass" } else { "dns_cleanup_marker_missing" }
  return [pscustomobject]@{ ok=$ok; log=$logPath; reason=$reason; marker_found=$markerFound }
}

function Get-BaselineConsecutiveStable(
  [string]$Phase,
  [int]$MaxAttempts = 8,
  [int]$ConsecutivePasses = 3,
  [int]$SleepSec = 5,
  [switch]$RequireIpFullPass
) {
  if ($MaxAttempts -lt 1) { $MaxAttempts = 1 }
  if ($ConsecutivePasses -lt 1) { $ConsecutivePasses = 1 }
  if ($MaxAttempts -lt $ConsecutivePasses) { $MaxAttempts = $ConsecutivePasses }

  $attempts = New-Object System.Collections.Generic.List[object]
  $last = $null
  $streak = 0
  for ($i = 1; $i -le $MaxAttempts; $i++) {
    $attemptPhase = if ($i -eq 1) { $Phase } else { ("{0}_stabilize{1}" -f $Phase, $i) }
    $last = Get-Baseline $attemptPhase -RequireIpFullPass:$RequireIpFullPass
    if ($last.ok) { $streak++ } else { $streak = 0 }
    $failed = Get-FailedCheckNames $last.checks
    [void]$attempts.Add([pscustomobject]@{
      attempt = $i
      ok = [bool]$last.ok
      consecutive_passes = $streak
      failed_checks = $failed
      checks = $last.checks
    })
    $last | Add-Member -NotePropertyName baseline_attempt -NotePropertyValue $i -Force
    $last | Add-Member -NotePropertyName baseline_max_attempts -NotePropertyValue $MaxAttempts -Force
    $last | Add-Member -NotePropertyName baseline_consecutive_required -NotePropertyValue $ConsecutivePasses -Force
    $last | Add-Member -NotePropertyName baseline_consecutive_passes -NotePropertyValue $streak -Force
    $last | Add-Member -NotePropertyName baseline_attempts -NotePropertyValue ([object[]]$attempts.ToArray()) -Force
    if ($streak -ge $ConsecutivePasses) {
      $last | Add-Member -NotePropertyName baseline_stabilized -NotePropertyValue $true -Force
      Write-Host ("[baseline:{0}] stable after {1}/{2} attempts with {3} consecutive PASS" -f $Phase, $i, $MaxAttempts, $ConsecutivePasses) -ForegroundColor Green
      return $last
    }
    if ($i -lt $MaxAttempts) {
      Write-Host ("[baseline:{0}] consecutive PASS {1}/{2}; retry {3}/{4} after {5}s" -f $Phase, $streak, $ConsecutivePasses, ($i + 1), $MaxAttempts, $SleepSec) -ForegroundColor DarkYellow
      Start-Sleep -Seconds $SleepSec
    }
  }

  if ($last) {
    $last | Add-Member -NotePropertyName ok -NotePropertyValue $false -Force
    $last | Add-Member -NotePropertyName baseline_stabilized -NotePropertyValue $false -Force
    $last | Add-Member -NotePropertyName baseline_attempts -NotePropertyValue ([object[]]$attempts.ToArray()) -Force
  }
  return $last
}

function Clear-CollectorEnv {
  $names = @(
    "WK_FAULT_TYPE","WK_NET_PING_IP","NET_PING_IP","WK_NET_DNS_HOST","NET_DNS_HOST",
    "WK_NET_IFACE","NET_IFACE","WK_NET_WLAN_IFACE","NET_WLAN_IFACE",
    "WK_NET_LINK_DOWN_HOLD_SEC","NET_HOLD_SEC","WK_NET_FLAP_COUNT","NET_FLAP_COUNT",
    "WK_NET_FLAP_DOWN_SEC","NET_FLAP_DOWN_SEC","WK_NET_FLAP_UP_SEC","NET_FLAP_UP_SEC",
    "WK_NET_TARGET_IP","NET_TARGET_IP",
    "WK_ENABLE_NET_SNAPSHOT","WK_ENABLE_WUKONG_EXEC","WK_ENABLE_SPECIAL_SWEEP",
    "WK_SCENARIO_TAG","SCENARIO_TAG","WK_LABELS","NET_DNS_PROOF_BASE_IP",
    "WK_NET_DNS_PROOF_BASE_IP","WK_NET_BAD_DNS","NET_BAD_DNS","NET_MODE",
    "WK_NET_PROBE_PROFILE_ID","NET_PROBE_PROFILE_ID","WK_NET_ACTIVE_PING_IP",
    "NET_ACTIVE_PING_IP","WK_NET_ACTIVE_DNS_HOST","NET_ACTIVE_DNS_HOST",
    "WK_NET_BASELINE_POLICY","NET_BASELINE_POLICY",
    "WK_NET_BASELINE_MAX_ATTEMPTS","NET_BASELINE_MAX_ATTEMPTS",
    "WK_NET_BASELINE_CONSECUTIVE_REQUIRED","NET_BASELINE_CONSECUTIVE_REQUIRED",
    "WK_NET_BASELINE_SLEEP_SEC","NET_BASELINE_SLEEP_SEC",
    "WK_NET_PROBE_PROFILE_REASON","NET_PROBE_PROFILE_REASON",
    "WK_NET_AP_OR_NETWORK_PROFILE","NET_AP_OR_NETWORK_PROFILE",
    "WK_NET_PROFILE_EFFECTIVE_FROM","NET_PROFILE_EFFECTIVE_FROM",
    "WK_NET_PROFILE_APPROVED_BY","NET_PROFILE_APPROVED_BY",
    "WK_NET_PROFILE_APPROVAL_TIME","NET_PROFILE_APPROVAL_TIME",
    "WK_NET_DIAGNOSTIC_PROBE_TARGETS","NET_DIAGNOSTIC_PROBE_TARGETS"
  )
  foreach ($name in $names) {
    Remove-Item -Path ("Env:{0}" -f $name) -ErrorAction SilentlyContinue
  }
}

function Set-CollectorEnv([string]$Subtype) {
  Clear-CollectorEnv
  $env:WK_FAULT_TYPE = $Subtype
  $env:WK_NET_PING_IP = $PingIP
  $env:NET_PING_IP = $PingIP
  $env:WK_NET_DNS_HOST = $DnsHost
  $env:NET_DNS_HOST = $DnsHost
  $env:WK_NET_DNS_PROOF_BASE_IP = $DnsProofBaseIP
  $env:NET_DNS_PROOF_BASE_IP = $DnsProofBaseIP
  $env:WK_NET_PROBE_PROFILE_ID = [string]$ProbeProfileRecord.probe_profile_id
  $env:NET_PROBE_PROFILE_ID = [string]$ProbeProfileRecord.probe_profile_id
  $env:WK_NET_ACTIVE_PING_IP = [string]$ProbeProfileRecord.active_ping_ip
  $env:NET_ACTIVE_PING_IP = [string]$ProbeProfileRecord.active_ping_ip
  $env:WK_NET_ACTIVE_DNS_HOST = [string]$ProbeProfileRecord.active_dns_host
  $env:NET_ACTIVE_DNS_HOST = [string]$ProbeProfileRecord.active_dns_host
  $env:WK_NET_BASELINE_POLICY = [string]$ProbeProfileRecord.baseline_policy
  $env:NET_BASELINE_POLICY = [string]$ProbeProfileRecord.baseline_policy
  $env:WK_NET_BASELINE_MAX_ATTEMPTS = [string]$ProbeProfileRecord.baseline_max_attempts
  $env:NET_BASELINE_MAX_ATTEMPTS = [string]$ProbeProfileRecord.baseline_max_attempts
  $env:WK_NET_BASELINE_CONSECUTIVE_REQUIRED = [string]$ProbeProfileRecord.baseline_consecutive_required
  $env:NET_BASELINE_CONSECUTIVE_REQUIRED = [string]$ProbeProfileRecord.baseline_consecutive_required
  $env:WK_NET_BASELINE_SLEEP_SEC = [string]$ProbeProfileRecord.baseline_sleep_sec
  $env:NET_BASELINE_SLEEP_SEC = [string]$ProbeProfileRecord.baseline_sleep_sec
  $env:WK_NET_PROBE_PROFILE_REASON = [string]$ProbeProfileRecord.probe_profile_reason
  $env:NET_PROBE_PROFILE_REASON = [string]$ProbeProfileRecord.probe_profile_reason
  $env:WK_NET_AP_OR_NETWORK_PROFILE = [string]$ProbeProfileRecord.ap_or_network_profile
  $env:NET_AP_OR_NETWORK_PROFILE = [string]$ProbeProfileRecord.ap_or_network_profile
  $env:WK_NET_PROFILE_EFFECTIVE_FROM = [string]$ProbeProfileRecord.profile_effective_from
  $env:NET_PROFILE_EFFECTIVE_FROM = [string]$ProbeProfileRecord.profile_effective_from
  $env:WK_NET_PROFILE_APPROVED_BY = [string]$ProbeProfileRecord.profile_approved_by
  $env:NET_PROFILE_APPROVED_BY = [string]$ProbeProfileRecord.profile_approved_by
  $env:WK_NET_PROFILE_APPROVAL_TIME = [string]$ProbeProfileRecord.profile_approval_time
  $env:NET_PROFILE_APPROVAL_TIME = [string]$ProbeProfileRecord.profile_approval_time
  if ($null -ne $ProbeProfileRecord.diagnostic_probe_targets) {
    $diagJson = $ProbeProfileRecord.diagnostic_probe_targets | ConvertTo-Json -Compress -Depth 8
    $env:WK_NET_DIAGNOSTIC_PROBE_TARGETS = $diagJson
    $env:NET_DIAGNOSTIC_PROBE_TARGETS = $diagJson
  }
  $env:WK_NET_IFACE = $Iface
  $env:NET_IFACE = $Iface
  $env:WK_NET_WLAN_IFACE = $WlanIface
  $env:NET_WLAN_IFACE = $WlanIface
  $env:WK_NET_LINK_DOWN_HOLD_SEC = [string]$NetLinkDownHoldSec
  $env:NET_HOLD_SEC = [string]$NetLinkDownHoldSec
  $env:WK_NET_FLAP_COUNT = [string]$NetFlapCount
  $env:NET_FLAP_COUNT = [string]$NetFlapCount
  $env:WK_NET_FLAP_DOWN_SEC = [string]$NetFlapDownSec
  $env:NET_FLAP_DOWN_SEC = [string]$NetFlapDownSec
  $env:WK_NET_FLAP_UP_SEC = [string]$NetFlapUpSec
  $env:NET_FLAP_UP_SEC = [string]$NetFlapUpSec
  $env:WK_NET_BAD_DNS = $NetBadDns
  $env:NET_BAD_DNS = $NetBadDns
  $env:WK_NET_TARGET_IP = $NetTargetIP
  $env:NET_TARGET_IP = $NetTargetIP
  $env:WK_ENABLE_NET_SNAPSHOT = "1"
  $env:WK_ENABLE_WUKONG_EXEC = "0"
  $env:WK_ENABLE_SPECIAL_SWEEP = "0"
  if ($Smoke) {
    $env:WK_SCENARIO_TAG = ("net_batch2_smoke_{0}" -f $Subtype)
    $env:WK_LABELS = ("smoke=1,batch2=1,subtype={0}" -f $Subtype)
  } else {
    $env:WK_SCENARIO_TAG = ("net_batch2_bulk_{0}" -f $Subtype)
    $env:WK_LABELS = ("smoke=0,batch2=1,subtype={0}" -f $Subtype)
  }
  if ($Subtype -eq "net_dns_fail") {
    $env:NET_DNS_PROOF_BASE_IP = $DnsProofBaseIP
    $env:WK_NET_DNS_PROOF_BASE_IP = $DnsProofBaseIP
  }
}

function Get-ArtifactsPath([string[]]$Lines) {
  if (-not $Lines -or $Lines.Count -eq 0) { return "" }
  $joined = $Lines -join "`n"
  $m = [regex]::Matches($joined, '(?m)Artifacts under:\s*(.+?)\s*$')
  if ($m.Count -lt 1) { return "" }
  return ($m[$m.Count - 1].Groups[1].Value).Trim()
}

function Add-JsonLine([string]$Path, [object]$Obj) {
  $json = $Obj | ConvertTo-Json -Compress -Depth 8
  Add-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Invoke-OneRun([string]$Subtype, [int]$AttemptIdx, [int]$AttemptMax) {
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $collectLog = Join-Path $LogDir ("collect_{0}_{1}.log" -f $Subtype, $stamp)
  $validateLog = Join-Path $LogDir ("validate_{0}_{1}.log" -f $Subtype, $stamp)

  Write-Host ""
  Write-Host ("===== [{0}] attempt {1}/{2} ({3}) =====" -f $Subtype, $AttemptIdx, $AttemptMax, $stamp) -ForegroundColor Cyan

  if ($DryRun) {
    Set-CollectorEnv $Subtype
    Write-Host ("[{0}] DRY RUN: skip baseline, collector, validator, and file writes" -f $Subtype) -ForegroundColor DarkYellow
    Clear-CollectorEnv
    return [pscustomobject]@{ status="DRYRUN"; subtype=$Subtype; reason="dry_run_skip"; run_id="" }
  }

  $pre = Get-BaselineConsecutiveStable ("pre_{0}" -f $Subtype) -MaxAttempts $PreBaselineMaxAttempts -ConsecutivePasses $PreBaselineConsecutivePasses -SleepSec $PreBaselineSleepSec
  if ($pre.PSObject.Properties.Name -contains "baseline_attempts") {
    [void]$PreBaselineStabilizationRecords.Add([pscustomobject]@{
      subtype = $Subtype
      probe_profile = $ProbeProfileRecord
      attempt_index = $AttemptIdx
      stabilized = [bool]$pre.ok
      final_attempt = $pre.baseline_attempt
      max_attempts = $pre.baseline_max_attempts
      consecutive_required = $pre.baseline_consecutive_required
      consecutive_passes = $pre.baseline_consecutive_passes
      attempts = $pre.baseline_attempts
    })
  }
  if (-not $pre.ok) {
    $rec = [pscustomobject]@{
      run_id=""; path=""; subtype=$Subtype
      probe_profile=$ProbeProfileRecord
      reason="pre_baseline_fail"; baseline=$pre.checks
      pre_baseline_attempt=$pre.baseline_attempt
      pre_baseline_max_attempts=$pre.baseline_max_attempts
      pre_baseline_consecutive_required=$pre.baseline_consecutive_required
      pre_baseline_consecutive_passes=$pre.baseline_consecutive_passes
      pre_baseline_attempts=$pre.baseline_attempts
      action=$(if ($FixcheckOnly) { "fixcheck_only_pre_baseline_blocker" } elseif ($CandidateOnly) { "candidate_only_rejected_from_batch" } else { "rejected_from_batch" })
      attempt_index=$AttemptIdx
    }
    if ($FixcheckOnly) {
      $rec | Add-Member -NotePropertyName accepted_ledger_written -NotePropertyValue $false
      $rec | Add-Member -NotePropertyName l1l2_written -NotePropertyValue $false
      $rec | Add-Member -NotePropertyName schema_mutated -NotePropertyValue $false
    }
    if ($FixcheckOnly) { Add-JsonLine $FixcheckLedger $rec } else { Add-JsonLine $RejectedLedger $rec }
    [void]$RejectedRuns.Add($rec)
    [void]$Blockers.Add(("{0}: pre baseline failed (attempt {1})" -f $Subtype, $AttemptIdx))
    return [pscustomobject]@{ status="BLOCKER"; subtype=$Subtype; reason="pre_baseline_fail"; run_id="" }
  }

  Set-CollectorEnv $Subtype
  Add-CommandRun ("powershell -ExecutionPolicy Bypass -File .\run_wukong_collect_refactor.ps1 -SN {0}" -f $SN)
  Write-Host ("[{0}] collector start" -f $Subtype)
  $script:CollectorStartedCount++
  $collectorLines = @(powershell -ExecutionPolicy Bypass -File $CollectScript -SN $SN 2>&1 | Tee-Object -FilePath $collectLog)
  $collectorExit = $LASTEXITCODE
  $artifactPath = Get-ArtifactsPath $collectorLines
  if ($artifactPath) { $script:ArtifactParsedCount++ }
  $runId = if ($artifactPath) { Split-Path -Leaf $artifactPath } else { "" }
  Write-Host ("[{0}] collector exit={1} run_id={2}" -f $Subtype, $collectorExit, $runId)

  if (-not $artifactPath -or -not (Test-Path -LiteralPath $artifactPath)) {
    $postMissing = Get-Baseline ("post_missing_{0}" -f $Subtype)
    $rec = [pscustomobject]@{
      run_id=$runId; path=$artifactPath; subtype=$Subtype
      probe_profile=$ProbeProfileRecord
      validator=[pscustomobject]@{ RESULT="NOT_RUN"; log=$validateLog }
      reason="collector_no_artifact_path"
      post_baseline=$postMissing.checks
      action=$(if ($FixcheckOnly) { "fixcheck_only_missing_artifact" } else { "rejected_from_batch" })
      attempt_index=$AttemptIdx
    }
    if ($FixcheckOnly) { Add-JsonLine $FixcheckLedger $rec } else { Add-JsonLine $RejectedLedger $rec }
    [void]$RejectedRuns.Add($rec)
    if (-not $postMissing.ok) {
      [void]$Blockers.Add(("{0}: post baseline failed after missing artifacts" -f $Subtype))
      return [pscustomobject]@{ status="BLOCKER"; subtype=$Subtype; reason="post_baseline_fail_after_missing_artifact"; run_id=$runId }
    }
    return [pscustomobject]@{ status="REJECTED"; subtype=$Subtype; reason="collector_no_artifact_path"; run_id=$runId }
  }

  Add-CommandRun ("powershell -ExecutionPolicy Bypass -File .\wk_validate_run_net.ps1 -RUN_DIR '{0}' -AsJson" -f $artifactPath)
  Write-Host ("[{0}] validator start" -f $Subtype)
  $validatorText = (powershell -ExecutionPolicy Bypass -File $ValidatorScript -RUN_DIR $artifactPath -AsJson 2>&1 | Tee-Object -FilePath $validateLog | Out-String).Trim()
  $validatorObj = $null
  try {
    $validatorObj = $validatorText | ConvertFrom-Json
  } catch {
    $validatorObj = [pscustomobject]@{
      RESULT="PARSE_FAIL"; RUN_ID=$runId; RUN_DIR=$artifactPath
      FAILED_CRITERIA=@("VALIDATOR_JSON_PARSE_FAIL"); WARNINGS=@(); NEXT_FIX=@($validatorText)
    }
  }

  $outcomePath = Join-Path $artifactPath "_net_outcome.json"
  $outcome = if (Test-Path -LiteralPath $outcomePath) {
    Get-Content -Raw -LiteralPath $outcomePath | ConvertFrom-Json
  } else { $null }

  $post = Get-BaselineStable ("post_{0}" -f $Subtype) -MaxAttempts 3 -SleepSec 5
  $dnsCleanup = if ($Subtype -eq "net_dns_fail") {
    Test-DnsCleanupVerify -RunDir $artifactPath
  } else {
    [pscustomobject]@{ ok=$true; log=""; reason=""; marker_found=$false }
  }

  $outcomeOk = $false
  $gateRequiredSubtypes = @("net_wifi_disconnect","net_no_default_route","net_no_ipv4_on_iface","net_wrong_default_route","net_gateway_unreachable")
  $requiresRecoveryGate = $gateRequiredSubtypes -contains $Subtype
  $recoveryGateOk = -not $requiresRecoveryGate
  if ($outcome) {
    $hasGate = $outcome.PSObject.Properties.Name -contains "recovery_gate_ok"
    if ($hasGate) { $recoveryGateOk = [bool]$outcome.recovery_gate_ok }
    $outcomeOk = (
      $outcome.net_fault_type -eq $Subtype -and
      $outcome.iface_used -eq $Iface -and
      [bool]$outcome.inject_ok -and
      [bool]$outcome.fault_observed -and
      [bool]$outcome.recovery_observed -and
      $recoveryGateOk
    )
  }

  $artifactPathLeaf = Split-Path -Leaf $artifactPath
  $accepted = (
    $validatorObj.RESULT -eq "PASS" -and
    $outcomeOk -and
    $post.ok -and
    $dnsCleanup.ok -and
    $runId -ne "" -and
    $artifactPathLeaf -eq $runId
  )

  if ($accepted) {
    if ($FixcheckOnly) {
      $rec = [pscustomobject]@{
        run_id=$runId; path=$artifactPath; subtype=$Subtype
        probe_profile=$ProbeProfileRecord
        validator=[pscustomobject]@{
          RESULT=$validatorObj.RESULT
          warnings=$validatorObj.WARNINGS
          log=$validateLog
        }
        outcome=$outcome
        post_baseline=$post.checks
        post_baseline_attempt=$post.baseline_attempt
        post_baseline_max_attempts=$post.baseline_max_attempts
        dns_cleanup_verify=$dnsCleanup
        action="fixcheck_only_pass_not_accepted"
        attempt_index=$AttemptIdx
        accepted_ledger_written=$false
        cleanup_marker_expected=$(if ($Subtype -eq "net_dns_fail") { "DNS_CLEANUP_VERIFY_RESULT status=pass" } else { "NET_CLEANUP_RESULT" })
      }
      Add-JsonLine $FixcheckLedger $rec
      Write-Host ("[{0}] FIXCHECK_PASS run_id={1}; accepted ledger not written" -f $Subtype, $runId) -ForegroundColor Green
      return [pscustomobject]@{ status="FIXCHECK_PASS"; subtype=$Subtype; run_id=$runId; reason="" }
    }
    $acceptAction = if ($CandidateOnly) {
      if ($Smoke) { "candidate_only_smoke_first_round" } else { "candidate_only_bulk" }
    } else {
      if ($Smoke) { "accepted_smoke_first_round" } else { "accepted_bulk" }
    }
    $recData = [ordered]@{
      run_id=$runId; path=$artifactPath; subtype=$Subtype
      probe_profile=$ProbeProfileRecord
      validator=[pscustomobject]@{
        RESULT=$validatorObj.RESULT
        warnings=$validatorObj.WARNINGS
        log=$validateLog
      }
      outcome=$outcome
      post_baseline=$post.checks
      post_baseline_attempt=$post.baseline_attempt
      post_baseline_max_attempts=$post.baseline_max_attempts
      dns_cleanup_verify=$dnsCleanup
      action=$acceptAction
      attempt_index=$AttemptIdx
      cleanup_marker_expected=$(if ($Subtype -eq "net_dns_fail") { "DNS_CLEANUP_VERIFY_RESULT status=pass" } else { "NET_CLEANUP_RESULT" })
    }
    if ($CandidateOnly) {
      $recData["candidate_only"] = $true
      $recData["not_for_training"] = $true
      $recData["accepted_provenance"] = $false
      $recData["project_accepted"] = $false
      $recData["formal_candidate"] = $true
      $recData["accepted_ledger_written"] = $false
      $recData["candidate_ledger_written"] = $true
    }
    $rec = [pscustomobject]$recData
    Add-JsonLine $AcceptedLedger $rec
    [void]$AcceptedRuns.Add($rec)
    $statusText = if ($CandidateOnly) { "CANDIDATE" } else { "ACCEPTED" }
    Write-Host ("[{0}] {1} run_id={2}" -f $Subtype, $statusText, $runId) -ForegroundColor Green
    return [pscustomobject]@{ status="ACCEPTED"; subtype=$Subtype; run_id=$runId; reason="" }
  }

  $reasonParts = New-Object System.Collections.Generic.List[string]
  if ($validatorObj.RESULT -ne "PASS") { [void]$reasonParts.Add(("validator_{0}" -f $validatorObj.RESULT)) }
  if (-not $outcomeOk) { [void]$reasonParts.Add("outcome_acceptance_fields_not_ok") }
  if (-not $post.ok) { [void]$reasonParts.Add("post_baseline_fail") }
  if (-not $dnsCleanup.ok) { [void]$reasonParts.Add($dnsCleanup.reason) }
  $reason = ($reasonParts -join ";")

  $rec = [pscustomobject]@{
    run_id=$runId; path=$artifactPath; subtype=$Subtype
    probe_profile=$ProbeProfileRecord
    validator=$validatorObj; outcome=$outcome
    reason=$reason; post_baseline=$post.checks
    post_baseline_attempt=$post.baseline_attempt
    post_baseline_max_attempts=$post.baseline_max_attempts
    dns_cleanup_verify=$dnsCleanup
    action=$(if ($FixcheckOnly) { "fixcheck_only_not_accepted" } elseif ($CandidateOnly) { "candidate_only_rejected_from_batch" } else { "rejected_from_batch" })
    attempt_index=$AttemptIdx
  }
  if ($FixcheckOnly) { Add-JsonLine $FixcheckLedger $rec } else { Add-JsonLine $RejectedLedger $rec }
  [void]$RejectedRuns.Add($rec)
  Write-Host ("[{0}] REJECTED run_id={1} reason={2}" -f $Subtype, $runId, $reason) -ForegroundColor Yellow

  if (-not $post.ok) {
    [void]$Blockers.Add(("{0}: post baseline failed (run_id={1})" -f $Subtype, $runId))
    return [pscustomobject]@{ status="BLOCKER"; subtype=$Subtype; run_id=$runId; reason="post_baseline_fail" }
  }
  return [pscustomobject]@{ status="REJECTED"; subtype=$Subtype; run_id=$runId; reason=$reason }
}

# Helper to compose action string with conditional, since PS5 doesn't have
# ternary. Used inline via call.
function _Concat([string]$a,[string]$b){ return ($a + $b) }

# ---- Main loop ----

$Result = "PASS"
$AcceptedBySubtype = [ordered]@{}
foreach ($s in $Subtypes) { $AcceptedBySubtype[$s] = 0 }
$FixcheckPassedBySubtype = [ordered]@{}
foreach ($s in $Subtypes) { $FixcheckPassedBySubtype[$s] = 0 }

# Print plan
Write-Host ""
Write-Host "### run_wukong_weekend NET batch plan" -ForegroundColor Cyan
Write-Host ("- SN: {0}" -f $SN)
Write-Host ("- Subtypes: {0}" -f ($Subtypes -join ","))
Write-Host ("- AcceptedPerSubtype: {0}" -f $AcceptedPerSubtype)
Write-Host ("- MaxRetryPerSubtype: {0}" -f $MaxRetryPerSubtype)
Write-Host ("- PingIP: {0} | Iface: {1} | WlanIface: {2} | DnsHost: {3}" -f $PingIP, $Iface, $WlanIface, $DnsHost)
Write-Host ("- ProbeProfile: {0} selected={1} dns_proof_base_ip={2}" -f $ProbeProfileId, $ProfileSelected, $DnsProofBaseIP)
Write-Host ("- PreBaselineStable: consecutive={0},max_attempts={1},sleep={2}s" -f $PreBaselineConsecutivePasses, $PreBaselineMaxAttempts, $PreBaselineSleepSec)
Write-Host ("- NetHoldSec: {0} | NetFlap: count={1},down={2},up={3}" -f $NetLinkDownHoldSec, $NetFlapCount, $NetFlapDownSec, $NetFlapUpSec)
Write-Host ("- NetBadDns: {0} | NetTargetIP: {1}" -f $NetBadDns, $NetTargetIP)
Write-Host ("- OutDir: {0}" -f $OutDir)
Write-Host ("- Smoke: {0} | DryRun: {1} | FixcheckOnly: {2} | CandidateOnly: {3} | StopOnBlocker: {4} | NoL1L2: {5}" -f $Smoke, $DryRun, $FixcheckOnly, $CandidateOnly, $StopOnBlocker, $NoL1L2)
Write-Host ""

foreach ($subtype in $Subtypes) {
  $accForSubtype = 0
  $attempt = 0
  $attemptMax = [int]$AcceptedPerSubtype + [int]$MaxRetryPerSubtype
  if ($attemptMax -lt 1) { $attemptMax = 1 }

  while ($accForSubtype -lt $AcceptedPerSubtype -and $attempt -lt $attemptMax) {
    $attempt++
    $r = Invoke-OneRun -Subtype $subtype -AttemptIdx $attempt -AttemptMax $attemptMax
    if ($r.status -eq "ACCEPTED") {
      $accForSubtype++
      $AcceptedBySubtype[$subtype] = $accForSubtype
      Write-Host ("[{0}] accepted {1}/{2}" -f $subtype, $accForSubtype, $AcceptedPerSubtype) -ForegroundColor Green
    } elseif ($r.status -eq "FIXCHECK_PASS") {
      $accForSubtype++
      $FixcheckPassedBySubtype[$subtype] = $accForSubtype
      Write-Host ("[{0}] fixcheck pass {1}/{2} (not accepted)" -f $subtype, $accForSubtype, $AcceptedPerSubtype) -ForegroundColor Green
    } elseif ($r.status -eq "BLOCKER") {
      $Result = "BLOCKER"
      Write-Host ("[{0}] BLOCKER {1}; stop subtype" -f $subtype, $r.reason) -ForegroundColor Red
      if ($StopOnBlocker) { break }
    } elseif ($r.status -eq "DRYRUN") {
      Write-Host ("[{0}] DRY RUN attempt complete" -f $subtype) -ForegroundColor DarkYellow
      break
    } else {
      Write-Host ("[{0}] attempt {1} rejected" -f $subtype, $attempt) -ForegroundColor Yellow
    }

    if ($accForSubtype -lt $AcceptedPerSubtype -and $attempt -lt $attemptMax) {
      if ($SleepBetweenRunsSec -gt 0) {
        Write-Host ("[{0}] cooldown {1}s before next attempt" -f $subtype, $SleepBetweenRunsSec) -ForegroundColor DarkGray
        Start-Sleep -Seconds $SleepBetweenRunsSec
      }
    }
  }

  if ($Result -eq "BLOCKER" -and $StopOnBlocker) { break }

  if (-not $DryRun -and $accForSubtype -lt $AcceptedPerSubtype) {
    if ($Result -ne "BLOCKER") { $Result = "PARTIAL" }
    Write-Host ("[{0}] WARNING: only {1}/{2} accepted after {3} attempts" -f $subtype, $accForSubtype, $AcceptedPerSubtype, $attempt) -ForegroundColor DarkYellow
  }

  if (-not $DryRun -and $SleepBetweenRunsSec -gt 0) {
    Write-Host ("[interval] cooldown {0}s before next subtype" -f $SleepBetweenRunsSec) -ForegroundColor DarkGray
    Start-Sleep -Seconds $SleepBetweenRunsSec
  }
}

# After the loop: final baseline and summary
$finalBaseline = if ($DryRun) {
  [pscustomobject]@{
    ok = $true
    checks = [ordered]@{
      dry_run = $true
      baseline_skipped = $true
    }
  }
} else {
  Get-Baseline "after_batch"
}

$summaryLines = New-Object System.Collections.Generic.List[string]
[void]$summaryLines.Add("# run_wukong_weekend NET batch summary")
[void]$summaryLines.Add("")
[void]$summaryLines.Add(("- run_stamp: {0}" -f $RunStamp))
[void]$summaryLines.Add(("- result: {0}" -f $Result))
$modeStr = if ($CandidateOnly) {
  if ($Smoke) { "candidate_only_smoke" } else { "candidate_only_bulk" }
} else {
  if ($Smoke) { "smoke" } else { "bulk" }
}
[void]$summaryLines.Add(("- mode: {0}" -f $modeStr))
[void]$summaryLines.Add(("- dry_run: {0}" -f $DryRun))
[void]$summaryLines.Add(("- fixcheck_only: {0}" -f $FixcheckOnly))
[void]$summaryLines.Add(("- candidate_only: {0}" -f $CandidateOnly))
[void]$summaryLines.Add(("- stop_on_blocker: {0}" -f $StopOnBlocker))
[void]$summaryLines.Add(("- ping_ip: {0}" -f $PingIP))
[void]$summaryLines.Add(("- iface: {0}, wlan_iface: {1}, dns_host: {2}" -f $Iface, $WlanIface, $DnsHost))
[void]$summaryLines.Add(("- probe_profile_id: {0}" -f $ProbeProfileRecord.probe_profile_id))
[void]$summaryLines.Add(("- probe_profile_selected: {0}" -f $ProbeProfileRecord.profile_selected))
[void]$summaryLines.Add(("- dns_proof_base_ip: {0}" -f $ProbeProfileRecord.dns_proof_base_ip))
[void]$summaryLines.Add(("- probe_profile_path: {0}" -f $ProbeProfileRecord.profile_path))
[void]$summaryLines.Add(("- pre_baseline_stable: consecutive={0}, max_attempts={1}, sleep_sec={2}" -f $PreBaselineConsecutivePasses, $PreBaselineMaxAttempts, $PreBaselineSleepSec))
if ($CandidateOnly) {
  [void]$summaryLines.Add(("- candidate_ledger: {0}" -f $AcceptedLedger))
  [void]$summaryLines.Add("- accepted_ledger: (not written in candidate-only mode)")
} else {
  [void]$summaryLines.Add(("- accepted_ledger: {0}" -f $AcceptedLedger))
}
[void]$summaryLines.Add(("- rejected_ledger: {0}" -f $RejectedLedger))
[void]$summaryLines.Add(("- fixcheck_ledger: {0}" -f $FixcheckLedger))
[void]$summaryLines.Add("")
if ($CandidateOnly) {
  [void]$summaryLines.Add("## candidate by subtype")
} else {
  [void]$summaryLines.Add("## accepted by subtype")
}
foreach ($k in $AcceptedBySubtype.Keys) {
  [void]$summaryLines.Add(("- {0}: {1}/{2}" -f $k, $AcceptedBySubtype[$k], $AcceptedPerSubtype))
}
[void]$summaryLines.Add("")
if ($FixcheckOnly) {
  [void]$summaryLines.Add("## fixcheck passed by subtype")
  foreach ($k in $FixcheckPassedBySubtype.Keys) {
    [void]$summaryLines.Add(("- {0}: {1}/{2}" -f $k, $FixcheckPassedBySubtype[$k], $AcceptedPerSubtype))
  }
  [void]$summaryLines.Add("")
}
[void]$summaryLines.Add("## blockers")
if ($Blockers.Count -eq 0) { [void]$summaryLines.Add("- (none)") }
foreach ($b in $Blockers) { [void]$summaryLines.Add(("- {0}" -f $b)) }
[void]$summaryLines.Add("")
[void]$summaryLines.Add("## pre_baseline.stabilization")
if ($PreBaselineStabilizationRecords.Count -eq 0) {
  [void]$summaryLines.Add("- (none)")
} else {
  foreach ($rec in $PreBaselineStabilizationRecords) {
    [void]$summaryLines.Add(("- {0} attempt={1}: stabilized={2}, final_attempt={3}/{4}, consecutive={5}/{6}" -f $rec.subtype, $rec.attempt_index, $rec.stabilized, $rec.final_attempt, $rec.max_attempts, $rec.consecutive_passes, $rec.consecutive_required))
    foreach ($a in $rec.attempts) {
      $failedItems = @()
      if ($null -ne $a.failed_checks) {
        $failedItems = @(@($a.failed_checks) | Where-Object { $null -ne $_ -and ([string]$_) -ne "" })
      }
      $failed = if ($failedItems.Count -gt 0) { ($failedItems -join ",") } else { "none" }
      [void]$summaryLines.Add(("  - attempt {0}: ok={1}, consecutive={2}, failed_checks={3}" -f $a.attempt, $a.ok, $a.consecutive_passes, $failed))
    }
  }
}
[void]$summaryLines.Add("")
[void]$summaryLines.Add("## final_baseline.checks")
foreach ($k in $finalBaseline.checks.Keys) {
  [void]$summaryLines.Add(("- {0}: {1}" -f $k, $finalBaseline.checks[$k]))
}
[void]$summaryLines.Add("")
[void]$summaryLines.Add("## notes")
if ($CollectorStartedCount -eq 0) {
  [void]$summaryLines.Add("- collector not started; no 'Artifacts under:' line appeared; run_id=none.")
} elseif ($ArtifactParsedCount -gt 0) {
  [void]$summaryLines.Add("- run_id was parsed from collector stdout 'Artifacts under:'.")
} else {
  [void]$summaryLines.Add("- collector started but no 'Artifacts under:' line was parsed; run_id=none for missing-artifact attempt(s).")
}
[void]$summaryLines.Add("- frozen first batch dataset_batches\\net_formal_batch_70_20260429 was NOT touched.")
[void]$summaryLines.Add("- L1/L2 export was not triggered from this script.")
[void]$summaryLines.Add("- 1.1.1.1 was never used as active baseline / recovery probe.")

Write-Host ""
if ($DryRun) {
  Write-Host "### DRY RUN: summary/commands files not written" -ForegroundColor DarkYellow
} else {
  Set-Content -LiteralPath $SummaryPath -Value ($summaryLines -join "`r`n") -Encoding UTF8

  # Write commands.md
  $cmdLines = New-Object System.Collections.Generic.List[string]
  [void]$cmdLines.Add("# run_wukong_weekend commands log")
  [void]$cmdLines.Add("")
  [void]$cmdLines.Add(("- run_stamp: {0}" -f $RunStamp))
  [void]$cmdLines.Add(("- subtypes: {0}" -f ($Subtypes -join ",")))
  [void]$cmdLines.Add("")
  [void]$cmdLines.Add('```powershell')
  foreach ($c in $CommandsRun) { [void]$cmdLines.Add($c) }
  [void]$cmdLines.Add('```')
  Set-Content -LiteralPath $CommandsPath -Value ($cmdLines -join "`r`n") -Encoding UTF8

  Write-Host ("### SUMMARY: {0}" -f $SummaryPath) -ForegroundColor Cyan
  Write-Host ("### COMMANDS: {0}" -f $CommandsPath) -ForegroundColor Cyan
}
$resultColor = "Red"
if ($Result -eq "PASS") { $resultColor = "Green" }
elseif ($Result -eq "PARTIAL") { $resultColor = "Yellow" }
Write-Host ("### RESULT: {0}" -f $Result) -ForegroundColor $resultColor

if ($Result -eq "BLOCKER") { exit 2 }
if ($Result -eq "PARTIAL") { exit 1 }
exit 0
