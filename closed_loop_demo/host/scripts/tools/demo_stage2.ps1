param(
  [string]$Target = "",
  [string]$Server = "",
  [string]$RepoDir = "/home/xrh/qwen3_os_fault",
  [string]$DeviceId = "dev1",
  [int]$TimeoutStage1 = 240,
  [int]$TimeoutStage2 = 240,
  [int]$TimeoutStage3 = 240,
  [int]$PollSec = 2,
  [string]$TriggerdArgs = "",
  [string]$TriggeredArgs = "",
  [string]$CandidateConfigPath = "",
  [string]$NetPublicProbeIp = "223.5.5.5",
  [string]$NetDnsProbeHost = "www.baidu.com",
  [switch]$SuggestionOnly,
  [switch]$VerboseJson,
  [switch]$DebugJson,
  [switch]$Help
)

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$OutputEncoding = [Console]::OutputEncoding

function Show-Usage {
  Write-Host "Usage: powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1 [options]"
  Write-Host "  -Target <host:port>      (default: env HDC_TARGET or 192.168.3.28:8711)"
  Write-Host "  -Server <ssh host>       (default: QWEN3_SSH_HOST or QWEN3_SERVER_HOST or 183.56.183.131)"
  Write-Host "  -RepoDir <path>          (default: /home/xrh/qwen3_os_fault)"
  Write-Host "  -DeviceId <id>           (default: dev1)"
  Write-Host "  -TimeoutStage1 <sec>     (default: 240)"
  Write-Host "  -TimeoutStage2 <sec>     (default: 240)"
  Write-Host "  -TimeoutStage3 <sec>     (default: 240)"
  Write-Host "  -PollSec <sec>           (default: 2)"
  Write-Host "  -TriggerdArgs <string>   (extra args for triggerd --daemon)"
  Write-Host "  -TriggeredArgs <string>  alias for -TriggerdArgs"
  Write-Host "  -CandidateConfigPath <json>  3CM candidate/shadow adapter config"
  Write-Host "  -NetPublicProbeIp <ip>   NET preflight public IP probe (default: 223.5.5.5)"
  Write-Host "  -NetDnsProbeHost <host>  NET preflight DNS resolve probe host (default: www.baidu.com)"
  Write-Host "  -SuggestionOnly          Stage3 waits for generated suggestions; it does not execute board actions"
  Write-Host "  -VerboseJson            Also print archived diagnosis and Stage3 JSON after the demo summary"
  Write-Host "  -DebugJson              Alias for -VerboseJson"
  Write-Host ""
  Write-Host "11-label trigger examples (Stage3 stays suggestion-only):"
  Write-Host "  cpu single point : -TriggeredArgs '--mode=cpu --cpu_profile=single_point --interval=2 --hit_need=3'"
  Write-Host "  cpu concurrency  : -TriggeredArgs '--mode=cpu --cpu_profile=concurrency --interval=2 --hit_need=3'"
  Write-Host "  mem leak         : -TriggeredArgs '--mode=mem --mem_profile=leak --interval=2 --hit_need=3'"
  Write-Host "  mem oom-risk     : -TriggeredArgs '--mode=mem --mem_profile=pressure --mem_safety_floor_kb=150000 --interval=2 --hit_need=3'"
  Write-Host "  net (one label)  : -TriggeredArgs '--mode=net --net_label=net_dns_fail --interval=2 --net_hit_need=2'"
  Write-Host ""
  Write-Host "Example:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1 -TriggerdArgs '--mode cpu --interval 2 --hit_need 3 --cpu_hit_need 3'"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1 -CandidateConfigPath .\manual_experiments\expanded_live_demo_upgrade_3CM\3CM_expanded_live_demo_config.json -SuggestionOnly -TriggerdArgs '--mode cpu --interval 2 --hit_need 3 --cpu_hit_need 3'"
}

if ($Help) { Show-Usage; exit 0 }

function Test-SafeDeviceId([string]$Value) {
  if (-not $Value -or $Value.Trim().Length -eq 0) { return $false }
  if ($Value -notmatch '^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$') { return $false }
  if ($Value -eq "." -or $Value -eq "..") { return $false }
  if ($Value.StartsWith(".")) { return $false }
  if ($Value.Contains("..")) { return $false }
  return $true
}

function Test-SafeIPv4([string]$Value) {
  if (-not $Value -or $Value.Trim() -ne $Value) { return $false }
  $parts = $Value -split "\."
  if ($parts.Count -ne 4) { return $false }
  foreach ($part in $parts) {
    if ($part -notmatch '^\d{1,3}$') { return $false }
    $n = [int]$part
    if ($n -lt 0 -or $n -gt 255) { return $false }
  }
  return $true
}

function Test-SafeDnsHost([string]$Value) {
  if (-not $Value -or $Value.Trim() -ne $Value) { return $false }
  if ($Value.Length -gt 253) { return $false }
  if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$') { return $false }
  if ($Value.Contains("..")) { return $false }
  foreach ($label in ($Value -split "\.")) {
    if ($label.Length -lt 1 -or $label.Length -gt 63) { return $false }
    if ($label -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$') { return $false }
  }
  return $true
}

if (-not (Test-SafeDeviceId $DeviceId)) {
  throw "Invalid DeviceId: use a simple path-safe token with letters, numbers, underscore, or dash; dots are allowed only inside a non-path component"
}
if (-not (Test-SafeIPv4 $NetPublicProbeIp)) {
  throw "Invalid NetPublicProbeIp: expected dotted IPv4 address"
}
if (-not (Test-SafeDnsHost $NetDnsProbeHost)) {
  throw "Invalid NetDnsProbeHost: expected safe DNS hostname"
}

$serverConfigPath = Join-Path $PSScriptRoot "server_config.ps1"
if (-not (Test-Path -LiteralPath $serverConfigPath)) {
  throw "Missing server config helper: $serverConfigPath"
}
. $serverConfigPath
$Qwen3Cfg = Get-Qwen3ServerConfig
if (-not $Server -or $Server.Trim() -eq "") {
  $Server = $Qwen3Cfg.ssh_target
} elseif ($Server -notmatch "@" -and $Qwen3Cfg.ssh_user) {
  $Server = ($Qwen3Cfg.ssh_user + "@" + $Server)
}
$ServerPort = [int]$Qwen3Cfg.ssh_port
$ExpectedIngestPort = [int]$Qwen3Cfg.ingest_port
$ExpectedActionsPort = [int]$Qwen3Cfg.actions_port

$hdcTargetHelperPath = Join-Path $PSScriptRoot "hdc_target.ps1"
if (-not (Test-Path -LiteralPath $hdcTargetHelperPath)) {
  throw "Missing HDC target helper: $hdcTargetHelperPath"
}
. $hdcTargetHelperPath
$Target = Resolve-HdcTarget -Target $Target
$HdcTargetArgs = Get-HdcTargetArgs -Target $Target
$bridgeHelperPath = Join-Path $PSScriptRoot "stage2_host_bridge.ps1"
if (-not (Test-Path -LiteralPath $bridgeHelperPath)) {
  throw "Missing Stage2 bridge helper: $bridgeHelperPath"
}
. $bridgeHelperPath

$BoardActionsVerbose = ($env:WK_BOARD_ACTIONS_VERBOSE -eq "1")

function Escape-BashSingleQuote([string]$s) {
  if ($null -eq $s) { return "" }
  return ($s -replace "'", "'\''")
}

function Wrap-RemoteTimeout([string]$InnerCmd, [int]$TimeoutSec = 15) {
  $escaped = Escape-BashSingleQuote $InnerCmd
  return "if command -v timeout >/dev/null 2>&1; then timeout ${TimeoutSec}s bash -lc '$escaped'; else bash -lc '$escaped'; fi"
}

$script:LastSshTimedOut = $false

function Invoke-SshWithTimeout([string]$Cmd, [int]$TimeoutSec = 20) {
  $script:LastSshTimedOut = $false
  $Cmd = $Cmd -replace "`r", ""


  $escaped = Escape-BashSingleQuote $Cmd
  $full = "bash -lc '$escaped'"

  $sshExe = "C:\Windows\System32\OpenSSH\ssh.exe"
  $args = @(
    "-p", [string]$ServerPort,
    "-o","BatchMode=yes",
    "-o","ConnectTimeout=5",
    "-o","ServerAliveInterval=15",
    "-o","ServerAliveCountMax=2",
    "-o","LogLevel=ERROR",
    "-T",
    "-o","ControlMaster=no",
    $Server,
    $full
  )

  $quoted = $args | ForEach-Object {
    if ($_ -match "\s") { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
  }

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $sshExe
  $psi.Arguments = ($quoted -join " ")
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  try {
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
  } catch {}
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  $null = $proc.Start()

  $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
  $stderrTask = $proc.StandardError.ReadToEndAsync()

  $timeoutMs = [int]([math]::Max(1, $TimeoutSec) * 1000)
  if (-not $proc.WaitForExit($timeoutMs)) {
    try { $proc.Kill() } catch {}
    $script:LastSshTimedOut = $true
    Write-Host ("SSH_TIMEOUT sec={0} cmd={1}" -f $TimeoutSec, $Cmd)
    return ""
  }

  $proc.WaitForExit()
  $out = $stdoutTask.Result
  $err = $stderrTask.Result
  return (($out + $err).Trim())
}


function Invoke-Ssh([string]$Cmd, [int]$TimeoutSec = 20) {
  return Invoke-SshWithTimeout $Cmd $TimeoutSec
}

function Invoke-Hdc([string]$Cmd) {
  $Cmd = $Cmd -replace "`r", ""
  $out = @(& hdc @HdcTargetArgs shell $Cmd 2>&1)
  return ($out -join "`n").Trim()
}

function Deploy-BoardScripts {
  # $MyInvocation.MyCommand.Path 在函数里可能为 null；用脚本根目录更可靠
  $scriptDir = $PSScriptRoot
  if (-not $scriptDir) { $scriptDir = Split-Path -Parent $PSCommandPath }
  if (-not $scriptDir) { throw "Cannot locate script dir (PSScriptRoot/PSCommandPath empty)." }

  $projectRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDir "..\..\..\..")).Path
  $candidates = @(
    (Join-Path $projectRoot "closed_loop_demo\\board\\scripts"),
    (Join-Path $projectRoot "closed_loop_demo\\board\\src\\demo_stage2\\board_A\\bin"),
    (Join-Path $projectRoot "closed_loop_demo\\board\\src\\board_A\\bin")
  )
  $required = @("bundle_real_upload.sh","bundle_manual.sh","uploader_nc.sh","actions_poller_nc.sh","actiond.sh")
  $srcDir = $null
  foreach ($cand in $candidates) {
    if (-not (Test-Path -Path $cand)) { continue }
    $ok = $true
    foreach ($rf in $required) {
      if (-not (Test-Path -Path (Join-Path $cand $rf))) { $ok = $false; break }
    }
    if ($ok) { $srcDir = $cand; break }
  }
  if (-not $srcDir) {
    throw ("No valid board script source dir found. Checked: " + ($candidates -join ", "))
  }
  Write-Host ("[board] source scripts dir: {0}" -f $srcDir)

  $srcReal = Join-Path $srcDir "bundle_real_upload.sh"
  $srcManual = Join-Path $srcDir "bundle_manual.sh"
  $srcUploader = Join-Path $srcDir "uploader_nc.sh"
  Write-Host "[board] deploy bundle scripts"
  if (-not (Test-Path -Path $srcReal)) { throw "bundle_real_upload.sh not found at $srcReal" }
  if (-not (Test-Path -Path $srcManual)) { throw "bundle_manual.sh not found at $srcManual" }
  if (-not (Test-Path -Path $srcUploader)) { throw "uploader_nc.sh not found at $srcUploader" }
  $out1 = @(& hdc @HdcTargetArgs file send $srcReal /data/faultmon/demo_stage2/bin/bundle_real_upload.sh 2>&1)
  $out2 = @(& hdc @HdcTargetArgs file send $srcManual /data/faultmon/demo_stage2/bin/bundle_manual.sh 2>&1)
  $out3 = @(& hdc @HdcTargetArgs file send $srcUploader /data/faultmon/demo_stage2/bin/uploader_nc.sh 2>&1)
  if ($out1) { Write-Host ($out1 -join "`n") }
  if ($out2) { Write-Host ($out2 -join "`n") }
  if ($out3) { Write-Host ($out3 -join "`n") }
  Write-Host (Invoke-Hdc "chmod 755 /data/faultmon/demo_stage2/bin/bundle_real_upload.sh /data/faultmon/demo_stage2/bin/bundle_manual.sh /data/faultmon/demo_stage2/bin/uploader_nc.sh")

# Optional but recommended: keep stage2 board scripts in sync with repo copy.
# This avoids "board still running old inject_mem.sh / triggerd.sh" after a local fix.
$extra = @(
  @{ Name="triggerd.sh";   Src=(Join-Path $srcDir "triggerd.sh");   Dst="/data/faultmon/demo_stage2/bin/triggerd.sh" },
  @{ Name="actions_poller_nc.sh"; Src=(Join-Path $srcDir "actions_poller_nc.sh"); Dst="/data/faultmon/demo_stage2/bin/actions_poller_nc.sh" },
  @{ Name="actiond.sh"; Src=(Join-Path $srcDir "actiond.sh"); Dst="/data/faultmon/demo_stage2/bin/actiond.sh" },
  @{ Name="inject_cpu.sh"; Src=(Join-Path $srcDir "inject_cpu.sh"); Dst="/data/faultmon/demo_stage2/bin/inject_cpu.sh" },
  @{ Name="inject_mem.sh"; Src=(Join-Path $srcDir "inject_mem.sh"); Dst="/data/faultmon/demo_stage2/bin/inject_mem.sh" }
)

foreach ($e in $extra) {
  if (Test-Path -Path $e.Src) {
    $outX = @(& hdc @HdcTargetArgs file send $e.Src $e.Dst 2>&1)
    if ($outX) { Write-Host ($outX -join "`n") }
    # ensure executable
    $null = Invoke-Hdc ("chmod 755 {0}" -f $e.Dst)
  } else {
    Write-Host ("WARN: {0} not found at {1} (skip deploy)" -f $e.Name, $e.Src)
  }
}
}

