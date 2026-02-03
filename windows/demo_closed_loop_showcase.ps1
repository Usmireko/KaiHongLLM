param(
  [string]$Target = "",
  [string]$Server = "qwen3-server",
  [string]$RepoDir = "/home/xrh/qwen3_os_fault",
  [string]$DeviceId = "dev1",

  [int]$TimeoutStage1 = 240,
  [int]$TimeoutStage2 = 300,
  [int]$TimeoutStage3 = 300,
  [int]$PollSec = 2,

  [string]$TriggerdArgs = "",
  [switch]$Help
)

function Show-Usage {
  Write-Host "Usage: powershell -ExecutionPolicy Bypass -File .\demo_closed_loop_showcase.ps1 [options]"
  Write-Host "Options:"
  Write-Host "  -Target <hdc id>       (auto-detect USB Connected if empty)"
  Write-Host "  -Server <ssh alias>    (default: qwen3-server)"
  Write-Host "  -RepoDir <path>        (default: /home/xrh/qwen3_os_fault)"
  Write-Host "  -DeviceId <id>         (default: dev1)"
  Write-Host "  -TimeoutStage1 <sec>   (default: 240)"
  Write-Host "  -TimeoutStage2 <sec>   (default: 300)"
  Write-Host "  -TimeoutStage3 <sec>   (default: 300)"
  Write-Host "  -PollSec <sec>         (default: 2)"
  Write-Host "  -TriggerdArgs <string> (extra args for triggerd --daemon)"
  Write-Host ""
  Write-Host "Example:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\demo_closed_loop_showcase.ps1"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\demo_closed_loop_showcase.ps1 -TriggerdArgs ""--mode cpu --interval 2 --hit_need 3 --cpu_hit_need 3"""
}

if ($Help) { Show-Usage; exit 0 }

function Get-DefaultTarget {
  $list = @(& hdc list targets -v 2>$null)
  foreach ($line in $list) {
    if ($line -match "^(\S+)\s+USB\s+Connected") { return $Matches[1] }
  }
  return ""
}

if (-not $Target) {
  $Target = Get-DefaultTarget
  if (-not $Target) { throw "No USB connected target found via 'hdc list targets -v'." }
}

function Escape-BashSingleQuote([string]$s) {
  if ($null -eq $s) { return "" }
  return ($s -replace "'", "'\''")
}

function Flatten-ShellScript([string]$s) {
  $s = $s -replace "`r", ""
  $lines = @()
  foreach ($ln in ($s -split "`n")) {
    $t = $ln.Trim()
    if ($t.Length -eq 0) { continue }
    $lines += $t
  }
  return ($lines -join "; ")
}

function Wrap-RemoteTimeout([string]$InnerCmd, [int]$TimeoutSec = 15) {
  $escaped = Escape-BashSingleQuote $InnerCmd
  return "if command -v timeout >/dev/null 2>&1; then timeout ${TimeoutSec}s bash -lc '$escaped'; else bash -lc '$escaped'; fi"
}

$script:LastSshTimedOut = $false

function Invoke-SshWithTimeout([string]$Cmd, [int]$TimeoutSec = 20) {
  $script:LastSshTimedOut = $false
  $Cmd = $Cmd -replace "`r", ""
  if ($Cmd -notmatch "<<") {
    $Cmd = Flatten-ShellScript $Cmd
  }
  $escaped = Escape-BashSingleQuote $Cmd
  $full = "bash -lc '$escaped'"
  $sshExe = "C:\\Windows\\System32\\OpenSSH\\ssh.exe"
  $args = @(
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
  $Cmd = Flatten-ShellScript $Cmd
  $out = @(& hdc -t $Target shell $Cmd 2>&1)
  return ($out -join "`n").Trim()
}

function Now-Date { return Get-Date }
function ElapsedSec([datetime]$Start) { return [double]((Get-Date) - $Start).TotalSeconds }

function Parse-RunIdFromLine([string]$Text) {
  if ($Text -match "run_id=([A-Za-z0-9_]+)") { return $Matches[1] }
  return ""
}

# ---------------- Board ops ----------------
function Board-CleanBaseline {
  $cmd = @'
BB=/data/local/tmp/busybox
BIN=/data/faultmon/demo_stage2/bin
LOG=/data/faultmon/demo_stage2/logs/triggerd.log
PIDF=/data/faultmon/demo_stage2/pids/triggerd.pid
LOCK=/data/faultmon/demo_stage2/state/triggerd.lock

sh "$BIN/inject_cpu.sh" stop >/dev/null 2>&1
sh "$BIN/triggerd.sh" stop >/dev/null 2>&1

"$BB" rm -f "$PIDF" "$LOCK" /data/faultmon/state/last_trigger_epoch /data/faultmon/state/last_trigger.json 2>/dev/null

TS=$("$BB" date +%Y%m%d_%H%M%S 2>/dev/null)
if [ -z "$TS" ]; then TS=$(date +%Y%m%d_%H%M%S 2>/dev/null); fi
if [ -f "$LOG" ]; then "$BB" mv "$LOG" "$LOG.$TS" 2>/dev/null; fi

"$BB" rm -f /data/faultmon/demo_stage2/*__bundle.tar.gz 2>/dev/null
"$BB" rm -f /data/faultmon/demo_stage2/*__action_result.tar.gz 2>/dev/null

echo CLEAN_OK
'@
  return Invoke-Hdc $cmd
}

function Board-StartFaultmon {
  return Invoke-Hdc 'sh /data/faultmon/faultmon.sh start >/dev/null 2>&1; echo faultmon_ok'
}

function Board-StartTriggerd([string]$ExtraArgs) {
  if ($ExtraArgs -and $ExtraArgs.Trim().Length -gt 0) {
    return Invoke-Hdc ("sh /data/faultmon/demo_stage2/bin/triggerd.sh --daemon {0}" -f $ExtraArgs)
  }
  return Invoke-Hdc "sh /data/faultmon/demo_stage2/bin/triggerd.sh --daemon"
}

function Board-TriggerdAliveCheckOnce {
  $cmd = @'
BB=/data/local/tmp/busybox
PIDF=/data/faultmon/demo_stage2/pids/triggerd.pid
pid=$("$BB" cat "$PIDF" 2>/dev/null)
case "$pid" in ''|*[!0-9]*) echo NO_PID; exit 1;; esac
"$BB" ps | "$BB" grep -E "^[ ]*$pid[ ]" >/dev/null 2>&1
if [ $? -eq 0 ]; then
  echo ALIVE pid=$pid
  exit 0
fi
echo NOT_ALIVE pid=$pid
exit 1
'@
  return Invoke-Hdc $cmd
}

function Board-WaitTriggerdAlive([int]$TimeoutSec) {
  $start = Now-Date
  while (((Get-Date) -lt $start.AddSeconds($TimeoutSec))) {
    $out = Board-TriggerdAliveCheckOnce
    if ($out -match "ALIVE pid=") { return $true }
    Start-Sleep -Milliseconds 400
  }
  return $false
}

function Board-GetLastTriggeredRunId {
  $cmd = @'
BB=/data/local/tmp/busybox
LOG=/data/faultmon/demo_stage2/logs/triggerd.log
"$BB" tail -n 250 "$LOG" 2>/dev/null | "$BB" grep -F -e "trigger_bundle run_id=" | "$BB" tail -n 1
'@
  $line = Invoke-Hdc $cmd
  return (Parse-RunIdFromLine $line)
}

function Board-HasUploadOk([string]$RunId) {
  if (-not $RunId) { return $false }
  $cmd = @"
BB=/data/local/tmp/busybox
LOG=/data/faultmon/demo_stage2/logs/triggerd.log
"$BB" grep -F -e "OK: uploaded type=bundle" "$LOG" 2>/dev/null | "$BB" grep -F -e "run=$RunId" | "$BB" grep -F -e "device=$DeviceId" | "$BB" tail -n 1
"@
  $line = Invoke-Hdc $cmd
  return ($line -and $line.Trim().Length -gt 0)
}

function Wait-Stage1([int]$TimeoutSec) {
  $start = Now-Date
  $deadline = $start.AddSeconds($TimeoutSec)
  $rid = ""
  $triggerAt = $null

  while ((Now-Date) -lt $deadline) {
    if (-not $triggerAt) {
      $rid = Board-GetLastTriggeredRunId
      if ($rid) { $triggerAt = Now-Date }
    }

    $ridShow = "none"
    if ($rid) { $ridShow = $rid }

    if ($triggerAt) {
      if (Board-HasUploadOk $rid) {
        $done = Now-Date
        return @{ Rid = $rid; TriggerTime = $triggerAt; DoneTime = $done }
      }
    }

    Write-Host ("WAIT_STAGE1 elapsed={0:n2}s rid={1}" -f (ElapsedSec $start), $ridShow)
    Start-Sleep -Seconds $PollSec
  }
  return $null
}

# ---------------- Server ops ----------------
function Server-RestartServices {
  return Invoke-Ssh "cd '$RepoDir'; bash server_B/tcp/demo_services.sh restart; bash server_B/tcp/demo_services.sh status"
}

function Server-CleanInbox {
  $cmd = @'
REPO="__REPO__"
DEV="__DEV__"
cd "$REPO"
if [ $? -ne 0 ]; then exit 1; fi
d="storage/tcp_inbox/$DEV"
mkdir -p "$d"
rm -f "$d"/*.tar.gz 2>/dev/null
rm -f "$d"/*.tar.gz.done 2>/dev/null
rm -f "$d"/*.tar.gz.infer_done 2>/dev/null
rm -f "$d"/*.tar.gz.bad 2>/dev/null
echo CLEAN_INBOX_OK
'@
  $cmd = $cmd.Replace("__REPO__", $RepoDir).Replace("__DEV__", $DeviceId)
  return Invoke-Ssh $cmd
}

function Server-TestStage2Done([string]$RunId) {
  $cmd = @'
REPO="__REPO__"
RID="__RID__"
cd "$REPO"
if [ $? -ne 0 ]; then exit 1; fi
test -f "storage/runs/$RID/_server_out/.infer_done" || exit 1
ec=$(cat "storage/runs/$RID/_server_out/infer_ec.txt" 2>/dev/null)
if [ "x$ec" = "x0" ]; then
  sz=0
  if [ -f "storage/runs/$RID/_server_out/diagnosis_v2.json" ]; then
    sz="$(wc -c < "storage/runs/$RID/_server_out/diagnosis_v2.json" 2>/dev/null || echo 0)"
  elif [ -f "storage/runs/$RID/_server_out/diagnosis.json" ]; then
    sz="$(wc -c < "storage/runs/$RID/_server_out/diagnosis.json" 2>/dev/null || echo 0)"
  fi
  case "$sz" in ''|*[!0-9]*) sz=0 ;; esac
  if [ "$sz" -gt 20 ]; then echo OK; fi
fi
'@
  $cmd = $cmd.Replace("__REPO__", $RepoDir).Replace("__RID__", $RunId)
  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) {
    Write-Host "WARN: stage2 ssh timeout, continue polling"
    return $false
  }
  return ($out -match "OK")
}

function Server-TestStage3Done([string]$RunId) {
  $cmd = @'
REPO="__REPO__"
RID="__RID__"
cd "$REPO"
if [ $? -ne 0 ]; then exit 1; fi
test -f "storage/runs/$RID/_action_result/action_result.json" || exit 1
rc=$(cat "storage/runs/$RID/_action_result/actiond_rc.txt" 2>/dev/null)
if [ "x$rc" = "x0" ]; then echo OK; fi
'@
  $cmd = $cmd.Replace("__REPO__", $RepoDir).Replace("__RID__", $RunId)
  $out = Invoke-Ssh $cmd 20
  if ($script:LastSshTimedOut) {
    Write-Host "WARN: stage3 ssh timeout, continue polling"
    return $false
  }
  return ($out -match "OK")
}

function Wait-Stage([string]$Name, [int]$TimeoutSec, [scriptblock]$Check) {
  $start = Now-Date
  $deadline = $start.AddSeconds($TimeoutSec)
  while ((Now-Date) -lt $deadline) {
    if (& $Check) {
      $done = Now-Date
      return @{ Start = $start; Done = $done }
    }
    Write-Host ("WAIT_{0} elapsed={1:n2}s" -f $Name, (ElapsedSec $start))
    Start-Sleep -Seconds $PollSec
  }
  return $null
}

function Server-ReadDiagnosisSmall([string]$RunId) {
  $cmd = @'
REPO="__REPO__"
RID="__RID__"
cd "$REPO"
if [ $? -ne 0 ]; then exit 1; fi
if [ -f "storage/runs/$RID/_server_out/diagnosis.json" ]; then
  cat "storage/runs/$RID/_server_out/diagnosis.json"
elif [ -f "storage/runs/$RID/_server_out/diagnosis_v2.json" ]; then
  cat "storage/runs/$RID/_server_out/diagnosis_v2.json"
fi
'@
  $cmd = $cmd.Replace("__REPO__", $RepoDir).Replace("__RID__", $RunId)
  return Invoke-Ssh $cmd
}

function Server-ReadActionsSmall([string]$RunId) {
  $cmd = @'
REPO="__REPO__"
RID="__RID__"
cd "$REPO"
if [ $? -ne 0 ]; then exit 1; fi
if [ -f "storage/runs/$RID/_server_out/actions_v2.json" ]; then
  cat "storage/runs/$RID/_server_out/actions_v2.json"
elif [ -f "storage/runs/$RID/_server_out/actions.json" ]; then
  cat "storage/runs/$RID/_server_out/actions.json"
fi
'@
  $cmd = $cmd.Replace("__REPO__", $RepoDir).Replace("__RID__", $RunId)
  return Invoke-Ssh $cmd
}

function Extract-DiagnosisText([string]$JsonText) {
  if (-not $JsonText) { return "(not found)" }
  try {
    $obj = $JsonText | ConvertFrom-Json -ErrorAction Stop
    $cands = @()
    foreach ($k in @("narrative","diagnosis_human","narrative_human","root_cause","root_cause_text","summary","reason","diagnosis")) {
      if ($obj.PSObject.Properties.Name -contains $k) {
        $v = $obj.$k
        if ($v) { $cands += $v.ToString() }
      }
    }
    $cands = $cands | Where-Object { $_ -and $_.Trim().Length -gt 0 } | Select-Object -Unique
    if ($cands.Count -eq 0) { return "(empty)" }
    return ($cands -join "; ")
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
    $stack = New-Object System.Collections.ArrayList
    [void]$stack.Add($obj)

    while ($stack.Count -gt 0) {
      $cur = $stack[$stack.Count - 1]
      $stack.RemoveAt($stack.Count - 1)

      if ($cur -is [System.Collections.IDictionary]) {
        foreach ($k in $cur.Keys) { [void]$stack.Add($cur[$k]) }
        continue
      }
      if ($cur -is [System.Collections.IEnumerable] -and -not ($cur -is [string])) {
        foreach ($x in $cur) { [void]$stack.Add($x) }
        continue
      }

      if ($cur -and ($cur.PSObject.Properties.Name -contains "cmd")) {
        $v = $cur.cmd
        if ($v -is [System.Collections.IEnumerable] -and -not ($v -is [string])) {
          $lines += ($v | ForEach-Object { $_.ToString() })
        } else { $lines += $v.ToString() }
      }
      if ($cur -and ($cur.PSObject.Properties.Name -contains "command")) {
        $v = $cur.command
        if ($v -is [System.Collections.IEnumerable] -and -not ($v -is [string])) {
          $lines += ($v | ForEach-Object { $_.ToString() })
        } else { $lines += $v.ToString() }
      }
      if ($cur -and ($cur.PSObject.Properties.Name -contains "name")) {
        $lines += $cur.name.ToString()
      }
    }
  } catch {
    $lines += $JsonText.Trim()
  }
  return ($lines | Where-Object { $_ -and $_.Trim().Length -gt 0 } | ForEach-Object { $_.Trim() } | Select-Object -Unique)
}

# ================= MAIN =================
Write-Host "[server] restart demo_services"
$serverOut = Server-RestartServices
Write-Host $serverOut
if ($serverOut -notmatch ":18080" -or $serverOut -notmatch ":28081") {
  Write-Host "FAIL: SERVER_SERVICES_NOT_LISTENING"
  Write-Host ("HINT: ssh {0} ""cd {1}; bash server_B/tcp/demo_services.sh status""" -f $Server, $RepoDir)
  exit 1
}

