# requires -Version 5.1
param(
  [Parameter(Mandatory=$true)]
  [string]$InputPath,
  [string]$OutputRoot = "",
  [switch]$KeepExpanded
)

$ErrorActionPreference = 'Stop'

function Ensure-Dir {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return }
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
  }
}

function Test-PathSafe {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  try { return (Test-Path -LiteralPath $Path) } catch { return $false }
}

function Write-JsonUtf8 {
  param(
    [Parameter(Mandatory=$true)]$Object,
    [Parameter(Mandatory=$true)][string]$Path,
    [int]$Depth = 12
  )
  $dir = Split-Path -Parent $Path
  Ensure-Dir $dir
  $json = ConvertTo-Json -InputObject $Object -Depth $Depth
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

function Read-JsonSafe {
  param([string]$Path)
  if (-not (Test-PathSafe $Path)) { return $null }
  try {
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
  } catch {
    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
  }
}

function Read-TextSafe {
  param([string]$Path)
  if (-not (Test-PathSafe $Path)) { return "" }
  try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8) } catch {}
  try { return (Get-Content -LiteralPath $Path -Raw) } catch {}
  return ""
}

function Get-RelativePathSimple {
  param([string]$BasePath,[string]$TargetPath)
  if ([string]::IsNullOrWhiteSpace($TargetPath)) { return $null }
  if ([string]::IsNullOrWhiteSpace($BasePath)) { return $TargetPath }

  $base = [System.IO.Path]::GetFullPath($BasePath)
  $target = [System.IO.Path]::GetFullPath($TargetPath)
  if ($target.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
    $rel = $target.Substring($base.Length)
    return ($rel -replace '^[\\/]+', '')
  }
  return $TargetPath
}

function Get-Sha1Hex {
  param([string]$Text)
  if ($null -eq $Text) { return $null }
  $sha1 = [System.Security.Cryptography.SHA1]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Text)
    $hash = $sha1.ComputeHash($bytes)
    return ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha1.Dispose()
  }
}

function ConvertTo-StableStringArray {
  param($Value)

  $blockedNames = @('Count','Keys','Values','IsReadOnly','IsFixedSize','SyncRoot','IsSynchronized')
  $bag = New-Object System.Collections.ArrayList

  if ($null -eq $Value) { return @() }

  if ($Value -is [string]) {
    $text = $Value.Trim()
    if ($text) { [void]$bag.Add($text) }
    return @($bag)
  }

  if ($Value -is [System.Collections.IDictionary]) {
    foreach ($entry in $Value.GetEnumerator()) {
      $keyText = [string]$entry.Key
      $valText = [string]$entry.Value
      if ($keyText -and ($blockedNames -notcontains $keyText) -and $valText) {
        [void]$bag.Add($keyText.Trim())
      }
    }
    return @($bag | Where-Object { $_ } | Sort-Object -Unique)
  }

  if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
    foreach ($item in $Value) {
      if ($item -is [string]) {
        $text = $item.Trim()
        if ($text) { [void]$bag.Add($text) }
        continue
      }
      if ($item -is [System.Collections.IDictionary]) { continue }
      if ($item -and $item.PSObject -and $item.PSObject.Properties.Count -gt 0) { continue }
      if ($item) {
        $text = [string]$item
        if ($text) { [void]$bag.Add($text.Trim()) }
      }
    }
    return @($bag | Where-Object { $_ } | Sort-Object -Unique)
  }

  return @()
}

function ConvertTo-StableScoreMap {
  param($Value)

  $ret = [ordered]@{}
  if ($null -eq $Value) { return [pscustomobject]$ret }

  if ($Value -is [System.Collections.IDictionary]) {
    foreach ($entry in $Value.GetEnumerator() | Sort-Object Key) {
      $num = 0.0
      if ([double]::TryParse([string]$entry.Value, [ref]$num)) {
        $ret[[string]$entry.Key] = [math]::Round($num, 3)
      }
    }
    return [pscustomobject]$ret
  }

  foreach ($prop in $Value.PSObject.Properties) {
    if (-not $prop.Name) { continue }
    $num = 0.0
    if ([double]::TryParse([string]$prop.Value, [ref]$num)) {
      $ret[[string]$prop.Name] = [math]::Round($num, 3)
    }
  }
  return [pscustomobject]$ret
}

function New-Span {
  param(
    [Nullable[Int64]]$StartMs,
    [Nullable[Int64]]$EndMs
  )

  if ($null -eq $StartMs -and $null -eq $EndMs) { return $null }
  return [pscustomobject]@{
    start_ms = $StartMs
    end_ms = $EndMs
  }
}

function Normalize-ProcessNameHint {
  param([string]$Text)

  if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
  $normalized = ([string]$Text).ToLowerInvariant() -replace '[^a-z0-9]+', '_'
  $normalized = $normalized.Trim('_')
  if (-not $normalized) { return $null }
  return $normalized
}

function Resolve-RunRoot {
  param([string]$InputPath,[bool]$KeepExpanded)

  $ret = [ordered]@{
    run_root = $null
    cleanup_dir = $null
  }

  if (-not (Test-Path -LiteralPath $InputPath)) {
    throw "InputPath not found: $InputPath"
  }

  $item = Get-Item -LiteralPath $InputPath
  if ($item.PSIsContainer) {
    $ret.run_root = $item.FullName
    return [pscustomobject]$ret
  }

  if ($item.Extension.ToLowerInvariant() -ne '.zip') {
    throw "InputPath must be a run directory or .zip archive."
  }

  $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("wk_case_" + [guid]::NewGuid().ToString('N'))
  Ensure-Dir $tempRoot
  Expand-Archive -LiteralPath $item.FullName -DestinationPath $tempRoot -Force

  $dirs = @(Get-ChildItem -LiteralPath $tempRoot -Directory)
  if ($dirs.Count -eq 1) {
    $ret.run_root = $dirs[0].FullName
  } else {
    $ret.run_root = $tempRoot
  }
  if (-not $KeepExpanded) {
    $ret.cleanup_dir = $tempRoot
  }
  return [pscustomobject]$ret
}

function Find-FirstFile {
  param([string]$Root,[string[]]$Patterns)
  foreach ($pat in $Patterns) {
    $hit = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $pat -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { return $hit.FullName }
  }
  return $null
}

function Find-AllFiles {
  param([string]$Root,[string[]]$Patterns)
  $all = @()
  foreach ($pat in $Patterns) {
    $all += @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $pat -ErrorAction SilentlyContinue)
  }
  return @($all | Sort-Object FullName -Unique)
}

function Parse-FaultInjectSummary {
  param([string]$LogPath)

  $ret = [ordered]@{
    exists = $false
    kind = $null
    process_name_hint = $null
    process_name_hint_norm = $null
    baseline_sec = $null
    max_leak_mib = $null
    allocated_milestones_mib = @()
    reached_configured_limit = $false
    keepalive_pause = $false
    first_wall = $null
    last_wall = $null
  }

  if (-not (Test-PathSafe $LogPath)) { return [pscustomobject]$ret }
  $ret.exists = $true
  $ret.kind = [System.IO.Path]::GetFileNameWithoutExtension($LogPath)

  $milestones = New-Object System.Collections.ArrayList
  foreach ($line in (Get-Content -LiteralPath $LogPath -ErrorAction SilentlyContinue)) {
    if ($line -match '\[wall:([^\]]+)\]') {
      if (-not $ret.first_wall) { $ret.first_wall = $matches[1] }
      $ret.last_wall = $matches[1]
    }
    if (-not $ret.process_name_hint -and $line -match '\]\s+(.+?)\s+initialized\b') {
      $ret.process_name_hint = $matches[1].Trim()
      $ret.process_name_hint_norm = Normalize-ProcessNameHint -Text $ret.process_name_hint
    }
    if ($line -match 'Baseline window\s+(\d+)\s+second') {
      $ret.baseline_sec = [int]$matches[1]
    }
    if ($line -match 'max_leak=(\d+)\s+MiB') {
      $ret.max_leak_mib = [int]$matches[1]
    }
    if ($line -match 'Allocated\s+(\d+)\s+MiB so far') {
      [void]$milestones.Add([int]$matches[1])
    }
    if ($line -match 'Reached configured leak limit') {
      $ret.reached_configured_limit = $true
    }
    if ($line -match 'Entering pause\(\)') {
      $ret.keepalive_pause = $true
    }
  }
  $ret.allocated_milestones_mib = @($milestones)
  return [pscustomobject]$ret
}

function Read-EventsWindowDetails {
  param([string]$EventsPath,[Int64]$StartMs,[Int64]$EndMs)

  $ret = [ordered]@{
    exists = $false
    count_total = 0
    count_in_window = 0
    counts_by_tag = [ordered]@{}
    ordered_events = @()
  }
  if (-not (Test-PathSafe $EventsPath)) { return [pscustomobject]$ret }
  $ret.exists = $true

  $events = New-Object System.Collections.ArrayList
  $counts = @{}
  foreach ($line in (Get-Content -LiteralPath $EventsPath -ErrorAction SilentlyContinue)) {
    if (-not $line) { continue }
    $ret.count_total++
    try { $obj = $line | ConvertFrom-Json -ErrorAction Stop } catch { continue }
    $ts = 0L
    if (-not [Int64]::TryParse([string]$obj.ts, [ref]$ts)) { continue }
    if ($StartMs -gt 0 -and $EndMs -gt 0) {
      if ($ts -lt $StartMs -or $ts -gt $EndMs) { continue }
    }
    $ret.count_in_window++
    $tag = if ($obj.tag) { [string]$obj.tag } else { 'unknown' }
    if (-not $counts.ContainsKey($tag)) { $counts[$tag] = 0 }
    $counts[$tag] = [int]$counts[$tag] + 1

    [void]$events.Add([pscustomobject]@{
      ts = $ts
      tag = $tag
      level = [string]$obj.level
      component = [string]$obj.component
      msg = [string]$obj.msg
      source = [string]$obj.source
    })
  }

  $ordered = @($events | Sort-Object ts)
  $ret.ordered_events = $ordered
  $ret.counts_by_tag = [ordered]@{}
  foreach ($k in ($counts.Keys | Sort-Object)) {
    $ret.counts_by_tag[$k] = [int]$counts[$k]
  }
  return [pscustomobject]$ret
}