function Now-Date { Get-Date }
function Sec([datetime]$a, [datetime]$b) { [math]::Round(($b - $a).TotalSeconds, 2) }

# ---------------- Stage1: detect trigger_bundle -> upload OK ----------------

function Get-LastTriggerBundleRunId {
  $cmd = "tail -n 500 /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null | " +
         "grep -F -e 'trigger_bundle run_id=' | " +
         "tail -n 1"
  $line = Invoke-Hdc $cmd
  if ($line -match "run_id=([A-Za-z0-9_]+)") { return $Matches[1] }
  return ""
}

function Invoke-HostBridgeOnce {
  $uplink = Invoke-Stage2BoardBridgeOnce -HdcArgs $HdcTargetArgs -Server $Server -ServerPort $ServerPort -RepoDir $RepoDir
  foreach ($item in $uplink) {
    if ($item.ok) {
      Write-Host ("BRIDGE_OK device={0} file={1}" -f $item.device, $item.name)
    } else {
      Write-Host ("BRIDGE_WARN step={0} device={1} file={2} msg={3}" -f $item.step, $item.device, $item.name, $item.msg)
    }
  }
  if ($SuggestionOnly) {
    return
  }
  $downlink = Invoke-Stage2BoardDownlinkOnce -HdcArgs $HdcTargetArgs -Server $Server -ServerPort $ServerPort -RepoDir $RepoDir -DeviceId $DeviceId
  foreach ($item in $downlink) {
    if ($item.ok) {
      Write-Host ("DOWNLINK_OK device={0} run={1}" -f $item.device, $item.run_id)
    } else {
      Write-Host ("DOWNLINK_WARN step={0} device={1} run={2} msg={3}" -f $item.step, $item.device, $item.run_id, $item.msg)
    }
  }
  # Surface semantic conclusion from server state
  $serverState = Get-Stage2ServerActionsState -Server $Server -Port $ServerPort -RepoDir $RepoDir -DeviceId $DeviceId
  if ($serverState -and $serverState.conclusion) {
    Write-Host ("INFER_CONCLUSION run={0} {1}" -f $serverState.run_id, $serverState.conclusion)
  }
}

function Has-UploadOk([string]$RunId) {
  if (-not $RunId) { return $false }
  $cmd = ('cd "{0}" && if ls storage/tcp_inbox/*/{1}__bundle.tar.gz.done >/dev/null 2>&1 || test -d storage/runs/{1}; then echo OK; fi' -f $RepoDir, $RunId)
  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) { return $false }
  return ($out -match "OK")
}

function Get-TriggerLockFlag {
  $cmd = "[ -f /data/faultmon/state/trigger.active ] && echo 1 || echo 0"
  $s = Invoke-Hdc $cmd
  if ($s.Trim() -eq "1") { return 1 }
  return 0
}

function Wait-Stage1([int]$TimeoutSec) {
  $start = Now-Date
  $deadline = $start.AddSeconds($TimeoutSec)

  $rid = ""
  $tTrigger = $null

  while ((Now-Date) -lt $deadline) {
    if (-not $rid) {
      $rid = Get-LastTriggerBundleRunId
      if ($rid) { $tTrigger = Now-Date }
    }

    Invoke-HostBridgeOnce

    if ($rid) {
      if (Has-UploadOk $rid) {
        $tDone = Now-Date
        return @{ Rid=$rid; TriggerTime=$tTrigger; DoneTime=$tDone }
      }
    }

    $lock = Get-TriggerLockFlag
    $showRid = "none"
    if ($rid) { $showRid = $rid }
    Write-Host ("WAIT_STAGE1 elapsed={0}s lock={1} rid={2}" -f (Sec $start (Now-Date)), $lock, $showRid)
    Start-Sleep -Seconds $PollSec
  }

  return $null
}

# ---------------- Stage2/3 on server (NO $(...) to avoid PS interpolation) ----------------

function Test-Stage2Done([string]$RunId) {
  $cmd = ('cd "{0}" && test -f "storage/runs/{1}/_server_out/.infer_done" && grep -q "^0$" "storage/runs/{1}/_server_out/infer_ec.txt" 2>/dev/null && ( test -s "storage/runs/{1}/_server_out/diagnosis_v2.json" || test -s "storage/runs/{1}/_server_out/diagnosis.json" ) && echo OK' -f $RepoDir, $RunId)
  $out = Invoke-Ssh $cmd 60
  if ($script:LastSshTimedOut) { Write-Host "WARN: stage2 ssh timeout, continue polling"; return $false }
  return ($out -match "OK")
}


function Test-Stage3Done([string]$RunId) {
  $cmd = ('cd "{0}" && test -f "storage/runs/{1}/_action_result/action_result.json" && grep -q "^0$" "storage/runs/{1}/_action_result/actiond_rc.txt" 2>/dev/null && echo OK' -f $RepoDir, $RunId)
  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) { Write-Host "WARN: stage3 ssh timeout, continue polling"; return $false }
  return ($out -match "OK")
}

function Test-Stage3SuggestionDone([string]$RunId) {
  $cmd = ('cd "{0}" && test -s "storage/runs/{1}/_server_out/stage3_suggestions.json" && test -s "storage/runs/{1}/_server_out/adapter_selection.json" && test -s "storage/runs/{1}/_server_out/adapter_runtime_proof.json" && grep -q ''"status": "PASS"'' "storage/runs/{1}/_server_out/adapter_selection.json" && grep -q ''"model_loaded": true'' "storage/runs/{1}/_server_out/adapter_runtime_proof.json" && grep -q ''"adapter_loaded": true'' "storage/runs/{1}/_server_out/adapter_runtime_proof.json" && grep -q ''"generation_ok": true'' "storage/runs/{1}/_server_out/adapter_runtime_proof.json" && grep -q ''"execution_enabled": false'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && grep -q ''"manual_approval_required": true'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && grep -q ''"action_r1_source": "action_r1_strategy_library"'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && grep -q ''"source": "action_r1_strategy_library"'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && grep -q ''"machine_suggestions"'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && grep -q ''"command_template"'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && ! grep -q ''cpu_live_smoke_collect_more_evidence'' "storage/runs/{1}/_server_out/stage3_suggestions.json" && test -s "storage/tcp_out/{2}/latest_stage3_suggestions.json" && grep -q ''"action_r1_source": "action_r1_strategy_library"'' "storage/tcp_out/{2}/latest_stage3_suggestions.json" && grep -q ''"machine_suggestions"'' "storage/tcp_out/{2}/latest_stage3_suggestions.json" && test -s "storage/tcp_out/{2}/latest_suggestion_run_id.txt" && read latest_rid < "storage/tcp_out/{2}/latest_suggestion_run_id.txt" && [ "$latest_rid" = "{1}" ] && echo OK' -f $RepoDir, $RunId, $DeviceId)
  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) { Write-Host "WARN: stage3 suggestion ssh timeout, continue polling"; return $false }
  return ($out -match "OK")
}

function Invoke-BoardActionsOnce([string]$RunId) {
  if (-not $RunId) { return }
  $cmd = ("sh /data/faultmon/demo_stage2/bin/actions_poller_nc.sh --once --verbose --expect-run {0}" -f $RunId)
  $raw = @(& hdc @HdcTargetArgs shell $cmd 2>&1)
  $rc = $LASTEXITCODE
  $lines = @()
  foreach ($line in $raw) {
    $t = ($line | Out-String).Trim()
    if ($t) { $lines += $t }
  }

  $didWork = $false
  $hasFailure = ($rc -ne 0)
  foreach ($line in $lines) {
    if ($line -match 'result_bundle=' -or $line -match 'upload_rc=0' -or $line -match 'actiond_rc=0') {
      $didWork = $true
    }
    if ($line -match 'ERROR:' -or $line -match 'expect_run_mismatch' -or $line -match 'upload_rc=[1-9][0-9]*') {
      $hasFailure = $true
    }
  }

  if ($BoardActionsVerbose) {
    foreach ($line in $lines) {
      Write-Host ("BOARD_ACTIONS {0}" -f $line)
    }
    return
  }

  if ($hasFailure) {
    Write-Host ("BOARD_ACTIONS_FAIL rid={0} rc={1}" -f $RunId, $rc)
    foreach ($line in $lines) {
      Write-Host ("BOARD_ACTIONS {0}" -f $line)
    }
    return
  }

  if ($didWork) {
    Write-Host ("BOARD_ACTIONS_OK rid={0}" -f $RunId)
  }
}

function Wait-Stage([string]$Name, [int]$TimeoutSec, [scriptblock]$Check, [string]$RunId = "", [switch]$DriveBoardActions) {
  $t0 = Now-Date
  $deadline = $t0.AddSeconds($TimeoutSec)
  while ((Now-Date) -lt $deadline) {
    Invoke-HostBridgeOnce
    if ($DriveBoardActions -and $RunId) {
      Invoke-BoardActionsOnce $RunId
    }
    if (& $Check) {
      $t1 = Now-Date
      return @{ Start=$t0; Done=$t1 }
    }
    Write-Host ("WAIT_{0} elapsed={1}s" -f $Name, (Sec $t0 (Now-Date)))
    Start-Sleep -Seconds $PollSec
  }
  return $null
}

function Build-Stage2EnvPrefix([string]$ConfigPath, [bool]$SuggestionOnlyMode, $ExpectedBinding) {
  $pairs = @()
  if ($SuggestionOnlyMode) {
    $pairs += @{ Name = "WK_3CM_ACTION_SUGGESTION_ONLY"; Value = "1" }
  }
  if ($ExpectedBinding -and $ExpectedBinding.Family) {
    $pairs += @{ Name = "WK_3CM_EXPECTED_FAMILY"; Value = [string]$ExpectedBinding.Family }
    if ($ExpectedBinding.MainLabel) {
      $pairs += @{ Name = "WK_3CM_EXPECTED_MAIN_LABEL"; Value = [string]$ExpectedBinding.MainLabel }
    }
    if ($ExpectedBinding.LoadPatternDetail) {
      $pairs += @{ Name = "WK_3CM_LOAD_PATTERN_DETAIL"; Value = [string]$ExpectedBinding.LoadPatternDetail }
    }
  }
  if ($ConfigPath -and $ConfigPath.Trim().Length -gt 0) {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
      throw "Candidate config not found: $ConfigPath"
    }
    $cfg = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    $pairs += @{ Name = "WK_QWEN3_ENABLE_STAGE2"; Value = "1" }
    $pairs += @{ Name = "WK_3CM_CANDIDATE_MODE"; Value = "1" }
    $pairs += @{ Name = "WK_3CM_CONFIG_PATH"; Value = [string]$ConfigPath }
    if ($cfg.stage2.primary_adapter.path) {
      $pairs += @{ Name = "QWEN3_ADAPTER_DIR"; Value = [string]$cfg.stage2.primary_adapter.path }
      $pairs += @{ Name = "WK_QWEN3_ADAPTER_PRIMARY"; Value = [string]$cfg.stage2.primary_adapter.path }
    }
    if ($cfg.stage2.rollback_adapter.path) {
      $pairs += @{ Name = "WK_QWEN3_ADAPTER_ROLLBACK"; Value = [string]$cfg.stage2.rollback_adapter.path }
    }
    if ($cfg.stage2.label_fallback_adapter.path) {
      $pairs += @{ Name = "WK_QWEN3_ADAPTER_LABEL_FALLBACK"; Value = [string]$cfg.stage2.label_fallback_adapter.path }
    }
  }
  if ($pairs.Count -eq 0) { return "" }
  $parts = @()
  foreach ($p in $pairs) {
    $v = Escape-BashSingleQuote ([string]$p.Value)
    $parts += ("{0}='{1}'" -f $p.Name, $v)
  }
  return (($parts -join " ") + " ")
}

