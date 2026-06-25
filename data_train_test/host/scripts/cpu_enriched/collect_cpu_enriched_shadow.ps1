# Shadow-only CPU enriched collector for KaiHongOS / OpenHarmony boards.
# This script is intentionally additive: it does not modify existing collection
# scripts, accepted data, adapters, ledgers, labels, or active contracts.
# requires -Version 5.1

param(
  [Parameter(Mandatory=$false)][string]$SN = "",
  [Parameter(Mandatory=$false)][string]$Target = "",
  [Parameter(Mandatory=$false)][string]$OutDir = "",
  [Parameter(Mandatory=$false)][string]$RunId = "",
  [Parameter(Mandatory=$false)][int]$Samples = 3,
  [Parameter(Mandatory=$false)][int]$IntervalSec = 1,
  [Parameter(Mandatory=$false)][int]$TopN = 5,
  [Parameter(Mandatory=$false)][int]$MaxThreadsPerPid = 5,
  [Parameter(Mandatory=$false)][string]$ReportsDir = "",
  [Parameter(Mandatory=$false)][switch]$UseBoardSnapshot,
  [Parameter(Mandatory=$false)][string]$BoardSnapshotDir = "/data/local/tmp"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch {}

if ($PSScriptRoot) { $ScriptDir = $PSScriptRoot }
else { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)

$HdcTargetHelperPath = Join-Path $ProjectRoot "tools\hdc_target.ps1"
if (Test-Path -LiteralPath $HdcTargetHelperPath) {
  . $HdcTargetHelperPath
}

function Resolve-ShadowHdcTarget {
  param([string]$SN, [string]$Target)
  $candidate = ""
  if (-not [string]::IsNullOrWhiteSpace($SN)) { $candidate = $SN.Trim() }
  elseif (-not [string]::IsNullOrWhiteSpace($Target)) { $candidate = $Target.Trim() }
  elseif (-not [string]::IsNullOrWhiteSpace($env:HDC_TARGET)) { $candidate = $env:HDC_TARGET.Trim() }
  elseif (-not [string]::IsNullOrWhiteSpace($env:WK_DEVICE_TARGET)) { $candidate = $env:WK_DEVICE_TARGET.Trim() }
  if (Get-Command Resolve-HdcTarget -ErrorAction SilentlyContinue) {
    return (Resolve-HdcTarget -Target $candidate)
  }
  if ([string]::IsNullOrWhiteSpace($candidate)) { return "192.168.3.28:8711" }
  return $candidate
}

function Invoke-ShadowHdcShell {
  param(
    [string]$ResolvedTarget,
    [string]$RemoteCommand,
    [string]$Label,
    [int]$SampleIndex,
    [string]$RunId,
    [string]$Phase
  )
  $started = (Get-Date).ToUniversalTime()
  $stdout = (& hdc -t $ResolvedTarget shell $RemoteCommand 2>&1 | Out-String)
  $exitCode = $LASTEXITCODE
  $ended = (Get-Date).ToUniversalTime()
  $unsafeToken = $false
  if ($RemoteCommand -match '(^|[;&| ]+)awk([;&| ]+|$)' -or $RemoteCommand -match '(^|[;&| ]+)tr([;&| ]+|$)') {
    $unsafeToken = $true
  }
  return [ordered]@{
    schema_version = "goal3av_shadow_raw_command_sample_v1"
    run_id = $RunId
    phase = $Phase
    sample_index = $SampleIndex
    command_label = $Label
    board_command = $RemoteCommand
    started_at = $started.ToString("o")
    ended_at = $ended.ToString("o")
    duration_ms = [int](($ended - $started).TotalMilliseconds)
    exit_code = $exitCode
    stdout = $stdout
    stderr = ""
    used_awk_tr = $unsafeToken
    shadow_only = $true
    not_for_training = $true
    not_accepted = $true
    active_contract = $false
    production_baseline = $false
  }
}

function Write-JsonLine {
  param([string]$Path, [object]$Object)
  $json = $Object | ConvertTo-Json -Depth 20 -Compress
  Add-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Test-SafeBoardSnapshotDir {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  if ($Path -ne "/data/local/tmp" -and $Path -notmatch '^/data/local/tmp/') { return $false }
  $rest = $Path.Substring("/data/local/tmp".Length).Trim("/")
  if ([string]::IsNullOrWhiteSpace($rest)) { return $true }
  foreach ($component in ($rest -split "/")) {
    if ([string]::IsNullOrWhiteSpace($component)) { return $false }
    if ($component -match '^\.+$') { return $false }
    if ($component -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*$') { return $false }
  }
  return $true
}

function Convert-BoardSnapshotToRows {
  param(
    [string]$SnapshotText,
    [int]$SampleIndex,
    [string]$RunId,
    [string]$Phase,
    [string]$RemotePath,
    [string]$LocalPath,
    [datetime]$StartedAt,
    [datetime]$EndedAt
  )
  $rows = New-Object System.Collections.ArrayList
  $currentLabel = $null
  $currentPid = $null
  $buffer = New-Object System.Collections.ArrayList
  foreach ($line in ($SnapshotText -split "`r?`n")) {
    if ($line -match '^__GOAL3AY_BEGIN__\s+(\S+)(?:\s+pid=([0-9]+))?') {
      $currentLabel = $Matches[1]
      if ($Matches.Count -ge 3) { $currentPid = $Matches[2] } else { $currentPid = $null }
      $buffer.Clear()
      continue
    }
    if ($line -match '^__GOAL3AY_END__\s+(\S+)') {
      if ($null -ne $currentLabel) {
        $stdout = (($buffer | ForEach-Object { [string]$_ }) -join "`n")
        if ($stdout.Length -gt 0) { $stdout = $stdout + "`n" }
        $row = [ordered]@{
          schema_version = "goal3ay_shadow_raw_command_sample_v1"
          run_id = $RunId
          phase = $Phase
          sample_index = $SampleIndex
          command_label = $currentLabel
          board_command = ("board_snapshot:{0}" -f $currentLabel)
          started_at = $StartedAt.ToUniversalTime().ToString("o")
          ended_at = $EndedAt.ToUniversalTime().ToString("o")
          duration_ms = [int](($EndedAt - $StartedAt).TotalMilliseconds)
          exit_code = 0
          stdout = $stdout
          stderr = ""
          used_awk_tr = $false
          board_snapshot_mode = $true
          board_snapshot_remote_path = $RemotePath
          board_snapshot_local_path = $LocalPath
          shadow_only = $true
          not_for_training = $true
          not_accepted = $true
          active_contract = $false
          production_baseline = $false
        }
        if ($null -ne $currentPid -and $currentPid -match '^[0-9]+$') { $row["pid"] = [int]$currentPid }
        [void]$rows.Add([pscustomobject]$row)
      }
      $currentLabel = $null
      $currentPid = $null
      $buffer.Clear()
      continue
    }
    if ($null -ne $currentLabel) { [void]$buffer.Add($line) }
  }
  return @($rows)
}

function Invoke-ShadowHdcFileRecv {
  param(
    [string]$ResolvedTarget,
    [string]$RemotePath,
    [string]$LocalPath
  )
  $parent = Split-Path -Parent $LocalPath
  if (-not [string]::IsNullOrWhiteSpace($parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  $stdout = (& hdc -t $ResolvedTarget file recv $RemotePath $LocalPath 2>&1 | Out-String)
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    throw ("hdc file recv failed for {0}: {1}" -f $RemotePath, $stdout)
  }
}

function Invoke-BoardSnapshotCleanup {
  param(
    [string]$ResolvedTarget,
    [string]$RemotePath,
    [int]$SampleIndex,
    [string]$RunId,
    [string]$Phase,
    [string]$Label,
    [string]$LocalPath
  )
  $started = (Get-Date).ToUniversalTime()
  $remoteCommand = ("rm -f {0}; if [ -e {0} ]; then echo CLEANUP_FAILED; else echo CLEANUP_OK; fi" -f $RemotePath)
  $stdout = (& hdc -t $ResolvedTarget shell $remoteCommand 2>&1 | Out-String)
  $exitCode = $LASTEXITCODE
  $ended = (Get-Date).ToUniversalTime()
  $row = [ordered]@{
    schema_version = "goal3ay_shadow_raw_command_sample_v1"
    run_id = $RunId
    phase = $Phase
    sample_index = $SampleIndex
    command_label = $Label
    board_command = "board_snapshot_cleanup"
    started_at = $started.ToString("o")
    ended_at = $ended.ToString("o")
    duration_ms = [int](($ended - $started).TotalMilliseconds)
    exit_code = $exitCode
    stdout = $stdout
    stderr = ""
    used_awk_tr = $false
    board_snapshot_mode = $true
    board_snapshot_remote_path = $RemotePath
    board_snapshot_local_path = $LocalPath
    shadow_only = $true
    not_for_training = $true
    not_accepted = $true
    active_contract = $false
    production_baseline = $false
  }
  $row["cleanup_verified"] = ($exitCode -eq 0 -and $stdout -match 'CLEANUP_OK')
  return [pscustomobject]$row
}

function New-BoardSnapshotBaseCommand {
  param([string]$RemotePath)
  $parts = @()
  $parts += ("rm -f {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_SNAPSHOT_BEGIN__ > {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ shell_ok >> {0}" -f $RemotePath)
  $parts += ("echo GOAL3AY_SHELL_OK >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_END__ shell_ok >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ proc_loadavg >> {0}" -f $RemotePath)
  $parts += ("cat /proc/loadavg >> {0} 2>/dev/null" -f $RemotePath)
  $parts += ("echo __GOAL3AY_END__ proc_loadavg >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ proc_stat >> {0}" -f $RemotePath)
  $parts += ("cat /proc/stat >> {0} 2>/dev/null" -f $RemotePath)
  $parts += ("echo __GOAL3AY_END__ proc_stat >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ proc_pressure_cpu >> {0}" -f $RemotePath)
  $parts += ("if [ -r /proc/pressure/cpu ]; then cat /proc/pressure/cpu; else echo UNAVAILABLE; fi >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_END__ proc_pressure_cpu >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ ps_a >> {0}" -f $RemotePath)
  $parts += ("ps -A >> {0} 2>/dev/null || ps >> {0} 2>/dev/null" -f $RemotePath)
  $parts += ("echo __GOAL3AY_END__ ps_a >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ ps_t >> {0}" -f $RemotePath)
  $parts += ("ps -T >> {0} 2>/dev/null || echo UNSUPPORTED >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_END__ ps_t >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_BEGIN__ proc_pid_stat_all >> {0}" -f $RemotePath)
  $parts += ('for p in /proc/[0-9]*; do cat "$p/stat" 2>/dev/null; done >> ' + $RemotePath)
  $parts += ("echo __GOAL3AY_END__ proc_pid_stat_all >> {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_SNAPSHOT_END__ >> {0}" -f $RemotePath)
  return ($parts -join "; ")
}

function New-BoardSnapshotDetailCommand {
  param([string]$RemotePath, [int[]]$Pids, [int]$MaxThreadsPerPid)
  $parts = @()
  $parts += ("rm -f {0}" -f $RemotePath)
  $parts += ("echo __GOAL3AY_DETAIL_SNAPSHOT_BEGIN__ > {0}" -f $RemotePath)
  foreach ($procId in $Pids) {
    $pidText = [string]$procId
    $parts += ("echo __GOAL3AY_BEGIN__ proc_pid_status pid={0} >> {1}" -f $pidText, $RemotePath)
    $parts += ("cat /proc/{0}/status >> {1} 2>/dev/null || echo UNAVAILABLE >> {1}" -f $pidText, $RemotePath)
    $parts += ("echo __GOAL3AY_END__ proc_pid_status >> {0}" -f $RemotePath)
    $parts += ("echo __GOAL3AY_BEGIN__ proc_pid_stat pid={0} >> {1}" -f $pidText, $RemotePath)
    $parts += ("cat /proc/{0}/stat >> {1} 2>/dev/null || echo UNAVAILABLE >> {1}" -f $pidText, $RemotePath)
    $parts += ("echo __GOAL3AY_END__ proc_pid_stat >> {0}" -f $RemotePath)
    $parts += ("echo __GOAL3AY_BEGIN__ proc_pid_cpuset pid={0} >> {1}" -f $pidText, $RemotePath)
    $parts += ("cat /proc/{0}/cpuset >> {1} 2>/dev/null || echo UNAVAILABLE >> {1}" -f $pidText, $RemotePath)
    $parts += ("echo __GOAL3AY_END__ proc_pid_cpuset >> {0}" -f $RemotePath)
    $parts += ("echo __GOAL3AY_BEGIN__ proc_pid_task_list pid={0} >> {1}" -f $pidText, $RemotePath)
    $parts += ("ls /proc/{0}/task >> {1} 2>/dev/null || echo UNAVAILABLE >> {1}" -f $pidText, $RemotePath)
    $parts += ("echo __GOAL3AY_END__ proc_pid_task_list >> {0}" -f $RemotePath)
    $parts += ("echo __GOAL3AY_BEGIN__ proc_pid_task_stat pid={0} >> {1}" -f $pidText, $RemotePath)
    $parts += ('i=0; for t in /proc/' + $pidText + '/task/*; do cat "$t/stat" 2>/dev/null; i=$((i+1)); [ "$i" -ge ' + $MaxThreadsPerPid + ' ] && break; done >> ' + $RemotePath)
    $parts += ("echo __GOAL3AY_END__ proc_pid_task_stat >> {0}" -f $RemotePath)
  }
  $parts += ("echo __GOAL3AY_DETAIL_SNAPSHOT_END__ >> {0}" -f $RemotePath)
  return ($parts -join "; ")
}

function Invoke-BoardSnapshotSample {
  param(
    [string]$ResolvedTarget,
    [string]$RemotePath,
    [string]$LocalPath,
    [int]$SampleIndex,
    [string]$RunId,
    [string]$Phase
  )
  $started = (Get-Date).ToUniversalTime()
  $remoteCommand = New-BoardSnapshotBaseCommand -RemotePath $RemotePath
  $stdout = (& hdc -t $ResolvedTarget shell $remoteCommand 2>&1 | Out-String)
  $exitCode = $LASTEXITCODE
  $ended = (Get-Date).ToUniversalTime()
  if ($exitCode -ne 0) { throw ("board snapshot command failed: {0}" -f $stdout) }
  Invoke-ShadowHdcFileRecv -ResolvedTarget $ResolvedTarget -RemotePath $RemotePath -LocalPath $LocalPath
  $text = Get-Content -LiteralPath $LocalPath -Raw -Encoding UTF8
  $rows = @(Convert-BoardSnapshotToRows -SnapshotText $text -SampleIndex $SampleIndex -RunId $RunId -Phase $Phase -RemotePath $RemotePath -LocalPath $LocalPath -StartedAt $started -EndedAt $ended)
  $cleanup = Invoke-BoardSnapshotCleanup -ResolvedTarget $ResolvedTarget -RemotePath $RemotePath -SampleIndex $SampleIndex -RunId $RunId -Phase $Phase -Label "board_snapshot_cleanup" -LocalPath $LocalPath
  return @($rows + $cleanup)
}

function Invoke-BoardSnapshotDetails {
  param(
    [string]$ResolvedTarget,
    [string]$RemotePath,
    [string]$LocalPath,
    [int[]]$Pids,
    [int]$SampleIndex,
    [string]$RunId,
    [string]$Phase,
    [int]$MaxThreadsPerPid
  )
  if ($Pids.Count -eq 0) { return @() }
  $started = (Get-Date).ToUniversalTime()
  $remoteCommand = New-BoardSnapshotDetailCommand -RemotePath $RemotePath -Pids $Pids -MaxThreadsPerPid $MaxThreadsPerPid
  $stdout = (& hdc -t $ResolvedTarget shell $remoteCommand 2>&1 | Out-String)
  $exitCode = $LASTEXITCODE
  $ended = (Get-Date).ToUniversalTime()
  if ($exitCode -ne 0) { throw ("board detail snapshot command failed: {0}" -f $stdout) }
  Invoke-ShadowHdcFileRecv -ResolvedTarget $ResolvedTarget -RemotePath $RemotePath -LocalPath $LocalPath
  $text = Get-Content -LiteralPath $LocalPath -Raw -Encoding UTF8
  $rows = @(Convert-BoardSnapshotToRows -SnapshotText $text -SampleIndex $SampleIndex -RunId $RunId -Phase $Phase -RemotePath $RemotePath -LocalPath $LocalPath -StartedAt $started -EndedAt $ended)
  $cleanup = Invoke-BoardSnapshotCleanup -ResolvedTarget $ResolvedTarget -RemotePath $RemotePath -SampleIndex $SampleIndex -RunId $RunId -Phase $Phase -Label "board_detail_snapshot_cleanup" -LocalPath $LocalPath
  return @($rows + $cleanup)
}

function Parse-ProcPidStatLine {
  param([string]$Line)
  if ([string]::IsNullOrWhiteSpace($Line)) { return $null }
  $left = $Line.IndexOf("(")
  $right = $Line.LastIndexOf(")")
  if ($left -lt 1 -or $right -le $left) { return $null }
  $pidText = $Line.Substring(0, $left).Trim()
  $rest = $Line.Substring($right + 1).Trim() -split "\s+"
  if ($rest.Count -lt 13) { return $null }
  $parsedPid = 0
  [void][int]::TryParse($pidText, [ref]$parsedPid)
  $utime = [int64]0
  $stime = [int64]0
  [void][int64]::TryParse($rest[11], [ref]$utime)
  [void][int64]::TryParse($rest[12], [ref]$stime)
  return [ordered]@{ pid = $parsedPid; cpu_ticks = ($utime + $stime) }
}

function Select-TopPidCandidates {
  param([object[]]$RawRows, [int]$TopN)
  $pidStats = @{}
  foreach ($row in $RawRows) {
    if ($row.command_label -ne "proc_pid_stat_all") { continue }
    $sampleIndex = [int]$row.sample_index
    $lines = ($row.stdout -split "`r?`n")
    foreach ($line in $lines) {
      $parsed = Parse-ProcPidStatLine -Line $line
      if ($null -eq $parsed -or $parsed.pid -le 0) { continue }
      $key = [string]$parsed.pid
      if (-not $pidStats.ContainsKey($key)) { $pidStats[$key] = @{} }
      $pidStats[$key][$sampleIndex] = [int64]$parsed.cpu_ticks
    }
  }
  $scores = @()
  foreach ($key in $pidStats.Keys) {
    $samplesForPid = $pidStats[$key]
    $indices = @($samplesForPid.Keys | Sort-Object)
    if ($indices.Count -eq 0) { continue }
    $first = [int64]$samplesForPid[$indices[0]]
    $last = [int64]$samplesForPid[$indices[$indices.Count - 1]]
    $delta = $last - $first
    if ($delta -lt 0) { $delta = 0 }
    $scores += [pscustomobject]@{ pid = [int]$key; delta = [int64]$delta }
  }
  $selected = @($scores | Sort-Object -Property @{Expression="delta";Descending=$true}, @{Expression="pid";Descending=$false} | Select-Object -First $TopN)
  if ($selected.Count -eq 0) {
    foreach ($row in $RawRows) {
      if ($row.command_label -ne "proc_pid_stat_all") { continue }
      foreach ($line in ($row.stdout -split "`r?`n")) {
        $parsed = Parse-ProcPidStatLine -Line $line
        if ($null -ne $parsed -and $parsed.pid -gt 0) {
          $selected += [pscustomobject]@{ pid = [int]$parsed.pid; delta = [int64]0 }
          if ($selected.Count -ge $TopN) { break }
        }
      }
      if ($selected.Count -ge $TopN) { break }
    }
  }
  return @($selected | ForEach-Object { $_.pid })
}

if ($Samples -lt 2) { throw "Samples must be at least 2 for CPU delta parsing" }
if ($Samples -gt 5) { throw "Samples must be <= 5 for shadow dry-run overhead control" }
if ($IntervalSec -lt 1 -or $IntervalSec -gt 2) { throw "IntervalSec must be 1 or 2" }
if ($TopN -lt 1 -or $TopN -gt 8) { throw "TopN must be between 1 and 8" }
if ($MaxThreadsPerPid -lt 1 -or $MaxThreadsPerPid -gt 8) { throw "MaxThreadsPerPid must be between 1 and 8" }

$ResolvedTarget = Resolve-ShadowHdcTarget -SN $SN -Target $Target
if ([string]::IsNullOrWhiteSpace($RunId)) {
  $RunId = "goal3av_shadow_" + (Get-Date -Format "yyyyMMdd_HHmmss")
}
if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $ProjectRoot (Join-Path ".codex-harness\reports" $RunId)
}
if ([string]::IsNullOrWhiteSpace($ReportsDir)) {
  $ReportsDir = Join-Path $ProjectRoot "manual_experiments\qwen3_training_sweep_net_only_20260531"
}

$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$ReportsDir = [System.IO.Path]::GetFullPath($ReportsDir)
$CpuDir = Join-Path $OutDir "cpu_enriched"
New-Item -ItemType Directory -Force -Path $CpuDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null

$RawSamplesPath = Join-Path $ReportsDir "goal3av_shadow_raw_command_samples.jsonl"
if (Test-Path -LiteralPath $RawSamplesPath) { Remove-Item -LiteralPath $RawSamplesPath -Force }
$SnapshotDir = Join-Path $ReportsDir "board_snapshots"
if ($UseBoardSnapshot) { New-Item -ItemType Directory -Force -Path $SnapshotDir | Out-Null }

$phase = "shadow_cpu_enriched_dry_run"
$rawRows = New-Object System.Collections.ArrayList
$commands = @(
  @{ label = "shell_ok"; command = "echo GOAL3AV_SHELL_OK" },
  @{ label = "proc_loadavg"; command = "cat /proc/loadavg" },
  @{ label = "proc_stat"; command = "cat /proc/stat" },
  @{ label = "proc_pressure_cpu"; command = "if [ -r /proc/pressure/cpu ]; then cat /proc/pressure/cpu; else echo UNAVAILABLE; fi" },
  @{ label = "ps_a"; command = "ps -A 2>/dev/null || ps 2>/dev/null" },
  @{ label = "ps_t"; command = "ps -T 2>/dev/null || echo UNSUPPORTED" },
  @{ label = "proc_pid_stat_all"; command = 'for p in /proc/[0-9]*; do cat "$p/stat" 2>/dev/null; done' }
)

$startedAll = (Get-Date).ToUniversalTime()
if ($UseBoardSnapshot) {
  $safeRunId = ($RunId -replace '[^A-Za-z0-9_.-]', '_')
  $safeSnapshotDir = $BoardSnapshotDir.TrimEnd("/")
  if (-not (Test-SafeBoardSnapshotDir -Path $safeSnapshotDir)) {
    throw "BoardSnapshotDir must be under /data/local/tmp and contain only / A-Z a-z 0-9 _ . -"
  }
  for ($i = 0; $i -lt $Samples; $i++) {
    $remotePath = ("{0}/goal3ay_{1}_sample_{2}.txt" -f $safeSnapshotDir, $safeRunId, $i)
    $localPath = Join-Path $SnapshotDir ("goal3ay_{0}_sample_{1}.txt" -f $safeRunId, $i)
    $snapshotRows = Invoke-BoardSnapshotSample -ResolvedTarget $ResolvedTarget -RemotePath $remotePath -LocalPath $localPath -SampleIndex $i -RunId $RunId -Phase $phase
    foreach ($row in $snapshotRows) {
      [void]$rawRows.Add([pscustomobject]$row)
      Write-JsonLine -Path $RawSamplesPath -Object $row
    }
    if ($i -lt ($Samples - 1)) { Start-Sleep -Seconds $IntervalSec }
  }
  $selectedPids = Select-TopPidCandidates -RawRows @($rawRows) -TopN $TopN
  $detailRemotePath = ("{0}/goal3ay_{1}_detail.txt" -f $safeSnapshotDir, $safeRunId)
  $detailLocalPath = Join-Path $SnapshotDir ("goal3ay_{0}_detail.txt" -f $safeRunId)
  $detailRows = Invoke-BoardSnapshotDetails -ResolvedTarget $ResolvedTarget -RemotePath $detailRemotePath -LocalPath $detailLocalPath -Pids @($selectedPids) -SampleIndex $Samples -RunId $RunId -Phase $phase -MaxThreadsPerPid $MaxThreadsPerPid
  foreach ($row in $detailRows) {
    [void]$rawRows.Add([pscustomobject]$row)
    Write-JsonLine -Path $RawSamplesPath -Object $row
  }
} else {
  for ($i = 0; $i -lt $Samples; $i++) {
    foreach ($entry in $commands) {
      $row = Invoke-ShadowHdcShell -ResolvedTarget $ResolvedTarget -RemoteCommand $entry.command -Label $entry.label -SampleIndex $i -RunId $RunId -Phase $phase
      [void]$rawRows.Add([pscustomobject]$row)
      Write-JsonLine -Path $RawSamplesPath -Object $row
    }
    if ($i -lt ($Samples - 1)) { Start-Sleep -Seconds $IntervalSec }
  }

  $selectedPids = Select-TopPidCandidates -RawRows @($rawRows) -TopN $TopN
  foreach ($procId in $selectedPids) {
    $pidText = [string]$procId
    $detailCommands = @(
      @{ label = "proc_pid_status"; command = "cat /proc/$pidText/status 2>/dev/null || echo UNAVAILABLE"; pid = $procId },
      @{ label = "proc_pid_stat"; command = "cat /proc/$pidText/stat 2>/dev/null || echo UNAVAILABLE"; pid = $procId },
      @{ label = "proc_pid_cpuset"; command = "cat /proc/$pidText/cpuset 2>/dev/null || echo UNAVAILABLE"; pid = $procId },
      @{ label = "proc_pid_task_list"; command = "ls /proc/$pidText/task 2>/dev/null || echo UNAVAILABLE"; pid = $procId },
      @{ label = "proc_pid_task_stat"; command = "i=0; for t in /proc/$pidText/task/*; do cat `"`$t/stat`" 2>/dev/null; i=`$((i+1)); [ `"`$i`" -ge $MaxThreadsPerPid ] && break; done"; pid = $procId }
    )
    foreach ($entry in $detailCommands) {
      $row = Invoke-ShadowHdcShell -ResolvedTarget $ResolvedTarget -RemoteCommand $entry.command -Label $entry.label -SampleIndex $Samples -RunId $RunId -Phase $phase
      $row["pid"] = $entry.pid
      [void]$rawRows.Add([pscustomobject]$row)
      Write-JsonLine -Path $RawSamplesPath -Object $row
    }
  }
}
$endedAll = (Get-Date).ToUniversalTime()

$manifestPath = Join-Path $ReportsDir "goal3av_shadow_dry_run_manifest.json"
$manifest = [ordered]@{
  schema_version = "goal3av_shadow_dry_run_manifest_v1"
  created_at = (Get-Date).ToUniversalTime().ToString("o")
  run_id = $RunId
  selected_target = $ResolvedTarget
  samples = $Samples
  interval_sec = $IntervalSec
  top_n = $TopN
  max_threads_per_pid = $MaxThreadsPerPid
  started_at = $startedAll.ToString("o")
  ended_at = $endedAll.ToString("o")
  approximate_runtime_ms = [int](($endedAll - $startedAll).TotalMilliseconds)
  out_dir = $OutDir
  cpu_enriched_dir = $CpuDir
  reports_dir = $ReportsDir
  raw_command_samples = $RawSamplesPath
  selected_pids = @($selectedPids)
  use_board_snapshot = [bool]$UseBoardSnapshot
  board_snapshot_dir = $BoardSnapshotDir
  board_snapshot_local_dir = if ($UseBoardSnapshot) { $SnapshotDir } else { $null }
  hdc_small_read_reduction = if ($UseBoardSnapshot) { "sample commands collapsed into board snapshot shell plus file recv" } else { "disabled" }
  board_collection_executed = $true
  formal_batch_collection = $false
  fault_injection = $false
  daemon_started_or_stopped = $false
  shadow_only = $true
  not_for_training = $true
  not_accepted = $true
  active_contract = $false
  production_baseline = $false
}
$manifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

$parserPath = Join-Path $ScriptDir "parse_cpu_enriched_shadow.py"
if (-not (Test-Path -LiteralPath $parserPath)) {
  throw "Missing parser: $parserPath"
}

$pythonCandidates = @("py", "python")
$pythonExe = $null
foreach ($candidate in $pythonCandidates) {
  $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
  if ($cmd) { $pythonExe = $candidate; break }
}
if ($null -eq $pythonExe) { throw "Python executable not found" }

& $pythonExe -B $parserPath `
  --raw-samples $RawSamplesPath `
  --out-dir $CpuDir `
  --reports-dir $ReportsDir `
  --run-id $RunId `
  --manifest $manifestPath `
  --target $ResolvedTarget `
  --sample-count $Samples `
  --interval-sec $IntervalSec `
  --top-n $TopN
if ($LASTEXITCODE -ne 0) {
  throw "cpu_enriched parser failed with exit code $LASTEXITCODE"
}

Write-Host ("GOAL3AV shadow cpu_enriched dry-run complete: {0}" -f $CpuDir)