function Read-ProcessSuspects {
  param([string]$ProcsDir)

  $ret = [ordered]@{
    exists = $false
    snapshot_count = 0
    suspects = @()
  }
  if (-not (Test-PathSafe $ProcsDir)) { return [pscustomobject]$ret }

  $files = @(Get-ChildItem -LiteralPath $ProcsDir -File -Filter 'procs_*.txt' -ErrorAction SilentlyContinue | Sort-Object Name)
  if ($files.Count -le 0) { return [pscustomobject]$ret }
  $ret.exists = $true
  $ret.snapshot_count = $files.Count

  $map = @{}
  foreach ($f in $files) {
    $reason = 'unknown'
    $snapshotKey = $f.Name
    foreach ($line in (Get-Content -LiteralPath $f.FullName -ErrorAction SilentlyContinue)) {
      if ($line -match '^###\s+ps snapshot at\s+(\d+)\s+ms,\s+reason=(.+)$') {
        $reason = $matches[2].Trim()
        continue
      }
      if ($line -match '^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(.+?)\s*$') {
        $procPid = [int]$matches[1]
        $rss = [int]$matches[4]
        $comm = $matches[5].Trim()
        if (-not $comm) { continue }
        if (-not $map.ContainsKey($comm)) {
          $map[$comm] = [ordered]@{
            comm = $comm
            max_rss_kb = 0
            min_rss_kb = [int]::MaxValue
            seen_in_snapshots = 0
            occurrence_count = 0
            reasons = New-Object System.Collections.ArrayList
            pids = New-Object System.Collections.ArrayList
            snapshot_keys = New-Object System.Collections.ArrayList
            mem_pressure_snapshots = New-Object System.Collections.ArrayList
            cpu_hotspot_snapshots = New-Object System.Collections.ArrayList
            periodic_snapshots = New-Object System.Collections.ArrayList
          }
        }
        $entry = $map[$comm]
        if ($rss -gt [int]$entry.max_rss_kb) { $entry.max_rss_kb = $rss }
        if ($rss -lt [int]$entry.min_rss_kb) { $entry.min_rss_kb = $rss }
        $entry.occurrence_count = [int]$entry.occurrence_count + 1
        if (-not ($entry.snapshot_keys -contains $snapshotKey)) {
          [void]$entry.snapshot_keys.Add($snapshotKey)
          $entry.seen_in_snapshots = [int]$entry.seen_in_snapshots + 1
        }
        if (-not ($entry.reasons -contains $reason)) { [void]$entry.reasons.Add($reason) }
        if (-not ($entry.pids -contains $procPid)) { [void]$entry.pids.Add($procPid) }
        if ($reason -eq 'cpu_hotspot' -and -not ($entry.cpu_hotspot_snapshots -contains $snapshotKey)) {
          [void]$entry.cpu_hotspot_snapshots.Add($snapshotKey)
        }
        if ($reason -eq 'mem_pressure' -and -not ($entry.mem_pressure_snapshots -contains $snapshotKey)) {
          [void]$entry.mem_pressure_snapshots.Add($snapshotKey)
        }
        if ($reason -eq 'periodic' -and -not ($entry.periodic_snapshots -contains $snapshotKey)) {
          [void]$entry.periodic_snapshots.Add($snapshotKey)
        }
      }
    }
  }

  $ret.suspects = @(
    $map.GetEnumerator() |
      ForEach-Object {
        $minRss = if ([int]$_.Value.min_rss_kb -eq [int]::MaxValue) { [int]$_.Value.max_rss_kb } else { [int]$_.Value.min_rss_kb }
        [pscustomobject]@{
          comm = $_.Value.comm
          max_rss_kb = [int]$_.Value.max_rss_kb
          min_rss_kb = [int]$minRss
          rss_growth_kb = [int]$_.Value.max_rss_kb - [int]$minRss
          seen_in_snapshots = [int]$_.Value.seen_in_snapshots
          occurrence_count = [int]$_.Value.occurrence_count
          mem_pressure_snapshot_hits = @($_.Value.mem_pressure_snapshots).Count
          cpu_hotspot_snapshot_hits = @($_.Value.cpu_hotspot_snapshots).Count
          periodic_snapshot_hits = @($_.Value.periodic_snapshots).Count
          reasons = @($_.Value.reasons)
          pids = @($_.Value.pids)
        }
      } |
      Sort-Object @{Expression='seen_in_snapshots';Descending=$true}, @{Expression='max_rss_kb';Descending=$true}
  )
  return [pscustomobject]$ret
}

function Parse-NetSnapshotFile {
  param([string]$Path)

  $ret = [ordered]@{
    exists = $false
    phase = $null
    iface_states = [ordered]@{}
    wlan_status = $null
  }
  if (-not (Test-PathSafe $Path)) { return [pscustomobject]$ret }

  $ret.exists = $true
  $lines = @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)
  $inLinkState = $false
  $inWpaStatus = $false
  $currentIface = $null
  $ifaceStep = 0

  foreach ($line in $lines) {
    if (-not $ret.phase -and $line -match '^###\s+phase=(.+)$') {
      $ret.phase = $matches[1].Trim()
      continue
    }

    if ($line -match '^###\s+link_state_sysfs$') {
      $inLinkState = $true
      $inWpaStatus = $false
      $currentIface = $null
      $ifaceStep = 0
      continue
    }
    if ($line -match '^###\s+wpa_cli status') {
      $inWpaStatus = $true
      $inLinkState = $false
      continue
    }
    if ($line -eq '__END__') {
      $inLinkState = $false
      $inWpaStatus = $false
      $currentIface = $null
      $ifaceStep = 0
      continue
    }
    if ($line -match '^###\s+') {
      $inLinkState = $false
      $inWpaStatus = $false
    }

    if ($inLinkState) {
      if ($line -match '^--\s+(\S+)$') {
        $currentIface = $matches[1]
        $ret.iface_states[$currentIface] = [ordered]@{
          operstate = $null
          carrier = $null
        }
        $ifaceStep = 1
        continue
      }
      if ($currentIface -and $ifaceStep -eq 1 -and $line.Trim()) {
        $ret.iface_states[$currentIface].operstate = $line.Trim()
        $ifaceStep = 2
        continue
      }
      if ($currentIface -and $ifaceStep -eq 2 -and $line.Trim()) {
        $ret.iface_states[$currentIface].carrier = $line.Trim()
        $ifaceStep = 0
        $currentIface = $null
        continue
      }
    }

    if ($inWpaStatus -and $line -match 'wpa_state=(.+)$') {
      $ret.wlan_status = $matches[1].Trim()
    }
  }

  if (-not $ret.phase) {
    $ret.phase = [System.IO.Path]::GetFileNameWithoutExtension($Path).Replace('net_', '')
  }
  return [pscustomobject]$ret
}

function Parse-NetProbeFile {
  param([string]$Path)

  $ret = [ordered]@{
    exists = $false
    phase = $null
    ping_ip_ok = $false
    ping_ip_fail = $false
    dns_resolution_ok = $false
    dns_resolution_fail = $false
    target_ping_ok = $false
    target_ping_fail = $false
    gateway_ping_ok = $false
    gateway_ping_fail = $false
  }
  if (-not (Test-PathSafe $Path)) { return [pscustomobject]$ret }

  $ret.exists = $true
  $text = Read-TextSafe $Path
  if ($text -match '(?m)^###\s+phase=(.+)$') {
    $ret.phase = $matches[1].Trim()
  } else {
    $ret.phase = [System.IO.Path]::GetFileNameWithoutExtension($Path).Replace('probe_', '')
  }

  $mainPingIp = if ($env:WK_NET_PING_IP -and $env:WK_NET_PING_IP -ne "") {
    $env:WK_NET_PING_IP
  } elseif ($env:NET_PING_IP -and $env:NET_PING_IP -ne "") {
    $env:NET_PING_IP
  } else {
    "1.1.1.1"
  }
  $mainPingBlock = [regex]::Match($text, ("(?ms)^###\s+ping -c 3 {0}\s*\r?\n(.*?)(?=^###\s|\z)" -f [regex]::Escape($mainPingIp))).Value
  if (-not $mainPingBlock) {
    $mainPingBlock = [regex]::Match($text, '(?ms)^###\s+ping -c 3 \d{1,3}(?:\.\d{1,3}){3}\s*\r?\n(.*?)(?=^###\s|\z)').Value
  }

  if ($mainPingBlock -match '(?m)([1-9][0-9]*\s+(?:packets?\s+)?received|(?<![0-9])0% packet loss|bytes from)') {
    $ret.ping_ip_ok = $true
  }
  if ($mainPingBlock -match '(?i)Network (is )?unreachable|Destination Host Unreachable|connect:\s*Network is unreachable|sendto:\s*Network is unreachable|Operation not permitted|(?m)\b100% packet loss\b|(?m)\b0\s+(?:packets?\s+)?received\b') {
    $ret.ping_ip_fail = $true
  }
  $_dnsFail = '(?i)Name does not resolve|Temporary failure|Try again'
  if (($text -match '### dns_host_probe_primary' -and $text -match $_dnsFail) -or
      ($text -match '### ping_nip_io' -and $text -match $_dnsFail)) {
    $ret.dns_resolution_fail = $true
  }
  if (-not $ret.dns_resolution_fail) {
    if (($text -match '### dns_host_probe_primary' -and ($text -match '(?m)1 received' -or $text -match '(?m)(?<!\d)0% packet loss')) -or
        ($text -match '### ping_nip_io' -and ($text -match '(?m)1 received' -or $text -match '(?m)(?<!\d)0% packet loss'))) {
      $ret.dns_resolution_ok = $true
    }
  }

  if ($text -match '### ping_target_ip' -and ($text -match '(?m)(?<!\d)0% packet loss' -or $text -match '(?m)3 received' -or $text -match '(?m)1 received')) { $ret.target_ping_ok = $true }
  if ($text -match '### ping_target_ip' -and ($text -match '(?m)100% packet loss' -or $text -match '(?m)0 received' -or $text -match 'Network unreachable' -or $text -match 'Operation not permitted')) { $ret.target_ping_fail = $true }

  if ($text -match '### ping_gateway' -and ($text -match '(?m)(?<!\d)0% packet loss' -or $text -match '(?m)1 received')) { $ret.gateway_ping_ok = $true }
  if ($text -match '### ping_gateway' -and ($text -match '(?m)100% packet loss' -or $text -match '(?m)0 received' -or $text -match 'Network unreachable' -or $text -match 'NO_GATEWAY')) { $ret.gateway_ping_fail = $true }

  return [pscustomobject]$ret
}