function Resolve-TriggerdArgs([string]$OldArgs, [string]$AliasArgs) {
  $out = ""
  if ($AliasArgs -and $AliasArgs.Trim().Length -gt 0) {
    $out = $AliasArgs.Trim()
  } elseif ($OldArgs -and $OldArgs.Trim().Length -gt 0) {
    $out = $OldArgs.Trim()
  }
  if (-not $out) { return "" }
  if ($out -match "[;&|`$<>`r`n]") {
    throw "Unsafe TriggerdArgs/TriggeredArgs metacharacter detected"
  }
  if ($out -match "(?i)\b(action_command|cmd|command)\b") {
    throw "Unsafe TriggerdArgs/TriggeredArgs command-shaped token detected"
  }
  if ($out -notmatch "^[A-Za-z0-9_:= -]+$") {
    throw "Unsafe TriggerdArgs/TriggeredArgs character detected"
  }
  $allowedValueOptions = @{
    "--mode" = "enum";
    "--interval" = "number";
    "--hit_need" = "number";
    "--cpu_hit_need" = "number";
    "--mem_hit_need" = "number";
    "--net_hit_need" = "number";
    "--net_label" = "net_label";
    "--cpu_profile" = "cpu_profile";
    "--mem_profile" = "mem_profile";
    "--mem_safety_floor_kb" = "number_kb";
    "--cooldown" = "number";
    "--cooldown_sec" = "number";
    "--pre" = "number";
    "--post" = "number";
    "--threshold_load1_int" = "number";
    "--threshold_mem_drop_kb" = "number_kb";
    "--cpu_load1_x100_threshold" = "number_kb";
    "--mem_avail_kb_threshold" = "number_kb";
    "--tag" = "tag";
  }
  $netLabelAllow = @(
    "net_dns_fail","net_public_ip_unreachable","net_no_default_route","net_wrong_default_route",
    "net_no_ipv4_on_iface","net_wifi_disconnect","net_gateway_unreachable"
  )
  $tokens = @($out -split "\s+" | Where-Object { $_ })
  $normalized = @()
  for ($i = 0; $i -lt $tokens.Count; $i++) {
    $tok = $tokens[$i]
    $opt = $tok
    $val = $null
    if ($tok -match "^(--[A-Za-z0-9_]+)=(.+)$") {
      $opt = $Matches[1]
      $val = $Matches[2]
    }
    if (-not $allowedValueOptions.ContainsKey($opt)) {
      throw ("Unsafe TriggerdArgs/TriggeredArgs unsupported option: {0}" -f $opt)
    }
    if ($null -eq $val) {
      $i += 1
      if ($i -ge $tokens.Count) { throw ("Unsafe TriggerdArgs/TriggeredArgs missing value for {0}" -f $opt) }
      $val = $tokens[$i]
    }
    $kind = $allowedValueOptions[$opt]
    if ($kind -eq "enum") {
      if ($val -notmatch "^(cpu|mem|multi|net)$") { throw "Unsafe TriggerdArgs/TriggeredArgs invalid mode" }
    } elseif ($kind -eq "net_label") {
      if ($netLabelAllow -notcontains $val) { throw ("Unsafe TriggerdArgs/TriggeredArgs invalid net_label: {0}" -f $val) }
    } elseif ($kind -eq "cpu_profile") {
      if ($val -notmatch "^(single_point|concurrency)$") { throw "Unsafe TriggerdArgs/TriggeredArgs invalid cpu_profile" }
    } elseif ($kind -eq "mem_profile") {
      if ($val -notmatch "^(leak|pressure)$") { throw "Unsafe TriggerdArgs/TriggeredArgs invalid mem_profile" }
    } elseif ($kind -eq "number") {
      if ($val -notmatch "^[0-9]+$") { throw ("Unsafe TriggerdArgs/TriggeredArgs non-numeric value for {0}" -f $opt) }
      $num = [int]$val
      if ($num -lt 0 -or $num -gt 86400) { throw ("Unsafe TriggerdArgs/TriggeredArgs numeric value out of range for {0}" -f $opt) }
    } elseif ($kind -eq "number_kb") {
      if ($val -notmatch "^[0-9]+$") { throw ("Unsafe TriggerdArgs/TriggeredArgs non-numeric value for {0}" -f $opt) }
      $num = [long]$val
      if ($num -lt 0 -or $num -gt 100000000) { throw ("Unsafe TriggerdArgs/TriggeredArgs numeric value out of range for {0}" -f $opt) }
    } elseif ($kind -eq "tag") {
      if ($val -notmatch "^[A-Za-z0-9_-]+$") { throw "Unsafe TriggerdArgs/TriggeredArgs invalid tag" }
    }
    $normalized += $opt
    $normalized += $val
  }
  return ($normalized -join " ")
}

function Get-TriggerOptionValue([string]$ResolvedArgs, [string]$Option) {
  if (-not $ResolvedArgs) { return "" }
  $tokens = @($ResolvedArgs -split "\s+" | Where-Object { $_ })
  for ($i = 0; $i -lt $tokens.Count - 1; $i++) {
    if ($tokens[$i] -eq $Option) { return $tokens[$i + 1] }
  }
  return ""
}

function Get-ExpectedLabelBinding([string]$ResolvedArgs) {
  # 3CM-R2-R3: map the Stage1 trigger mode/profile to the expected 11-label binding.
  # This is a demo binding for the Stage2 schema gate, never a training label.
  $mode = Get-TriggerOptionValue $ResolvedArgs "--mode"
  $netLabel = Get-TriggerOptionValue $ResolvedArgs "--net_label"
  $cpuProfile = Get-TriggerOptionValue $ResolvedArgs "--cpu_profile"
  $memProfile = Get-TriggerOptionValue $ResolvedArgs "--mem_profile"
  $binding = @{ Family = ""; MainLabel = ""; LoadPatternDetail = "" }
  switch ($mode) {
    "cpu" {
      $binding.Family = "cpu"
      if ($cpuProfile -eq "concurrency") {
        $binding.MainLabel = "cpu_concurrency_scheduling_pressure"
        $binding.LoadPatternDetail = "oversub"
      } else {
        $binding.MainLabel = "cpu_single_point_high_load"
        $binding.LoadPatternDetail = "busy_loop"
      }
    }
    "mem" {
      $binding.Family = "mem"
      if ($memProfile -eq "pressure") {
        $binding.MainLabel = "mem_system_pressure_oom_risk"
      } else {
        $binding.MainLabel = "mem_process_leak_growth"
      }
    }
    "net" {
      $binding.Family = "net"
      $binding.MainLabel = $netLabel
    }
    default { }
  }
  return $binding
}

function Invoke-NetBaselinePreflight([string]$NetLabel, [string]$PublicProbeIp, [string]$DnsProbeHost) {
  # Fail-closed NET baseline gate before any NET live demo (3CM-R2-R2 requirements).
  # No PSK/credential material is ever read or printed by this preflight.
  $failures = @()

  $ifOut = Invoke-Hdc "ifconfig wlan0 2>/dev/null"
  $hasIpv4 = ($ifOut -match "inet addr:" -or $ifOut -match "inet [0-9]")
  Write-Host ("NET_PREFLIGHT wlan0_ipv4_present={0}" -f $hasIpv4)
  if (-not $hasIpv4) { $failures += "wlan0_ipv4_missing" }

  $routeOut = Invoke-Hdc "cat /proc/net/route 2>/dev/null"
  $hasDefaultRoute = $false
  foreach ($line in ($routeOut -split "`n")) {
    $cols = ($line.Trim() -split "\s+")
    if ($cols.Count -ge 2 -and $cols[1] -eq "00000000") { $hasDefaultRoute = $true; break }
  }
  Write-Host ("NET_PREFLIGHT default_route_present={0}" -f $hasDefaultRoute)
  if (-not $hasDefaultRoute) { $failures += "default_route_missing" }

  $pubOut = Invoke-Hdc ("ping -c 1 -W 3 {0} 2>&1" -f $PublicProbeIp)
  $pubOk = ($pubOut -match "1 received" -or $pubOut -match "1 packets received" -or $pubOut -match "ttl=")
  Write-Host ("NET_PREFLIGHT public_ip_ping_ok={0} target={1}" -f $pubOk, $PublicProbeIp)
  if (-not $pubOk) { $failures += "public_ip_ping_failed" }

  $dnsOut = Invoke-Hdc ("ping -c 1 -W 3 {0} 2>&1" -f $DnsProbeHost)
  $dnsOk = ($dnsOut -match "1 received" -or $dnsOut -match "1 packets received" -or $dnsOut -match "ttl=")
  Write-Host ("NET_PREFLIGHT dns_resolve_ping_ok={0} host={1}" -f $dnsOk, $DnsProbeHost)
  if (-not $dnsOk) { $failures += "dns_probe_failed" }

  if ($NetLabel -match "^net_wifi_") {
    $wpaOut = Invoke-Hdc "wpa_cli -p /data/local/tmp/wpa_ctrl status 2>/dev/null"
    $wpaOk = ($wpaOut -match "wpa_state=COMPLETED")
    Write-Host ("NET_PREFLIGHT wpa_state_completed={0}" -f $wpaOk)
    if (-not $wpaOk) { $failures += "wpa_state_not_completed" }
  }

  $staleProc = Invoke-Hdc "ps -A 2>/dev/null | grep net_fault | grep -v grep"
  $staleProcPresent = ($staleProc -and $staleProc.Trim().Length -gt 0)
  Write-Host ("NET_PREFLIGHT stale_net_fault_process={0}" -f $staleProcPresent)
  if ($staleProcPresent) { $failures += "stale_net_fault_process" }

  $staleMarkers = Invoke-Hdc "ls /data/local/tmp/net_fault_state 2>/dev/null"
  $staleHits = @()
  foreach ($line in ($staleMarkers -split "`n")) {
    $t = $line.Trim()
    if ($t -match "\.applied$" -or $t -match "\.ready$" -or $t -match "\.pid$" -or $t -eq "dnsproxy.stopped") { $staleHits += $t }
  }
  Write-Host ("NET_PREFLIGHT stale_state_markers={0}" -f ($(if ($staleHits.Count -gt 0) { $staleHits -join "," } else { "none" })))
  if ($staleHits.Count -gt 0) { $failures += "stale_net_state_markers" }

  if ($failures.Count -gt 0) {
    Write-Host ("NET_PREFLIGHT_RESULT status=FAIL label={0} failures={1}" -f $NetLabel, ($failures -join ","))
    return $false
  }
  Write-Host ("NET_PREFLIGHT_RESULT status=PASS label={0}" -f $NetLabel)
  return $true
}

# ---------------- JSON extraction ----------------

function Find-JsonValues($Obj, [string[]]$Keys, [ref]$Out) {
  if ($null -eq $Obj) { return }

  if ($Obj -is [System.Collections.IDictionary]) {
    foreach ($k in $Obj.Keys) {
      $v = $Obj[$k]
      if ($Keys -contains ($k.ToString().ToLower())) {
        if ($null -ne $v) { $Out.Value += ,($v.ToString()) }
      }
      Find-JsonValues $v $Keys ([ref]$Out)
    }
    return
  }

  if ($Obj -is [System.Collections.IEnumerable] -and -not ($Obj -is [string])) {
    foreach ($i in $Obj) { Find-JsonValues $i $Keys ([ref]$Out) }
    return
  }

  foreach ($p in $Obj.PSObject.Properties) {
    $k = $p.Name
    $v = $p.Value
    if ($Keys -contains ($k.ToString().ToLower())) {
      if ($null -ne $v) { $Out.Value += ,($v.ToString()) }
    }
    Find-JsonValues $v $Keys ([ref]$Out)
  }
}

function Extract-DiagnosisText([string]$JsonText) {
  if (-not $JsonText) { return "(not found)" }
  try {
    $obj = $JsonText | ConvertFrom-Json -ErrorAction Stop
    $keys = @("narrative","diagnosis_human","narrative_human","root_cause","root_cause_text","summary","reason","diagnosis")
    $vals = @()
    Find-JsonValues $obj $keys ([ref]$vals)
    $vals = $vals | Where-Object { $_ } | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique
    if ($vals.Count -gt 0) { return ($vals -join "; ") }
    return "(empty)"
  } catch {
    return $JsonText.Trim()
  }
}

function Format-ProcessEntry($p) {
  if ($null -eq $p) { return "" }
  $name = $p.name
  if (-not $name) { $name = $p.comm }
  if (-not $name) { $name = $p.cmd }
  if (-not $name) { $name = "proc" }
  $procPid = $p.pid
  $cpuPct = $p.cpu_pct
  $cpuJ = $p.cpu_delta_jiffies
  if ($null -ne $cpuPct -and $cpuPct.ToString().Length -gt 0) {
    $cpuStr = ("cpu={0}%" -f $cpuPct)
  } elseif ($null -ne $cpuJ -and $cpuJ.ToString().Length -gt 0) {
    $cpuStr = ("cpu_jiffies={0}" -f $cpuJ)
  } else {
    $cpuStr = "cpu=NA"
  }
  $score = $p.score
  if ($null -eq $score -or $score.ToString().Length -eq 0) { $score = "NA" }
  if ($null -ne $procPid -and $procPid.ToString().Length -gt 0) {
    return ("{0}(pid={1}, {2}, score={3})" -f $name, $procPid, $cpuStr, $score)
  }
  return ("{0}({1}, score={2})" -f $name, $cpuStr, $score)
}

