param(
  [string]$RUN_NAME = "",
  [string]$RUN_DIR  = "",
  [switch]$Quiet,
  [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NewestRunDir([string[]]$roots) {
  foreach ($r in $roots) {
    if (Test-Path -LiteralPath $r) {
      $d = Get-ChildItem -LiteralPath $r -Directory -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
      if ($d) { return $d.FullName }
    }
  }
  return $null
}

function Resolve-RunDir([string]$Name,[string]$Dir) {
  $roots = @(".\inbox\runs", ".\inbox\run")
  if ($Dir -and (Test-Path -LiteralPath $Dir)) { return (Resolve-Path -LiteralPath $Dir).Path }
  if ($Name) {
    foreach ($r in $roots) {
      $cand = Join-Path $r $Name
      if (Test-Path -LiteralPath $cand) { return (Resolve-Path -LiteralPath $cand).Path }
    }
    return $null
  }
  $n = Get-NewestRunDir $roots
  return $n
}

function Read-Text([string]$path) {
  if (Test-Path -LiteralPath $path) {
    return (Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue)
  }
  return ""
}

function Add-Item([ref]$arr,[string]$s) {
  if (-not $s) { return }
  [void]$arr.Value.Add($s)
}

function Get-PingOutcome([string]$txt) {
  if (-not $txt) { return "UNKNOWN" }
  if ($txt -match '(?i)\b0%[ ]+packet loss\b' -or
      $txt -match '(?i)\b3 packets transmitted,\s*3 (packets )?received\b' -or
      $txt -match '(?i)\b1 packets transmitted,\s*1 (packets )?received\b' -or
      $txt -match '(?i)\bbytes from\b') {
    return "OK"
  }
  if ($txt -match '(?i)Network (is )?unreachable|Network unreachable|Destination Host Unreachable|connect:\s*Network is unreachable|sendto:\s*Network is unreachable|Operation not permitted') {
    return "UNREACHABLE"
  }
  if ($txt -match '(?i)\b100%[ ]+packet loss\b|\b0 (packets )?received\b') {
    return "LOSS"
  }
  return "UNKNOWN"
}

function Has-DnsFailure([string]$txt) {
  if (-not $txt) { return $false }
  return ($txt -match '(?i)Name does not resolve|Temporary failure in name resolution|no servers could be reached|SERVFAIL|NXDOMAIN|Try again')
}

function Has-DnsSuccess([string]$txt) {
  if (-not $txt) { return $false }
  if ($txt -notmatch '(?i)nip\.io') { return $false }
  if (Has-DnsFailure $txt) { return $false }
  return ($txt -match '(?i)\b0%[ ]+packet loss\b|\b1 (packets )?received\b|bytes from\b')
}

function Get-IfConfigIface([string]$netPre) {
  if (-not $netPre) { return "" }
  $m = [regex]::Match($netPre, '(?m)^\#\#\# ifconfig\s+([^\s]+)\s*$')
  if ($m.Success) { return $m.Groups[1].Value }
  return ""
}

function Get-SectionPingOutcome([string]$txt, [string]$sectionMarker) {
  if (-not $txt) { return "UNKNOWN" }
  $esc = [regex]::Escape($sectionMarker)
  $m = [regex]::Match($txt, "(?ms)^###\s+$esc\s*\r?\n(.*?)(?=^###\s|\z)")
  if (-not $m.Success) { return "UNKNOWN" }
  return (Get-PingOutcome $m.Groups[1].Value)
}

function Get-SectionText([string]$txt, [string]$sectionMarker) {
  if (-not $txt) { return "" }
  $esc = [regex]::Escape($sectionMarker)
  $m = [regex]::Match($txt, "(?ms)^###\s+$esc\s*\r?\n(.*?)(?=^###\s|\z)")
  if (-not $m.Success) { return "" }
  return $m.Groups[1].Value
}

function Get-LinkState([string]$txt,[string]$iface) {
  $out = [ordered]@{ oper=""; carrier="" }
  if (-not $txt -or -not $iface) { return $out }
  $esc = [regex]::Escape($iface)
  # 匹配:
  # -- eth1
  # up
  # 1
  $m = [regex]::Match($txt, "(?ms)^\-\-\s+$esc\s*\r?\n(?<oper>[^\r\n]+)\r?\n(?<car>[^\r\n]+)")
  if ($m.Success) {
    $out.oper = ($m.Groups["oper"].Value).Trim()
    $out.carrier = ($m.Groups["car"].Value).Trim()
  }
  return $out
}

function Extract-IptablesBlock([string]$txt,[string]$marker) {
  if (-not $txt) { return "" }
  try {
    $markerEsc = [regex]::Escape($marker)
    $pat = "(?ms)^$markerEsc\s*\r?\n(?<body>.*?)(?=^\#\#\# |\z)"
    $m = [regex]::Match($txt, $pat)
    if ($m.Success) { return $m.Groups["body"].Value }
  } catch {
    # ignore regex errors; treat as no block
  }
  return ""
}


function Parse-DropCounters([string]$block) {
  # 返回 @{ ip => @{ pkts=int; bytes=int } }
  $map = @{}
  if (-not $block) { return $map }
  $lines = $block -split "`r?`n"
  foreach ($ln in $lines) {
    # 例： "0 0 DROP all -- * * 0.0.0.0/0 8.8.8.8"
    $m = [regex]::Match($ln, '^\s*(\d+)\s+(\d+)\s+DROP\b.*\s(\d{1,3}(?:\.\d{1,3}){3})\s*$')
    if ($m.Success) {
      $ip = $m.Groups[3].Value
      $map[$ip] = @{ pkts = [int]$m.Groups[1].Value; bytes = [int]$m.Groups[2].Value }
    }
  }
  return $map
}

function Get-WpaState([string]$txt) {
  # Returns wpa_state value from wpa_cli status output, or "" if missing
  if (-not $txt) { return "" }
  $m = [regex]::Match($txt, '(?m)wpa_state=(\S+)')
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  return ""
}

function Has-IfaceIp([string]$txt, [string]$iface) {
  if (-not $txt) { return $false }
  $esc = [regex]::Escape($iface)
  $ipv4Pat = '(?m)(inet\s+(addr:)?\s*\d+\.\d+\.\d+\.\d+\b|^ip_address=\d+\.\d+\.\d+\.\d+\s*$)'
  # 1. Named section: ### ifconfig wlan0
  $m = [regex]::Match($txt, "(?ms)^###\s+ifconfig\s+$esc\s*\r?\n(.*?)(?=^###\s|\z)")
  if ($m.Success -and $m.Groups[1].Value -match $ipv4Pat) { return $true }
  # 2. Named section: ### ifconfig_wlan (probe file)
  $m2 = [regex]::Match($txt, "(?ms)^###\s+ifconfig_wlan\s*\r?\n(.*?)(?=^###\s|\z)")
  if ($m2.Success -and $m2.Groups[1].Value -match $ipv4Pat) { return $true }
  # 3. Within ### ifconfig -a: find iface-named block (starts at ^iface, ends before next non-indented line)
  $m3 = [regex]::Match($txt, "(?ms)^###\s+ifconfig\s+-a\s*\r?\n(.*?)(?=^###\s|\z)")
  if ($m3.Success) {
    $m4 = [regex]::Match($m3.Groups[1].Value, "(?ms)^$esc\b.*?(?=\r?\n\S|\z)")
    if ($m4.Success -and $m4.Value -match $ipv4Pat) { return $true }
  }
  # 4. wpa_cli status for the same interface may expose ip_address even when ifconfig format varies.
  $m5 = [regex]::Match($txt, "(?ms)^###\s+wpa_cli(?:_probe|\s+status)?\s*\r?\n(.*?)(?=^###\s|\z)")
  if ($m5.Success -and $m5.Groups[1].Value -match $ipv4Pat) { return $true }
  return $false
}

function Has-DefaultRoute([string]$txt, [string]$iface) {
  # Returns $true if the '### /proc/net/route' section has a default entry for iface
  if (-not $txt) { return $false }
  if (-not $iface) { $iface = "wlan0" }
  $esc = [regex]::Escape($iface)
  # Extract /proc/net/route section
  $m = [regex]::Match($txt, "(?ms)^###\s+/proc/net/route\s*\r?\n(.*?)(?=^###\s|\z)")
  $block = if ($m.Success) { $m.Groups[1].Value } else { $txt }
  return ($block -match "(?m)^$esc\s+00000000\s")
}

function Get-DefaultRouteGatewayHexes([string]$txt, [string]$iface) {
  if (-not $txt) { return @() }
  if (-not $iface) { $iface = "wlan0" }
  $esc = [regex]::Escape($iface)
  $m = [regex]::Match($txt, "(?ms)^###\s+/proc/net/route\s*\r?\n(.*?)(?=^###\s|\z)")
  $block = if ($m.Success) { $m.Groups[1].Value } else { $txt }
  $matches = [regex]::Matches($block, "(?m)^$esc\s+00000000\s+([0-9A-Fa-f]{8})\b")
  $ret = New-Object System.Collections.Generic.List[string]
  foreach ($match in $matches) {
    [void]$ret.Add($match.Groups[1].Value.ToUpperInvariant())
  }
  return [string[]]$ret.ToArray()
}

function Get-WrongRouteExpectedFakeGatewayHexes([string]$runDir) {
  $ret = New-Object System.Collections.Generic.List[string]
  if (-not $runDir) { return @() }
  $faultLogDir = Join-Path $runDir "fault_inject"
  $candidates = @(
    (Join-Path $faultLogDir "fault_net_wrong_default_route.log"),
    (Join-Path $faultLogDir "fault_net_wrong_default_route.txt")
  )
  foreach ($path in $candidates) {
    if (-not (Test-Path $path)) { continue }
    $text = [System.IO.File]::ReadAllText($path)
    foreach ($m in [regex]::Matches($text, '(?i)\bfake_gw_hex=([0-9a-f]{8})\b')) {
      $hex = $m.Groups[1].Value.ToUpperInvariant()
      if ($ret -notcontains $hex) { [void]$ret.Add($hex) }
    }
  }
  return [string[]]$ret.ToArray()
}

# ---- main ----
$runDir = Resolve-RunDir $RUN_NAME $RUN_DIR
$meta = $null
$resultObj = [ordered]@{
  RESULT="FAIL"
  RUN_ID=""
  SCENARIO=""
  RUN_DIR=$runDir
  FAILED_CRITERIA=[System.Collections.ArrayList]@()
  WARNINGS=[System.Collections.ArrayList]@()
  NEXT_FIX=[System.Collections.ArrayList]@()
}

if (-not $runDir) {
  Add-Item ([ref]$resultObj.FAILED_CRITERIA) "RUN_NOT_FOUND"
  $resultObj.RESULT = "FAIL"
  if ($AsJson) { $resultObj | ConvertTo-Json -Compress; exit 0 }
  if (-not $Quiet) {
    Write-Host "RESULT: FAIL"
    Write-Host "RUN_ID: "
    Write-Host "SCENARIO: "
    Write-Host "FAILED_CRITERIA: RUN_NOT_FOUND"
    Write-Host "WARNINGS:"
    Write-Host "NEXT_FIX: provide RUN_NAME or RUN_DIR"
  }
  $resultObj
  exit 0
}

$metaPath = Join-Path $runDir "_run_meta.json"
$outcomePath = Join-Path $runDir "_net_outcome.json"
if (-not (Test-Path -LiteralPath $metaPath)) {
  Add-Item ([ref]$resultObj.FAILED_CRITERIA) "INCOMPLETE_FILES(_run_meta.json)"
} else {
  try {
    $meta = (Get-Content -LiteralPath $metaPath -Raw) | ConvertFrom-Json
    if ($meta.run_id) { $resultObj.RUN_ID = [string]$meta.run_id } else { $resultObj.RUN_ID = (Split-Path $runDir -Leaf) }
    if ($meta.scenario_tag) { $resultObj.SCENARIO = [string]$meta.scenario_tag } else { $resultObj.SCENARIO = "" }
  } catch {
    Add-Item ([ref]$resultObj.FAILED_CRITERIA) "META_PARSE_ERROR"
  }
}

$netOutcome = $null
if (Test-Path -LiteralPath $outcomePath) {
  try {
    $netOutcome = (Get-Content -LiteralPath $outcomePath -Raw) | ConvertFrom-Json
  } catch {
    Add-Item ([ref]$resultObj.WARNINGS) "NET_OUTCOME_PARSE_WARNING"
  }
}

# required net files
$need = @(
  "net\net_pre.txt",
  "net\net_fault.txt",
  "net\net_post.txt",
  "net\probe_pre.txt",
  "net\probe_fault.txt",
  "net\probe_post.txt"
)
foreach ($rel in $need) {
  $p = Join-Path $runDir $rel
  if (-not (Test-Path -LiteralPath $p)) {
    Add-Item ([ref]$resultObj.FAILED_CRITERIA) ("INCOMPLETE_FILES({0})" -f $rel)
  }
}

if ($resultObj.FAILED_CRITERIA.Count -gt 0) {
  $resultObj.RESULT = "FAIL"
  if ($AsJson) { $resultObj | ConvertTo-Json -Compress; exit 0 }
  if (-not $Quiet) {
    Write-Host ("RESULT: {0}" -f $resultObj.RESULT)
    Write-Host ("RUN_ID: {0}" -f $resultObj.RUN_ID)
    Write-Host ("SCENARIO: {0}" -f $resultObj.SCENARIO)
    Write-Host "FAILED_CRITERIA:"
    $resultObj.FAILED_CRITERIA | ForEach-Object { Write-Host ("- {0}" -f $_) }
    Write-Host "WARNINGS:"
    Write-Host "NEXT_FIX:"
    Write-Host "- re-run collection; ensure net/ and probe_* exist"
  }
  $resultObj
  exit 0
}

# read contents
$netPre   = Read-Text (Join-Path $runDir "net\net_pre.txt")
$netFault = Read-Text (Join-Path $runDir "net\net_fault.txt")
$netPost  = Read-Text (Join-Path $runDir "net\net_post.txt")
$probePre = Read-Text (Join-Path $runDir "net\probe_pre.txt")
$probeFault = Read-Text (Join-Path $runDir "net\probe_fault.txt")
$probePost  = Read-Text (Join-Path $runDir "net\probe_post.txt")

# optional flap extra phases
$netFault2 = Read-Text (Join-Path $runDir "net\net_fault2.txt")
$netPost2  = Read-Text (Join-Path $runDir "net\net_post2.txt")
$probeFault2 = Read-Text (Join-Path $runDir "net\probe_fault2.txt")
$probePost2  = Read-Text (Join-Path $runDir "net\probe_post2.txt")

$scenario = $resultObj.SCENARIO
$class = "unknown"
if ($scenario -match '^bg_net') { $class="bg_net" }
elseif ($scenario -match '^net_dns_fail') { $class="net_dns_fail" }
elseif ($scenario -match '^net_link_down') { $class="net_link_down" }
elseif ($scenario -match '^net_link_flap') { $class="net_link_flap" }
elseif ($scenario -match '^net_wifi_disconnect') { $class="net_wifi_disconnect" }
elseif ($scenario -match '^net_wifi_auth_fail') { $class="net_wifi_auth_fail_wrong_psk" }
elseif ($scenario -match '^net_no_default_route') { $class="net_no_default_route" }
elseif ($scenario -match '^net_no_ipv4_on_iface') { $class="net_no_ipv4_on_iface" }
elseif ($scenario -match '^net_wrong_default_route') { $class="net_wrong_default_route" }
elseif ($scenario -match '^net_gateway_unreachable') { $class="net_gateway_unreachable" }
elseif ($scenario -match '^net_public_ip_unreachable') { $class="net_public_ip_unreachable" }
else {
  # 容错：有些 run 的 scenario_tag 可能不带 net_ 前缀
  if ($scenario -match 'dns_fail') { $class="net_dns_fail" }
  elseif ($scenario -match 'link_down') { $class="net_link_down" }
  elseif ($scenario -match 'link_flap') { $class="net_link_flap" }
  elseif ($scenario -match 'bg_net') { $class="bg_net" }
  elseif ($scenario -match 'wifi_disconnect') { $class="net_wifi_disconnect" }
  elseif ($scenario -match 'wifi_auth_fail') { $class="net_wifi_auth_fail_wrong_psk" }
  elseif ($scenario -match 'no_default_route') { $class="net_no_default_route" }
  elseif ($scenario -match 'no_ipv4_on_iface') { $class="net_no_ipv4_on_iface" }
  elseif ($scenario -match 'wrong_default_route') { $class="net_wrong_default_route" }
  elseif ($scenario -match 'gateway_unreachable') { $class="net_gateway_unreachable" }
  elseif ($scenario -match 'public_ip_unreachable') { $class="net_public_ip_unreachable" }
}

$iface = Get-IfConfigIface $netPre
if (-not $iface) { Add-Item ([ref]$resultObj.WARNINGS) "NO_IFACE_EVIDENCE" }

$linkPre  = Get-LinkState $netPre $iface
$linkFault= Get-LinkState $netFault $iface
$linkPost = Get-LinkState $netPost $iface
$linkFault2= Get-LinkState $netFault2 $iface
$linkPost2 = Get-LinkState $netPost2 $iface

$pingPre   = Get-PingOutcome $probePre
$pingFault = Get-PingOutcome $probeFault
$pingPost  = Get-PingOutcome $probePost
$pingFault2 = Get-PingOutcome $probeFault2
$pingPost2  = Get-PingOutcome $probePost2

# ---- per-class checks ----
switch ($class) {

  "bg_net" {
    # 纯背景：三段 IP ping 都应 OK；不应出现 DNS failure 文本（如果你在背景里有做域名探针）
    if ($pingPre -ne "OK" -or $pingFault -ne "OK" -or $pingPost -ne "OK") {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "BG_PING_NOT_OK"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure network is stable; check NET_IFACE and routing"
    }
    if (Has-DnsFailure ($probePre+$probeFault+$probePost)) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "BG_HAS_DNS_FAILURE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "bg_net should be healthy; re-run or fix upstream DNS"
    }
    if ($linkPre.oper -and $linkPre.oper -notmatch '(?i)^up$') {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "BG_LINK_NOT_UP(pre)"
    }
    if ($linkPost.oper -and $linkPost.oper -notmatch '(?i)^up$') {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "BG_LINK_NOT_UP(post)"
    }
  }

  "net_dns_fail" {
    # A) DNS_PROOF markers
    if ($probeFault -notmatch 'DNS_PROOF_BEGIN' -or $probeFault -notmatch 'DNS_PROOF_END') {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "DNS_PROOF_MISSING"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure Collect-NetSnapshot includes DNS_PROOF in fault phase"
    }
    if ($probeFault -match '(?i)/bin/sh:|syntax error|unmatched|inaccessible or not found') {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "DNS_PROOF_BROKEN"
      Add-Item ([ref]$resultObj.NEXT_FIX) "fix probe command quoting/path; rerun"
    }

    # B) IP must be OK in fault
    if ($pingFault -ne "OK") {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "FAULT_IP_PING_NOT_OK"
      Add-Item ([ref]$resultObj.NEXT_FIX) "fault should be DNS-only; check that link/routing not broken"
    }

    # D) must show DNS failure
    if (-not (Has-DnsFailure $probeFault)) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_DNS_FAILURE_PROOF"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure probe triggers a real resolution attempt and captures error"
    }

    # B injection evidence: marker or iptables drop lines
    $hasMarker = ($probeFault -match 'iptables_dns_block\.applied' -and $probeFault -notmatch 'NO_MARKER')
    $hasDropRule = ($netFault -match '(?m)^\s*\d+\s+\d+\s+DROP\b.*\s(127\.0\.0\.2|8\.8\.8\.8|114\.114\.114\.114)\s*$')
    if (-not ($hasMarker -or $hasDropRule)) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "FAULT_NOT_ENABLED"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure net_fault.sh applied DNS blocking (marker/iptables)"
    }

    # toggle evidence (optional but recommended)
    $preOk = Has-DnsSuccess $probePre
    $postOk = Has-DnsSuccess $probePost
    if (-not ($preOk -and $postOk)) {
      Add-Item ([ref]$resultObj.WARNINGS) "NO_TOGGLE_EVIDENCE(pre/post)"
      Add-Item ([ref]$resultObj.NEXT_FIX) "optional: add a dns success probe in pre/post (nip.io) for stronger toggle"
    }

    # hit evidence (optional): parse iptables before/after within DNS_PROOF
    $beforeBlock = Extract-IptablesBlock $probeFault "### iptables_OUTPUT_before"
    $afterBlock  = Extract-IptablesBlock $probeFault "### iptables_OUTPUT_after"
    if ($beforeBlock -and $afterBlock) {
      $b = Parse-DropCounters $beforeBlock
      $a = Parse-DropCounters $afterBlock
      $hit = $false
      foreach ($k in $a.Keys) {
        if ($b.ContainsKey($k)) {
          if (($a[$k].pkts - $b[$k].pkts) -gt 0 -or ($a[$k].bytes - $b[$k].bytes) -gt 0) { $hit = $true; break }
        }
      }
      if (-not $hit) { Add-Item ([ref]$resultObj.WARNINGS) "NO_HIT_EVIDENCE" }
    } else {
      Add-Item ([ref]$resultObj.WARNINGS) "NO_IPTABLES_BEFORE_AFTER_BLOCK"
    }
  }

  "net_link_down" {
    $preUp = ($linkPre.oper -match '(?i)^up$') -or ($pingPre -eq "OK")
    $faultDown = ($linkFault.oper -match '(?i)^down$') -or ($pingFault -in @("UNREACHABLE","LOSS"))
    $postUp = ($linkPost.oper -match '(?i)^up$') -and ($pingPost -eq "OK")

    if (-not $preUp) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    if (-not $faultDown) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_LINK_DOWN_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure NET_IFACE matches the actual wired iface; verify link_down injection"
    }
    if (-not $postUp) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_RECOVERY_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure injection is reverted and link comes back in post phase"
    }
  }

  "net_link_flap" {
    $preUp = ($linkPre.oper -match '(?i)^up$') -or ($pingPre -eq "OK")
    $hasDown = $false
    if ( ($linkFault.oper -match '(?i)^down$') -or ($pingFault -in @("UNREACHABLE","LOSS")) ) { $hasDown = $true }
    if ( (-not $hasDown) -and ( ($linkFault2.oper -match '(?i)^down$') -or ($pingFault2 -in @("UNREACHABLE","LOSS")) ) ) { $hasDown = $true }

    $hasUpAfter = $false
    if ( ($linkPost.oper -match '(?i)^up$') -and ($pingPost -eq "OK") ) { $hasUpAfter = $true }
    if ( (-not $hasUpAfter) -and ($linkPost2.oper -match '(?i)^up$') -and ($pingPost2 -eq "OK") ) { $hasUpAfter = $true }

    if (-not $preUp) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    if (-not $hasDown) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_FLAP_DOWN_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "increase flap duration or add fault2 sampling; ensure iface correct"
    }
    if (-not $hasUpAfter) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_RECOVERY_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ensure flap stops and link recovers in post/post2"
    }

    if (-not $netFault2 -or -not $probeFault2) {
      Add-Item ([ref]$resultObj.WARNINGS) "MISSING_FAULT2_SNAPSHOT"
    }
    if (-not $netPost2 -or -not $probePost2) {
      Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT"
    }
  }

  "net_wifi_disconnect" {
    # Pre: wpa COMPLETED + IP; Fault: DISCONNECTED + no IP; Post: recovered
    $wpaStatePre   = Get-WpaState $netPre
    $wpaStateFault = Get-WpaState $netFault
    $wpaStatePost  = Get-WpaState $netPost
    $hasIpFault = Has-IfaceIp $netFault "wlan0"
    $hasIpPost  = Has-IfaceIp $netPost  "wlan0"
    if (-not $hasIpPost) { $hasIpPost = Has-IfaceIp $netPost2 "wlan0" }

    $preOk = ($wpaStatePre -eq "COMPLETED" -or $pingPre -eq "OK")
    $faultOk = ($wpaStateFault -match "DISCONNECTED") -or (-not $hasIpFault -and $pingFault -in @("UNREACHABLE","LOSS","UNKNOWN"))
    $postOk = ($wpaStatePost -eq "COMPLETED" -or $pingPost -eq "OK" -or $hasIpPost)

    if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    if (-not $faultOk) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_WIFI_DISCONNECT_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "check wpa_cli status in net_fault.txt; ensure net_wifi_disconnect mode ran"
    }
    if (-not $postOk) {
      Add-Item ([ref]$resultObj.WARNINGS) "NO_RECOVERY_EVIDENCE(post)"
    }
    if (-not $netPost2 -or -not $probePost2) {
      Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT"
    }
  }

  "net_wifi_auth_fail_wrong_psk" {
    # Fault: wpa_supplicant running with wrong PSK → state cycles through active-auth states
    # Distinguishing: wpa_state IN {SCANNING,ASSOCIATING,4WAY_HANDSHAKE,AUTHENTICATING}
    # NOT just "not COMPLETED" (which would conflate with net_wifi_disconnect)
    $wpaStateFault = Get-WpaState $netFault
    $wpaStatePre   = Get-WpaState $netPre
    $wpaStatePost  = Get-WpaState $netPost
    if (-not $wpaStatePost) { $wpaStatePost = Get-WpaState $netPost2 }

    $preOk = ($wpaStatePre -eq "COMPLETED" -or $pingPre -eq "OK")

    # Strong evidence 1: wpa_state is in active-auth cycle (proves supplicant is trying, not just idle)
    $activeAuthStates = @("SCANNING","ASSOCIATING","4WAY_HANDSHAKE","AUTHENTICATING","GROUP_HANDSHAKE")
    $inActiveAuth = ($wpaStateFault -and $wpaStateFault -in $activeAuthStates)

    # Strong evidence 2: wpa_bad.applied marker probed from board (proves injection was active)
    $hasBadMarker = ($netFault -match 'wpa_bad\.applied' -or $probeFault -match 'WPA_BAD_APPLIED=yes' -or $probeFault -match 'wpa_bad\.applied')

    # Recovery evidence: post goes back to COMPLETED (proves original conf was restored)
    $postRecovered = ($wpaStatePost -eq "COMPLETED")

    $faultOk = $inActiveAuth -or $hasBadMarker

    if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    if (-not $faultOk) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_AUTH_FAIL_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "wpa_state must be SCANNING/ASSOCIATING/4WAY_HANDSHAKE during fault, or wpa_bad.applied marker must be present in snapshot"
    }
    if (-not $postRecovered) {
      Add-Item ([ref]$resultObj.WARNINGS) "NO_RECOVERY_TO_COMPLETED"
    }
    if (-not $netPost2 -or -not $probePost2) {
      Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT"
    }
  }

  "net_no_default_route" {
    # Fault: wlan0 has IP but no default route in /proc/net/route
    $hasIpFault    = Has-IfaceIp $netFault "wlan0"
    $hasRouteFault = Has-DefaultRoute $netFault "wlan0"
    $hasRoutePre   = Has-DefaultRoute $netPre  "wlan0"
    $hasRoutePost  = Has-DefaultRoute $netPost "wlan0"
    if (-not $hasRoutePost) { $hasRoutePost = Has-DefaultRoute $netPost2 "wlan0" }

    $preOk  = $hasRoutePre -or ($pingPre -eq "OK")
    $faultOk = $hasIpFault -and -not $hasRouteFault
    $postOk = $hasRoutePost -or ($pingPost -eq "OK")

    if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    if (-not $faultOk) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_MISSING_ROUTE_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "check /proc/net/route in net_fault.txt; default route should be absent for wlan0"
    }
    if (-not $postOk) {
      Add-Item ([ref]$resultObj.WARNINGS) "NO_RECOVERY_EVIDENCE(post)"
    }
    if (-not $netPost2 -or -not $probePost2) {
      Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT"
    }
  }

  "net_no_ipv4_on_iface" {
    # Fault: wlan0 has no inet addr BUT wpa_state must still be COMPLETED
    # (distinguishes from net_wifi_disconnect where L1 is also down)
    $hasIpFault    = Has-IfaceIp $netFault "wlan0"
    $hasIpPre      = Has-IfaceIp $netPre   "wlan0"
    $hasIpPost     = Has-IfaceIp $netPost  "wlan0"
    if (-not $hasIpPost) { $hasIpPost = Has-IfaceIp $netPost2 "wlan0" }

    $wpaStateFault = Get-WpaState $netFault
    $wpaStatePre   = Get-WpaState $netPre
    # Hard requirement: wpa must still be COMPLETED during fault (L1 still associated)
    $wpaStillUp = ($wpaStateFault -eq "COMPLETED")

    $preOk  = ($hasIpPre -or ($pingPre -eq "OK")) -and ($wpaStatePre -eq "COMPLETED")
    # Both conditions required: no IP AND wpa still COMPLETED
    $faultOk = (-not $hasIpFault) -and $wpaStillUp
    $postOk  = $hasIpPost -or ($pingPost -eq "OK")

    if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    if (-not $faultOk) {
      if ($hasIpFault) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_NO_IP_EVIDENCE"
        Add-Item ([ref]$resultObj.NEXT_FIX) "check ifconfig wlan0 in net_fault.txt; inet addr should be absent"
      } elseif (-not $wpaStillUp) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "WPA_NOT_COMPLETED_DURING_FAULT"
        Add-Item ([ref]$resultObj.NEXT_FIX) "wpa_state must be COMPLETED during fault to distinguish from net_wifi_disconnect; check that wpa_supplicant was not killed"
      }
    }
    if (-not $postOk) {
      Add-Item ([ref]$resultObj.WARNINGS) "NO_RECOVERY_EVIDENCE(post)"
    }
    if (-not $netPost2 -or -not $probePost2) {
      Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT"
    }
  }

  "net_wrong_default_route" {
    # Check for inject-not-ready marker first (written by collector when ready marker timed out)
    $wdrTimeoutMarker = Join-Path $runDir "fault_inject\wrong_route_timeout.marker"
    if (Test-Path -LiteralPath $wdrTimeoutMarker) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "WRONG_ROUTE_READY_TIMEOUT"
      Add-Item ([ref]$resultObj.NEXT_FIX) "net_wrong_default_route injector did not achieve stable fake route within 15s; check fault_inject/fault_net_wrong_default_route.log for route del/add/verify details"
    } else {
      # Fault: wlan0 has IP + default route entry still present, but public ping fails (fake GW unreachable)
      # Distinguishes from net_no_default_route (which has NO route entry) and net_wifi_disconnect (L1 down)
      $wdrIface = if ($iface -and $iface -ne "-a") { $iface } else { "wlan0" }
      $hasIpFault = Has-IfaceIp $netFault $wdrIface

      $preGwHexes = @(Get-DefaultRouteGatewayHexes $netPre $wdrIface)
      if ($preGwHexes.Count -eq 0) { $preGwHexes = @(Get-DefaultRouteGatewayHexes $probePre $wdrIface) }
      $preGwHex = if ($preGwHexes.Count -gt 0) { $preGwHexes[0] } else { "" }

      $faultGwHexes = @(Get-DefaultRouteGatewayHexes $netFault $wdrIface)
      if ($faultGwHexes.Count -eq 0) { $faultGwHexes = @(Get-DefaultRouteGatewayHexes $probeFault $wdrIface) }
      $faultGwHex = if ($faultGwHexes.Count -gt 0) { $faultGwHexes[0] } else { "" }
      $hasRouteFault = ($faultGwHexes.Count -gt 0)
      $expectedFakeGwHexes = @(Get-WrongRouteExpectedFakeGatewayHexes $runDir)
      $fakeGwHex = if (($expectedFakeGwHexes.Count -gt 0) -and $faultGwHex -and ($expectedFakeGwHexes -contains $faultGwHex)) { $faultGwHex } else { "" }

      $postGwHexes = @()
      foreach ($routeSource in @($netPost2, $netPost, $probePost2, $probePost)) {
        foreach ($gwHex in @(Get-DefaultRouteGatewayHexes $routeSource $wdrIface)) {
          if ($postGwHexes -notcontains $gwHex) { $postGwHexes += $gwHex }
        }
      }

      $activePingIp = "223.5.5.5"
      if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "active_ping_ip") -and $meta.active_ping_ip) {
        $activePingIp = [string]$meta.active_ping_ip
      }
      $proofPost2 = Get-SectionPingOutcome $probePost2 ("ping -c 3 {0}" -f $activePingIp)
      $proofPost  = Get-SectionPingOutcome $probePost  ("ping -c 3 {0}" -f $activePingIp)
      $gatewayPost2 = Get-SectionPingOutcome $probePost2 "ping_gateway"
      $gatewayPost  = Get-SectionPingOutcome $probePost  "ping_gateway"
      $dnsPost2 = Get-SectionPingOutcome $probePost2 "dns_host_probe_primary"
      $dnsPost  = Get-SectionPingOutcome $probePost  "dns_host_probe_primary"

      $preOk = (-not [string]::IsNullOrWhiteSpace($preGwHex)) -and ($pingPre -eq "OK")
      $faultOk = $hasRouteFault -and $hasIpFault -and (-not [string]::IsNullOrWhiteSpace($fakeGwHex)) -and ($pingFault -in @("LOSS","UNREACHABLE"))
      $realRouteRestored = (-not [string]::IsNullOrWhiteSpace($preGwHex)) -and ($postGwHexes -contains $preGwHex)
      $fakeRouteRemoved = ([string]::IsNullOrWhiteSpace($fakeGwHex)) -or ($postGwHexes -notcontains $fakeGwHex)
      $proofRecovered = ($proofPost2 -eq "OK") -or ($proofPost -eq "OK")
      $dnsRecovered = ($dnsPost2 -eq "OK") -or ($dnsPost -eq "OK")
      $gatewayRecovered = ($gatewayPost2 -eq "OK") -or ($gatewayPost -eq "OK")
      $postOk = $realRouteRestored -and $fakeRouteRemoved -and $proofRecovered -and $gatewayRecovered -and $dnsRecovered

      if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
      if ([string]::IsNullOrWhiteSpace($preGwHex)) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_DEFAULT_GATEWAY_NOT_CAPTURED"
      }
      if ($expectedFakeGwHexes.Count -eq 0) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "WRONG_ROUTE_EXPECTED_FAKE_GATEWAY_MISSING"
        Add-Item ([ref]$resultObj.NEXT_FIX) "fault_inject log must record fake_gw_hex for exact wrong-route evidence"
      }
      if (($expectedFakeGwHexes.Count -gt 0) -and $faultGwHex -and ($expectedFakeGwHexes -notcontains $faultGwHex)) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "WRONG_ROUTE_FAKE_GATEWAY_MISMATCH"
        Add-Item ([ref]$resultObj.NEXT_FIX) "fault route gateway must match injector fake_gw_hex evidence"
      }
      if (-not $faultOk) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_WRONG_ROUTE_EVIDENCE"
        Add-Item ([ref]$resultObj.NEXT_FIX) "fault must show: default route entry present for wlan0 AND wlan0 has inet addr AND public ping fails; check fake GW (.254) is unreachable and DHCP did not restore correct route during hold window"
      }
      if (-not $realRouteRestored) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "RECOVERY_ROUTE_NOT_PRE_GATEWAY"
        Add-Item ([ref]$resultObj.NEXT_FIX) "post/post2 route evidence must show default route restored to the pre gateway"
      }
      if (-not $fakeRouteRemoved) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "RECOVERY_FAKE_GATEWAY_STILL_PRESENT"
      }
      if ($realRouteRestored -and $fakeRouteRemoved -and (-not ($proofRecovered -and $gatewayRecovered -and $dnsRecovered))) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "RECOVERY_CONNECTIVITY_NOT_CONFIRMED"
        Add-Item ([ref]$resultObj.NEXT_FIX) "post/post2 probes must confirm active proof IP, gateway, and DNS host recovery"
      }
      if (-not $postOk) { Add-Item ([ref]$resultObj.WARNINGS) "NO_STRICT_RECOVERY_EVIDENCE(post)" }
      if (-not $netPost2 -or -not $probePost2) { Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT" }
    }
  }

  "net_gateway_unreachable" {
    $gatewayTimeoutMarker = Join-Path $runDir "fault_inject\gateway_unreachable_timeout.marker"
    if (Test-Path -LiteralPath $gatewayTimeoutMarker) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "GATEWAY_UNREACHABLE_READY_TIMEOUT"
      Add-Item ([ref]$resultObj.NEXT_FIX) "net_gateway_unreachable injector did not reach ready state within 15s; check route replacement and gateway verification in fault_inject logs"
    } else {
      # Fault: wlan0 keeps IPv4 and a default route, but the real default gateway becomes unreachable.
      # This distinguishes gateway failure from no-default-route and specific public-IP blocking.
      $gwIface = if ($iface -and $iface -ne "-a") { $iface } else { "wlan0" }
      $combinedFault = $netFault + "`n" + $probeFault
      $hasIpFault = Has-IfaceIp $combinedFault $gwIface
      $hasRouteFault = Has-DefaultRoute $combinedFault $gwIface

      $preGwHexes = @(Get-DefaultRouteGatewayHexes $netPre $gwIface)
      if ($preGwHexes.Count -eq 0) { $preGwHexes = @(Get-DefaultRouteGatewayHexes $probePre $gwIface) }
      $preGwHex = if ($preGwHexes.Count -gt 0) { $preGwHexes[0] } else { "" }

      $postGwHexes = @()
      foreach ($routeSource in @($netPost2, $netPost, $probePost2, $probePost)) {
        foreach ($gwHex in @(Get-DefaultRouteGatewayHexes $routeSource $gwIface)) {
          if ($postGwHexes -notcontains $gwHex) { $postGwHexes += $gwHex }
        }
      }

      $activePingIp = "223.5.5.5"
      if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "active_ping_ip") -and $meta.active_ping_ip) {
        $activePingIp = [string]$meta.active_ping_ip
      }
      $gatewayFault = Get-SectionPingOutcome $probeFault "ping_gateway"
      $proofPost2 = Get-SectionPingOutcome $probePost2 ("ping -c 3 {0}" -f $activePingIp)
      $proofPost  = Get-SectionPingOutcome $probePost  ("ping -c 3 {0}" -f $activePingIp)
      $gatewayPost2 = Get-SectionPingOutcome $probePost2 "ping_gateway"
      $gatewayPost  = Get-SectionPingOutcome $probePost  "ping_gateway"
      $dnsPost2 = Get-SectionPingOutcome $probePost2 "dns_host_probe_primary"
      $dnsPost  = Get-SectionPingOutcome $probePost  "dns_host_probe_primary"

      $preOk = (-not [string]::IsNullOrWhiteSpace($preGwHex)) -and ($pingPre -eq "OK")
      $faultOk = $hasRouteFault -and $hasIpFault -and ($gatewayFault -in @("LOSS","UNREACHABLE")) -and ($pingFault -in @("LOSS","UNREACHABLE"))
      $realRouteRestored = (-not [string]::IsNullOrWhiteSpace($preGwHex)) -and ($postGwHexes -contains $preGwHex)
      $proofRecovered = ($proofPost2 -eq "OK") -or ($proofPost -eq "OK")
      $gatewayRecovered = ($gatewayPost2 -eq "OK") -or ($gatewayPost -eq "OK")
      $dnsRecovered = ($dnsPost2 -eq "OK") -or ($dnsPost -eq "OK")
      $postOk = $realRouteRestored -and $proofRecovered -and $gatewayRecovered -and $dnsRecovered

      if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
      if ([string]::IsNullOrWhiteSpace($preGwHex)) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_DEFAULT_GATEWAY_NOT_CAPTURED"
      }
      if ($gatewayFault -eq "UNKNOWN") {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_GATEWAY_PING_PROBE"
        Add-Item ([ref]$resultObj.NEXT_FIX) "probe_fault must include '### ping_gateway' for net_gateway_unreachable"
      } elseif ($gatewayFault -notin @("LOSS","UNREACHABLE")) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "GATEWAY_REACHABLE_DURING_FAULT"
        Add-Item ([ref]$resultObj.NEXT_FIX) "fault evidence must show the default gateway probe failed while route and IPv4 remain present"
      }
      if ($pingFault -notin @("LOSS","UNREACHABLE")) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "MAIN_IP_NOT_DEGRADED_BY_GATEWAY"
        Add-Item ([ref]$resultObj.NEXT_FIX) "main public IP probe should fail downstream of gateway unreachability"
      }
      if (-not $faultOk) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_GATEWAY_UNREACHABLE_EVIDENCE"
        Add-Item ([ref]$resultObj.NEXT_FIX) "fault must show: default route present, wlan0 has IPv4, gateway ping fails, and public IP probe fails"
      }
      if (-not $realRouteRestored) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "RECOVERY_ROUTE_NOT_PRE_GATEWAY"
        Add-Item ([ref]$resultObj.NEXT_FIX) "post/post2 route evidence must show the original default gateway restored"
      }
      if ($realRouteRestored -and (-not ($proofRecovered -and $gatewayRecovered -and $dnsRecovered))) {
        Add-Item ([ref]$resultObj.FAILED_CRITERIA) "RECOVERY_CONNECTIVITY_NOT_CONFIRMED"
        Add-Item ([ref]$resultObj.NEXT_FIX) "post/post2 probes must confirm gateway, active proof IP, and DNS recovery"
      }
      if (-not $postOk) { Add-Item ([ref]$resultObj.WARNINGS) "NO_STRICT_RECOVERY_EVIDENCE(post)" }
      if (-not $netPost2 -or -not $probePost2) { Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT" }
    }
  }

  "net_public_ip_unreachable" {
    $targetFault  = Get-SectionPingOutcome $probeFault "ping_target_ip"
    $gatewayFault = Get-SectionPingOutcome $probeFault "ping_gateway"
    $targetPost   = Get-SectionPingOutcome ($probePost2 + $probePost) "ping_target_ip"
    $hasTargetProbe = ($probeFault -match '(?m)^###\s+ping_target_ip\s*$')
    $hasIpFault    = Has-IfaceIp ($netFault + "`n" + $probeFault) "wlan0"
    $wpaStateFault = Get-WpaState $netFault
    $hasMarker     = ($probeFault -match 'iptables_target_block\.applied' -and $probeFault -notmatch 'NO_MARKER')
    $markerText    = Get-SectionText $probeFault "target_block_marker"
    $targetIp = ""
    foreach ($candidate in @(
      $(if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "target_ip")) { $meta.target_ip } else { "" }),
      $(if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "net_target_ip")) { $meta.net_target_ip } else { "" }),
      $(if ($null -ne $netOutcome -and ($netOutcome.PSObject.Properties.Name -contains "target_ip")) { $netOutcome.target_ip } else { "" }),
      $(if ($null -ne $netOutcome -and ($netOutcome.PSObject.Properties.Name -contains "net_target_ip")) { $netOutcome.net_target_ip } else { "" })
    )) {
      if (-not [string]::IsNullOrWhiteSpace([string]$candidate)) {
        $targetIp = [string]$candidate
        break
      }
    }
    $activePingIp = ""
    foreach ($candidate in @(
      $(if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "active_ping_ip")) { $meta.active_ping_ip } else { "" }),
      $(if ($null -ne $netOutcome -and ($netOutcome.PSObject.Properties.Name -contains "active_ping_ip")) { $netOutcome.active_ping_ip } else { "" })
    )) {
      if (-not [string]::IsNullOrWhiteSpace([string]$candidate)) {
        $activePingIp = [string]$candidate
        break
      }
    }
    $dnsProofIp = ""
    foreach ($candidate in @(
      $(if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "dns_proof_base_ip")) { $meta.dns_proof_base_ip } else { "" }),
      $(if ($null -ne $netOutcome -and ($netOutcome.PSObject.Properties.Name -contains "dns_proof_base_ip")) { $netOutcome.dns_proof_base_ip } else { "" })
    )) {
      if (-not [string]::IsNullOrWhiteSpace([string]$candidate)) {
        $dnsProofIp = [string]$candidate
        break
      }
    }
    $activeDnsHost = ""
    foreach ($candidate in @(
      $(if ($null -ne $meta -and ($meta.PSObject.Properties.Name -contains "active_dns_host")) { $meta.active_dns_host } else { "" }),
      $(if ($null -ne $netOutcome -and ($netOutcome.PSObject.Properties.Name -contains "active_dns_host")) { $netOutcome.active_dns_host } else { "" })
    )) {
      if (-not [string]::IsNullOrWhiteSpace([string]$candidate)) {
        $activeDnsHost = [string]$candidate
        break
      }
    }

    $preOk   = ($pingPre -eq "OK")
    $outcomeRecoveryObserved = $false
    $outcomeRecoveryReason = ""
    if ($null -ne $netOutcome) {
      $outcomeNames = @($netOutcome.PSObject.Properties.Name)
      if ($outcomeNames -contains "recovery_observed") { $outcomeRecoveryObserved = [bool]$netOutcome.recovery_observed }
      if ($outcomeNames -contains "recovery_observation_reason") { $outcomeRecoveryReason = [string]$netOutcome.recovery_observation_reason }
    }
    $postOk  = ($targetPost -eq "OK") -or ($outcomeRecoveryObserved -and $outcomeRecoveryReason -eq "target_ip_probe_recovered")

    if (-not $preOk) { Add-Item ([ref]$resultObj.FAILED_CRITERIA) "PRE_NOT_HEALTHY" }
    $knownDnsResolverTargets = @("8.8.8.8","8.8.4.4","1.1.1.1","1.0.0.1","9.9.9.9","223.5.5.5","223.6.6.6","114.114.114.114","119.29.29.29")
    if ([string]::IsNullOrWhiteSpace($targetIp)) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "TARGET_IP_METADATA_MISSING"
      Add-Item ([ref]$resultObj.NEXT_FIX) "persist NET_TARGET_IP as target_ip/net_target_ip in _run_meta.json and _net_outcome.json"
    } elseif ($knownDnsResolverTargets -contains $targetIp) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "TARGET_IP_IS_KNOWN_DNS_RESOLVER"
      Add-Item ([ref]$resultObj.NEXT_FIX) "NET_TARGET_IP must be a reachable non-DNS public IP, not a public resolver"
    } elseif ($targetIp -eq $activePingIp -or $targetIp -eq $dnsProofIp -or $targetIp -eq $activeDnsHost) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "TARGET_IP_OVERLAPS_PROOF_TARGET"
      Add-Item ([ref]$resultObj.NEXT_FIX) "NET_TARGET_IP must differ from active proof IP, DNS proof IP, and active DNS host"
    }
    if (-not $hasTargetProbe) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_TARGET_PING_PROBE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "probe_fault must include '### ping_target_ip' section; check NET_TARGET_IP and Collect-NetSnapshotProbes"
    } elseif ($targetFault -eq "UNKNOWN") {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "TARGET_PING_RESULT_UNKNOWN"
      Add-Item ([ref]$resultObj.NEXT_FIX) "ping_target_ip section exists but parser did not recognize the result; update Get-PingOutcome"
    } elseif ($targetFault -notin @("LOSS","UNREACHABLE")) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "TARGET_NOT_BLOCKED"
      Add-Item ([ref]$resultObj.NEXT_FIX) "target IP must be unreachable during fault; check NET_TARGET_IP matches iptables DROP rule on device"
    }
    if ($pingFault -ne "OK") {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "MAIN_IP_DEGRADED"
      Add-Item ([ref]$resultObj.NEXT_FIX) "configured main IP probe must stay reachable; fault must not degrade to link-down/gateway-unreachable"
    }
    if ($gatewayFault -eq "UNKNOWN") {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_GATEWAY_PING_PROBE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "probe_fault must include '### ping_gateway' section; check Collect-NetSnapshotProbes for net_public_ip_unreachable"
    } elseif ($gatewayFault -notin @("OK")) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "GATEWAY_DEGRADED"
      Add-Item ([ref]$resultObj.NEXT_FIX) "gateway must remain reachable during fault; if gateway ping fails, this is net_gateway_unreachable not net_public_ip_unreachable"
    }
    if (Has-DnsFailure $probeFault) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "DNS_DEGRADED"
      Add-Item ([ref]$resultObj.NEXT_FIX) "DNS must remain functional; NET_TARGET_IP must not overlap with a DNS server IP"
    }
    if (-not $hasMarker) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_IPTABLES_TARGET_MARKER"
      Add-Item ([ref]$resultObj.NEXT_FIX) "probe_fault must show iptables_target_block.applied while the target is blocked"
    } elseif (-not [string]::IsNullOrWhiteSpace($targetIp) -and $markerText -notmatch "(?m)^\s*$([regex]::Escape($targetIp))\s*$") {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "IPTABLES_TARGET_MARKER_MISMATCH"
      Add-Item ([ref]$resultObj.NEXT_FIX) "target_block_marker section must include the blocked target IP from iptables_target_block.applied"
    }
    if (-not $hasIpFault)    { Add-Item ([ref]$resultObj.WARNINGS) "WLAN0_HAS_NO_IP_DURING_FAULT" }
    if ($wpaStateFault -and $wpaStateFault -ne "COMPLETED") { Add-Item ([ref]$resultObj.WARNINGS) "WPA_NOT_COMPLETED_DURING_FAULT" }
    if (-not $postOk) {
      Add-Item ([ref]$resultObj.FAILED_CRITERIA) "NO_TARGET_RECOVERY_EVIDENCE"
      Add-Item ([ref]$resultObj.NEXT_FIX) "post/post2 must show ping_target_ip recovered, or _net_outcome recovery_observation_reason=target_ip_probe_recovered"
    }
    if (-not $netPost2 -or -not $probePost2) { Add-Item ([ref]$resultObj.WARNINGS) "MISSING_POST2_SNAPSHOT" }
  }

  default {
    Add-Item ([ref]$resultObj.FAILED_CRITERIA) "UNKNOWN_NET_SCENARIO"
    Add-Item ([ref]$resultObj.NEXT_FIX) "ensure scenario_tag begins with bg_net/net_dns_fail/net_link_down/net_link_flap/net_wifi_disconnect/net_wifi_auth_fail_wrong_psk/net_no_default_route/net_no_ipv4_on_iface/net_wrong_default_route/net_gateway_unreachable/net_public_ip_unreachable"
  }
}

