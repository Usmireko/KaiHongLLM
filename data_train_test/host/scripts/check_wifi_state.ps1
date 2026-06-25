#Requires -Version 5.1
# Check Wi-Fi state on RK3568 KaiHongOS.
# Outputs: wifi_connected | wifi_connected_no_dns | wifi_not_connected

param(
    [string]$DeviceSerial = "7001005458323933328a017ce1c43800",
    [string]$ExpectedIP   = "",
    [string]$ExpectedSSID = "Guest",
    [string]$Gateway      = "",
    [string]$PublicIP     = "223.5.5.5",
    [string]$DnsTestHost  = "www.baidu.com",
    [string]$WpaCtrl      = "/data/local/tmp/wpa_ctrl",
    [string]$BadDns       = "127.0.0.2"
)

$script:HdcExe = (Get-Command hdc -CommandType Application).Source

function Invoke-HdcShell([string]$Cmd) {
    return (& $script:HdcExe -t $DeviceSerial shell $Cmd 2>&1) -join "`n"
}

function Check([string]$Label, [string]$Result, [bool]$Ok) {
    $color = if ($Ok) { "Green" } else { "Yellow" }
    Write-Host ("  [{0}] {1}" -f $(if ($Ok) {"OK"} else {"--"}), $Label) -ForegroundColor $color
    if ($Result.Trim()) { Write-Host "        $($Result.Trim())" -ForegroundColor DarkGray }
}

function Test-ReceivedPing([string]$Output) {
    $m = [regex]::Match($Output, "(\d+)\s+received")
    if ($m.Success -and [int]$m.Groups[1].Value -gt 0) { return $true }
    return ($Output -match "(?im)^\s*(64\s+)?bytes\s+from\s+")
}

function Convert-RouteGateway([string]$HexGateway) {
    if (-not ($HexGateway -match "^[0-9A-Fa-f]{8}$")) { return "" }
    $parts = @()
    for ($i = 0; $i -lt 8; $i += 2) { $parts += [Convert]::ToInt32($HexGateway.Substring($i, 2), 16) }
    [array]::Reverse($parts)
    return ($parts -join ".")
}

function Get-FirstMatch([string]$Text, [string]$Pattern) {
    $m = [regex]::Match($Text, $Pattern)
    if ($m.Success) { return $m.Groups[1].Value }
    return ""
}

function Assert-Token([string]$Name, [string]$Value, [string]$Pattern, [bool]$AllowEmpty = $false) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        if ($AllowEmpty) { return }
        throw "$Name must not be empty"
    }
    if ($Value -notmatch $Pattern) {
        throw "$Name contains unsupported characters: $Value"
    }
}

$ipv4Pattern = '^(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}$'
$hostPattern = '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$'
$targetPattern = '^[A-Za-z0-9_.:-]+$'
$pathPattern = '^/[A-Za-z0-9_./-]+$'
Assert-Token "DeviceSerial" $DeviceSerial $targetPattern
Assert-Token "ExpectedIP" $ExpectedIP $ipv4Pattern $true
Assert-Token "Gateway" $Gateway $ipv4Pattern $true
Assert-Token "PublicIP" $PublicIP $ipv4Pattern
Assert-Token "DnsTestHost" $DnsTestHost $hostPattern
Assert-Token "WpaCtrl" $WpaCtrl $pathPattern
Assert-Token "BadDns" $BadDns $ipv4Pattern $true

Write-Host ""
Write-Host "-- Wi-Fi state check ---------------------" -ForegroundColor White

# 0. wpa_cli status -> Layer 1 (ssid + wpa_state)
$wpaSt        = (& $script:HdcExe -t $DeviceSerial shell "wpa_cli -p $WpaCtrl -i wlan0 status 2>&1") -join "`n"
$wpaAvailable = $wpaSt -notmatch "ctrl_ifname|No such file|not found|FAIL"
$wpaOk        = $wpaSt -match "wpa_state=COMPLETED" -and ($ExpectedSSID -eq "" -or $wpaSt -match "ssid=$([regex]::Escape($ExpectedSSID))")
$wpaStateLine = if ($wpaSt -match "(wpa_state=\S+)") { $Matches[1] } else { "no_response" }
Check "wpa_cli -> ssid=$ExpectedSSID wpa_state=COMPLETED" $wpaStateLine $wpaOk

# 1. ifconfig wlan0 -> IP
$ifcfg = Invoke-HdcShell "ifconfig wlan0"
$currentIP = Get-FirstMatch $ifcfg "inet addr:\s*([0-9.]+)"
$hasIP = $currentIP -ne "" -and ($ExpectedIP -eq "" -or $currentIP -eq $ExpectedIP)
$ipLabel = if ($ExpectedIP) { "ifconfig wlan0 -> $ExpectedIP" } else { "ifconfig wlan0 -> any IPv4" }
Check $ipLabel ($ifcfg | Select-String "inet addr") $hasIP