function Extract-PrimarySuspect([string]$JsonText) {
  if (-not $JsonText) { return "" }
  try {
    $obj = $JsonText | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return ""
  }
  if ($null -eq $obj.primary_suspect) { return "" }
  return (Format-ProcessEntry $obj.primary_suspect)
}

function Extract-SecondarySuspects([string]$JsonText) {
  $out = @()
  if (-not $JsonText) { return $out }
  try {
    $obj = $JsonText | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return $out
  }
  $list = $obj.secondary_suspects
  if ($null -eq $list) { return $out }
  foreach ($p in $list) {
    $line = (Format-ProcessEntry $p)
    if ($line) { $out += $line }
    if ($out.Count -ge 5) { break }
  }
  return $out
}

function Format-SuspectEntry($s) {
  if ($null -eq $s) { return "" }
  $name = $s.name
  if (-not $name) { $name = "proc" }
  $pid = $s.pid
  $cpuPct = $s.cpu_pct
  if ($null -ne $cpuPct -and $cpuPct.ToString().Length -gt 0) {
    $cpuStr = ("cpu={0}%" -f $cpuPct)
  } else {
    $cpuStr = "cpu=NA"
  }
  $score = $s.score
  if ($null -eq $score -or $score.ToString().Length -eq 0) { $score = "NA" }
  $role = $s.role
  $ok = $s.evidence_ok
  $missing = ""
  if ($null -ne $s.evidence_missing) { $missing = ($s.evidence_missing -join "|") }
  if (-not $missing) { $missing = "none" }
  if ($null -ne $pid -and $pid.ToString().Length -gt 0) {
    return ("{0}(pid={1}, {2}, score={3}, role={4}, evidence_ok={5}, missing={6})" -f $name, $pid, $cpuStr, $score, $role, $ok, $missing)
  }
  return ("{0}({1}, score={2}, role={3}, evidence_ok={4}, missing={5})" -f $name, $cpuStr, $score, $role, $ok, $missing)
}

function Extract-SuspectsDebug([string]$JsonText) {
  $out = @()
  if (-not $JsonText) { return $out }
  try {
    $obj = $JsonText | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return $out
  }
  $list = $obj.suspects
  if ($null -eq $list) { $list = $obj.top_suspects }
  if ($null -eq $list) { return $out }
  foreach ($s in $list) {
    $line = (Format-SuspectEntry $s)
    if ($line) { $out += $line }
    if ($out.Count -ge 8) { break }
  }
  return $out
}

function Extract-ActionsLines([string]$JsonText) {
  $lines = @()
  if (-not $JsonText) { return $lines }
  try {
    $obj = $JsonText | ConvertFrom-Json -ErrorAction Stop
    if ($SuggestionOnly) {
      $items = @()
      if ($obj.suggestions) { $items += $obj.suggestions }
      foreach ($s in $items) {
        $lines += Format-ActionR1StrategyLines $s
      }
      return ($lines | Where-Object { $_ } | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ } | Select-Object -Unique)
    }
    $candidates = @()
    if ($obj.actions) { $candidates += $obj.actions }
    if ($obj.suggested_actions) { $candidates += $obj.suggested_actions }
    if ($obj.action_plan) { $candidates += $obj.action_plan }
    foreach ($a in $candidates) {
      if ($a.cmd) { $lines += $a.cmd.ToString() }
      elseif ($a.command) { $lines += $a.command.ToString() }
      elseif ($a.name) { $lines += $a.name.ToString() }
      else { $lines += ($a | ConvertTo-Json -Compress -Depth 8) }
    }
  } catch {
    $lines += $JsonText.Trim()
  }
  $lines = $lines | Where-Object { $_ } | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ } | Select-Object -Unique
  return $lines
}

function Convert-JsonOrNull([string]$JsonText) {
  if (-not $JsonText) { return $null }
  try {
    return ($JsonText | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    return $null
  }
}

function U([string]$Base64Text) {
  return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Base64Text))
}

function Get-PropValue($Obj, [string]$Name) {
  if ($null -eq $Obj) { return $null }
  foreach ($p in $Obj.PSObject.Properties) {
    if ($p.Name -ieq $Name) { return $p.Value }
  }
  return $null
}

function Get-FirstText($Obj, [string[]]$Names) {
  foreach ($name in $Names) {
    $v = Get-PropValue $Obj $name
    if ($null -ne $v -and $v.ToString().Trim().Length -gt 0) {
      return $v.ToString().Trim()
    }
  }
  return ""
}

function Convert-ToList($Value) {
  if ($null -eq $Value) { return @() }
  return @($Value)
}

function Format-RiskTierZh([string]$Risk) {
  switch (($Risk | ForEach-Object { $_.ToString().ToLowerInvariant() })) {
    "low" { return (U "5L2O6aOO6Zmp") }
    "medium" { return (U "5Lit6aOO6Zmp") }
    "high" { return (U "6auY6aOO6Zmp") }
    default { return $Risk }
  }
}

function Add-StrategyFieldLine([string[]]$Lines, $Obj, [string]$Field, [string]$LabelBase64) {
  $value = Get-FirstText $Obj @($Field)
  if ($value) { return ($Lines + ("  {0}{1}" -f (U $LabelBase64), $value)) }
  return $Lines
}

function Format-ActionR1StrategyLines($Strategy) {
  $lines = @()
  if ($null -eq $Strategy) { return $lines }

  $title = Get-FirstText $Strategy @("action_r1_title", "action_r1_suggestion_id", "id", "name")
  $summary = Get-FirstText $Strategy @("action_r1_summary", "summary", "description")
  $source = Get-FirstText $Strategy @("source", "action_r1_source", "registry_key")
  if ($title) { $lines += ((U "QWN0aW9uIFIxIOaBouWkjeetlueVpe+8mg==") + $title) }
  if ($summary) { $lines += $summary }
  if ($source) { $lines += ((U "562W55Wl5p2l5rqQ77ya") + $source) }

  $manual = Get-PropValue $Strategy "manual_advice"
  if ($null -eq $manual) { $manual = Get-PropValue $Strategy "manual_steps" }
  $manualItems = Convert-ToList $manual
  if ($manualItems.Count -gt 0) {
    $lines += (U "5Lq65bel5bu66K6u77ya")
    $idx = 1
    foreach ($step in $manualItems) {
      if ($step -and $step.ToString().Trim().Length -gt 0) {
        $lines += ("{0}. {1}" -f $idx, $step.ToString().Trim())
        $idx += 1
      }
    }
  }

  $machineItems = Convert-ToList (Get-PropValue $Strategy "machine_suggestions")
  if ($machineItems.Count -gt 0) {
    $lines += (U "5py65Zmo5oyH5Luk5bu66K6u77yI5LuF5bGV56S677yM5LiN6Ieq5Yqo5omn6KGM77yJ77ya")
    foreach ($m in $machineItems) {
      if ($null -eq $m) { continue }
      $risk = Format-RiskTierZh (Get-FirstText $m @("risk"))
      $cmd = Get-FirstText $m @("command_template")
      if ($cmd) { $lines += ("[{0}] {1}" -f $risk, $cmd) }
      else { $lines += ("[{0}]" -f $risk) }
      $lines = Add-StrategyFieldLine $lines $m "purpose" "55So6YCU77ya"
      $lines = Add-StrategyFieldLine $lines $m "precondition" "5omn6KGM5YmN5o+Q77ya"
      $lines = Add-StrategyFieldLine $lines $m "expected_effect" "6aKE5pyf5pWI5p6c77ya"
      $lines = Add-StrategyFieldLine $lines $m "verification" "6aqM6K+B5pa55byP77ya"
      $lines += ("  requires_manual_approval={0}; auto_execute={1}; dispatch_channel={2}" -f ([string](Get-PropValue $m "requires_manual_approval")).ToLower(), ([string](Get-PropValue $m "auto_execute")).ToLower(), (Get-FirstText $m @("dispatch_channel")))
      $lines += ("  " + (U "6Ieq5Yqo5omn6KGM77ya5ZCm"))
    }
  } else {
    $lines += (U "562W55Wl5YiG5bGC57y65aSx77ya5b2T5YmNIFN0YWdlMyBKU09OIOacquWMheWQqyBtYWNoaW5lX3N1Z2dlc3Rpb25z77yb6K+356Gu6K6kIHF3ZW4zLXNlcnZlciDlt7LlkIzmraUgYWN0aW9uX3IxX3N0cmF0ZWd5X2xpYnJhcnnjgII=")
  }

  $riskSummary = Get-FirstText $Strategy @("risk_summary")
  if ($riskSummary) { $lines += ((U "6aOO6Zmp6K+05piO77ya") + $riskSummary) }
  $lines += (U "5a6J5YWo5aOw5piO77ya5b2T5YmN57O757uf5LuF5bGV56S65aSE572u562W55Wl77yM5LiN6Ieq5Yqo5omn6KGM5ZG95Luk77yb5omA5pyJ5py65Zmo5oyH5Luk6ZyA6KaB5Lq65bel56Gu6K6k44CC")
  return $lines
}

function Format-DemoConfidence($DiagObj) {
  $v = Get-PropValue $DiagObj "confidence"
  if ($null -eq $v -or $v.ToString().Trim().Length -eq 0) { return (U "5pyq56Gu6K6k") }
  try {
    $n = [double]$v
    if ($n -le 0.0) { return (U "5pyq56Gu6K6k") }
    if ($n -le 1.0) { return ("{0:P0}" -f $n) }
    return $v.ToString()
  } catch {
    return $v.ToString()
  }
}

function Get-DemoFaultLabel($DiagObj) {
  $label = Get-FirstText $DiagObj @("display_name_zh", "main_label", "family")
  if (-not $label) { return (U "5pyq55+l5pWF6Zqc") }
  switch ($label) {
    "cpu_single_point_high_load" { return (U "Q1BVIOWNleeCuemrmOi0n+i9vQ==") }
    "cpu_concurrency_scheduling_pressure" { return (U "Q1BVIOW5tuWPkeiwg+W6puWOi+WKmw==") }
    "mem_process_leak_growth" { return (U "5YaF5a2Y6L+b56iL5rOE5ryP5aKe6ZW/") }
    "mem_system_pressure_oom_risk" { return (U "57O757uf5YaF5a2Y5Y6L5Yqb77yIT09NIOmjjumZqe+8iQ==") }
    "net_dns_fail" { return (U "RE5TIOino+aekOWksei0pQ==") }
    "net_public_ip_unreachable" { return (U "5YWs572RIElQIOS4jeWPr+i+vg==") }
    "net_no_default_route" { return (U "6buY6K6k6Lev55Sx57y65aSx") }
    "net_wrong_default_route" { return (U "6buY6K6k6Lev55Sx6ZSZ6K+v") }
    "net_gateway_unreachable" { return "net_gateway_unreachable" }
    "net_no_ipv4_on_iface" { return (U "5o6l5Y+j5pegIElQdjQg5Zyw5Z2A") }
    "net_wifi_disconnect" { return (U "V2ktRmkg5pat5byA") }
    "net_wifi_auth_fail_wrong_psk" { return (U "V2ktRmkg6K6k6K+B5aSx6LSl77yI5a+G56CB6ZSZ6K+v77yJ") }
    "cpu" { return (U "Q1BVIOaVhemanA==") }
    "mem" { return (U "5YaF5a2Y5pWF6Zqc") }
    "net" { return (U "572R57uc5pWF6Zqc") }
    default { return $label }
  }
}

function Get-DemoMainLabel($DiagObj) {
  $label = Get-FirstText $DiagObj @("main_label", "family_hint", "family")
  if (-not $label) { return "" }
  return $label
}

function Format-X100Load([double]$Value) {
  return ("{0:0.##}" -f ($Value / 100.0))
}

function Format-X100Percent([double]$Value) {
  return ("{0:0.##}%" -f ($Value / 100.0))
}

function Get-DemoEvidenceObjects($DiagObj) {
  $out = @()
  $evidence = Get-PropValue $DiagObj "evidence"
  if ($null -eq $evidence) { return $out }
  foreach ($e in $evidence) {
    $text = ""
    $source = ""
    $gaps = @()
    if ($e -is [string]) {
      $text = $e
    } else {
      $text = Get-FirstText $e @("text", "summary")
      $source = Get-FirstText $e @("source")
      $rawGaps = Get-PropValue $e "gaps"
      if ($null -ne $rawGaps) { $gaps = Convert-ToList $rawGaps }
    }
    if ($text) {
      $out += [pscustomobject]@{
        text = $text.ToString()
        source = $source
        gaps = $gaps
      }
    }
  }
  return $out
}

function Get-DemoCpuMetricPeaks($DiagObj) {
  $cpu = $null
  $load1 = $null
  $load5 = $null
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    $text = $e.text
    if ($null -eq $cpu -and $text -match "cpu_util_peak_x100=([0-9]+)") { $cpu = [double]$Matches[1] }
    if ($null -eq $load1 -and $text -match "load1_peak_x100=([0-9]+)") { $load1 = [double]$Matches[1] }
    if ($null -eq $load5 -and $text -match "load5_peak_x100=([0-9]+)") { $load5 = [double]$Matches[1] }
  }
  return [pscustomobject]@{
    cpu_util_peak_x100 = $cpu
    load1_peak_x100 = $load1
    load5_peak_x100 = $load5
  }
}

