#Requires -Version 5.1
# Ensure Wi-Fi on RK3568 KaiHongOS. Verified path:
#   Layer 1: spawn wpa_supplicant -> poll wpa_cli -> COMPLETED
#   Layer 2: ifconfig + /data/busybox route

param(
    [string]$DeviceSerial = "7001005458323933328a017ce1c43800",
    [string]$SSID         = "Guest",
    [string]$PSK          = "",
    [string]$StaticIP     = "",
    [string]$Netmask      = "255.255.255.0",
    [string]$Gateway      = "",
    [string]$WpaConf      = "/data/local/tmp/wpa.conf",
    [string]$WpaPid       = "/data/local/tmp/wpa.pid",
    [string]$WpaCtrl      = "/data/local/tmp/wpa_ctrl",
    [string]$CheckScript  = "$PSScriptRoot\check_wifi_state.ps1",
    [string]$PublicIP     = "223.5.5.5",
    [string]$DnsTestHost  = "www.baidu.com",
    [string]$DnsFixServer = "223.5.5.5",
    [int]   $AssocTimeout = 25,
    [switch]$AllowFallbackStaticL3,
    [switch]$AllowDnsHookFix
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:HdcExe = (Get-Command hdc -CommandType Application).Source
$script:RedactionToken = "<OWNER_PROVIDED_GUEST_WIFI_PSK_REDACTED>"
$Log = "$PSScriptRoot\wifi_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

function Redact-Sensitive([AllowNull()][string]$Text) {
    if ($null -eq $Text) { return "" }
    $r = $Text
    $r = [regex]::Replace($r, '(?i)(-PSK\s+)\S+', {
        param($m) $m.Groups[1].Value + $script:RedactionToken
    })
    $r = [regex]::Replace($r, '(?is)(psk\s*=\\x22).*?(\\x22)', {
        param($m) $m.Groups[1].Value + $script:RedactionToken + $m.Groups[2].Value
    })
    $r = [regex]::Replace($r, '(?i)(psk\s*=\s*["'']?)[^"''\s]+(["'']?)', {
        param($m) $m.Groups[1].Value + $script:RedactionToken + $m.Groups[2].Value
    })
    $r = [regex]::Replace($r, '(?i)((password|passphrase|secret|credential)\s*[:=]\s*)\S+', {
        param($m) $m.Groups[1].Value + $script:RedactionToken
    })
    return $r
}

function Invoke-HdcShell([string]$Cmd) {
    $out = (& $script:HdcExe -t $DeviceSerial shell $Cmd 2>&1) -join "`n"
    $safeCmd = Redact-Sensitive $Cmd
    $safeOut = Redact-Sensitive ($out.Substring(0,[Math]::Min($out.Length,200)))
    Add-Content $Log "  [hdc] $safeCmd`n       $safeOut"
    return $out
}

function Invoke-HdcFileSend([string]$LocalPath, [string]$RemotePath) {
    $out = (& $script:HdcExe -t $DeviceSerial file send $LocalPath $RemotePath 2>&1) -join "`n"
    $safeOut = Redact-Sensitive ($out.Substring(0,[Math]::Min($out.Length,200)))
    Add-Content $Log "  [hdc-file-send] <local_redacted> -> $RemotePath`n       $safeOut"
    return $out
}

function Log([string]$Msg, [string]$c = "Cyan") {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $Msg"
    Write-Host $line -ForegroundColor $c
    Add-Content $Log $line
}

function GetState {
    $r = & $CheckScript -DeviceSerial $DeviceSerial -ExpectedIP $StaticIP -ExpectedSSID $SSID -Gateway $Gateway -PublicIP $PublicIP -DnsTestHost $DnsTestHost
    return [string]($r | Where-Object { $_ -match "^wifi_" } | Select-Object -Last 1)
}

function Test-ReceivedPing([string]$Output) {
    if ($Output -match "(\d+)\s+received") {
        return ([int]$Matches[1] -gt 0)
    }
    return ($Output -match "(?im)^\s*(64\s+)?bytes\s+from\s+")
}

function Test-DeviceCommand([string]$Path) {
    $safePath = $Path -replace "'", "'\''"
    $r = Invoke-HdcShell "if [ -x '$safePath' ]; then echo present; else echo missing; fi"
    return ($r -match "present")
}

function Test-GatewayReachable([string]$TargetGateway) {
    if ([string]::IsNullOrWhiteSpace($TargetGateway)) { return $false }
    Invoke-HdcShell "ping -c 1 -W 2 $TargetGateway" | Out-Null
    $arp = Invoke-HdcShell "cat /proc/net/arp"
    $arpLine = ($arp -split "`n" | Where-Object { $_ -match "^$([regex]::Escape($TargetGateway))\s" } | Select-Object -First 1)
    if ($arpLine -match "\s0x2\s" -and $arpLine -notmatch "00:00:00:00:00:00") { return $true }
    $ping = Invoke-HdcShell "ping -c 3 -W 2 $TargetGateway"
    return (Test-ReceivedPing $ping)
}

function Convert-IPv4ToRouteGatewayHex([string]$Address) {
    $parts = $Address -split "\."
    if ($parts.Count -ne 4) { return "" }
    $bytes = foreach ($part in $parts) {
        $n = 0
        if (-not [int]::TryParse($part, [ref]$n) -or $n -lt 0 -or $n -gt 255) { return "" }
        $n
    }
    return "{0:X2}{1:X2}{2:X2}{3:X2}" -f $bytes[3], $bytes[2], $bytes[1], $bytes[0]
}

function Test-DefaultRouteViaGateway([string]$TargetGateway) {
    if ([string]::IsNullOrWhiteSpace($TargetGateway)) { return $false }
    $expectedGatewayHex = Convert-IPv4ToRouteGatewayHex $TargetGateway
    if ([string]::IsNullOrWhiteSpace($expectedGatewayHex)) { return $false }
    $route = Invoke-HdcShell "cat /proc/net/route"
    foreach ($line in ($route -split "`n")) {
        $cols = @($line -split "\s+" | Where-Object { $_ -ne "" })
        if ($cols.Count -ge 3 -and $cols[0] -eq "wlan0" -and $cols[1] -eq "00000000" -and $cols[2].ToUpperInvariant() -eq $expectedGatewayHex) {
            return $true
        }
    }
    return $false
}

function Invoke-SafeDhcpRefresh {
    Log "STEP 3 -- DHCP/system network capability probe" White
    $candidates = @(
        "/system/bin/dhcp_client_service",
        "/system/bin/udhcpc",
        "/system/bin/dhcpcd",
        "/system/bin/dhclient",
        "/data/busybox"
    )
    $found = @()
    foreach ($candidate in $candidates) {
        if (Test-DeviceCommand $candidate) { $found += $candidate }
    }
    if ($found.Count -eq 0) {
        Log "  no safe DHCP/system network command found" Yellow
        return $false
    }

    foreach ($cmd in $found) {
        Log "  DHCP/system candidate found: $cmd" DarkGray
    }

    if ($found -contains "/system/bin/dhcp_client_service") {
        Log "  invoking bounded DHCP refresh through /system/bin/dhcp_client_service" Yellow
        Invoke-HdcShell "/system/bin/dhcp_client_service status wlan0 2>&1" | Out-Null
        Invoke-HdcShell "/system/bin/dhcp_client_service start wlan0 -4 2>&1" | Out-Null
        Start-Sleep 5
        return $true
    }

    if ($found -contains "/system/bin/udhcpc") {
        Log "  invoking bounded DHCP refresh through /system/bin/udhcpc" Yellow
        Invoke-HdcShell "/system/bin/udhcpc -i wlan0 -q -n 2>&1" | Out-Null
        Start-Sleep 3
        return $true
    }

    if ($found -contains "/system/bin/dhcpcd") {
        Log "  invoking bounded DHCP refresh through /system/bin/dhcpcd" Yellow
        Invoke-HdcShell "/system/bin/dhcpcd wlan0 2>&1" | Out-Null
        Start-Sleep 3
        return $true
    }

    if ($found -contains "/system/bin/dhclient") {
        Log "  invoking bounded DHCP refresh through /system/bin/dhclient" Yellow
        Invoke-HdcShell "/system/bin/dhclient wlan0 2>&1" | Out-Null
        Start-Sleep 3
        return $true
    }

    Log "  busybox present but no verified DHCP applet path is used as production restore" Yellow
    return $false
}

function Write-WpaConfToDevice {
    if ([string]::IsNullOrWhiteSpace($PSK)) {
        Log "  PSK is required when Layer 1 is not already associated" Red
        exit 1
    }

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("wpa_" + [guid]::NewGuid().ToString("N") + ".conf")
    try {
        $content = @"
ctrl_interface=$WpaCtrl
update_config=1

network={
    ssid="$SSID"
    psk="$PSK"
    key_mgmt=WPA-PSK
}
"@
        [System.IO.File]::WriteAllText($tmp, $content, [System.Text.Encoding]::ASCII)
        Invoke-HdcFileSend $tmp $WpaConf | Out-Null
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
}

function Get-WifiBaselineSnapshot {
    $wpa = Invoke-HdcShell "wpa_cli -p $WpaCtrl -i wlan0 status 2>&1"
    $ifcfg = Invoke-HdcShell "ifconfig wlan0"
    $route = Invoke-HdcShell "cat /proc/net/route"
    $gwPing = Invoke-HdcShell "ping -c 3 -W 2 $Gateway"
    $gwOk = Test-ReceivedPing $gwPing
    $publicPing = ""
    $dnsPing = ""
    if ($gwOk) {
        $publicPing = Invoke-HdcShell "ping -c 3 -W 2 $PublicIP"
        $dnsPing = Invoke-HdcShell "ping -c 3 -W 3 $DnsTestHost"
    }

    return [pscustomobject]@{
        WpaCompleted = ($wpa -match "wpa_state=COMPLETED" -and $wpa -match "ssid=$SSID")
        HasIp        = ($ifcfg -match "inet addr:\s*$([regex]::Escape($StaticIP))")
        HasRoute     = (($route -split "`n") -match "^wlan0\s+00000000\s")
        GatewayOk    = $gwOk
        GatewayReachable = (Test-GatewayReachable $Gateway)
        PublicOk     = (Test-ReceivedPing $publicPing)
        DnsOk        = (Test-ReceivedPing $dnsPing)
        WpaRaw       = $wpa
        IfconfigRaw  = $ifcfg
        RouteRaw     = $route
        GatewayRaw   = $gwPing
        PublicRaw    = $publicPing
        DnsRaw       = $dnsPing
    }
}

function Set-StaticIpAndDefaultRoute([string]$Ip) {
    if ([string]::IsNullOrWhiteSpace($Ip) -or [string]::IsNullOrWhiteSpace($Gateway)) {
        Log "BLOCKED_NO_SAFE_DHCP_OR_REAL_GATEWAY_PATH" Red
        return $false
    }
    Log "  fallback_static_l3 ip=$Ip gateway=$Gateway" Yellow
    Invoke-HdcShell "ifconfig wlan0 $Ip netmask $Netmask" | Out-Null
    Invoke-HdcShell "/data/busybox route del default dev wlan0 2>/dev/null; true" | Out-Null
    Invoke-HdcShell "/data/busybox route add default gw $Gateway dev wlan0 2>&1" | Out-Null
    if (-not (Test-DefaultRouteViaGateway $Gateway)) {
        Log "BLOCKER: default_route_missing_after_l3_restore" Red
        return $false
    }
    if (-not (Test-GatewayReachable $Gateway)) {
        Log "BLOCKER: gateway_unreachable_after_l3_restore" Red
        return $false
    }
    return $true
}

function Invoke-GatewayUnreachableRecovery {
    Log "gateway_unreachable_after_assoc -- recovery branch" Yellow
    Log "  ARP before reconnect:" DarkGray
    Invoke-HdcShell "cat /proc/net/arp" | Out-Null

    Invoke-HdcShell "wpa_cli -p $WpaCtrl -i wlan0 reconnect 2>/dev/null; true" | Out-Null
    Start-Sleep 5

    $afterReconnect = Invoke-HdcShell "ping -c 3 -W 2 $Gateway"
    if (Test-ReceivedPing $afterReconnect) {
        Log "  recovered_by_wpa_reconnect" Green
        return $true
    }

    Log "  gateway still unreachable after reconnect; try fallback_static_l3 restore" Yellow
    Invoke-HdcShell "cat /proc/net/arp" | Out-Null

    try {
        if (-not $AllowFallbackStaticL3) {
            Log "BLOCKED_NO_SAFE_DHCP_OR_REAL_GATEWAY_PATH" Red
            return $false
        }
        if (-not (Set-StaticIpAndDefaultRoute $StaticIP)) { return $false }
        Start-Sleep 2
    } finally {
        if ($AllowFallbackStaticL3) { Set-StaticIpAndDefaultRoute $StaticIP | Out-Null }
        Start-Sleep 2
    }

    $gwPing = Invoke-HdcShell "ping -c 3 -W 2 $Gateway"
    $publicPing = Invoke-HdcShell "ping -c 3 -W 2 $PublicIP"
    $dnsPing = Invoke-HdcShell "ping -c 3 -W 3 $DnsTestHost"

    $ok = (Test-ReceivedPing $gwPing) -and (Test-ReceivedPing $publicPing) -and (Test-ReceivedPing $dnsPing)
    if ($ok) {
        Log "  recovered_by_l3_route_restore" Green
    } else {
        Log "  gateway_unreachable_after_l3_restore" Red
        Invoke-HdcShell "cat /proc/net/arp" | Out-Null
    }
    return $ok
}

# -- STEP 0: check ------------------------------------------------------------
Log "Log: $Log" DarkGray
Log "STEP 0 -- initial check" White

if ((GetState) -eq "wifi_connected") {
    Log "Already connected -- done." Green
    exit 0
}

Log "Not connected -- starting recovery." Yellow

# -- Patch 1: check if Layer 1 is already good --------------------------------
# If wpa_state=COMPLETED + ssid=Guest, skip cleanup+spawn and go straight to Layer 2.
$wpaNow = Invoke-HdcShell "wpa_cli -p $WpaCtrl -i wlan0 status 2>&1"
$layer1Ok = $wpaNow -match "wpa_state=COMPLETED" -and $wpaNow -match "ssid=$SSID"

if ($layer1Ok) {
    Log "Layer 1 already associated (ssid=$SSID COMPLETED) -- skipping cleanup and spawn" Green
} else {

# -- STEP 1a: clear stale L3 before touching Layer 1 --------------------------
# Stale IP/route can linger after a reboot even when wlan0 is not associated.
# Remove them now so Layer 2 starts from a clean slate.
Log "STEP 1a -- clear stale L3 (no live Layer 1 detected)" Yellow
Invoke-HdcShell "ifconfig wlan0 0.0.0.0 2>/dev/null; true"                    | Out-Null
Invoke-HdcShell "/data/busybox route del default dev wlan0 2>/dev/null; true"  | Out-Null
Log "  stale L3 cleared" Green

# -- STEP 1: cleanup ----------------------------------------------------------
Log "STEP 1 -- cleanup stale wpa_supplicant" White

$oldPid = (Invoke-HdcShell "cat $WpaPid 2>/dev/null").Trim()
if ($oldPid -match "^\d+$") { Invoke-HdcShell "kill $oldPid 2>/dev/null; true" | Out-Null; Start-Sleep 1 }
Invoke-HdcShell "killall wpa_supplicant 2>/dev/null; true" | Out-Null  # coarse kill -- only method confirmed on this board
Start-Sleep 1
Invoke-HdcShell "rm -rf $WpaCtrl $WpaPid 2>/dev/null; true" | Out-Null
Log "  cleanup done" Green

# -- STEP 2: Layer 1 -- wpa_supplicant ----------------------------------------
Log "STEP 2 -- Layer 1: association" White

# Patch 2: pre-create ctrl dir so wpa_supplicant can create its socket inside it
Invoke-HdcShell "mkdir -p $WpaCtrl && chmod 777 $WpaCtrl" | Out-Null
Log "  ctrl dir ready: $WpaCtrl" Green

# Write conf through hdc file send so the PSK is not embedded in a remote shell command.
Write-WpaConfToDevice

# Verify conf written
$confCheck = Invoke-HdcShell "grep -c ssid $WpaConf 2>/dev/null"
if (-not ($confCheck -match "[1-9]")) {
    Log "  conf write failed" Red; exit 1
}
Log "  conf written: $WpaConf" Green

# Spawn
Invoke-HdcShell "/system/bin/wpa_supplicant -B -D nl80211 -i wlan0 -c $WpaConf -P $WpaPid 2>&1" | Out-Null
Start-Sleep 1

$procCheck = Invoke-HdcShell "ps -ef | grep wpa_supplicant | grep -v grep"
if (-not ($procCheck -match "wpa_supplicant")) {
    Log "  wpa_supplicant failed to start" Red; exit 1
}
Log "  wpa_supplicant running" Green

# Wait for ctrl socket to become responsive (wpa_cli ping -> PONG)
$ctrlReady = $false
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep 1
    $pong = Invoke-HdcShell "wpa_cli -p $WpaCtrl -i wlan0 ping 2>&1"
    if ($pong -match "PONG") {
        Log "  ctrl ready after ${i}s (PONG)" Green; $ctrlReady = $true; break
    }
    Log "  ${i}s -- ctrl not ready yet" DarkGray
}
if (-not $ctrlReady) {
    Log "  ctrl socket never responded -- supplicant may have crashed" Red; exit 1
}

# Poll for COMPLETED
$ok = $false
for ($i = 2; $i -le $AssocTimeout; $i += 2) {
    Start-Sleep 2
    $st = Invoke-HdcShell "wpa_cli -p $WpaCtrl -i wlan0 status 2>&1"
    if ($st -match "wpa_state=COMPLETED" -and $st -match "ssid=$SSID") {
        Log "  COMPLETED after ${i}s" Green; $ok = $true; break
    }
    $cur = if ($st -match "wpa_state=(\S+)") { $Matches[1] } else { "no_response" }
    Log "  ${i}s -- $cur" DarkGray
}

if (-not $ok) {
    Log "  Association timeout after ${AssocTimeout}s" Red; exit 1
}

} # end else (Layer 1 was not already good)

if (Invoke-SafeDhcpRefresh) {
    if ((GetState) -eq "wifi_connected") {
        Log "SUCCESS after DHCP/system network refresh" Green
        Log "Log: $Log" DarkGray
        exit 0
    }
}

if ($AllowDnsHookFix -and (GetState) -eq "wifi_connected_no_dns") {
    Log "STEP 3 -- L3 already healthy; proceeding to DNS hook fix" Yellow
} elseif (-not $AllowFallbackStaticL3) {
    Log "BLOCKED_NO_SAFE_DHCP_OR_REAL_GATEWAY_PATH" Red
    Log "FAILED -- see $Log" Red
    exit 1
} else {

    Log "STEP 3 -- fallback_static_l3: static IP + route" White

    if (-not (Set-StaticIpAndDefaultRoute $StaticIP)) {
        Log "FAILED -- see $Log" Red
        exit 1
    }
    Log "  default route via $Gateway verified reachable" Green
}

# -- STEP 3b: DNS fix ----------------------------------------------------------
# OHOS musl dlopen()s libnetsys_client.z.so to hook DNS through NetsysNative.
# NetsysNative has no configured DNS because WifiDevice SA never registered the
# connection. Fix: bind-mount an empty file over the so to force dlopen() failure,
# making musl fall back to reading /etc/resolv.conf directly.
# Also bind-mount /etc/resolv.conf to use the configured known-good DNS server.
if ($AllowDnsHookFix) {
    Log "STEP 3b -- DNS fix: disable netsys hook + set resolv.conf" White

    $effectiveDnsFixServer = if (-not [string]::IsNullOrWhiteSpace($DnsFixServer)) { $DnsFixServer } else { "223.5.5.5" }
    Invoke-HdcShell "echo 'nameserver $effectiveDnsFixServer' > /data/local/tmp/resolv.conf" | Out-Null
    Invoke-HdcShell "echo '' > /data/local/tmp/libnetsys_client_fake.z.so" | Out-Null

    # bind mounts are idempotent: if already mounted, the inner mount --bind is a no-op
    Invoke-HdcShell "mount | grep -q libnetsys_client || mount --bind /data/local/tmp/libnetsys_client_fake.z.so /system/lib/platformsdk/libnetsys_client.z.so 2>/dev/null; true" | Out-Null
    Invoke-HdcShell "mount | grep -q 'resolv.conf' || mount --bind /data/local/tmp/resolv.conf /etc/resolv.conf 2>/dev/null; true" | Out-Null

    Log "  DNS fix applied" Green
} else {
    Log "STEP 3b -- DNS hook fix skipped (requires -AllowDnsHookFix)" Yellow
}

Start-Sleep 2

# -- STEP 4: final check ------------------------------------------------------
Log "STEP 4 -- final check" White

if ((GetState) -eq "wifi_connected") {
    Log "SUCCESS" Green
    Log "Log: $Log" DarkGray
    exit 0
} else {
    $snap = Get-WifiBaselineSnapshot
    if ($snap.WpaCompleted -and $snap.HasIp -and $snap.HasRoute -and -not $snap.GatewayOk) {
        if (Invoke-GatewayUnreachableRecovery) {
            if ((GetState) -eq "wifi_connected") {
                Log "SUCCESS after gateway_unreachable recovery" Green
                Log "Log: $Log" DarkGray
                exit 0
            }
        }
        Log "BLOCKER: gateway_unreachable_after_assoc" Red
    } elseif (-not $snap.WpaCompleted) {
        Log "BLOCKER: wpa_not_ready" Red
    } elseif (-not $snap.HasIp) {
        Log "BLOCKER: no_ipv4" Red
    } elseif (-not $snap.HasRoute) {
        Log "BLOCKER: no_default_route" Red
    } elseif (-not $snap.PublicOk) {
        Log "BLOCKER: public_ip_unreachable" Red
    } elseif (-not $snap.DnsOk) {
        Log "BLOCKER: dns_unreachable" Red
    } else {
        Log "BLOCKER: wifi_not_connected_unknown" Red
    }
    Log "FAILED -- see $Log" Red
    exit 1
}