function Read-NetSummary {
  param([string]$NetDir)

  $ret = [ordered]@{
    exists = $false
    snapshots = [ordered]@{}
    probes = [ordered]@{}
    dns_resolution_fail = $false
    dns_resolution_ok = $false
    ping_ip_ok = $false
    ping_ip_fail = $false
    iface_down = $false
    iface_recovered = $false
    link_flap_observed = $false
    wlan_disconnected = $false
    wlan_auth_fail = $false
    no_default_route = $false
    no_ipv4_on_iface = $false
    wrong_default_route = $false
    target_ping_fail = $false
    target_ping_ok = $false
    gateway_ping_ok = $false
    gateway_ping_fail = $false
    affected_iface = $null
    observed_phases = @()
  }
  if (-not (Test-PathSafe $NetDir)) { return [pscustomobject]$ret }

  $ret.exists = $true
  $phaseNames = New-Object System.Collections.ArrayList

  foreach ($file in @(Get-ChildItem -LiteralPath $NetDir -File -Filter 'net_*.txt' -ErrorAction SilentlyContinue | Sort-Object Name)) {
    $snapshot = Parse-NetSnapshotFile -Path $file.FullName
    if ($snapshot.phase) {
      $ret.snapshots[$snapshot.phase] = $snapshot
      if (-not ($phaseNames -contains $snapshot.phase)) { [void]$phaseNames.Add($snapshot.phase) }
    }
  }
  foreach ($file in @(Get-ChildItem -LiteralPath $NetDir -File -Filter 'probe_*.txt' -ErrorAction SilentlyContinue | Sort-Object Name)) {
    $probe = Parse-NetProbeFile -Path $file.FullName
    if ($probe.phase) {
      $ret.probes[$probe.phase] = $probe
      if (-not ($phaseNames -contains $probe.phase)) { [void]$phaseNames.Add($probe.phase) }
    }
    if ($probe.dns_resolution_fail) { $ret.dns_resolution_fail = $true }
    if ($probe.dns_resolution_ok) { $ret.dns_resolution_ok = $true }
    if ($probe.ping_ip_ok) { $ret.ping_ip_ok = $true }
    if ($probe.ping_ip_fail) { $ret.ping_ip_fail = $true }
    if ($probe.target_ping_fail) { $ret.target_ping_fail = $true }
    if ($probe.target_ping_ok) { $ret.target_ping_ok = $true }
    if ($probe.gateway_ping_ok) { $ret.gateway_ping_ok = $true }
    if ($probe.gateway_ping_fail) { $ret.gateway_ping_fail = $true }
  }

  $ret.observed_phases = @($phaseNames)

  $preSnapshot = $null
  if ($ret.snapshots.Contains('pre')) { $preSnapshot = $ret.snapshots['pre'] }
  $faultSnapshots = @()
  foreach ($phase in @('fault','fault2')) {
    if ($ret.snapshots.Contains($phase)) { $faultSnapshots += @($ret.snapshots[$phase]) }
  }
  $recoverySnapshots = @()
  foreach ($phase in @('post','post2')) {
    if ($ret.snapshots.Contains($phase)) { $recoverySnapshots += @($ret.snapshots[$phase]) }
  }

  if ($preSnapshot -and $preSnapshot.iface_states.Count -gt 0) {
    foreach ($ifaceName in $preSnapshot.iface_states.Keys) {
      $preState = $preSnapshot.iface_states[$ifaceName]
      $preUp = ($preState.operstate -eq 'up' -and $preState.carrier -eq '1')
      if (-not $preUp) { continue }

      $wasDown = $false
      foreach ($faultSnapshot in $faultSnapshots) {
        if (-not $faultSnapshot.iface_states.Contains($ifaceName)) { continue }
        $faultState = $faultSnapshot.iface_states[$ifaceName]
        $faultUp = ($faultState.operstate -eq 'up' -and $faultState.carrier -eq '1')
        if (-not $faultUp) {
          $ret.iface_down = $true
          $wasDown = $true
          if (-not $ret.affected_iface) { $ret.affected_iface = $ifaceName }
          break
        }
      }

      if ($wasDown) {
        foreach ($recoverySnapshot in $recoverySnapshots) {
          if (-not $recoverySnapshot.iface_states.Contains($ifaceName)) { continue }
          $recoveryState = $recoverySnapshot.iface_states[$ifaceName]
          $recoveryUp = ($recoveryState.operstate -eq 'up' -and $recoveryState.carrier -eq '1')
          if ($recoveryUp) {
            $ret.iface_recovered = $true
            if (-not $ret.affected_iface) { $ret.affected_iface = $ifaceName }
            break
          }
        }
      }
    }
  }

  if ($ret.iface_down -and $ret.iface_recovered -and ($ret.observed_phases -contains 'fault2' -or $ret.observed_phases -contains 'post2')) {
    $ret.link_flap_observed = $true
  }

  foreach ($phase in @('fault','fault2','post','post2')) {
    if ($ret.snapshots.Contains($phase) -and $ret.snapshots[$phase].wlan_status -match 'DISCONNECTED') {
      $ret.wlan_disconnected = $true
    }
    # auth fail: wpa_state in active-auth cycle (SCANNING/ASSOCIATING/4WAY_HANDSHAKE/etc.)
    # Narrowed from "not COMPLETED" to avoid overlap with net_wifi_disconnect (which shows DISCONNECTED)
    if ($ret.snapshots.Contains($phase) -and $ret.snapshots[$phase].wlan_status -and
        $ret.snapshots[$phase].wlan_status -match '^(SCANNING|ASSOCIATING|4WAY_HANDSHAKE|AUTHENTICATING|GROUP_HANDSHAKE)') {
      $ret.wlan_auth_fail = $true
    }
  }

  # no_default_route: wlan0 has IP in fault but no default route entry
  foreach ($phase in @('fault','fault2')) {
    if (-not $ret.snapshots.Contains($phase)) { continue }
    $snap = $ret.snapshots[$phase]
    $rawPath = $null
    foreach ($file in @(Get-ChildItem -LiteralPath $NetDir -File -Filter "net_${phase}.txt" -ErrorAction SilentlyContinue)) {
      $rawPath = $file.FullName; break
    }
    if ($rawPath) {
      $raw = Read-TextSafe $rawPath
      $hasIp = ($raw -match '(?ms)### ifconfig wlan0.*?inet addr:\s*\d+') -or
               ($raw -match '(?ms)### ifconfig -a.*?^wlan0\b.*?inet addr:\s*\d+')
      if (-not $hasIp) {
        $probePhasePath = Join-Path $NetDir "probe_${phase}.txt"
        $hasIp = (Read-TextSafe $probePhasePath) -match '(?ms)### ifconfig_wlan.*?inet addr:\s*\d+'
      }
      # Extract route section content; empty section means collection gap, not absence of routes
      $routeSecM = [regex]::Match($raw, '(?ms)^### /proc/net/route\s*\r?\n(.*?)(?=^###\s|\z)')
      $routeSecC = if ($routeSecM.Success) { $routeSecM.Groups[1].Value.Trim() } else { '' }
      $hasRoute = ($routeSecC -match '(?m)^wlan0\s+00000000\s')
      # Probe fallback: if net snapshot route section is empty, read from probe_fault ### proc_net_route
      if (-not $hasRoute -and $routeSecC -eq '') {
        $probePhasePath = Join-Path $NetDir "probe_${phase}.txt"
        $probeRouteM = [regex]::Match((Read-TextSafe $probePhasePath), '(?ms)### proc_net_route\s*\r?\n(.*?)(?=### |\z)')
        if ($probeRouteM.Success) {
          $routeSecC = $probeRouteM.Groups[1].Value.Trim()
          $hasRoute  = ($routeSecC -match '(?m)^wlan0\s+00000000\s')
        }
      }
      if ($hasIp -and $routeSecC -ne '' -and -not $hasRoute) { $ret.no_default_route = $true }
      if ($hasIp -and $hasRoute) {
        # Cond 2: fault gateway must be the known fake gateway or differ from pre-phase normal gateway hex
        $fltGwM = [regex]::Match($routeSecC, '(?m)^wlan0\s+00000000\s+([0-9A-Fa-f]{8})\b')
        $fltGwHex = if ($fltGwM.Success) { $fltGwM.Groups[1].Value.ToUpper() } else { '' }
        $preGwHex2 = ''
        $preRaw2 = Read-TextSafe (Join-Path $NetDir "net_pre.txt")
        $preRtM2 = [regex]::Match($preRaw2, '(?ms)^### /proc/net/route\s*\r?\n(.*?)(?=^###\s|\z)')
        if ($preRtM2.Success) {
          $preGwM2 = [regex]::Match($preRtM2.Groups[1].Value, '(?m)^wlan0\s+00000000\s+([0-9A-Fa-f]{8})\b')
          if ($preGwM2.Success) { $preGwHex2 = $preGwM2.Groups[1].Value.ToUpper() }
        }
        # Cond 4: subnet direct route (wlan0 <net> 00000000) must be present alongside the default
        $hasSubnetRoute = ($routeSecC -match '(?m)^wlan0\s+[0-9A-Fa-f]{8}\s+00000000\s')
        # Cond 5: fault-phase main IP ping must have failed; if probe absent, do not block (conservative)
        $fltPingFail = $true
        if ($ret.probes.Contains($phase) -and $ret.probes[$phase].exists) {
          $fltPingFail = $ret.probes[$phase].ping_ip_fail
        }
        $hasWrongGw = ($fltGwHex -eq 'FE0246AC') -or ($preGwHex2 -and $fltGwHex -and ($fltGwHex -ne $preGwHex2))
        if ($hasWrongGw -and $hasSubnetRoute -and $fltPingFail) {
          $ret.wrong_default_route = $true
        }
      }
      # no_ipv4_on_iface: ifconfig wlan0 section has no inet addr AND wpa_state=COMPLETED
      # (the wpa_state=COMPLETED requirement distinguishes from net_wifi_disconnect)
      $wlanBlock = [regex]::Match($raw, '(?ms)### ifconfig wlan0.*?(?=### |\z)')
      if (-not $wlanBlock.Success) {
        $ifcaM = [regex]::Match($raw, '(?ms)### ifconfig -a.*?(?=### |\z)')
        if ($ifcaM.Success) { $wlanBlock = [regex]::Match($ifcaM.Value, '(?ms)^wlan0\b.*?(?=\r?\n\S|\z)') }
      }
      if (-not $wlanBlock.Success) {
        $probePhasePath = Join-Path $NetDir "probe_${phase}.txt"
        $probeFaultRaw2 = Read-TextSafe $probePhasePath
        $wlanBlock = [regex]::Match($probeFaultRaw2, '(?ms)### ifconfig_wlan.*?(?=### |\z)')
      }
      $wpaMatch = [regex]::Match($raw, '(?ms)### wpa_cli status.*?wpa_state=(\S+)')
      $wpaStateInSnap = if ($wpaMatch.Success) { $wpaMatch.Groups[1].Value.Trim() } else { '' }
      if ($wlanBlock.Success -and $wlanBlock.Value -notmatch 'inet addr' -and $wpaStateInSnap -eq 'COMPLETED') {
        $ret.no_ipv4_on_iface = $true
      }
    }
  }

  return [pscustomobject]$ret
}