# finalize
if ($resultObj.FAILED_CRITERIA.Count -gt 0) {
  $resultObj.RESULT = "FAIL"
} else {
  $resultObj.RESULT = "PASS"
}

if ($AsJson) {
  $resultObj | ConvertTo-Json -Compress
  exit 0
}

if (-not $Quiet) {
  Write-Host ("RESULT: {0}" -f $resultObj.RESULT)
  Write-Host ("RUN_ID: {0}" -f $resultObj.RUN_ID)
  Write-Host ("SCENARIO: {0}" -f $resultObj.SCENARIO)
  Write-Host "FAILED_CRITERIA:"
  if ($resultObj.FAILED_CRITERIA.Count -eq 0) { Write-Host "(none)" } else { $resultObj.FAILED_CRITERIA | ForEach-Object { Write-Host ("- {0}" -f $_) } }
  Write-Host "WARNINGS:"
  if ($resultObj.WARNINGS.Count -eq 0) { Write-Host "(none)" } else { $resultObj.WARNINGS | ForEach-Object { Write-Host ("- {0}" -f $_) } }
  Write-Host "NEXT_FIX:"
  if ($resultObj.NEXT_FIX.Count -eq 0) { Write-Host "(none)" } else { $resultObj.NEXT_FIX | ForEach-Object { Write-Host ("- {0}" -f $_) } }
}

# return object to pipeline (方便 weekend 捕获并写 CSV)
$resultObj
