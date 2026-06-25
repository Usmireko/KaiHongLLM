#Requires -Version 5.1
# Manual net fault collection helper for hardware validation
param(
  [string]$SN         = "7001005458323933328a017ce1c43800",
  [string]$FaultType  = "net_no_default_route",
  [string]$RunId      = "",
  [string]$WlanIface  = "wlan0",
  [int]   $FaultSec   = 12
)

$ErrorActionPreference = "Stop"
if (-not $RunId) { $RunId = "run_${FaultType}_$(Get-Date -Format 'yyyyMMdd_HHmmss')" }

$RunDir = Join-Path $PSScriptRoot "inbox\runs\$RunId"
$NetDir = Join-Path $RunDir "net"
New-Item -ItemType Directory -Path $NetDir -Force | Out-Null

# meta
@{
  run_id = $RunId
  scenario_tag = $FaultType
  fault_type = $FaultType
  gt_family = "net"
  gt_severity = if ($FaultType -match 'dns|no_default') { "mild" } else { "severe" }
  run_window_board_ms_start = 0
  run_window_board_ms_end = 0
} | ConvertTo-Json | Set-Content (Join-Path $RunDir "_run_meta.json")

function HdcS([string]$cmd) {
  (& hdc -t $SN shell $cmd 2>&1) -join "`n"
}

function Snapshot([string]$phase) {
  $out = @()
  $out += HdcS "echo '### NET_SNAPSHOT_MARK=20251223_sysfs_v1'"
  $out += HdcS ("echo '### phase={0}'; date" -f $phase)
  $out += HdcS "echo '### ifconfig -a'; ifconfig -a 2>/dev/null || true"
  $out += HdcS ("echo '### ifconfig {0}'; ifconfig {0} 2>/dev/null || true" -f $WlanIface)
  $out += HdcS ('echo "### link_state_sysfs"; for i in /sys/class/net/*; do n=${i##*/}; echo "-- $n"; cat "$i/operstate" 2>/dev/null || true; cat "$i/carrier" 2>/dev/null || echo NA; done; echo __END__')
  $out += HdcS ("echo '### wpa_cli status ({0})'; wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} status 2>/dev/null || true" -f $WlanIface)
  $out += HdcS "echo '### /proc/net/route'; cat /proc/net/route 2>/dev/null || true"
  $out += HdcS "echo '### iptables -L -n -v'; iptables -L -n -v 2>/dev/null || true"
  $out | Set-Content (Join-Path $NetDir "net_${phase}.txt") -Encoding UTF8
}

function Probe([string]$phase, [bool]$isWlanFault = $false) {
  $out = @()
  $out += HdcS ("echo '### phase={0}'; date" -f $phase)
  $out += HdcS "echo '### ping -c 3 8.8.8.8'; ping -c 3 8.8.8.8 2>&1 || true"
  $out += HdcS "echo '### dns_host_probe_primary'; ping -c 1 -W 1 baidu.com 2>&1 || true"
  $out += HdcS ('echo "### ping_nip_io"; u=$(cat /proc/uptime 2>/dev/null | sed "s/[.].*$//"); [ -n "$u" ] || u=0; h=t${u}.8.8.8.8.nip.io; echo host=$h; RES_OPTIONS="attempts:1 timeout:1" ping -c 1 -W 1 "$h" 2>&1 || true')
  if ($isWlanFault) {
    $out += HdcS ("echo '### wpa_cli_probe'; wpa_cli -p /data/local/tmp/wpa_ctrl -i {0} status 2>/dev/null || echo WPA_CLI_FAILED" -f $WlanIface)
    $out += HdcS ("echo '### ifconfig_wlan'; ifconfig {0} 2>/dev/null || true" -f $WlanIface)
    $out += HdcS "echo '### proc_net_route'; cat /proc/net/route 2>/dev/null || true"
  }
  $out | Set-Content (Join-Path $NetDir "probe_${phase}.txt") -Encoding UTF8
}