function Read-NetOutcomeSummary {
  param(
    $Meta,
    [string]$RunRoot
  )

  $ret = [ordered]@{
    exists = $false
    net_fault_type = $null
    iface_used = $null
    inject_ok = $false
    fault_observed = $false
    recovery_observed = $false
    fault_observation_reason = $null
    recovery_observation_reason = $null
    recovery_gate_ok = $null
    recovery_gate_reason = $null
    injector_stop_ok = $null
    probe_profile_id = $null
    active_ping_ip = $null
    active_dns_host = $null
    dns_proof_base_ip = $null
    baseline_policy = $null
    baseline_max_attempts = $null
    baseline_consecutive_required = $null
    baseline_sleep_sec = $null
    probe_profile_reason = $null
    ap_or_network_profile = $null
    profile_effective_from = $null
    profile_approved_by = $null
    profile_approval_time = $null
    diagnostic_probe_targets = $null
  }

  if ($Meta) {
    try { if ($Meta.net_fault_type) { $ret.net_fault_type = [string]$Meta.net_fault_type } } catch {}
    try { if ($Meta.iface_used) { $ret.iface_used = [string]$Meta.iface_used } } catch {}
    try { if ($null -ne $Meta.inject_ok) { $ret.inject_ok = [bool]$Meta.inject_ok } } catch {}
    try { if ($null -ne $Meta.fault_observed) { $ret.fault_observed = [bool]$Meta.fault_observed } } catch {}
    try { if ($null -ne $Meta.recovery_observed) { $ret.recovery_observed = [bool]$Meta.recovery_observed } } catch {}
    try { if ($Meta.fault_observation_reason) { $ret.fault_observation_reason = [string]$Meta.fault_observation_reason } } catch {}
    try { if ($Meta.recovery_observation_reason) { $ret.recovery_observation_reason = [string]$Meta.recovery_observation_reason } } catch {}
    try { if ($null -ne $Meta.recovery_gate_ok) { $ret.recovery_gate_ok = [bool]$Meta.recovery_gate_ok } } catch {}
    try { if ($Meta.recovery_gate_reason) { $ret.recovery_gate_reason = [string]$Meta.recovery_gate_reason } } catch {}
    try { if ($null -ne $Meta.injector_stop_ok) { $ret.injector_stop_ok = [bool]$Meta.injector_stop_ok } } catch {}
    foreach ($name in @(
      'probe_profile_id',
      'active_ping_ip',
      'active_dns_host',
      'dns_proof_base_ip',
      'baseline_policy',
      'baseline_max_attempts',
      'baseline_consecutive_required',
      'baseline_sleep_sec',
      'probe_profile_reason',
      'ap_or_network_profile',
      'profile_effective_from',
      'profile_approved_by',
      'profile_approval_time',
      'diagnostic_probe_targets'
    )) {
      try {
        if ($Meta.PSObject.Properties.Name -contains $name) {
          $value = $Meta.$name
          if ($null -ne $value -and [string]$value -ne '') { $ret[$name] = [string]$value }
        }
      } catch {}
    }
  }

  $path = Join-Path $RunRoot '_net_outcome.json'
  $obj = Read-JsonSafe $path
  if ($null -ne $obj) {
    $ret.exists = $true
    try { if ($obj.net_fault_type) { $ret.net_fault_type = [string]$obj.net_fault_type } } catch {}
    try { if ($obj.iface_used) { $ret.iface_used = [string]$obj.iface_used } } catch {}
    try { if ($null -ne $obj.inject_ok) { $ret.inject_ok = [bool]$obj.inject_ok } } catch {}
    try { if ($null -ne $obj.fault_observed) { $ret.fault_observed = [bool]$obj.fault_observed } } catch {}
    try { if ($null -ne $obj.recovery_observed) { $ret.recovery_observed = [bool]$obj.recovery_observed } } catch {}
    try { if ($obj.fault_observation_reason) { $ret.fault_observation_reason = [string]$obj.fault_observation_reason } } catch {}
    try { if ($obj.recovery_observation_reason) { $ret.recovery_observation_reason = [string]$obj.recovery_observation_reason } } catch {}
    try { if ($null -ne $obj.recovery_gate_ok) { $ret.recovery_gate_ok = [bool]$obj.recovery_gate_ok } } catch {}
    try { if ($obj.recovery_gate_reason) { $ret.recovery_gate_reason = [string]$obj.recovery_gate_reason } } catch {}
    try { if ($null -ne $obj.injector_stop_ok) { $ret.injector_stop_ok = [bool]$obj.injector_stop_ok } } catch {}
    foreach ($name in @(
      'probe_profile_id',
      'active_ping_ip',
      'active_dns_host',
      'dns_proof_base_ip',
      'baseline_policy',
      'baseline_max_attempts',
      'baseline_consecutive_required',
      'baseline_sleep_sec',
      'probe_profile_reason',
      'ap_or_network_profile',
      'profile_effective_from',
      'profile_approved_by',
      'profile_approval_time',
      'diagnostic_probe_targets'
    )) {
      try {
        if ($obj.PSObject.Properties.Name -contains $name) {
          $value = $obj.$name
          if ($null -ne $value -and [string]$value -ne '') { $ret[$name] = [string]$value }
        }
      } catch {}
    }
  }

  if (-not $ret.net_fault_type -and $Meta -and $Meta.fault_type) {
    $ret.net_fault_type = [string]$Meta.fault_type
  }
  return [pscustomobject]$ret
}

function Get-EventTagSummaries {
  param($EventsDetail)

  $ret = New-Object System.Collections.ArrayList
  if (-not $EventsDetail -or -not $EventsDetail.exists) { return @($ret) }

  $grouped = @($EventsDetail.ordered_events | Group-Object -Property tag)
  foreach ($group in ($grouped | Sort-Object Name)) {
    if (-not $group.Name) { continue }
    $events = @($group.Group | Sort-Object ts)
    if ($events.Count -le 0) { continue }
    $firstSource = $null
    foreach ($evt in $events) {
      if ($evt.source) {
        $firstSource = [string]$evt.source
        break
      }
    }
    [void]$ret.Add([pscustomobject]@{
      tag = [string]$group.Name
      count = $events.Count
      first_ts = [Int64]$events[0].ts
      last_ts = [Int64]$events[$events.Count - 1].ts
      source = $firstSource
    })
  }
  return @($ret)
}

function Get-CpuHotspotReferences {
  param($EventsDetail)

  $counts = @{}
  if (-not $EventsDetail -or -not $EventsDetail.exists) { return $counts }

  foreach ($evt in @($EventsDetail.ordered_events)) {
    if ([string]$evt.tag -ne 'cpu_hotspot') { continue }
    foreach ($match in ([regex]::Matches([string]$evt.msg, 'comm=([A-Za-z0-9_\-\.]+)'))) {
      $comm = $match.Groups[1].Value
      if (-not $comm) { continue }
      if (-not $counts.ContainsKey($comm)) { $counts[$comm] = 0 }
      $counts[$comm] = [int]$counts[$comm] + 1
    }
  }
  return $counts
}

function Get-EventProcessSuspects {
  param($EventsDetail)

  $map = @{}
  if (-not $EventsDetail -or -not $EventsDetail.exists) { return @() }

  foreach ($evt in @($EventsDetail.ordered_events)) {
    $tag = [string]$evt.tag
    if (-not $tag -or ($tag -notin @('mem_pressure','cpu_hotspot'))) { continue }

    foreach ($match in ([regex]::Matches([string]$evt.msg, 'P\d+\(pid=(\d+),ppid=\d+,rss=(\d+)kB,comm=([^)]+)\)'))) {
      $procPid = [int]$match.Groups[1].Value
      $rssKb = [int]$match.Groups[2].Value
      $comm = $match.Groups[3].Value.Trim()
      if (-not $comm) { continue }

      if (-not $map.ContainsKey($comm)) {
        $map[$comm] = [ordered]@{
          comm = $comm
          max_event_rss_kb = 0
          min_event_rss_kb = [int]::MaxValue
          event_hit_count = 0
          mem_pressure_refs = 0
          cpu_hotspot_refs = 0
          first_event_ts = $null
          last_event_ts = $null
          pids = New-Object System.Collections.ArrayList
          event_tags = New-Object System.Collections.ArrayList
        }
      }

      $entry = $map[$comm]
      if ($rssKb -gt [int]$entry.max_event_rss_kb) { $entry.max_event_rss_kb = $rssKb }
      if ($rssKb -lt [int]$entry.min_event_rss_kb) { $entry.min_event_rss_kb = $rssKb }
      $entry.event_hit_count = [int]$entry.event_hit_count + 1
      if ($tag -eq 'mem_pressure') { $entry.mem_pressure_refs = [int]$entry.mem_pressure_refs + 1 }
      if ($tag -eq 'cpu_hotspot') { $entry.cpu_hotspot_refs = [int]$entry.cpu_hotspot_refs + 1 }
      if ($null -eq $entry.first_event_ts -or [Int64]$evt.ts -lt [Int64]$entry.first_event_ts) { $entry.first_event_ts = [Int64]$evt.ts }
      if ($null -eq $entry.last_event_ts -or [Int64]$evt.ts -gt [Int64]$entry.last_event_ts) { $entry.last_event_ts = [Int64]$evt.ts }
      if (-not ($entry.pids -contains $procPid)) { [void]$entry.pids.Add($procPid) }
      if (-not ($entry.event_tags -contains $tag)) { [void]$entry.event_tags.Add($tag) }
    }
  }

  return @(
    $map.GetEnumerator() |
      ForEach-Object {
        $minEventRss = if ([int]$_.Value.min_event_rss_kb -eq [int]::MaxValue) { [int]$_.Value.max_event_rss_kb } else { [int]$_.Value.min_event_rss_kb }
        [pscustomobject]@{
          comm = [string]$_.Value.comm
          max_event_rss_kb = [int]$_.Value.max_event_rss_kb
          min_event_rss_kb = [int]$minEventRss
          event_hit_count = [int]$_.Value.event_hit_count
          mem_pressure_refs = [int]$_.Value.mem_pressure_refs
          cpu_hotspot_refs = [int]$_.Value.cpu_hotspot_refs
          first_event_ts = $_.Value.first_event_ts
          last_event_ts = $_.Value.last_event_ts
          pids = @($_.Value.pids)
          event_tags = @($_.Value.event_tags)
        }
      }
  )
}