# 2. /proc/net/route -> default route on wlan0
$route    = Invoke-HdcShell "cat /proc/net/route"
$defaultRouteLine = ($route -split "`n" | Where-Object { $_ -match "^wlan0\s+00000000\s" } | Select-Object -First 1)
$routeGateway = ""
if ($defaultRouteLine -and $defaultRouteLine -match "^wlan0\s+00000000\s+([0-9A-Fa-f]{8})\s") {
    $routeGateway = Convert-RouteGateway $Matches[1]
}
$effectiveGateway = if ($Gateway) { $Gateway } else { $routeGateway }
$hasRoute = [bool]$defaultRouteLine -and ($Gateway -eq "" -or $routeGateway -eq $Gateway)
Check "/proc/net/route -> wlan0 default gateway=$effectiveGateway" $defaultRouteLine $hasRoute

# 2b. ARP gateway
$arp = Invoke-HdcShell "cat /proc/net/arp"
$arpLine = ""
$arpResolved = $false
if ($effectiveGateway) {
    $arpLine = ($arp -split "`n" | Where-Object { $_ -match "^$([regex]::Escape($effectiveGateway))\s" } | Select-Object -First 1)
    $arpResolved = $arpLine -match "\s0x2\s" -and $arpLine -notmatch "00:00:00:00:00:00"
}
Check "/proc/net/arp -> gateway resolved" $arpLine $arpResolved

# 3. ping gateway
$pingGW = if ($effectiveGateway) { Invoke-HdcShell "ping -c 3 -W 2 $effectiveGateway" } else { "skipped (no gateway)" }
$gwOk   = Test-ReceivedPing $pingGW
Check "ping $effectiveGateway" ($pingGW | Select-String "packets") $gwOk

# 4. ping public IP (L3 connectivity, no DNS)
$pingPub = if ($hasRoute) { Invoke-HdcShell "ping -c 3 -W 2 $PublicIP" } else { "skipped (no default route)" }
$pubOk   = Test-ReceivedPing $pingPub
Check "ping $PublicIP" ($pingPub | Select-String "packets") $pubOk

# 5. DNS resolution check (ping by hostname; no nslookup/getprop on this device)
$resolvCmd = "echo '### resolv_conf_candidates'; for p in /data/service/el1/public/netmanager/resolv.conf /etc/resolv.conf /system/etc/resolv.conf; do echo `"-- `$p`"; if [ -e `"`$p`" ]; then grep -n nameserver `"`$p`" 2>/dev/null || echo NO_NAMESERVER; else echo MISSING; fi; done"
$resolv = Invoke-HdcShell $resolvCmd
$badDnsResidual = $false
if (-not [string]::IsNullOrWhiteSpace($BadDns)) {
    $badDnsResidual = ($resolv -match ("nameserver\s+" + [regex]::Escape($BadDns) + "(\s|$)"))
}
Check "resolver no bad DNS $BadDns" (($resolv -split "`n" | Select-String "nameserver" | Out-String).Trim()) (-not $badDnsResidual)

$gatewayReachable = $arpResolved -or $gwOk
$l3Ok = $wpaOk -and $hasIP -and $hasRoute -and $gatewayReachable -and $gwOk -and $pubOk
$dnsOk = $false
if ($hasRoute) {
    $pingDns = Invoke-HdcShell "ping -c 2 -W 3 $DnsTestHost"
    $dnsOk = Test-ReceivedPing $pingDns
    Check "DNS: ping $DnsTestHost" ($pingDns -split "`n" | Select-Object -First 2 | Out-String).Trim() $dnsOk
} else {
    Check "DNS: ping $DnsTestHost" "skipped (L3 not ready)" $false
}

# Final verdict
$state = if (-not $l3Ok) {
    "wifi_not_connected"
} elseif (-not $dnsOk) {
    "wifi_connected_no_dns"
} elseif ($badDnsResidual) {
    "wifi_connected_no_dns"
} else {
    "wifi_connected"
}

$stateColor = switch ($state) {
    "wifi_connected"        { "Green" }
    "wifi_connected_no_dns" { "Yellow" }
    default                 { "Red" }
}

Write-Host ""
Write-Host ("  STATE: {0}" -f $state) -ForegroundColor $stateColor
Write-Host ("  SNAPSHOT_JSON: {0}" -f (([pscustomobject]@{
    wpa_cli_available = $wpaAvailable
    wpa_cli_completed = $wpaOk
    wlan0_ipv4 = $currentIP
    expected_ipv4 = $ExpectedIP
    default_route_gateway = $routeGateway
    expected_gateway = $Gateway
    gateway_arp_line = $arpLine
    gateway_arp_resolved = $arpResolved
    gateway_ping_ok = $gwOk
    public_ip_ping_ok = $pubOk
    dns_ping_ok = $dnsOk
    bad_dns = $BadDns
    bad_dns_residual = $badDnsResidual
    state = $state
}) | ConvertTo-Json -Compress)) -ForegroundColor DarkGray
Write-Host "------------------------------------------" -ForegroundColor White
Write-Host ""

return $state