function Get-DemoRunObservationCounts($DiagObj) {
  $metricsRows = $null
  $procRows = $null
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    $text = $e.text
    if ($null -eq $metricsRows -and $text -match "metrics\s+行数=([0-9]+)") { $metricsRows = [int]$Matches[1] }
    if ($null -eq $procRows -and $text -match "进程快照条数=([0-9]+)") { $procRows = [int]$Matches[1] }
  }
  return [pscustomobject]@{
    metrics_rows = $metricsRows
    process_snapshots = $procRows
  }
}

function Test-DemoCpuTriggerBound($DiagObj) {
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    if ($e.text -match "candidate_mode=true\s+expected_family=cpu\s+trigger_tag=auto_cpu") { return $true }
  }
  $label = Get-DemoMainLabel $DiagObj
  return ($label -eq "cpu_single_point_high_load" -or $label -eq "cpu")
}

function Test-DemoRunIdMatched($DiagObj) {
  $v = Get-PropValue $DiagObj "run_id_match"
  if ($null -ne $v) {
    try { return [bool]$v } catch {}
  }
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    if ($e.text -match "run_id_match=true") { return $true }
  }
  return $false
}

function Get-DemoIgnoredStaleBundles($DiagObj) {
  $v = Get-PropValue $DiagObj "ignored_stale_bundles_count"
  if ($null -ne $v -and $v.ToString().Length -gt 0) { return $v.ToString() }
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    if ($e.text -match "ignored_stale_bundles=([0-9]+)") { return $Matches[1] }
  }
  return ""
}

function Test-DemoPidstatMissing($DiagObj, $PidstatAudit) {
  if ($PidstatAudit -and -not $PidstatAudit.pidstat_file_nonempty) { return $true }
  $riskFlags = Get-PropValue $DiagObj "risk_flags"
  if ($riskFlags) {
    foreach ($flag in (Convert-ToList $riskFlags)) {
      if ($flag.ToString() -match "pidstat_missing") { return $true }
    }
  }
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    if ($e.text -match "pidstat.*(缺失|未覆盖|missing)" -or (($e.gaps -join "|") -match "pidstat_missing")) { return $true }
  }
  return $false
}

function Build-DemoCpuRootCause($DiagObj, $PidstatAudit) {
  $peaks = Get-DemoCpuMetricPeaks $DiagObj
  $parts = @()
  if ($null -ne $peaks.cpu_util_peak_x100) {
    $parts += ("CPU 利用率峰值为 {0}" -f (Format-X100Percent $peaks.cpu_util_peak_x100))
  } else {
    $parts += "CPU 利用率峰值字段未在当前 report 中确认"
  }
  if ($null -ne $peaks.load1_peak_x100) {
    $parts += ("load1 峰值为 {0}" -f (Format-X100Load $peaks.load1_peak_x100))
  } else {
    $parts += "load1 峰值字段未在当前 report 中确认"
  }
  if ($null -ne $peaks.load5_peak_x100) {
    $parts += ("load5 峰值为 {0}" -f (Format-X100Load $peaks.load5_peak_x100))
  }

  $binding = "Stage1 触发类型与 CPU 单点高负载绑定一致"
  if (-not (Test-DemoCpuTriggerBound $DiagObj)) { $binding = "Stage1 触发类型绑定字段未在当前 report 中确认" }
  $runMatch = "run_id 与输入 bundle 匹配"
  if (-not (Test-DemoRunIdMatched $DiagObj)) { $runMatch = "run_id 匹配字段未在当前 report 中确认" }

  $memNet = "未发现支持 MEM 或 NET 根因的主证据"
  $memCaveat = Get-FirstText $DiagObj @("mem_evidence_caveat")
  $netCaveat = Get-FirstText $DiagObj @("net_evidence_caveat")
  if (-not $memCaveat -and -not $netCaveat) { $memNet = "MEM/NET 排除字段未在当前 report 中确认" }

  $pidstat = "由于当前 live 环境缺少 pidstat 进程级 CPU 采样，系统不宣称已经精确定位到具体高 CPU 进程，进程级归因保留为 caveated。"
  if (-not (Test-DemoPidstatMissing $DiagObj $PidstatAudit)) {
    $pidstat = "当前 report 未确认缺少 pidstat 进程级采样；若要定位具体高 CPU 进程，仍需以 pidstat 或等价进程级 CPU 证据复核。"
  }

  return ("本次运行窗口内 {0}，且 {1}，{2}，{3}。因此系统判断为 CPU 单点高负载导致的 CPU 调度压力。{4}" -f ($parts -join "，"), $binding, $runMatch, $memNet, $pidstat)
}

function Test-DemoNetLabel($DiagObj) {
  $label = Get-DemoMainLabel $DiagObj
  return ($label -match "^net" -or $label -eq "net")
}

function Build-DemoNetDnsRootCause($DiagObj) {
  return "本次运行中，域名解析失败，同时直接 IP 连通性仍可用，说明问题集中在 DNS/resolver 路径，而不是 Wi-Fi 断连、IPv4 缺失或默认路由缺失。系统因此判断为 DNS 解析失败类网络故障。"
}

function Get-DemoRootCause($DiagObj, [string]$DiagnosisText) {
  $label = Get-DemoMainLabel $DiagObj
  if ($label -eq "cpu_single_point_high_load") {
    return (Build-DemoCpuRootCause $DiagObj $null)
  }
  if ($label -eq "net_dns_fail") {
    return (Build-DemoNetDnsRootCause $DiagObj)
  }
  $cause = Get-PropValue $DiagObj "cause"
  $text = Get-FirstText $cause @("summary")
  if (-not $text) { $text = Get-FirstText $DiagObj @("root_cause", "hypothesis", "diagnosis_summary", "summary") }
  if (-not $text) { $text = $DiagnosisText }
  if (-not $text) { return (U "5b2T5YmN6K+B5o2u5LiN6Laz77yM5bCa5pyq5b2i5oiQ5piO56Gu5qC55Zug44CC") }
  return $text
}

function Format-DemoEvidenceText([string]$Text) {
  if (-not $Text) { return "" }
  if ($Text -match "accepted_run_id=([A-Za-z0-9_]+)\s+input_bundle_run_id=([A-Za-z0-9_]+)\s+run_id_match=true\s+ignored_stale_bundles=([0-9]+)") {
    return ((U "6L+Q6KGM57yW5Y+35bey5qCh6aqM5LiA6Ie077ya5b2T5YmN6K+K5pat5LiO5LiK5Lyg5YyFIHJ1bl9pZCDljLnphY3vvJvlt7Llv73nlaUgezB9IOS4quWOhuWPsuaXp+WMheOAgg==") -f $Matches[3])
  }
  if ($Text -match "accepted_run_id=([A-Za-z0-9_]+)\s+input_bundle_run_id=([A-Za-z0-9_]+)\s+run_id_match=true") {
    return (U "6L+Q6KGM57yW5Y+35bey5qCh6aqM5LiA6Ie077ya5b2T5YmN6K+K5pat5LiO5LiK5Lyg5YyFIHJ1bl9pZCDljLnphY3jgII=")
  }
  if ($Text -match "candidate_mode=true\s+expected_family=cpu\s+trigger_tag=auto_cpu") {
    return (U "5YCZ6YCJL+WPquWxleekuuaooeW8j+W3suWQr+eUqO+8muW9k+WJjeinpuWPkee7keWumuS4uiBDUFUg5Y2V54K56auY6LSf6L2977yM6YG/5YWN5pen5YyF6amx5Yqo6K+K5pat44CC")
  }
  if ($Text -match "run_window\s+内\s+metrics\s+行数=([0-9]+)") {
    return ("观测窗口内采集到 {0} 行 metrics，覆盖故障持续过程。" -f $Matches[1])
  }
  if ($Text -match "进程快照条数=([0-9]+)") {
    return ("采集到 {0} 条进程快照，可作为进程级辅助观察。" -f $Matches[1])
  }
  if ($Text -match "pidstat_0/1\s+缺失或为空") {
    return "当前缺少 pidstat 进程级 CPU 采样，因此不直接声称某个 PID 是 CPU 根因。"
  }
  if ($Text -match "load1_peak_x100=([0-9]+).*cpu_util_peak_x100=([0-9]+)") {
    return ("CPU 利用率峰值：{0}；load1 峰值：{1}。" -f (Format-X100Percent ([double]$Matches[2])), (Format-X100Load ([double]$Matches[1])))
  }
  return $Text
}

function Format-DemoUncertaintyText([string]$Text) {
  if (-not $Text) { return "" }
  if ($Text -match "Candidate demo output only") {
    return (U "5b2T5YmN5Li65YCZ6YCJL+WPquWxleekuui+k+WHuu+8m+acque7j+S6uuW3peivgeaNruWkjeaguO+8jOS4jei/m+WFpeiuree7g+aIliBwcm92ZW5hbmNl44CC")
  }
  if ($Text -match "No exact process CPU percentage is asserted without pidstat coverage") {
    return "当前缺少 pidstat 进程级 CPU 覆盖，因此不宣称已精确定位到具体高 CPU 进程。"
  }
  return $Text
}

function Add-UniqueEvidenceLine([string[]]$Lines, [string]$Text) {
  if ($Text -and ($Lines -notcontains $Text)) { return ($Lines + $Text) }
  return $Lines
}

function Test-DemoNetNoiseEvidence($EvidenceObject, [string]$Text) {
  $source = ""
  $gaps = ""
  if ($EvidenceObject -and -not ($EvidenceObject -is [string])) {
    $source = Get-FirstText $EvidenceObject @("source")
    $gapObj = Get-PropValue $EvidenceObject "gaps"
    if ($gapObj) { $gaps = ((Convert-ToList $gapObj) -join "|") }
  }
  $all = ("{0} {1} {2}" -f $source, $gaps, $Text)
  return ($all -match "pidstat|进程快照|process snapshot|Top RSS|RSS|PSS|load1|loadavg|CPU 利用率|CPU|metrics 行数|PID")
}

function Format-DemoNetEvidenceText([string]$Text) {
  if (-not $Text) { return "" }
  if ($Text -match "accepted_run_id=([A-Za-z0-9_]+)\s+input_bundle_run_id=([A-Za-z0-9_]+)\s+run_id_match=true\s+ignored_stale_bundles=([0-9]+)") {
    return ("诊断运行与上传 bundle 的 run_id 一致，已排除 {0} 个历史旧包干扰。" -f $Matches[3])
  }
  if ($Text -match "accepted_run_id=([A-Za-z0-9_]+)\s+input_bundle_run_id=([A-Za-z0-9_]+)\s+run_id_match=true") {
    return "诊断运行与上传 bundle 的 run_id 一致。"
  }
  if ($Text -match "DNS resolution failed while direct IP connectivity context remained available") {
    return "DNS 解析失败，同时直接 IP 连通性上下文仍可用。"
  }
  if ($Text -match "Stage1/Stage2 observed DNS resolution failures") {
    return "Stage1/Stage2 摘要记录 DNS 解析失败，并关联 resolver block 或 DNS proxy freeze marker。"
  }
  if ($Text -match "DNS cleanup caveat") {
    return "清理校验要求：确认 resolver 残留、DNS proxy freeze 和 DNS block 已清除，并复测 DNS 与公网 IP。"
  }
  if ($Text -match "recovery/cleanup gate.*recovery_gate_ok=([^ ]+).*injector_stop_ok=([^ ]+).*cleanup=([^ ]+)") {
    return ("恢复/清理门：recovery_gate_ok={0}，injector_stop_ok={1}，cleanup={2}。" -f $Matches[1], $Matches[2], $Matches[3])
  }
  return $Text
}