function Rank-ProcessSuspects {
  param(
    $ProcessSummary,
    $EventsDetail,
    [string]$GtFamily,
    $Injector
  )

  if (-not $ProcessSummary) { return $ProcessSummary }

  $eventSuspects = @(Get-EventProcessSuspects -EventsDetail $EventsDetail)
  $combined = @{}

  foreach ($proc in @($ProcessSummary.suspects)) {
    if (-not $proc) { continue }
    $comm = [string]$proc.comm
    if (-not $comm) { continue }
    $combined[$comm] = [ordered]@{
      comm = $comm
      max_rss_kb = [int]$proc.max_rss_kb
      min_rss_kb = [int]$proc.min_rss_kb
      rss_growth_kb = [int]$proc.rss_growth_kb
      seen_in_snapshots = [int]$proc.seen_in_snapshots
      occurrence_count = [int]$proc.occurrence_count
      mem_pressure_refs = 0
      cpu_hotspot_refs = 0
      event_hit_count = 0
      max_event_rss_kb = 0
      first_event_ts = $null
      last_event_ts = $null
      mem_pressure_snapshot_hits = [int]$proc.mem_pressure_snapshot_hits
      cpu_hotspot_snapshot_hits = [int]$proc.cpu_hotspot_snapshot_hits
      periodic_snapshot_hits = [int]$proc.periodic_snapshot_hits
      reasons = @($proc.reasons)
      pids = @($proc.pids)
    }
  }

  foreach ($evtProc in $eventSuspects) {
    $comm = [string]$evtProc.comm
    if (-not $comm) { continue }
    if (-not $combined.ContainsKey($comm)) {
      $combined[$comm] = [ordered]@{
        comm = $comm
        max_rss_kb = [int]$evtProc.max_event_rss_kb
        min_rss_kb = [int]$evtProc.min_event_rss_kb
        rss_growth_kb = 0
        seen_in_snapshots = 0
        occurrence_count = 0
        mem_pressure_refs = 0
        cpu_hotspot_refs = 0
        event_hit_count = 0
        max_event_rss_kb = 0
        first_event_ts = $null
        last_event_ts = $null
        mem_pressure_snapshot_hits = 0
        cpu_hotspot_snapshot_hits = 0
        periodic_snapshot_hits = 0
        reasons = @()
        pids = @()
      }
    }

    $entry = $combined[$comm]
    if ([int]$evtProc.max_event_rss_kb -gt [int]$entry.max_rss_kb) { $entry.max_rss_kb = [int]$evtProc.max_event_rss_kb }
    if ([int]$evtProc.min_event_rss_kb -lt [int]$entry.min_rss_kb) { $entry.min_rss_kb = [int]$evtProc.min_event_rss_kb }
    $entry.mem_pressure_refs = [int]$entry.mem_pressure_refs + [int]$evtProc.mem_pressure_refs
    $entry.cpu_hotspot_refs = [int]$entry.cpu_hotspot_refs + [int]$evtProc.cpu_hotspot_refs
    $entry.event_hit_count = [int]$entry.event_hit_count + [int]$evtProc.event_hit_count
    if ([int]$evtProc.max_event_rss_kb -gt [int]$entry.max_event_rss_kb) { $entry.max_event_rss_kb = [int]$evtProc.max_event_rss_kb }
    if ($null -eq $entry.first_event_ts -or ($evtProc.first_event_ts -and [Int64]$evtProc.first_event_ts -lt [Int64]$entry.first_event_ts)) { $entry.first_event_ts = $evtProc.first_event_ts }
    if ($null -eq $entry.last_event_ts -or ($evtProc.last_event_ts -and [Int64]$evtProc.last_event_ts -gt [Int64]$entry.last_event_ts)) { $entry.last_event_ts = $evtProc.last_event_ts }
    foreach ($tag in @($evtProc.event_tags)) {
      if (-not ($entry.reasons -contains $tag)) { $entry.reasons += $tag }
    }
    foreach ($procPid in @($evtProc.pids)) {
      if (-not ($entry.pids -contains $procPid)) { $entry.pids += $procPid }
    }
  }

  $injectorHint = $null
  if ($Injector -and $Injector.process_name_hint_norm) { $injectorHint = [string]$Injector.process_name_hint_norm }
  $ranked = New-Object System.Collections.ArrayList
  foreach ($entryValue in @($combined.Values)) {
    if (-not $entryValue) { continue }
    $commNorm = Normalize-ProcessNameHint -Text ([string]$entryValue.comm)
    $injectorNameMatch = $false
    if ($injectorHint -and $commNorm -and $commNorm -eq $injectorHint) { $injectorNameMatch = $true }
    if (-not $injectorNameMatch -and $GtFamily -eq 'mem' -and [string]$entryValue.comm -match 'memory|leak') { $injectorNameMatch = $true }
    $injectorNameScore = 0
    if ($injectorNameMatch) { $injectorNameScore = 1 }

    $ranking = 0.0
    if ($GtFamily -eq 'cpu') {
      $ranking = ([int]$entryValue.cpu_hotspot_refs * 1000.0) + ([int]$entryValue.cpu_hotspot_snapshot_hits * 100.0) + ([int]$entryValue.seen_in_snapshots * 10.0) + ([int]$entryValue.max_rss_kb / 1024.0)
    } elseif ($GtFamily -eq 'mem') {
      $ranking = ([int]$entryValue.mem_pressure_refs * 1000000.0) +
        ([int]$entryValue.cpu_hotspot_refs * 100000.0) +
        ([int]$entryValue.event_hit_count * 50000.0) +
        ($injectorNameScore * 400000.0) +
        ([int]$entryValue.rss_growth_kb * 10.0) +
        ([int]$entryValue.max_rss_kb / 4.0) +
        ([int]$entryValue.seen_in_snapshots * 10.0)
    } else {
      $ranking = ([int]$entryValue.seen_in_snapshots * 10.0) + ([int]$entryValue.max_rss_kb / 1024.0)
    }

    [void]$ranked.Add([pscustomobject]@{
      comm = [string]$entryValue.comm
      max_rss_kb = [int]$entryValue.max_rss_kb
      min_rss_kb = [int]$entryValue.min_rss_kb
      rss_growth_kb = [int]$entryValue.rss_growth_kb
      seen_in_snapshots = [int]$entryValue.seen_in_snapshots
      occurrence_count = [int]$entryValue.occurrence_count
      mem_pressure_refs = [int]$entryValue.mem_pressure_refs
      cpu_hotspot_refs = [int]$entryValue.cpu_hotspot_refs
      event_hit_count = [int]$entryValue.event_hit_count
      max_event_rss_kb = [int]$entryValue.max_event_rss_kb
      first_event_ts = $entryValue.first_event_ts
      last_event_ts = $entryValue.last_event_ts
      mem_pressure_snapshot_hits = [int]$entryValue.mem_pressure_snapshot_hits
      cpu_hotspot_snapshot_hits = [int]$entryValue.cpu_hotspot_snapshot_hits
      periodic_snapshot_hits = [int]$entryValue.periodic_snapshot_hits
      injector_name_match = [bool]$injectorNameMatch
      reasons = @($entryValue.reasons)
      pids = @($entryValue.pids)
      ranking_score = [math]::Round($ranking, 3)
    })
  }

  $rankingBasis = switch ($GtFamily) {
    'cpu' { @('cpu_hotspot_refs','cpu_hotspot_snapshot_hits','seen_in_snapshots','max_rss_kb') }
    'mem' { @('mem_pressure_refs','cpu_hotspot_refs','event_hit_count','injector_name_match','rss_growth_kb','max_rss_kb','seen_in_snapshots') }
    default { @('seen_in_snapshots','max_rss_kb') }
  }

  return [pscustomobject]@{
    exists = [bool]$ProcessSummary.exists
    snapshot_count = [int]$ProcessSummary.snapshot_count
    ranking_basis = $rankingBasis
    suspects = @(
      $ranked |
        Sort-Object @{Expression='ranking_score';Descending=$true}, @{Expression='seen_in_snapshots';Descending=$true}, @{Expression='max_rss_kb';Descending=$true} |
        Select-Object -First 12
    )
  }
}

function Count-TextPatterns {
  param([string]$Path)
  $text = Read-TextSafe $Path
  $ret = [ordered]@{
    exists = (Test-PathSafe $Path)
    oom = 0
    killed_process = 0
    avc_denied = 0
    binder = 0
    hung_task = 0
    segfault = 0
    fatal = 0
  }
  if (-not $ret.exists) { return [pscustomobject]$ret }

  $ret.oom = ([regex]::Matches($text, '(?im)(out of memory|oom)')).Count
  $ret.killed_process = ([regex]::Matches($text, '(?im)killed process')).Count
  $ret.avc_denied = ([regex]::Matches($text, '(?im)avc')).Count
  $ret.binder = ([regex]::Matches($text, '(?im)binder')).Count
  $ret.hung_task = ([regex]::Matches($text, '(?im)(hung task|blocked for more than)')).Count
  $ret.segfault = ([regex]::Matches($text, '(?im)(segfault|segmentation fault)')).Count
  $ret.fatal = ([regex]::Matches($text, '(?im)fatal')).Count
  return [pscustomobject]$ret
}

function Build-QualityFlags {
  param(
    $Meta,
    [string]$RunRoot,
    $Modalities,
    $Injector,
    $DmesgAfterStats,
    $NetSummary
  )

  $flags = New-Object System.Collections.ArrayList

  if ($Meta -and $Meta.faultlog_all -and ([int]$Meta.faultlog_new_count -le 0)) {
    [void]$flags.Add('faultlog_all_is_historical_noise')
  }
  if (Test-PathSafe (Join-Path $RunRoot '_probe_dmesg_recv')) {
    [void]$flags.Add('probe_dmesg_duplicates_present')
  }
  if (-not $Modalities.metrics) { [void]$flags.Add('missing_metrics') }
  if (-not $Modalities.events) { [void]$flags.Add('missing_events') }
  if (-not $Modalities.procs) { [void]$flags.Add('missing_procs') }
  if (-not $Modalities.hilog_full) { [void]$flags.Add('missing_hilog_full') }
  if (-not $Modalities.dmesg_after) { [void]$flags.Add('missing_dmesg_after') }
  if ($Modalities.faultlog_all -and -not $Modalities.faultlog_new) { [void]$flags.Add('prefer_faultlog_new_over_faultlog_all') }
  if ($Meta -and $Meta.run_window_source -ne 'poke') { [void]$flags.Add('run_window_not_locked_by_poke') }
  if ($Injector -and $Injector.exists -and -not $Injector.reached_configured_limit) { [void]$flags.Add('injector_log_no_explicit_limit_reached_marker') }
  if ($DmesgAfterStats -and $DmesgAfterStats.oom -gt 0) { [void]$flags.Add('dmesg_contains_oom_markers') }
  if ($Meta -and [string]$Meta.gt_family -eq 'net') {
    if (-not ($NetSummary -and $NetSummary.exists -and ($NetSummary.observed_phases -contains 'pre'))) { [void]$flags.Add('missing_net_pre_snapshot') }
    if (-not ($NetSummary -and $NetSummary.exists -and (($NetSummary.observed_phases -contains 'fault') -or ($NetSummary.observed_phases -contains 'fault2')))) { [void]$flags.Add('missing_net_fault_snapshot') }
    if (-not ($NetSummary -and $NetSummary.exists -and (($NetSummary.observed_phases -contains 'post') -or ($NetSummary.observed_phases -contains 'post2')))) { [void]$flags.Add('missing_net_post_snapshot') }
    if (-not ($Injector -and $Injector.exists)) { [void]$flags.Add('net_fault_inject_log_missing') }
    if ($NetSummary -and $NetSummary.exists -and (($NetSummary.observed_phases -contains 'fault') -or ($NetSummary.observed_phases -contains 'fault2')) -and -not ($NetSummary.iface_recovered -or $NetSummary.dns_resolution_ok -or $NetSummary.ping_ip_ok)) {
      [void]$flags.Add('net_recovery_not_observed')
    }
  }

  return @($flags)
}

function New-EvidenceItem {
  param(
    [string]$Eid,
    [string]$Source,
    [string]$Kind,
    [string]$Target,
    [string]$Text,
    [double]$Score,
    [string]$SourceRel = $null,
    [Nullable[Int64]]$Ts = $null,
    $Span = $null
  )
  return [pscustomobject]@{
    eid = $Eid
    source = $Source
    source_rel = $SourceRel
    kind = $Kind
    target = $Target
    support_role = $Target
    ts = $Ts
    span = $Span
    text = $Text
    score = [math]::Round($Score, 3)
  }
}