Write-Host "[server] clean tcp_inbox residual (avoid old bundle re-consumed)"
Write-Host (Server-CleanInbox)

Write-Host "[board] prepare clean baseline (stop triggerd/inject, rotate log, clear state)"
Write-Host (Board-CleanBaseline)

Write-Host "[board] ensure device_id and start faultmon"
$null = Invoke-Hdc ("echo {0} > /data/faultmon/device_id" -f $DeviceId)
Write-Host (Board-StartFaultmon)

Write-Host "[board] start triggerd daemon"
Write-Host (Board-StartTriggerd $TriggerdArgs)

if (-not (Board-WaitTriggerdAlive 6)) {
  Write-Host ""
  Write-Host "FAIL: TRIGGERD_NOT_ALIVE (pidfile/ps check failed)"
  Write-Host "HINT:"
  Write-Host '  hdc shell "/data/local/tmp/busybox tail -n 200 /data/faultmon/demo_stage2/logs/triggerd.log"'
  Write-Host '  hdc shell "/data/local/tmp/busybox ls -l /data/faultmon/demo_stage2/pids/triggerd.pid"'
  exit 1
}

Write-Host "READY: run injection in another window (example):"
Write-Host '  hdc shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh start"'

$scriptStart = Now-Date

$stage1 = Wait-Stage1 $TimeoutStage1
if (-not $stage1) {
  Write-Host "FAIL: WAIT_STAGE1_TIMEOUT"
  Write-Host 'HINT: hdc shell "/data/local/tmp/busybox tail -n 200 /data/faultmon/demo_stage2/logs/triggerd.log"'
  exit 1
}