$isWlanFault = $FaultType -in @("net_no_default_route","net_no_ipv4_on_iface","net_wifi_disconnect","net_wifi_auth_fail_wrong_psk")

Write-Host ("[{0}] PRE snapshot" -f $FaultType) -ForegroundColor Cyan
Snapshot "pre"
Probe "pre" $isWlanFault

Write-Host ("[{0}] Starting fault (NET_DEBUG=1)" -f $FaultType) -ForegroundColor Yellow
$envPrefix = "NET_DEBUG=1 NET_MODE=$FaultType NET_WLAN_IFACE=$WlanIface NET_WPA_CTRL=/data/local/tmp/wpa_ctrl NET_WPA_CONF=/data/local/tmp/wpa.conf NET_WLAN_SSID=Guest"
$startCmd = "$envPrefix sh /data/local/tmp/net_fault.sh >/data/local/tmp/fault_${FaultType}.log 2>&1 & echo `$!"
$pidLine = HdcS $startCmd
$faultPid = 0
foreach ($l in ($pidLine -split "`n")) { if ($l.Trim() -match '^\d+$') { $faultPid = [int]$l.Trim(); break } }
Write-Host ("  fault PID=$faultPid") -ForegroundColor Yellow

Start-Sleep -Seconds 5

Write-Host ("[{0}] FAULT snapshot" -f $FaultType) -ForegroundColor Red
Snapshot "fault"
Probe "fault" $isWlanFault

Write-Host ("[{0}] Stopping fault (TERM -> restore_all)" -f $FaultType) -ForegroundColor Yellow
if ($faultPid -gt 0) {
  HdcS ("kill -TERM {0} 2>/dev/null || true" -f $faultPid) | Out-Null
  Start-Sleep -Seconds 5
}

Write-Host ("[{0}] POST snapshot" -f $FaultType) -ForegroundColor Cyan
Snapshot "post"
Probe "post" $isWlanFault
Start-Sleep -Seconds 3
Snapshot "post2"
Probe "post2" $isWlanFault

# pull injector log
$injLog = HdcS "cat /data/local/tmp/fault_${FaultType}.log 2>/dev/null || echo NO_LOG"
$injLog | Set-Content (Join-Path $RunDir "fault_inject.log") -Encoding UTF8

# generate _net_outcome.json from collected snapshots
function Get-WpaStateFromFile([string]$Path) {
  if (-not (Test-Path $Path)) { return '' }
  $raw = Get-Content $Path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
  if (-not $raw) { return '' }
  $m = [regex]::Match($raw, '(?ms)### wpa_cli status[^\n]*\n.*?wpa_state=(\S+)')
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  return ''
}
function Has-WlanIpInFile([string]$Path) {
  if (-not (Test-Path $Path)) { return $false }
  $raw = Get-Content $Path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
  if (-not $raw) { return $false }
  return ($raw -match '(?ms)### ifconfig wlan0.*?inet addr:\s*\d+')
}
function Has-DefaultRouteInFile([string]$Path) {
  if (-not (Test-Path $Path)) { return $false }
  $raw = Get-Content $Path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
  if (-not $raw) { return $false }
  return ($raw -match '(?m)^wlan0\s+00000000\s')
}

$netFaultFile  = Join-Path $NetDir "net_fault.txt"
$netPost2File  = Join-Path $NetDir "net_post2.txt"
$netPostFile   = Join-Path $NetDir "net_post.txt"
$bestPostFile  = if (Test-Path $netPost2File) { $netPost2File } else { $netPostFile }

$activeAuthStates = @("SCANNING","ASSOCIATING","4WAY_HANDSHAKE","AUTHENTICATING","GROUP_HANDSHAKE")
$wpaFault = Get-WpaStateFromFile $netFaultFile
$wpaPost  = Get-WpaStateFromFile $bestPostFile

$faultObserved = $false; $faultReason = 'not_evaluated'
$recoveryObserved = $false; $recoveryReason = 'not_evaluated'