function Build-EvidenceCandidates {
  param(
    $Meta,
    $Injector,
    $EventsDetail,
    $ProcessSuspects,
    $DmesgAfterStats,
    [string[]]$QualityFlags,
    $PathsRel,
    $NetSummary,
    $NetOutcome
  )

  $items = New-Object System.Collections.ArrayList
  $eid = 1
  $gtFamily = if ($Meta -and $Meta.gt_family) { [string]$Meta.gt_family } else { 'unknown' }

  if ($Injector -and $Injector.exists) {
    if ($Injector.max_leak_mib) {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'fault_inject' -SourceRel $PathsRel.fault_inject -Kind 'injector_config' -Target 'primary' -Text ("injector configured max_leak={0} MiB" -f $Injector.max_leak_mib) -Score 0.98))
    }
    if ($Injector.reached_configured_limit) {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'fault_inject' -SourceRel $PathsRel.fault_inject -Kind 'injector_runtime' -Target 'primary' -Text 'injector log shows configured leak limit reached' -Score 0.99))
    }
    if ($Injector.kind) {
      $injectText = "injector executed {0}" -f $Injector.kind
      if ($Injector.first_wall -and $Injector.last_wall) {
        $injectText = "{0} between {1} and {2}" -f $injectText, $Injector.first_wall, $Injector.last_wall
      }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'fault_inject' -SourceRel $PathsRel.fault_inject -Kind 'inject_summary' -Target 'primary' -Text $injectText -Score 0.88))
    }
  }

  if ($Meta -and $Meta.metrics_summary) {
    $m = $Meta.metrics_summary
    if ($m.mem_avail_drop_kb -and [int64]$m.mem_avail_drop_kb -ge 131072) {
      $target = if ($gtFamily -eq 'mem') { 'primary' } else { 'secondary' }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'metrics' -SourceRel $PathsRel.metrics -Kind 'mem_drop' -Target $target -Text ("mem_available dropped by {0} KB (min={1} KB)" -f $m.mem_avail_drop_kb, $m.mem_avail_min_kb) -Score 0.95 -Span (New-Span -StartMs ([Int64]$Meta.run_window_board_ms_start) -EndMs ([Int64]$Meta.run_window_board_ms_end))))
    }
    if ($m.load1_peak_x100 -and [int]$m.load1_peak_x100 -ge 300) {
      $target = if ($gtFamily -eq 'cpu') { 'primary' } else { 'symptom' }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'metrics' -SourceRel $PathsRel.metrics -Kind 'load_peak' -Target $target -Text ("load1 peak reached {0}" -f $m.load1_peak_x100) -Score 0.78 -Span (New-Span -StartMs ([Int64]$Meta.run_window_board_ms_start) -EndMs ([Int64]$Meta.run_window_board_ms_end))))
    }
    if ($m.cpu_util_peak_x100 -and [int]$m.cpu_util_peak_x100 -ge 7000) {
      $target = if ($gtFamily -eq 'cpu') { 'primary' } else { 'symptom' }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'metrics' -SourceRel $PathsRel.metrics -Kind 'cpu_util_peak' -Target $target -Text ("cpu_util_total peak reached {0}" -f $m.cpu_util_peak_x100) -Score 0.72 -Span (New-Span -StartMs ([Int64]$Meta.run_window_board_ms_start) -EndMs ([Int64]$Meta.run_window_board_ms_end))))
    }
  }

  if ($EventsDetail -and $EventsDetail.exists) {
    foreach ($summary in @(Get-EventTagSummaries -EventsDetail $EventsDetail)) {
      $tag = [string]$summary.tag
      if (-not $tag -or $tag -eq 'init' -or $tag -eq 'poke') { continue }
      $target = 'secondary'
      $eventTs = $null
      if ($tag -eq 'mem_pressure' -and $gtFamily -eq 'mem') { $target = 'primary' }
      elseif ($tag -eq 'cpu_hotspot' -and $gtFamily -ne 'cpu') { $target = 'symptom' }
      elseif ($tag -eq 'cpu_hotspot' -and $gtFamily -eq 'cpu') { $target = 'primary' }
      if ($summary.count -eq 1) { $eventTs = [Int64]$summary.first_ts }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'events' -SourceRel $PathsRel.events -Kind $tag -Target $target -Text ("event tag {0} occurred {1} time(s) in run window" -f $tag, $summary.count) -Score 0.70 -Ts $eventTs -Span (New-Span -StartMs ([Int64]$summary.first_ts) -EndMs ([Int64]$summary.last_ts))))
    }
  }

  if ($ProcessSuspects -and $ProcessSuspects.suspects) {
    $top = $ProcessSuspects.suspects | Select-Object -First 3
    foreach ($p in $top) {
      $target = 'secondary'
      $text = "process {0} max_rss={1} KB seen_in={2} snapshot(s)" -f $p.comm, $p.max_rss_kb, $p.seen_in_snapshots
      if ($gtFamily -eq 'cpu' -and [int]$p.cpu_hotspot_refs -gt 0) {
        $target = 'primary'
        $text = "{0} cpu_hotspot_refs={1}" -f $text, $p.cpu_hotspot_refs
      } elseif ($gtFamily -eq 'mem') {
        $text = "{0} rss_growth={1} KB mem_pressure_refs={2} cpu_hotspot_refs={3}" -f $text, $p.rss_growth_kb, $p.mem_pressure_refs, $p.cpu_hotspot_refs
        if ($p.injector_name_match) { $text = "{0} injector_name_match=true" -f $text }
        if ([int]$p.mem_pressure_refs -gt 0 -or [int]$p.rss_growth_kb -gt 0 -or ($p.comm -match 'memory|leak')) { $target = 'primary' }
      }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'procs' -SourceRel $PathsRel.procs_dir -Kind 'suspect_process' -Target $target -Text $text -Score 0.68))
    }
  }

  if ($gtFamily -eq 'mem' -and $ProcessSuspects -and $ProcessSuspects.suspects) {
    foreach ($p in @($ProcessSuspects.suspects | Where-Object { [int]$_.mem_pressure_refs -gt 0 -or [int]$_.cpu_hotspot_refs -gt 0 } | Select-Object -First 2)) {
      $eventText = "event top_rss implicates {0}" -f $p.comm
      if ($p.pids -and @($p.pids).Count -gt 0) { $eventText = "{0} pid={1}" -f $eventText, $p.pids[0] }
      $eventText = "{0} max_rss={1} KB mem_pressure_refs={2} cpu_hotspot_refs={3}" -f $eventText, $p.max_rss_kb, $p.mem_pressure_refs, $p.cpu_hotspot_refs
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'events' -SourceRel $PathsRel.events -Kind 'event_process_suspect' -Target 'primary' -Text $eventText -Score 0.84 -Ts $p.first_event_ts -Span (New-Span -StartMs $p.first_event_ts -EndMs $p.last_event_ts)))
    }
  }

  if ($gtFamily -eq 'net' -and $NetSummary -and $NetSummary.exists) {
    $netSubtype = if ($NetOutcome -and $NetOutcome.net_fault_type) { [string]$NetOutcome.net_fault_type } elseif ($Meta -and $Meta.fault_type) { [string]$Meta.fault_type } else { 'net_unknown' }
    $preferDnsPrimary = $false
    if ($netSubtype -eq 'net_dns_fail') {
      if ($NetOutcome -and [bool]$NetOutcome.fault_observed -and [string]$NetOutcome.fault_observation_reason -eq 'dns_host_probe_failed_while_ip_probe_still_ok') {
        $preferDnsPrimary = $true
      } elseif ($NetSummary.dns_resolution_fail -and $NetSummary.ping_ip_ok -and -not $NetSummary.iface_down -and -not $NetSummary.link_flap_observed) {
        $preferDnsPrimary = $true
      }
    }

    $preferLinkFlapPrimary = $false
    if ($netSubtype -eq 'net_link_flap') {
      if ($NetOutcome -and [bool]$NetOutcome.fault_observed -and [bool]$NetOutcome.recovery_observed -and [string]$NetOutcome.fault_observation_reason -eq 'down_phase_observed' -and [string]$NetOutcome.recovery_observation_reason -eq 'down_to_up_transition_observed') {
        $preferLinkFlapPrimary = $true
      } elseif ($NetSummary.link_flap_observed -and ($NetSummary.observed_phases -contains 'fault2')) {
        $preferLinkFlapPrimary = $true
      }
    }

    $preferLinkDownPrimary = $false
    if ($netSubtype -eq 'net_link_down') {
      if ($NetOutcome -and [string]$NetOutcome.fault_observation_reason -eq 'operstate_or_carrier_down' -and [bool]$NetOutcome.fault_observed) {
        $preferLinkDownPrimary = $true
      } elseif ($NetSummary.iface_down) {
        $preferLinkDownPrimary = $true
      }
    }

    if ($Injector -and $Injector.exists) {
      $injectText = "net injector marker observed via {0}" -f $Injector.kind
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'fault_inject' -SourceRel $PathsRel.fault_inject -Kind 'net_injector_marker' -Target 'primary' -Text $injectText -Score 0.95))
    }
    if ($NetSummary.dns_resolution_fail) {
      $target = if ($preferDnsPrimary) { 'primary' } else { 'secondary' }
      $text = if ($preferDnsPrimary) { 'DNS resolution failed during fault snapshot' } else { 'DNS resolution failed in probe evidence, but subtype boundary is dominated by subtype-specific connectivity evidence' }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_probe' -SourceRel $PathsRel.net_dir -Kind 'net_dns_resolution_fail' -Target $target -Text $text -Score 0.94))
    }
    if ($NetSummary.ping_ip_ok) {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_probe' -SourceRel $PathsRel.net_dir -Kind 'net_ping_ip_ok' -Target 'secondary' -Text 'IP ping remained reachable in at least one probe phase' -Score 0.66))
    }
    if ($NetSummary.ping_ip_fail) {
      $target = if ($preferDnsPrimary) { 'secondary' } elseif ($preferLinkDownPrimary -or $preferLinkFlapPrimary) { 'primary' } else { 'secondary' }
      $pingFailText = if ($target -eq 'primary') { 'IP ping failed during fault snapshot' } else { 'IP ping failed in at least one probe phase; keep as secondary connectivity symptom unless subtype-specific primary evidence supports it' }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_probe' -SourceRel $PathsRel.net_dir -Kind 'net_ping_ip_fail' -Target $target -Text $pingFailText -Score 0.91))
    }
    if ($NetSummary.iface_down) {
      $target = if ($preferLinkDownPrimary -or $preferLinkFlapPrimary) { 'primary' } else { 'secondary' }
      if ($NetSummary.affected_iface) {
        $ifaceText = if ($target -eq 'primary') { "interface {0} transitioned down in network snapshots" -f $NetSummary.affected_iface } else { "interface {0} showed down-state evidence in network snapshots" -f $NetSummary.affected_iface }
      } else {
        $ifaceText = if ($target -eq 'primary') { 'network interface transitioned down in network snapshots' } else { 'network interface showed down-state evidence in network snapshots' }
      }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_iface_down' -Target $target -Text $ifaceText -Score 0.96))
    }
    if ($NetSummary.iface_recovered) {
      $target = if ($preferLinkDownPrimary -or $preferLinkFlapPrimary) { 'primary' } else { 'secondary' }
      if ($NetSummary.affected_iface) {
        $ifaceText = if ($target -eq 'primary') { "interface {0} recovered in network snapshots" -f $NetSummary.affected_iface } else { "interface {0} showed recovery evidence in network snapshots" -f $NetSummary.affected_iface }
      } else {
        $ifaceText = if ($target -eq 'primary') { 'network interface recovered in network snapshots' } else { 'network interface showed recovery evidence in network snapshots' }
      }
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_iface_recovered' -Target $target -Text $ifaceText -Score 0.90))
    }
    if ($preferLinkFlapPrimary) {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_link_flap_observed' -Target 'primary' -Text 'link flap observed through fault and recovery snapshots' -Score 0.97))
    }
    if ($NetSummary.wlan_disconnected -and $netSubtype -eq 'net_wlan_disconnect') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_wlan_disconnected' -Target 'primary' -Text 'wlan interface reported disconnected state' -Score 0.93))
    }
    if ($NetSummary.wlan_disconnected -and $netSubtype -eq 'net_wifi_disconnect') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_wifi_disconnected' -Target 'primary' -Text 'wlan interface entered DISCONNECTED state during fault (wpa_state=DISCONNECTED)' -Score 0.94))
    }
    if ($NetSummary.wlan_auth_fail -and $netSubtype -eq 'net_wifi_auth_fail_wrong_psk') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_wifi_auth_fail' -Target 'primary' -Text 'wpa_supplicant running with bad PSK; wpa_state in active-auth cycle (SCANNING/ASSOCIATING/4WAY_HANDSHAKE) during fault' -Score 0.92))
    }
    if ($NetSummary.no_default_route -and $netSubtype -eq 'net_no_default_route') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_no_default_route' -Target 'primary' -Text 'wlan0 has IP but no default route in /proc/net/route during fault' -Score 0.93))
    }
    if ($NetSummary.no_ipv4_on_iface -and $netSubtype -eq 'net_no_ipv4_on_iface') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_no_ipv4_on_iface' -Target 'primary' -Text 'wlan0 has no inet addr during fault (IP cleared) while wpa_state=COMPLETED (L1 still associated)' -Score 0.94))
    }
    if ($NetSummary.wrong_default_route -and $netSubtype -eq 'net_wrong_default_route') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_snapshot' -SourceRel $PathsRel.net_dir -Kind 'net_wrong_default_route' -Target 'primary' -Text 'wlan0 has inet addr and default route entry present in /proc/net/route during fault, but route points to unreachable gateway (traffic black-holed)' -Score 0.93))
    }
    if ($NetSummary.gateway_ping_fail -and $NetSummary.ping_ip_fail -and $netSubtype -eq 'net_gateway_unreachable') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_probe' -SourceRel $PathsRel.net_dir -Kind 'net_gateway_unreachable' -Target 'primary' -Text 'default gateway ping failed while wlan0 retained IPv4 and a default route; public IP and DNS failures are downstream symptoms' -Score 0.95))
    }
    if ($NetSummary.target_ping_fail -and $netSubtype -eq 'net_public_ip_unreachable') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_probe' -SourceRel $PathsRel.net_dir -Kind 'net_target_ip_unreachable' -Target 'primary' -Text 'target IP ping failed during fault snapshot while main IP probe remained reachable (iptables OUTPUT DROP for specific target only)' -Score 0.95))
    }
    if ($NetSummary.gateway_ping_ok -and $netSubtype -eq 'net_public_ip_unreachable') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'net_probe' -SourceRel $PathsRel.net_dir -Kind 'net_gateway_reachable' -Target 'secondary' -Text 'default gateway remained reachable in probe evidence (distinguishes net_public_ip_unreachable from net_gateway_unreachable)' -Score 0.88))
    }
  }

  if ($DmesgAfterStats -and $DmesgAfterStats.oom -gt 0) {
    [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'dmesg_after' -SourceRel $PathsRel.dmesg_after -Kind 'oom_marker' -Target 'primary' -Text ("dmesg_after contains {0} OOM marker(s)" -f $DmesgAfterStats.oom) -Score 0.90))
  }

  foreach ($flag in @($QualityFlags)) {
    if ($flag -eq 'faultlog_all_is_historical_noise') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source 'faultlog_all' -SourceRel $PathsRel.faultlog_all_dir -Kind 'historical_noise' -Target 'noise' -Text 'faultlog_all exists but faultlog_new is empty; do not use faultlog_all as run-local evidence' -Score 0.99))
    }
    if ($flag -eq 'probe_dmesg_duplicates_present') {
      [void]$items.Add((New-EvidenceItem -Eid ("e{0}" -f $eid++) -Source '_probe_dmesg_recv' -SourceRel '_probe_dmesg_recv' -Kind 'duplicate_probe' -Target 'noise' -Text 'probe dmesg copies are duplicates for transport debugging, not primary training evidence' -Score 0.95))
    }
  }

  return @($items)
}