$rid = $stage1.Rid
$stage1Sec = [double](($stage1.DoneTime - $stage1.TriggerTime).TotalSeconds)
$total1 = [double](($stage1.DoneTime - $scriptStart).TotalSeconds)
Write-Host ("STAGE1_DONE rid={0} stage1_sec={1:n2} total_sec={2:n2}" -f $rid, $stage1Sec, $total1)

$stage2 = Wait-Stage "STAGE2" $TimeoutStage2 { Server-TestStage2Done $rid }
if (-not $stage2) {
  Write-Host "FAIL: WAIT_STAGE2_TIMEOUT"
  Write-Host ("HINT: ssh {0} ""cd {1}; tail -n 120 storage/logs/watcher.log""" -f $Server, $RepoDir)
  Write-Host ("HINT: ssh {0} ""cd {1}; ls -la storage/runs/{2}/_server_out""" -f $Server, $RepoDir, $rid)
  exit 1
}
$stage2Sec = [double](($stage2.Done - $stage2.Start).TotalSeconds)
$total2 = [double](($stage2.Done - $scriptStart).TotalSeconds)
Write-Host ("STAGE2_DONE rid={0} stage2_sec={1:n2} total_sec={2:n2}" -f $rid, $stage2Sec, $total2)

$stage3 = Wait-Stage "STAGE3" $TimeoutStage3 { Server-TestStage3Done $rid }
if (-not $stage3) {
  Write-Host "FAIL: WAIT_STAGE3_TIMEOUT"
  Write-Host ("HINT: ssh {0} ""cd {1}; ls -la storage/runs/{2}/_action_result""" -f $Server, $RepoDir, $rid)
  exit 1
}
$stage3Sec = [double](($stage3.Done - $stage3.Start).TotalSeconds)
$total3 = [double](($stage3.Done - $scriptStart).TotalSeconds)
Write-Host ("STAGE3_DONE rid={0} stage3_sec={1:n2} total_sec={2:n2}" -f $rid, $stage3Sec, $total3)