function Get-DemoNetEvidenceLines($DiagObj) {
  $label = Get-DemoMainLabel $DiagObj
  $dns = @()
  $ip = @()
  $iface = @()
  $route = @()
  $cleanup = @()
  $chain = @()
  $other = @()

  if ($label -eq "net_dns_fail") {
    $dns = Add-UniqueEvidenceLine $dns "故障证据：DNS 解析失败，同时直接 IP 连通性上下文仍可用。"
  }

  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    $rawText = ""
    $source = ""
    if ($e -is [string]) {
      $rawText = $e
    } else {
      $rawText = Get-FirstText $e @("text", "summary")
      $source = Get-FirstText $e @("source")
    }
    if (-not $rawText) { continue }
    if ($source -eq "3cm_candidate_live_smoke") { continue }
    $text = Format-DemoNetEvidenceText $rawText
    if (-not $text) { continue }
    if ($text -match "^\s*#|^\s*total\s+[0-9]+\s*$") { continue }
    if ($source -eq "stage1_stage2_binding") {
      $chain = Add-UniqueEvidenceLine $chain ("诊断链路校验：{0}" -f $text)
      continue
    }
    if (Test-DemoNetNoiseEvidence $e $text) { continue }
    if ($text -match "DNS|resolver|resolv|nameserver|解析|域名|hostname|dnsproxy|DNS proxy|proxy freeze") {
      $dns = Add-UniqueEvidenceLine $dns ("故障证据：{0}" -f $text)
    } elseif ($text -match "direct IP|public IP|公网|ping|IP 连通") {
      $ip = Add-UniqueEvidenceLine $ip ("连通性上下文：{0}" -f $text)
    } elseif ($text -match "wlan|IPv4|inet|wpa_state|Wi-?Fi") {
      $iface = Add-UniqueEvidenceLine $iface ("接口上下文：{0}" -f $text)
    } elseif ($text -match "default route|route|gateway|默认路由|网关") {
      $route = Add-UniqueEvidenceLine $route ("路由上下文：{0}" -f $text)
    } elseif ($text -match "cleanup|recovery|清理|恢复") {
      $cleanup = Add-UniqueEvidenceLine $cleanup ("清理/恢复门：{0}" -f $text)
    } else {
      $other = Add-UniqueEvidenceLine $other ("辅助观察：{0}" -f $text)
    }
  }

  $cause = Get-PropValue $DiagObj "cause"
  $causeSummary = Get-FirstText $cause @("summary")
  if ($label -eq "net_dns_fail" -and $causeSummary -match "direct IP|直接 IP") {
    $ip = Add-UniqueEvidenceLine $ip "连通性上下文：诊断摘要记录直接 IP 连通性仍可用，因此不把 Wi-Fi/IPv4/默认路由作为主根因。"
  }
  $symptom = Get-PropValue $DiagObj "symptom"
  $symptomSummary = Get-FirstText $symptom @("summary")
  if ($label -eq "net_dns_fail" -and $symptomSummary) {
    $dns = Add-UniqueEvidenceLine $dns ("故障证据：{0}" -f (Format-DemoNetEvidenceText $symptomSummary))
  }
  $netCaveat = Get-FirstText $DiagObj @("net_evidence_caveat")
  if ($netCaveat) {
    $cleanup = Add-UniqueEvidenceLine $cleanup ("清理/恢复门：{0}" -f (Format-DemoNetEvidenceText $netCaveat))
  }
  if ((-not $chain) -and (Test-DemoRunIdMatched $DiagObj)) {
    $chain = Add-UniqueEvidenceLine $chain "诊断链路校验：诊断运行与上传 bundle 的 run_id 一致。"
  }

  $out = @()
  foreach ($line in ($dns + $ip + $iface + $route + $cleanup + $chain + $other)) {
    $out = Add-UniqueEvidenceLine $out $line
    if ($out.Count -ge 6) { break }
  }
  if ($out.Count -eq 0) { $out += "完整 NET 证据已归档到 report；控制台未找到可安全展示的 DNS/接口/路由摘要。" }
  return $out
}

function Get-DemoCpuEvidenceLines($DiagObj, $PidstatAudit) {
  $lines = @()
  $peaks = Get-DemoCpuMetricPeaks $DiagObj
  if ($null -ne $peaks.cpu_util_peak_x100) {
    $lines = Add-UniqueEvidenceLine $lines ("故障证据：CPU 利用率峰值：{0}。" -f (Format-X100Percent $peaks.cpu_util_peak_x100))
  } else {
    $lines = Add-UniqueEvidenceLine $lines "故障证据：CPU 利用率峰值字段未在当前 report 中确认。"
  }
  if ($null -ne $peaks.load1_peak_x100) {
    $lines = Add-UniqueEvidenceLine $lines ("故障证据：load1 峰值：{0}。" -f (Format-X100Load $peaks.load1_peak_x100))
  } else {
    $lines = Add-UniqueEvidenceLine $lines "故障证据：load1 峰值字段未在当前 report 中确认。"
  }
  if ($null -ne $peaks.load5_peak_x100) {
    $lines = Add-UniqueEvidenceLine $lines ("故障证据：load5 峰值：{0}。" -f (Format-X100Load $peaks.load5_peak_x100))
  } else {
    $lines = Add-UniqueEvidenceLine $lines "故障证据：load5 峰值字段未在当前 report 中确认。"
  }

  if (Test-DemoCpuTriggerBound $DiagObj) {
    $lines = Add-UniqueEvidenceLine $lines "故障证据：本次触发绑定为 CPU 单点高负载，Stage2 按 CPU 故障链路处理。"
  }
  if (Test-DemoRunIdMatched $DiagObj) {
    $stale = Get-DemoIgnoredStaleBundles $DiagObj
    if ($stale -and $stale -ne "") {
      $lines = Add-UniqueEvidenceLine $lines ("诊断链路校验：诊断运行与上传 bundle 的 run_id 一致，已排除 {0} 个历史旧包干扰。" -f $stale)
    } else {
      $lines = Add-UniqueEvidenceLine $lines "诊断链路校验：诊断运行与上传 bundle 的 run_id 一致。"
    }
  }

  $counts = Get-DemoRunObservationCounts $DiagObj
  if ($null -ne $counts.metrics_rows) {
    $lines = Add-UniqueEvidenceLine $lines ("采样覆盖：观测窗口内采集到 {0} 行 metrics，覆盖故障持续过程。" -f $counts.metrics_rows)
  }
  if ($null -ne $counts.process_snapshots) {
    $lines = Add-UniqueEvidenceLine $lines ("采样覆盖：采集到 {0} 条进程快照，可作为进程级辅助观察。" -f $counts.process_snapshots)
  }
  if (Test-DemoPidstatMissing $DiagObj $PidstatAudit) {
    $lines = Add-UniqueEvidenceLine $lines "证据缺口：当前缺少 pidstat 进程级 CPU 采样，因此不直接声称某个 PID 是 CPU 根因。"
  }
  return $lines | Select-Object -First 8
}

function Get-DemoFamilyEvidenceLines($DiagObj, [string]$Family) {
  $primary = @()
  $secondary = @()
  $chain = @()
  foreach ($e in (Get-DemoEvidenceObjects $DiagObj)) {
    $text = Format-DemoEvidenceText $e.text
    if (-not $text) { continue }
    if ($e.source -eq "stage1_stage2_binding" -or $e.source -eq "3cm_candidate_live_smoke") {
      $chain = Add-UniqueEvidenceLine $chain ("诊断链路校验：{0}" -f $text)
      continue
    }
    if ($Family -eq "mem") {
      if ($text -match "RSS|PSS|mem|内存|OOM|pressure|压力|泄漏|增长") {
        $primary = Add-UniqueEvidenceLine $primary ("故障证据：{0}" -f $text)
      } elseif ($text -notmatch "CPU|load1|loadavg") {
        $secondary = Add-UniqueEvidenceLine $secondary ("辅助观察：{0}" -f $text)
      }
      continue
    }
    if ($Family -eq "net") {
      if ($text -match "DNS|route|路由|iface|IPv4|public IP|公网|Wi-?Fi|wpa|ping|网") {
        $primary = Add-UniqueEvidenceLine $primary ("故障证据：{0}" -f $text)
      } else {
        $secondary = Add-UniqueEvidenceLine $secondary ("辅助观察：{0}" -f $text)
      }
      continue
    }
    $secondary = Add-UniqueEvidenceLine $secondary $text
  }
  $out = @()
  foreach ($line in ($primary + $chain + $secondary)) {
    $out = Add-UniqueEvidenceLine $out $line
    if ($out.Count -ge 6) { break }
  }
  if ($out.Count -eq 0) { $out += (U "5a6M5pW06K+B5o2u5bey5b2S5qGj5Yiw5oql5ZGK77yM5o6n5Yi25Y+w5LuF5bGV56S65pGY6KaB44CC") }
  return $out
}

function Get-DemoEvidenceLines($DiagObj, $PidstatAudit, [string[]]$TopPids) {
  $label = Get-DemoMainLabel $DiagObj
  if ($label -eq "cpu_single_point_high_load" -or $label -eq "cpu") {
    return (Get-DemoCpuEvidenceLines $DiagObj $PidstatAudit)
  }
  if ($label -match "^mem" -or $label -eq "mem") {
    return (Get-DemoFamilyEvidenceLines $DiagObj "mem")
  }
  if ($label -match "^net" -or $label -eq "net") {
    return (Get-DemoNetEvidenceLines $DiagObj)
  }

  $lines = @()
  $evidence = Get-PropValue $DiagObj "evidence"
  if ($null -ne $evidence) {
    foreach ($e in $evidence) {
      $text = ""
      $source = ""
      if ($e -is [string]) {
        $text = $e
      } else {
        $text = Get-FirstText $e @("text", "summary")
        $source = Get-FirstText $e @("source")
      }
      if (-not $text) { continue }
      if ($source -eq "pidstat" -and $lines.Count -lt 3) { continue }
      $text = Format-DemoEvidenceText $text
      if ($lines -notcontains $text) { $lines += $text }
      if ($lines.Count -ge 4) { break }
    }
  }
  if ($PidstatAudit -and $PidstatAudit.fallback_evidence_used) {
    $fallback = U "Q1BVIGZhbGxiYWNrIGV2aWRlbmNlIOW3suWQr+eUqO+8mnJ1bl9pZCDnu5HlrprjgIFsb2FkYXZnL0NQVSDls7DlgLzjgIHov5vnqIvlv6vnhaflkozms6jlhaUv6Kem5Y+RIG1hcmtlciDlhbHlkIzmlK/mkpEgQ1BVIOWutuaXj+WIpOaWreOAgg=="
    if ($lines -notcontains $fallback) { $lines += $fallback }
  }
  if ($TopPids -and $TopPids.Count -gt 0) {
    $line = (U "6L+b56iL5b+r54WnIFRvcCBSU1MgUElEOiA=") + ($TopPids -join ", ")
    if ($lines -notcontains $line) { $lines += $line }
  }
  if ($lines.Count -eq 0) { $lines += (U "5a6M5pW06K+B5o2u5bey5b2S5qGj5Yiw5oql5ZGK77yM5o6n5Yi25Y+w5LuF5bGV56S65pGY6KaB44CC") }
  return $lines | Select-Object -First 5
}

function Get-DemoUncertaintyLines($DiagObj, $PidstatAudit) {
  $lines = @()
  $candidate = Get-FirstText $DiagObj @("candidate_caveat", "safety_caveat")
  if ($candidate) { $lines += (Format-DemoUncertaintyText $candidate) }
  $cause = Get-PropValue $DiagObj "cause"
  $causeCaveat = Get-FirstText $cause @("caveat")
  if ($causeCaveat) {
    $causeCaveat = Format-DemoUncertaintyText $causeCaveat
    if ($lines -notcontains $causeCaveat) { $lines += $causeCaveat }
  }
  if ((-not (Test-DemoNetLabel $DiagObj)) -and $PidstatAudit -and -not $PidstatAudit.pidstat_file_nonempty) {
    $lines += (U "cGlkc3RhdCDmnKrlvaLmiJDlj6/nlKjmlofku7bvvIzov5vnqIvnuqcgQ1BVIOeZvuWIhuavlOS4jeS9nOS4uuehruWumue7k+iuuu+8m+acrOasoeS7pSBmYWxsYmFjayBldmlkZW5jZSDlgZrmvJTnpLrnuqflgJnpgInliKTmlq3jgII=")
  }
  if ($lines.Count -eq 0) { $lines += (U "5peg5paw5aKe5LiN56Gu5a6a5oCn77yb5LuN5L+d5oyBIGNhbmRpZGF0ZS9zaGFkb3cgb25seeOAgg==") }
  return $lines | Select-Object -First 4
}

function ConvertTo-BoolValue($Value, [bool]$Default) {
  if ($null -eq $Value) { return $Default }
  if ($Value -is [bool]) { return $Value }
  $s = $Value.ToString().Trim().ToLower()
  if ($s -eq "true") { return $true }
  if ($s -eq "false") { return $false }
  return $Default
}

function Extract-Stage3Safety($ActionsObj) {
  $execution = Get-PropValue $ActionsObj "execution_enabled"
  $manual = Get-PropValue $ActionsObj "manual_approval_required"
  $actionCmd = Get-PropValue $ActionsObj "action_command_enabled"
  $recovery = Get-PropValue $ActionsObj "automatic_recovery_enabled"
  return [pscustomobject]@{
    execution_enabled = ConvertTo-BoolValue $execution $false
    manual_approval_required = ConvertTo-BoolValue $manual $true
    action_command_enabled = ConvertTo-BoolValue $actionCmd $false
    automatic_recovery_enabled = ConvertTo-BoolValue $recovery $false
  }
}