function Build-DiagnosisInputText {
  param(
    $Canonical,
    $Evidence
  )

  $lines = New-Object System.Collections.ArrayList
  [void]$lines.Add('[CASE_META]')
  [void]$lines.Add(("case_id={0}" -f $Canonical.case_id))
  [void]$lines.Add(("source_type={0}" -f $Canonical.source_type))
  [void]$lines.Add(("scenario_tag={0}" -f $Canonical.source.scenario_tag))
  [void]$lines.Add(("fault_type={0}" -f $Canonical.source.fault_type))
  [void]$lines.Add(("gt_family={0}" -f $Canonical.gt.family))
  [void]$lines.Add(("gt_subtype={0}" -f $Canonical.gt.subtype))
  [void]$lines.Add(("gt_severity={0}" -f $Canonical.gt.severity))
  [void]$lines.Add('')

  [void]$lines.Add('[OBS]')
  [void]$lines.Add(("obs_state={0}" -f $Canonical.obs.state))
  [void]$lines.Add(("obs_primary_family={0}" -f $Canonical.obs.primary_family))
  [void]$lines.Add(("obs_families={0}" -f (($Canonical.obs.fault_families -join ',') )))
  [void]$lines.Add(("obs_secondary_families={0}" -f (($Canonical.obs.secondary_families -join ',') )))
  [void]$lines.Add('')

  [void]$lines.Add('[MODALITY_SUMMARY]')
  $modalityNames = @()
  if ($Canonical.modality_mask -is [System.Collections.IDictionary]) {
    $modalityNames = @($Canonical.modality_mask.Keys | Sort-Object)
  } else {
    $modalityNames = @($Canonical.modality_mask.PSObject.Properties.Name | Sort-Object)
  }
  foreach ($name in $modalityNames) {
    $value = $Canonical.modality_mask.$name
    if ($Canonical.modality_mask -is [System.Collections.IDictionary]) {
      $value = $Canonical.modality_mask[$name]
    }
    [void]$lines.Add(("{0}={1}" -f $name, $value))
  }
  [void]$lines.Add('')

  if ($Canonical.derived.injector_summary -and $Canonical.derived.injector_summary.exists) {
    $inj = $Canonical.derived.injector_summary
    [void]$lines.Add('[INJECT_SUMMARY]')
    [void]$lines.Add(("kind={0}" -f $inj.kind))
    [void]$lines.Add(("first_wall={0}" -f $inj.first_wall))
    [void]$lines.Add(("last_wall={0}" -f $inj.last_wall))
    [void]$lines.Add(("reached_configured_limit={0}" -f $inj.reached_configured_limit))
    [void]$lines.Add('')
  }

  if ($Canonical.derived.metrics_summary) {
    $m = $Canonical.derived.metrics_summary
    [void]$lines.Add('[METRICS_SUMMARY]')
    [void]$lines.Add(("load1_peak_x100={0}" -f $m.load1_peak_x100))
    [void]$lines.Add(("cpu_util_peak_x100={0}" -f $m.cpu_util_peak_x100))
    [void]$lines.Add(("mem_avail_drop_kb={0}" -f $m.mem_avail_drop_kb))
    [void]$lines.Add(("mem_avail_min_kb={0}" -f $m.mem_avail_min_kb))
    [void]$lines.Add('')
  }

  if ($Canonical.derived.process_suspects -and $Canonical.derived.process_suspects.suspects) {
    [void]$lines.Add('[PROCESS_SUMMARY]')
    foreach ($p in @($Canonical.derived.process_suspects.suspects | Select-Object -First 3)) {
      $extra = ''
      if ($Canonical.gt.family -eq 'cpu') {
        $extra = " cpu_hotspot_refs={0}" -f $p.cpu_hotspot_refs
      } elseif ($Canonical.gt.family -eq 'mem') {
        $extra = " rss_growth_kb={0} mem_pressure_refs={1} cpu_hotspot_refs={2}" -f $p.rss_growth_kb, $p.mem_pressure_refs, $p.cpu_hotspot_refs
        if ($p.injector_name_match) {
          $extra = "{0} injector_name_match=true" -f $extra
        }
      }
      [void]$lines.Add(("{0} max_rss_kb={1} seen_in_snapshots={2}{3}" -f $p.comm, $p.max_rss_kb, $p.seen_in_snapshots, $extra))
    }
    [void]$lines.Add('')
  }

  [void]$lines.Add('[KEY_EVIDENCE]')
  foreach ($role in @('primary','symptom','secondary','noise')) {
    foreach ($e in @($Evidence | Where-Object { $_.support_role -eq $role } | Sort-Object score -Descending | Select-Object -First 4)) {
      [void]$lines.Add(("{0}`t{1}`t{2}`t{3}" -f $role, $e.source, $e.kind, $e.text))
    }
  }
  [void]$lines.Add('')

  if ($Canonical.quality_flags -and $Canonical.quality_flags.Count -gt 0) {
    [void]$lines.Add('[QUALITY_FLAGS]')
    foreach ($q in $Canonical.quality_flags) {
      [void]$lines.Add($q)
    }
  }

  return ($lines -join [Environment]::NewLine)
}