$diagJson = Server-ReadDiagnosisSmall $rid
if (-not $diagJson) {
  Write-Host "WARN: diagnosis read empty; dumping _server_out dir and infer.log tail"
  $dbg = Invoke-Ssh ("cd '{0}'; ls -la storage/runs/{1}/_server_out; tail -n 80 storage/runs/{1}/_server_out/infer.log 2>/dev/null" -f $RepoDir, $rid) 20
  if ($dbg) { Write-Host $dbg }
}
$diagnosisText = Extract-DiagnosisText $diagJson
$suspectDebug = @()
if ($env:WK_DEBUG_SUSPECTS -eq "1") {
  $suspectDebug = Extract-SuspectsDebug $diagJson
}

$actionsJson = Server-ReadActionsSmall $rid
if (-not $actionsJson) {
  Write-Host "WARN: actions read empty; dumping _server_out dir and infer.log tail"
  $dbg2 = Invoke-Ssh ("cd '{0}'; ls -la storage/runs/{1}/_server_out; tail -n 80 storage/runs/{1}/_server_out/infer.log 2>/dev/null" -f $RepoDir, $rid) 20
  if ($dbg2) { Write-Host $dbg2 }
}
$actionLines = Extract-ActionsLines $actionsJson

Write-Host "DIAGNOSIS:"
Write-Host $diagnosisText
if ($suspectDebug.Count -gt 0) {
  Write-Host "SUSPECTS(DEBUG):"
  foreach ($line in $suspectDebug) { Write-Host ("- {0}" -f $line) }
}
Write-Host "ACTIONS:"
if ($actionLines.Count -eq 0) {
  Write-Host "- (not found)"
} else {
  foreach ($l in $actionLines) { Write-Host ("- {0}" -f $l) }
}