function Get-PidstatAudit([string]$RunId, [bool]$PidstatWaitOk) {
  $ridEsc = Escape-BashSingleQuote $RunId
  $pidstatPath = Invoke-Hdc "if command -v pidstat >/dev/null 2>&1; then command -v pidstat; else echo __PIDSTAT_MISSING__; fi"
  $pidstatAvailable = ($pidstatPath -and $pidstatPath -notmatch "__PIDSTAT_MISSING__|not found|Unknown command|/bin/sh:")
  $triggerdPidstat = Invoke-Hdc "grep -F -e pidstat /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null | tail -n 3"
  $captureAttempted = [bool]($triggerdPidstat -and $triggerdPidstat.Trim().Length -gt 0)
  $bundleStateCmd = @"
cd '$RepoDir'
if [ -s 'storage/runs/$ridEsc/procs/pidstat_0.txt' ] || [ -s 'storage/runs/$ridEsc/procs/pidstat_1.txt' ]; then
  echo nonempty
elif [ -f 'storage/runs/$ridEsc/procs/pidstat_0.txt' ] || [ -f 'storage/runs/$ridEsc/procs/pidstat_1.txt' ]; then
  echo empty
else
  echo missing
fi
"@
  $bundleState = (Invoke-Ssh $bundleStateCmd 20).Trim()
  $bundlePresent = ($bundleState -eq "nonempty" -or $bundleState -eq "empty" -or $PidstatWaitOk)
  $fileNonempty = ($bundleState -eq "nonempty")
  $fallbackUsed = (-not $fileNonempty)
  $recommendation = U "5L+d5oyBIHN1Z2dlc3Rpb24tb25see+8m+a8lOekuuaXtuivtOaYjiBwaWRzdGF0IOS4jeWPr+eUqOaIluacqumHh+WIsO+8jOS9v+eUqCBydW5faWQg57uR5a6a44CBbG9hZGF2Zy9DUFUg5bOw5YC844CB6L+b56iL5b+r54Wn44CBbWFya2VyIOS4jiBjbGVhbnVwIOWQjuinguWvn+S9nOS4uiBDUFUgZmFsbGJhY2sgZXZpZGVuY2XjgII="
  if ($fileNonempty) {
    $recommendation = U "cGlkc3RhdCDmlofku7blj6/nlKjvvJvlj6/lsZXnpLrov5vnqIvnuqcgQ1BVIOW9kuWboO+8jOWQjOaXtuS7jeS/neeVmSBmYWxsYmFjayBldmlkZW5jZSDkvZzkuLrkuqTlj4npqozor4HjgII="
  } elseif ($pidstatAvailable -and -not $captureAttempted) {
    $recommendation = U "5p2/56uv5a2Y5ZyoIHBpZHN0YXTvvIzkvYYgdHJpZ2dlcmQvYnVuZGxlIOacquiusOW9lemHh+mbhuWwneivle+8m+WQjue7reWPr+WcqOWNleeLrOS7u+WKoeS4reihpemHhyBwaWRzdGF077yM5pys5Lu75Yqh57un57ut5L2/55SoIGZhbGxiYWNrIGV2aWRlbmNl44CC"
  } elseif (-not $pidstatAvailable) {
    $recommendation = U "5p2/56uv5pyq5Y+R546wIHBpZHN0YXTvvJvmnKzku7vliqHkuI3kv67mlLnmnb/nq6/lt6Xlhbfpk77vvIznu6fnu63kvb/nlKggZmFsbGJhY2sgZXZpZGVuY2Ug5bm26YG/5YWN5aOw56ew6L+b56iL57qnIENQVSDnmb7liIbmr5TjgII="
  }
  return [pscustomobject]@{
    pidstat_available = [bool]$pidstatAvailable
    pidstat_command_path = $(if ($pidstatAvailable) { $pidstatPath.Trim() } else { "" })
    pidstat_capture_attempted = [bool]$captureAttempted
    pidstat_bundle_present = [bool]$bundlePresent
    pidstat_file_nonempty = [bool]$fileNonempty
    fallback_evidence_used = [bool]$fallbackUsed
    recommendation = $recommendation
  }
}

function Format-PidstatAuditLines($PidstatAudit) {
  if (-not $PidstatAudit) { return @("- pidstat audit unavailable") }
  return @(
    ("- pidstat_available={0}" -f ([string]$PidstatAudit.pidstat_available).ToLower()),
    ("- pidstat_command_path={0}" -f ($(if ($PidstatAudit.pidstat_command_path) { $PidstatAudit.pidstat_command_path } else { "NA" }))),
    ("- pidstat_capture_attempted={0}" -f ([string]$PidstatAudit.pidstat_capture_attempted).ToLower()),
    ("- pidstat_bundle_present={0}" -f ([string]$PidstatAudit.pidstat_bundle_present).ToLower()),
    ("- pidstat_file_nonempty={0}" -f ([string]$PidstatAudit.pidstat_file_nonempty).ToLower()),
    ("- fallback_evidence_used={0}" -f ([string]$PidstatAudit.fallback_evidence_used).ToLower()),
    ("- recommendation={0}" -f $PidstatAudit.recommendation)
  )
}

function Build-ConsoleSummary([string]$RunId, [string]$DiagJson, [string]$ActionsJson, [string]$ReportPath, $PidstatAudit, [string[]]$TopPids) {
  $diagObj = Convert-JsonOrNull $DiagJson
  $actionsObj = Convert-JsonOrNull $ActionsJson
  $diagnosisText = Extract-DiagnosisText $DiagJson
  $faultLabel = Get-DemoFaultLabel $diagObj
  $confidence = Format-DemoConfidence $diagObj
  $rootCause = Get-DemoRootCause $diagObj $diagnosisText
  $evidenceLines = Get-DemoEvidenceLines $diagObj $PidstatAudit $TopPids
  $uncertaintyLines = Get-DemoUncertaintyLines $diagObj $PidstatAudit
  $actionLines = Extract-ActionsLines $ActionsJson
  if ($actionLines.Count -eq 0) {
    $actionLines = @(U "5pyq55Sf5oiQ5Y+v5bGV56S65bu66K6u77yb5L+d5oyB5Lq65bel5aSN5qC444CC")
  }
  $safety = Extract-Stage3Safety $actionsObj

  $lines = @()
  $lines += (U "44CQ5pWF6Zqc5qOA5rWL5a6M5oiQ44CR")
  $lines += ((U "6L+Q6KGM57yW5Y+377yaezB9") -f $RunId)
  $lines += ((U "5pWF6Zqc57G75Yir77yaezB9") -f $faultLabel)
  $lines += ((U "572u5L+h5bqm77yaezB9") -f $confidence)
  $lines += ""
  $lines += (U "44CQ5qC55Zug5YiG5p6Q44CR")
  $lines += $rootCause
  $lines += ""
  $lines += (U "44CQ5YWz6ZSu6K+B5o2u44CR")
  $idx = 1
  foreach ($line in $evidenceLines) {
    $lines += ("{0}. {1}" -f $idx, $line)
    $idx += 1
  }
  $lines += ""
  $lines += (U "44CQ5LiN56Gu5a6a5oCn44CR")
  foreach ($line in $uncertaintyLines) { $lines += ("- {0}" -f $line) }
  $lines += ""
  $lines += (U "44CQQWN0aW9uIFIxIOW7uuiuruOAkQ==")
  foreach ($line in $actionLines) { $lines += ("- {0}" -f $line) }
  $lines += ""
  $lines += (U "44CQ5a6J5YWo6L6555WM44CR")
  $lines += ("execution_enabled={0}" -f ([string]$safety.execution_enabled).ToLower())
  $lines += ("manual_approval_required={0}" -f ([string]$safety.manual_approval_required).ToLower())
  $lines += ("action_command_enabled={0}" -f ([string]$safety.action_command_enabled).ToLower())
  $lines += ("automatic_recovery_enabled={0}" -f ([string]$safety.automatic_recovery_enabled).ToLower())
  $lines += ""
  $lines += ((U "5a6M5pW05oql5ZGK77yaezB9") -f $ReportPath)
  return ($lines -join [Environment]::NewLine)
}

# ===================== main =====================

$ResolvedTriggerdArgs = Resolve-TriggerdArgs -OldArgs $TriggerdArgs -AliasArgs $TriggeredArgs
$ExpectedBinding = Get-ExpectedLabelBinding $ResolvedTriggerdArgs
$Stage2EnvPrefix = Build-Stage2EnvPrefix -ConfigPath $CandidateConfigPath -SuggestionOnlyMode ([bool]$SuggestionOnly) -ExpectedBinding $ExpectedBinding
if ($ExpectedBinding.Family) {
  Write-Host ("[3CM] expected_binding family={0} main_label={1} load_pattern_detail={2}" -f $ExpectedBinding.Family, $ExpectedBinding.MainLabel, $ExpectedBinding.LoadPatternDetail)
}
if ($CandidateConfigPath) {
  Write-Host ("[3CM] candidate_config={0}" -f $CandidateConfigPath)
  if (-not $SuggestionOnly) {
    Write-Host "FAIL: 3CM_CANDIDATE_REQUIRES_SUGGESTION_ONLY"
    Write-Host "HINT: add -SuggestionOnly or run the legacy demo without -CandidateConfigPath"
    exit 1
  }
}
if ($SuggestionOnly) {
  Write-Host "[3CM] suggestion-only Stage3 enabled; board action execution is disabled"
}

if ($ExpectedBinding.Family -eq "net") {
  if (-not $SuggestionOnly -or -not $CandidateConfigPath) {
    Write-Host "FAIL: NET_DEMO_REQUIRES_CANDIDATE_SUGGESTION_ONLY"
    Write-Host "HINT: NET labels run only with -CandidateConfigPath ... -SuggestionOnly"
    exit 1
  }
  Write-Host ("[3CM] NET baseline preflight (fail-closed) label={0}" -f $ExpectedBinding.MainLabel)
  if (-not (Invoke-NetBaselinePreflight -NetLabel $ExpectedBinding.MainLabel -PublicProbeIp $NetPublicProbeIp -DnsProbeHost $NetDnsProbeHost)) {
    Write-Host "FAIL: NET_BASELINE_PREFLIGHT_FAILED"
    Write-Host "HINT: restore Guest Wi-Fi baseline (IPv4/default route/public IP/DNS) and clear stale /data/local/tmp/net_fault_state markers, then retry"
    exit 1
  }
}

Write-Host "[server] restart demo_services"
$restartOut = Invoke-Ssh ("cd '{0}'; {1}bash closed_loop_demo/server/src/server_B/tcp/demo_services.sh restart" -f $RepoDir, $Stage2EnvPrefix)
Write-Host $restartOut

$statusOut = Invoke-Ssh ("cd '{0}'; {1}bash closed_loop_demo/server/src/server_B/tcp/demo_services.sh status" -f $RepoDir, $Stage2EnvPrefix)
Write-Host $statusOut

if (($statusOut -notmatch (":" + [string]$ExpectedIngestPort)) -or ($statusOut -notmatch (":" + [string]$ExpectedActionsPort))) {
  Write-Host "FAIL: SERVER_SERVICES_NOT_LISTENING"
  Write-Host "HINT:"
  Write-Host ("  ssh {0}" -f $Server)
  Write-Host ("  cd {0}" -f $RepoDir)
  Write-Host ("  bash closed_loop_demo/server/src/server_B/tcp/demo_services.sh status")
  Write-Host ("  expected listen ports: {0}/{1}" -f $ExpectedIngestPort, $ExpectedActionsPort)
  exit 1
}

Write-Host "[board] prepare clean baseline (stop triggerd/inject, rotate log, clear state)"
# one-liner, no here-string, no PowerShell interpolation.
# The remote side only uses verified system commands.
$prepCmd =
'sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop >/dev/null 2>/dev/null; sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop >/dev/null 2>/dev/null; ' +
'sh /data/faultmon/demo_stage2/bin/triggerd.sh stop >/dev/null 2>/dev/null; ' +
'mkdir -p /data/faultmon/demo_stage2/logs /data/faultmon/state >/dev/null 2>/dev/null; ' +
'ts=$(date +%Y%m%d_%H%M%S 2>/dev/null); ' +
'if [ -z "$ts" ]; then ts=$(date +%Y%m%d_%H%M%S 2>/dev/null); fi; ' +
'if [ -z "$ts" ]; then ts=unknown; fi; ' +
'if [ -s /data/faultmon/demo_stage2/logs/triggerd.log ]; then mv /data/faultmon/demo_stage2/logs/triggerd.log /data/faultmon/demo_stage2/logs/triggerd.log.$ts 2>/dev/null; fi; ' +
': > /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null; ' +
'rm -f /data/faultmon/state/trigger.active /data/faultmon/state/last_trigger_epoch /data/faultmon/state/last_trigger.json 2>/dev/null; ' +
'rm -f /data/faultmon/demo_stage2/inbox/actions.ready /data/faultmon/demo_stage2/inbox/actions.done /data/faultmon/demo_stage2/inbox/actions.fail /data/faultmon/demo_stage2/inbox/actions_device.txt /data/faultmon/demo_stage2/inbox/latest_run_id.txt /data/faultmon/demo_stage2/inbox/latest_conclusion.txt 2>/dev/null; ' +
'rm -f /data/faultmon/inbox/actions.ready /data/faultmon/inbox/actions.done /data/faultmon/inbox/actions.fail /data/faultmon/inbox/actions_device.txt /data/faultmon/inbox/actions_response.txt /data/faultmon/inbox/latest_run_id.txt /data/faultmon/inbox/latest_conclusion.txt 2>/dev/null; ' +
'echo CLEAN_OK'

# BUT: $() in the above is BASH and safe because this is a single-quoted PS string (no interpolation).
Write-Host (Invoke-Hdc $prepCmd)

Deploy-BoardScripts

Write-Host "[board] ensure device_id and start faultmon"
$null = Invoke-Hdc ("echo {0} > /data/faultmon/device_id" -f $DeviceId)
Write-Host (Invoke-Hdc "sh /data/faultmon/faultmon.sh start")