$resolved = $null
$runRoot = $null
try {
  $resolved = Resolve-RunRoot -InputPath $InputPath -KeepExpanded:$KeepExpanded
  $runRoot = $resolved.run_root

  if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    if ((Get-Item -LiteralPath $InputPath).PSIsContainer) {
      $OutputRoot = Join-Path $runRoot 'dataset_export'
    } else {
      $zipStem = [System.IO.Path]::GetFileNameWithoutExtension($InputPath)
      $OutputRoot = Join-Path (Split-Path -Parent $InputPath) ($zipStem + '_dataset_export')
    }
  }
  Ensure-Dir $OutputRoot
  Ensure-Dir (Join-Path $OutputRoot 'training_views')

  $metaPath = Join-Path $runRoot '_run_meta.json'
  $meta = Read-JsonSafe $metaPath
  if ($null -eq $meta) {
    throw "Missing or unreadable _run_meta.json under: $runRoot"
  }

  $metricsPath = Find-FirstFile -Root $runRoot -Patterns @('sys_*.csv')
  $eventsPath = Find-FirstFile -Root $runRoot -Patterns @('events_*.jsonl')
  $procsDir = Join-Path $runRoot 'procs'
  $dmesgBeforePath = Join-Path $runRoot 'dmesg_before.utf8.log'
  if (-not (Test-PathSafe $dmesgBeforePath)) { $dmesgBeforePath = Join-Path $runRoot 'dmesg_before.log' }
  $dmesgAfterPath = Join-Path $runRoot 'dmesg_after.utf8.log'
  if (-not (Test-PathSafe $dmesgAfterPath)) { $dmesgAfterPath = Join-Path $runRoot 'dmesg_after.log' }
  $hilogFullPath = Join-Path $runRoot 'hilog_text_full.log'
  $faultInjectPath = Find-FirstFile -Root (Join-Path $runRoot 'fault_inject') -Patterns @('fault_*.log','fault_*.txt')
  $faultlogNewDir = Join-Path $runRoot 'faultlog_new'
  $faultlogAllDir = Join-Path $runRoot 'faultlog'
  $netDir = Join-Path $runRoot 'net'
  $contextFiles = Find-AllFiles -Root (Join-Path $runRoot 'context') -Patterns @('*.txt','*.json')

  $modalities = [ordered]@{
    metrics = (Test-PathSafe $metricsPath)
    events = (Test-PathSafe $eventsPath)
    procs = (Test-PathSafe $procsDir)
    dmesg_before = (Test-PathSafe $dmesgBeforePath)
    dmesg_after = (Test-PathSafe $dmesgAfterPath)
    hilog_full = (Test-PathSafe $hilogFullPath)
    fault_inject = (Test-PathSafe $faultInjectPath)
    faultlog_new = (Test-PathSafe $faultlogNewDir) -and (@(Get-ChildItem -LiteralPath $faultlogNewDir -Recurse -File -ErrorAction SilentlyContinue).Count -gt 0)
    faultlog_all = (Test-PathSafe $faultlogAllDir)
    device_context = ($contextFiles.Count -gt 0)
  }
  if (Test-PathSafe $netDir) { $modalities.net = $true }

  $injector = Parse-FaultInjectSummary -LogPath $faultInjectPath
  $eventsDetail = Read-EventsWindowDetails -EventsPath $eventsPath -StartMs ([Int64]$meta.run_window_board_ms_start) -EndMs ([Int64]$meta.run_window_board_ms_end)
  $procsSummary = Read-ProcessSuspects -ProcsDir $procsDir
  $dmesgBeforeStats = Count-TextPatterns -Path $dmesgBeforePath
  $dmesgAfterStats = Count-TextPatterns -Path $dmesgAfterPath
  $hilogStats = Count-TextPatterns -Path $hilogFullPath
  $netSummary = Read-NetSummary -NetDir $netDir
  $netOutcome = Read-NetOutcomeSummary -Meta $meta -RunRoot $runRoot

  $pathsRel = [ordered]@{
    run_meta = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $metaPath)
    metrics = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $metricsPath)
    events = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $eventsPath)
    procs_dir = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $procsDir)
    dmesg_before = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $dmesgBeforePath)
    dmesg_after = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $dmesgAfterPath)
    hilog_full = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $hilogFullPath)
    fault_inject = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $faultInjectPath)
    faultlog_new_dir = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $faultlogNewDir)
    faultlog_all_dir = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $faultlogAllDir)
  }
  if ($netSummary -and $netSummary.exists) { $pathsRel.net_dir = (Get-RelativePathSimple -BasePath $runRoot -TargetPath $netDir) }
  if ($netOutcome -and $netOutcome.exists) { $pathsRel.net_outcome = (Get-RelativePathSimple -BasePath $runRoot -TargetPath (Join-Path $runRoot '_net_outcome.json')) }

  $procsSummary = Rank-ProcessSuspects -ProcessSummary $procsSummary -EventsDetail $eventsDetail -GtFamily ([string]$meta.gt_family) -Injector $injector
  $qualityFlags = Build-QualityFlags -Meta $meta -RunRoot $runRoot -Modalities $modalities -Injector $injector -DmesgAfterStats $dmesgAfterStats -NetSummary $netSummary
  $evidence = Build-EvidenceCandidates -Meta $meta -Injector $injector -EventsDetail $eventsDetail -ProcessSuspects $procsSummary -DmesgAfterStats $dmesgAfterStats -QualityFlags $qualityFlags -PathsRel $pathsRel -NetSummary $netSummary -NetOutcome $netOutcome

  $obsFaultFamilies = ConvertTo-StableStringArray $meta.obs_multi.obs_families
  $obsWarnFamilies = ConvertTo-StableStringArray $meta.obs_multi.obs_families_warn
  $obsSecondary = ConvertTo-StableStringArray $meta.obs_multi.obs_secondary_families
  $obsConfounders = ConvertTo-StableStringArray $meta.obs_multi.obs_confounders
  $obsScores = ConvertTo-StableScoreMap $meta.obs_multi.obs_scores

  $derived = [ordered]@{
    metrics_summary = $meta.metrics_summary
    event_summary = [ordered]@{
      count_total = $eventsDetail.count_total
      count_in_window = $eventsDetail.count_in_window
      counts_by_tag = $eventsDetail.counts_by_tag
      ordered_events = @($eventsDetail.ordered_events)
    }
    injector_summary = $injector
    process_suspects = $procsSummary
    dmesg_before_markers = $dmesgBeforeStats
    dmesg_after_markers = $dmesgAfterStats
    hilog_markers = $hilogStats
  }
  if ($netSummary -and $netSummary.exists) { $derived.net_summary = $netSummary }
  if ($netOutcome -and ($netOutcome.exists -or $netOutcome.net_fault_type)) { $derived.net_outcome = $netOutcome }

  $canonical = [ordered]@{
    schema_version = 'canonical_case_v1'
    source_type = 'kaihong_run_bundle'
    source_case_id = [string]$meta.run_id
    case_id = [string]$meta.run_id
    source = [ordered]@{
      run_id = [string]$meta.run_id
      collector_version = [string]$meta.script_version
      scenario_tag = [string]$meta.scenario_tag
      fault_type = [string]$meta.fault_type
      device_sn_hash = (Get-Sha1Hex -Text ([string]$meta.device_sn))
    }
    gt = [ordered]@{
      run_kind = [string]$meta.gt_run_kind
      family = [string]$meta.gt_family
      subtype = [string]$meta.fault_type
      severity = [string]$meta.gt_severity
      is_anomaly = [bool]$meta.gt_is_anomaly
      confidence = 'high'
    }
    obs = [ordered]@{
      state = [string]$meta.obs_fault_state
      primary_family = [string]$meta.obs_multi.obs_primary_family
      secondary_families = @($obsSecondary)
      fault_families = @($obsFaultFamilies)
      warn_families = @($obsWarnFamilies)
      confounders = @($obsConfounders)
      scores = $obsScores
    }
    timing = [ordered]@{
      run_window_host_epoch_ms_start = [Int64]$meta.run_window_host_epoch_ms_start
      run_window_host_epoch_ms_end = [Int64]$meta.run_window_host_epoch_ms_end
      run_window_board_ms_start = [Int64]$meta.run_window_board_ms_start
      run_window_board_ms_end = [Int64]$meta.run_window_board_ms_end
      run_window_source = [string]$meta.run_window_source
      time_skew_ms = $meta.time_skew_ms
    }
    modalities = $modalities
    modality_mask = $modalities
    quality_flags = @($qualityFlags)
    derived = $derived
    paths_rel = $pathsRel
  }

  $eventsPromptReady = ($eventsDetail.count_in_window -gt 0 -and $eventsDetail.count_in_window -le 20)
  $faultInjectPromptReady = [bool]$injector.exists
  $eventsPromptNote = 'raw event stream should be summarized first'
  if ($eventsPromptReady) { $eventsPromptNote = 'small in-window event stream is prompt-safe' }
  $faultInjectPromptNote = 'inject log missing'
  if ($faultInjectPromptReady) { $faultInjectPromptNote = 'inject log is short and semantically aligned' }
  $manifest = [ordered]@{
    schema_version = 'source_manifest_v1'
    case_id = [string]$meta.run_id
    files = @(
      [pscustomobject]@{ name='run_meta'; rel_path=$canonical.paths_rel.run_meta; role='metadata'; direct_prompt=$false; prompt_note='metadata only; summarize before prompt' },
      [pscustomobject]@{ name='metrics'; rel_path=$canonical.paths_rel.metrics; role='primary_signal'; direct_prompt=$false; prompt_note='tabular metrics should be summarized first' },
      [pscustomobject]@{ name='events'; rel_path=$canonical.paths_rel.events; role='primary_signal'; direct_prompt=$eventsPromptReady; prompt_note=$eventsPromptNote },
      [pscustomobject]@{ name='procs'; rel_path=$canonical.paths_rel.procs_dir; role='supporting_signal'; direct_prompt=$false; prompt_note='per-snapshot process dumps should be summarized first' },
      [pscustomobject]@{ name='dmesg_before'; rel_path=$canonical.paths_rel.dmesg_before; role='context'; direct_prompt=$false; prompt_note='baseline log; not prompt-ready' },
      [pscustomobject]@{ name='dmesg_after'; rel_path=$canonical.paths_rel.dmesg_after; role='supporting_signal'; direct_prompt=$false; prompt_note='kernel log can contain long noisy history' },
      [pscustomobject]@{ name='hilog_full'; rel_path=$canonical.paths_rel.hilog_full; role='long_context'; direct_prompt=$false; prompt_note='full hilog is too long for direct prompt use' },
      [pscustomobject]@{ name='fault_inject'; rel_path=$canonical.paths_rel.fault_inject; role='ground_truth'; direct_prompt=$faultInjectPromptReady; prompt_note=$faultInjectPromptNote },
      [pscustomobject]@{ name='faultlog_new'; rel_path=$canonical.paths_rel.faultlog_new_dir; role='run_local_faultlog'; direct_prompt=$false; prompt_note='faultlog should be summarized before prompting' },
      [pscustomobject]@{ name='faultlog_all'; rel_path=$canonical.paths_rel.faultlog_all_dir; role='historical_noise'; direct_prompt=$false; prompt_note='historical noise; never direct prompt' }
    )
  }
  if ($netSummary -and $netSummary.exists) {
    $manifest.files += [pscustomobject]@{ name='net'; rel_path=$canonical.paths_rel.net_dir; role='network_snapshot'; direct_prompt=$false; prompt_note='network snapshots should be summarized before prompting' }
  }

  Write-JsonUtf8 -Object $canonical -Path (Join-Path $OutputRoot 'canonical_case.json') -Depth 14
  Write-JsonUtf8 -Object @{ case_id = $canonical.case_id; quality_flags = @($qualityFlags) } -Path (Join-Path $OutputRoot 'quality_flags.json') -Depth 6
  Write-JsonUtf8 -Object $manifest -Path (Join-Path $OutputRoot 'source_manifest.json') -Depth 8

  $evidencePath = Join-Path $OutputRoot 'evidence_candidates.jsonl'
  if (Test-PathSafe $evidencePath) { Remove-Item -LiteralPath $evidencePath -Force }
  foreach ($e in @($evidence)) {
    $json = ConvertTo-Json -InputObject $e -Depth 6 -Compress
    Add-Content -LiteralPath $evidencePath -Value $json -Encoding UTF8
  }

  $diagInput = Build-DiagnosisInputText -Canonical $canonical -Evidence $evidence
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText((Join-Path $OutputRoot 'training_views\diagnosis_input.txt'), $diagInput, $utf8NoBom)

  Write-Host ("Exported dataset artifacts to: {0}" -f $OutputRoot) -ForegroundColor Green
} finally {
  if ($resolved -and $resolved.cleanup_dir) {
    try { Remove-Item -LiteralPath $resolved.cleanup_dir -Recurse -Force -ErrorAction SilentlyContinue } catch {}
  }
}