switch ($FaultType) {
  "net_wifi_disconnect" {
    $faultObserved = ($wpaFault -eq 'DISCONNECTED' -or $wpaFault -in $activeAuthStates)
    $faultReason   = if ($faultObserved) { 'wlan_interface_disconnected' } else { 'no_disconnect_evidence' }
    $recoveryObserved = ($wpaPost -eq 'COMPLETED')
    $recoveryReason   = if ($recoveryObserved) { 'wpa_state_returned_to_completed' } else { 'wpa_not_completed_post' }
  }
  "net_wifi_auth_fail_wrong_psk" {
    $faultObserved = ($wpaFault -in $activeAuthStates)
    $faultReason   = if ($faultObserved) { 'wpa_state_in_active_auth_cycle' } else { 'no_active_auth_state_observed' }
    $recoveryObserved = ($wpaPost -eq 'COMPLETED')
    $recoveryReason   = if ($recoveryObserved) { 'wpa_state_returned_to_completed' } else { 'wpa_not_completed_post' }
  }
  "net_no_default_route" {
    $hasIpFault   = Has-WlanIpInFile $netFaultFile
    $hasRouteFault = Has-DefaultRouteInFile $netFaultFile
    $hasRoutePost  = Has-DefaultRouteInFile $bestPostFile
    $faultObserved = ($hasIpFault -and -not $hasRouteFault)
    $faultReason   = if ($faultObserved) { 'wlan0_has_ip_but_no_default_route' } else { 'no_missing_route_evidence' }
    $recoveryObserved = $hasRoutePost
    $recoveryReason   = if ($recoveryObserved) { 'default_route_restored' } else { 'route_not_seen_post' }
  }
  "net_no_ipv4_on_iface" {
    $rawFault = if (Test-Path $netFaultFile) { Get-Content $netFaultFile -Raw -Encoding UTF8 } else { '' }
    $wlanBlock = [regex]::Match($rawFault, '(?ms)### ifconfig wlan0.*?(?=### |\z)')
    $noIp = ($wlanBlock.Success -and $wlanBlock.Value -notmatch 'inet addr')
    $wpaUp = ($wpaFault -eq 'COMPLETED')
    if ($noIp -and $wpaUp) { $faultObserved = $true; $faultReason = 'wlan0_no_ip_while_wpa_still_completed' }
    elseif ($noIp) { $faultObserved = $true; $faultReason = 'wlan0_no_ip_wpa_state_unknown' }
    else { $faultReason = 'no_missing_ip_evidence' }
    $rawPost = if (Test-Path $bestPostFile) { Get-Content $bestPostFile -Raw -Encoding UTF8 } else { '' }
    $wlanBlockPost = [regex]::Match($rawPost, '(?ms)### ifconfig wlan0.*?(?=### |\z)')
    $recoveryObserved = ($wlanBlockPost.Success -and $wlanBlockPost.Value -match 'inet addr')
    $recoveryReason   = if ($recoveryObserved) { 'wlan0_ip_restored' } else { 'ip_not_seen_post' }
  }
  default {
    $faultReason = 'fault_type_not_handled_by_manual_collector'
  }
}

@{
  net_fault_type             = $FaultType
  iface_used                 = $WlanIface
  inject_ok                  = ($faultPid -gt 0)
  fault_observed             = $faultObserved
  recovery_observed          = $recoveryObserved
  fault_observation_reason   = $faultReason
  recovery_observation_reason = $recoveryReason
} | ConvertTo-Json | Set-Content (Join-Path $RunDir "_net_outcome.json") -Encoding UTF8

Write-Host ("[{0}] Run dir: {1}" -f $FaultType, $RunDir) -ForegroundColor Green
Write-Host ("  Files: " + ((Get-ChildItem $NetDir).Name -join ", ")) -ForegroundColor DarkGray

# Check Wi-Fi state after
Write-Host "" ; Write-Host "=== POST Wi-Fi STATE ===" -ForegroundColor White
$wpaPost = HdcS "wpa_cli -p /data/local/tmp/wpa_ctrl -i $WlanIface status 2>&1 | grep -E 'wpa_state|ssid'"
Write-Host $wpaPost

return $RunDir