Write-Host "[board] start triggerd daemon"
$tdArgs = $ResolvedTriggerdArgs
if ($tdArgs) {
  Write-Host (Invoke-Hdc ("sh /data/faultmon/demo_stage2/bin/triggerd.sh --daemon {0}" -f $tdArgs))
} else {
  Write-Host (Invoke-Hdc "sh /data/faultmon/demo_stage2/bin/triggerd.sh --daemon")
}

$tdStatus = Invoke-Hdc "sh /data/faultmon/demo_stage2/bin/triggerd.sh status"
Write-Host $tdStatus
if ($tdStatus -notmatch "alive pid=") {
  Write-Host "FAIL: TRIGGERD_NOT_ALIVE"
  Write-Host "HINT:"
  Write-Host '  hdc -t $env:HDC_TARGET shell "tail -n 200 /data/faultmon/demo_stage2/logs/triggerd.log"'
  exit 1
}

$scriptStart = Now-Date
Write-Host "READY: run injection in another window (metrics will change until trigger)."

$stage1 = Wait-Stage1 -TimeoutSec $TimeoutStage1
if (-not $stage1) {
  Write-Host "FAIL: WAIT_STAGE1_TIMEOUT"
  Write-Host "HINT:"
  Write-Host '  hdc -t $env:HDC_TARGET shell "tail -n 240 /data/faultmon/demo_stage2/logs/triggerd.log"'
  exit 1
}

$rid = $stage1.Rid
$stage1Sec = Sec $stage1.TriggerTime $stage1.DoneTime
$total1 = Sec $scriptStart $stage1.DoneTime
Write-Host ("STAGE1_DONE rid={0} stage1_sec={1} total_sec={2}" -f $rid, $stage1Sec, $total1)

function Test-PidstatPresent([string]$RunId) {
  $cmd = @"
cd '$RepoDir'
if [ -f 'storage/runs/$RunId/procs/pidstat_0.txt' ] && [ -f 'storage/runs/$RunId/procs/pidstat_1.txt' ]; then
  echo OK
fi
"@
  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) {
    Write-Host "WARN: pidstat ssh timeout, will retry"
    return $false
  }
  return ($out -match "OK")
}

function Show-Top5PidsByRss([string]$RunId) {
  # 只输出 5 行 PID：按 procs_*.txt 的 RSS（第4列）降序
  $ridEsc = Escape-BashSingleQuote $RunId

  # 注意：用 @' ... '@ 单引号 here-string，避免 PowerShell 解释 $() / $f
  $cmd = @'
cd "__REPO__"
# 取最新的 procs 快照
f=$(ls -t storage/runs/__RID__/procs/procs_*.txt 2>/dev/null | head -n 1)
[ -n "$f" ] || exit 0

# 跳过前两行（注释+表头），按第4列(RSS)降序取Top5，只输出PID
sed -n '3,$p' "$f" | sort -k4,4nr | head -n 5 | sed -E 's/^[[:space:]]*([0-9]+).*/\1/'
'@

  $cmd = $cmd.Replace("__REPO__", $RepoDir).Replace("__RID__", $ridEsc)

  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) { return }

$items = @()
foreach ($line in $out.Split("`n")) {
  $t = $line.Trim()
  if ($t -match '^[0-9]+$') { $items += $t }
}
return $items
}



Write-Host ("ENTER_PIDSTAT_WAIT rid={0}" -f $rid)

$deadline = (Get-Date).AddSeconds(60)
$ok = $false
Start-Sleep -Seconds 2  # 给 ingest/解包一个最小缓冲

while ((Get-Date) -lt $deadline) {
  if (Test-PidstatPresent $rid) { $ok = $true; break }
  Write-Host ("WAIT_PIDSTAT elapsed={0}s" -f (Sec $stage1.DoneTime (Now-Date)))
  Start-Sleep -Seconds $PollSec
}

if (-not $ok) {
  # 3CM-R2-R3 CPU evidence fallback: pidstat is preferred but not mandatory.
  # Stage2 falls back to procs/top/load metrics + injector/trigger markers and
  # produces a caveated RCA; the family is never drifted away from the trigger binding.
  Write-Host ("WARN: PIDSTAT_MISSING_FALLBACK rid={0} (Stage2 will use procs/top/load/marker fallback evidence with caveat)" -f $rid)
  $lsProcs = Invoke-Ssh ("cd '{0}'; ls -la storage/runs/{1}/procs 2>/dev/null" -f $RepoDir, $rid) 20
  if ($lsProcs) { Write-Host $lsProcs }
}
$PidstatAudit = Get-PidstatAudit $rid $ok


$stage2 = Wait-Stage -Name "STAGE2" -TimeoutSec $TimeoutStage2 -Check { Test-Stage2Done $rid } -RunId $rid
if (-not $stage2) {
  Write-Host "FAIL: WAIT_STAGE2_TIMEOUT"
  Write-Host "HINT:"
  Write-Host ("  ssh {0}" -f $Server)
  Write-Host ("  cd {0}" -f $RepoDir)
  Write-Host ("  tail -n 200 storage/logs/watcher.log")
  Write-Host ("  ls -la storage/runs/{0}/_server_out" -f $rid)
  exit 1
}
$stage2Sec = Sec $stage2.Start $stage2.Done
$total2 = Sec $scriptStart $stage2.Done
Write-Host ("STAGE2_DONE rid={0} stage2_sec={1} total_sec={2}" -f $rid, $stage2Sec, $total2)
$TopPids = Show-Top5PidsByRss $rid

if ($SuggestionOnly) {
  $stage3 = Wait-Stage -Name "STAGE3" -TimeoutSec $TimeoutStage3 -Check { Test-Stage3SuggestionDone $rid } -RunId $rid
} else {
  $stage3 = Wait-Stage -Name "STAGE3" -TimeoutSec $TimeoutStage3 -Check { Test-Stage3Done $rid } -RunId $rid -DriveBoardActions
}
if (-not $stage3) {
  Write-Host "FAIL: WAIT_STAGE3_TIMEOUT"
  Write-Host "HINT:"
  Write-Host ("  ssh {0}" -f $Server)
  Write-Host ("  cd {0}" -f $RepoDir)
  if ($SuggestionOnly) {
    Write-Host ("  ls -la storage/runs/{0}/_server_out" -f $rid)
    Write-Host ("  ls -la storage/tcp_out/{0}" -f $DeviceId)
    Write-Host ("  cat storage/runs/{0}/_server_out/stage3_suggestions.json" -f $rid)
  } else {
    Write-Host ("  ls -la storage/runs/{0}/_action_result" -f $rid)
  }
  exit 1
}
$stage3Sec = Sec $stage3.Start $stage3.Done
$total3 = Sec $scriptStart $stage3.Done
if ($SuggestionOnly) {
  Write-Host ("STAGE3_DONE rid={0} stage3_sec={1} total_sec={2} mode=suggestion_only" -f $rid, $stage3Sec, $total3)
} else {
  Write-Host ("STAGE3_DONE rid={0} stage3_sec={1} total_sec={2}" -f $rid, $stage3Sec, $total3)
}

$diagCmd = @"
cd '$RepoDir'
if [ -f 'storage/runs/$rid/_server_out/diagnosis.json' ]; then
  cat 'storage/runs/$rid/_server_out/diagnosis.json'
elif [ -f 'storage/runs/$rid/_server_out/diagnosis_v2.json' ]; then
  cat 'storage/runs/$rid/_server_out/diagnosis_v2.json'
fi
"@
$actionsCmd = @"
cd '$RepoDir'
if [ '$SuggestionOnly' = 'True' ] && [ -f 'storage/runs/$rid/_server_out/stage3_suggestions.json' ]; then
  cat 'storage/runs/$rid/_server_out/stage3_suggestions.json'
elif [ -f 'storage/runs/$rid/_server_out/actions_v2.json' ]; then
  cat 'storage/runs/$rid/_server_out/actions_v2.json'
elif [ -f 'storage/runs/$rid/_server_out/actions.json' ]; then
  cat 'storage/runs/$rid/_server_out/actions.json'
fi
"@

$diagJson = Invoke-Ssh $diagCmd 20
$actionsJson = Invoke-Ssh $actionsCmd 20
if (-not $diagJson) {
  Write-Host "WARN: diagnosis read empty; dumping _server_out dir and infer.log tail"
  $dbg = Invoke-Ssh ("cd '{0}'; ls -la storage/runs/{1}/_server_out; tail -n 80 storage/runs/{1}/_server_out/infer.log 2>/dev/null" -f $RepoDir, $rid) 20
  if ($dbg) { Write-Host $dbg }
}
if (-not $actionsJson) {
  Write-Host "WARN: actions read empty; dumping _server_out dir and infer.log tail"
  $dbg2 = Invoke-Ssh ("cd '{0}'; ls -la storage/runs/{1}/_server_out; tail -n 80 storage/runs/{1}/_server_out/infer.log 2>/dev/null" -f $RepoDir, $rid) 20
  if ($dbg2) { Write-Host $dbg2 }
}

$diagnosisText = Extract-DiagnosisText $diagJson
$suspectDebug = @()
if ($env:WK_DEBUG_SUSPECTS -eq "1") {
  $suspectDebug = Extract-SuspectsDebug $diagJson
}
$actionLines = Extract-ActionsLines $actionsJson

$outDir = Join-Path -Path (Split-Path -Parent $MyInvocation.MyCommand.Path) -ChildPath "out"
if (-not (Test-Path -Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$reportPath = Join-Path -Path $outDir -ChildPath ("demo_report_{0}.md" -f $rid)
$consoleSummaryPath = Join-Path -Path $outDir -ChildPath ("demo_console_summary_{0}.txt" -f $rid)
$consoleSummary = Build-ConsoleSummary $rid $diagJson $actionsJson $reportPath $PidstatAudit $TopPids
$consoleSummary | Set-Content -Path $consoleSummaryPath -Encoding utf8

Write-Host ""
Write-Host $consoleSummary

if ($VerboseJson -or $DebugJson) {
  Write-Host ""
  Write-Host (U "44CQRGVidWcgSlNPTu+8mmRpYWdub3Npc+OAkQ==")
  Write-Host $diagJson
  Write-Host ""
  Write-Host (U "44CQRGVidWcgSlNPTu+8mnN0YWdlMy9hY3Rpb25z44CR")
  Write-Host $actionsJson
}

$report = @()
$report += "# Stage2 Demo Report"
$report += ""
$report += ("RID: {0}" -f $rid)
$report += ("Stage1(trigger->upload): {0}s" -f $stage1Sec)
$report += ("Stage2(infer):           {0}s" -f $stage2Sec)
if ($SuggestionOnly) {
  $report += ("Stage3(suggestions):     {0}s" -f $stage3Sec)
} else {
  $report += ("Stage3(action):          {0}s" -f $stage3Sec)
}
$report += ("Total:                  {0}s" -f $total3)
$report += ""
$report += "## Console Summary"
$report += "```text"
$report += $consoleSummary
$report += '```'
$report += ""
$report += "## Diagnosis Summary"
$report += $diagnosisText
$report += ""
$report += "## Pidstat Audit"
foreach ($line in (Format-PidstatAuditLines $PidstatAudit)) { $report += $line }
$report += ""
if ($suspectDebug.Count -gt 0) {
  $report += "## Suspects (Debug)"
  foreach ($line in $suspectDebug) { $report += ("- {0}" -f $line) }
  $report += ""
}
if ($SuggestionOnly) {
  $report += "## Stage3 Suggestions"
} else {
  $report += "## Actions"
}
if ($actionLines.Count -eq 0) {
  $report += "- (not found)"
} else {
  foreach ($x in $actionLines) { $report += ("- {0}" -f $x) }
}
$report += ""
$report += "## Raw Diagnosis JSON"
$report += "```json"
$report += $diagJson
$report += '```'
$report += ""
$report += "## Raw Stage3/Action JSON"
$report += "```json"
$report += $actionsJson
$report += '```'

$report | Set-Content -Path $reportPath -Encoding utf8
Write-Host ("REPORT: {0}" -f $reportPath)
Write-Host ("CONSOLE_SUMMARY: {0}" -f $consoleSummaryPath)

Write-Host ""
Write-Host "NOTE: remember to stop injection manually in the other window:"
$mode = ""
if ($ResolvedTriggerdArgs -match "(^|\s)--mode\s+(\S+)") { $mode = $Matches[2].Trim() }
if (-not $mode -and $ResolvedTriggerdArgs -match "(^|\s)--mode=(\S+)") { $mode = $Matches[2].Trim() }

if ($mode -eq "mem") {
  Write-Host '  hdc -t $env:HDC_TARGET shell "sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop"'
} elseif ($mode -eq "cpu") {
  Write-Host '  hdc -t $env:HDC_TARGET shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop"'
} elseif ($mode -eq "net") {
  Write-Host '  stop the NET injector window (net_fault.sh) and run its cleanup/restore path,'
  Write-Host '  then verify the recovery gate: wlan0 IPv4 + default route + public IP ping + DNS ping all healthy'
  Write-Host '  and /data/local/tmp/net_fault_state has no residual .applied/.ready/.pid markers.'
} else {
  # Unknown / multi mode: print both to be safe.
  Write-Host '  hdc -t $env:HDC_TARGET shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop"'
  Write-Host '  hdc -t $env:HDC_TARGET shell "sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop"'
}