$outDir = Join-Path -Path (Split-Path -Parent $MyInvocation.MyCommand.Path) -ChildPath "out"
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$reportPath = Join-Path -Path $outDir -ChildPath ("demo_report_{0}.md" -f $rid)

$report = @()
$report += "# Closed-loop Demo Report"
$report += ""
$report += ("RID: {0}" -f $rid)
$report += ("Stage1: {0:n2}s" -f $stage1Sec)
$report += ("Stage2: {0:n2}s" -f $stage2Sec)
$report += ("Stage3: {0:n2}s" -f $stage3Sec)
$report += ("Total:  {0:n2}s" -f $total3)
$report += ""
$report += "## Diagnosis"
$report += $diagnosisText
$report += ""
if ($suspectDebug.Count -gt 0) {
  $report += "## Suspects (Debug)"
  foreach ($line in $suspectDebug) { $report += ("- {0}" -f $line) }
  $report += ""
}
$report += "## Actions"
if ($actionLines.Count -eq 0) { $report += "(not found)" } else { foreach ($l in $actionLines) { $report += ("- {0}" -f $l) } }

$report | Set-Content -Path $reportPath -Encoding utf8
Write-Host ("REPORT: {0}" -f $reportPath)

Write-Host ""
Write-Host "NOTE: stop injection manually:"
Write-Host '  hdc shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop"'
