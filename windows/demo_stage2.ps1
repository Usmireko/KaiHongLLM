param(
  [string]$Target = "",
  [string]$Server = "qwen3-server",
  [string]$RepoDir = "/home/xrh/qwen3_os_fault",
  [string]$DeviceId = "dev1",
  [int]$TimeoutStage1 = 240,
  [int]$TimeoutStage2 = 240,
  [int]$TimeoutStage3 = 240,
  [int]$PollSec = 2,
  [string]$TriggerdArgs = "",
  [switch]$Help
)

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$OutputEncoding = [Console]::OutputEncoding

function Show-Usage {
  Write-Host "Usage: powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1 [options]"
  Write-Host "  -Target <hdc id>         (auto-detect USB Connected if empty)"
  Write-Host "  -Server <ssh alias>      (default: qwen3-server)"
  Write-Host "  -RepoDir <path>          (default: /home/xrh/qwen3_os_fault)"
  Write-Host "  -DeviceId <id>           (default: dev1)"
  Write-Host "  -TimeoutStage1 <sec>     (default: 240)"
  Write-Host "  -TimeoutStage2 <sec>     (default: 240)"
  Write-Host "  -TimeoutStage3 <sec>     (default: 240)"
  Write-Host "  -PollSec <sec>           (default: 2)"
  Write-Host "  -TriggerdArgs <string>   (extra args for triggerd --daemon)"
  Write-Host ""
  Write-Host "Example:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1"
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1 -TriggerdArgs '--mode cpu --interval 2 --hit_need 3 --cpu_hit_need 3'"
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
  $Cmd = $Cmd -replace "`r", ""
  $out = @(& hdc -t $Target shell $Cmd 2>&1)
  return ($out -join "`n").Trim()
}

function Deploy-BoardScripts {
  # $MyInvocation.MyCommand.Path 在函数里可能为 null；用脚本根目录更可靠
  $scriptDir = $PSScriptRoot
  if (-not $scriptDir) { $scriptDir = Split-Path -Parent $PSCommandPath }
  if (-not $scriptDir) { throw "Cannot locate script dir (PSScriptRoot/PSCommandPath empty)." }

  $repoRoot = Split-Path -Parent $scriptDir

  $srcReal = Join-Path $repoRoot "board\\data\\faultmon\\demo_stage2\\bin\\bundle_real_upload.sh"
  $srcManual = Join-Path $repoRoot "board\\data\\faultmon\\demo_stage2\\bin\\bundle_manual.sh"
  Write-Host "[board] deploy bundle scripts"
  if (-not (Test-Path -Path $srcReal)) { throw "bundle_real_upload.sh not found at $srcReal" }
  if (-not (Test-Path -Path $srcManual)) { throw "bundle_manual.sh not found at $srcManual" }
  $out1 = @(& hdc -t $Target file send $srcReal /data/faultmon/demo_stage2/bin/bundle_real_upload.sh 2>&1)
  $out2 = @(& hdc -t $Target file send $srcManual /data/faultmon/demo_stage2/bin/bundle_manual.sh 2>&1)
  if ($out1) { Write-Host ($out1 -join "`n") }
  if ($out2) { Write-Host ($out2 -join "`n") }
  Write-Host (Invoke-Hdc "/data/local/tmp/busybox chmod 755 /data/faultmon/demo_stage2/bin/bundle_real_upload.sh /data/faultmon/demo_stage2/bin/bundle_manual.sh")

# Optional but recommended: keep stage2 board scripts in sync with repo copy.
# This avoids "board still running old inject_mem.sh / triggerd.sh" after a local fix.
$extra = @(
  @{ Name="triggerd.sh";   Src=(Join-Path $repoRoot "board\\data\\faultmon\\demo_stage2\\bin\\triggerd.sh");   Dst="/data/faultmon/demo_stage2/bin/triggerd.sh" },
  @{ Name="inject_cpu.sh"; Src=(Join-Path $repoRoot "board\\data\\faultmon\\demo_stage2\\bin\\inject_cpu.sh"); Dst="/data/faultmon/demo_stage2/bin/inject_cpu.sh" },
  @{ Name="inject_mem.sh"; Src=(Join-Path $repoRoot "board\\data\\faultmon\\demo_stage2\\bin\\inject_mem.sh"); Dst="/data/faultmon/demo_stage2/bin/inject_mem.sh" }
)

foreach ($e in $extra) {
  if (Test-Path -Path $e.Src) {
    $outX = @(& hdc -t $Target file send $e.Src $e.Dst 2>&1)
    if ($outX) { Write-Host ($outX -join "`n") }
    # ensure executable
    $null = Invoke-Hdc ("/data/local/tmp/busybox chmod 755 {0}" -f $e.Dst)
  } else {
    Write-Host ("WARN: {0} not found at {1} (skip deploy)" -f $e.Name, $e.Src)
  }
}
}

function Now-Date { Get-Date }
function Sec([datetime]$a, [datetime]$b) { [math]::Round(($b - $a).TotalSeconds, 2) }

# ---------------- Stage1: detect trigger_bundle -> upload OK ----------------

function Get-LastTriggerBundleRunId {
  $cmd = "/data/local/tmp/busybox tail -n 500 /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null | " +
         "/data/local/tmp/busybox grep -F -e 'trigger_bundle run_id=' | " +
         "/data/local/tmp/busybox tail -n 1"
  $line = Invoke-Hdc $cmd
  if ($line -match "run_id=([A-Za-z0-9_]+)") { return $Matches[1] }
  return ""
}

function Has-UploadOk([string]$RunId) {
  if (-not $RunId) { return $false }
  $cmd = "/data/local/tmp/busybox tail -n 800 /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null | " +
         "/data/local/tmp/busybox grep -F -e 'OK: uploaded type=bundle' | " +
         "/data/local/tmp/busybox grep -F -e 'device=$DeviceId' | " +
         "/data/local/tmp/busybox grep -F -e 'run=$RunId' | " +
         "/data/local/tmp/busybox tail -n 1"
  $line = Invoke-Hdc $cmd
  if ($line) { return $true }
  return $false
}

function Get-TriggerLockFlag {
  $cmd = "/data/local/tmp/busybox test -f /data/faultmon/state/trigger.active; if [ `$? -eq 0 ]; then echo 1; else echo 0; fi"
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

function Wait-Stage([string]$Name, [int]$TimeoutSec, [scriptblock]$Check) {
  $t0 = Now-Date
  $deadline = $t0.AddSeconds($TimeoutSec)
  while ((Now-Date) -lt $deadline) {
    if (& $Check) {
      $t1 = Now-Date
      return @{ Start=$t0; Done=$t1 }
    }
    Write-Host ("WAIT_{0} elapsed={1}s" -f $Name, (Sec $t0 (Now-Date)))
    Start-Sleep -Seconds $PollSec
  }
  return $null
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

# ===================== main =====================

Write-Host "[server] restart demo_services"
$restartOut = Invoke-Ssh ("cd '{0}'; bash server_B/tcp/demo_services.sh restart" -f $RepoDir)
Write-Host $restartOut

$statusOut = Invoke-Ssh ("cd '{0}'; bash server_B/tcp/demo_services.sh status" -f $RepoDir)
Write-Host $statusOut

if (($statusOut -notmatch ":18080") -or ($statusOut -notmatch ":28081")) {
  Write-Host "FAIL: SERVER_SERVICES_NOT_LISTENING"
  Write-Host "HINT:"
  Write-Host ("  ssh {0}" -f $Server)
  Write-Host ("  cd {0}" -f $RepoDir)
  Write-Host ("  bash server_B/tcp/demo_services.sh status")
  exit 1
}

Write-Host "[board] prepare clean baseline (stop triggerd/inject, rotate log, clear state)"
# one-liner, no here-string, no $(...)
$prepCmd =
"BB=/data/local/tmp/busybox; " +
"sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop >/dev/null 2>/dev/null; sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop >/dev/null 2>/dev/null; " +
"sh /data/faultmon/demo_stage2/bin/triggerd.sh stop >/dev/null 2>/dev/null; " +
"$BB mkdir -p /data/faultmon/demo_stage2/logs /data/faultmon/state >/dev/null 2>/dev/null; " +
"ts=$($prep='')"

# NOTE: avoid PS $(...) by not using it. Use busybox date without command substitution:
$prepCmd =
"BB=/data/local/tmp/busybox; " +
"sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop >/dev/null 2>/dev/null; sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop >/dev/null 2>/dev/null; " +
"sh /data/faultmon/demo_stage2/bin/triggerd.sh stop >/dev/null 2>/dev/null; " +
"$BB mkdir -p /data/faultmon/demo_stage2/logs /data/faultmon/state >/dev/null 2>/dev/null; " +
"ts=`$($BB date +%Y%m%d_%H%M%S 2>/dev/null)`; " +
"if [ -z ""$ts"" ]; then ts=`$(date +%Y%m%d_%H%M%S 2>/dev/null)`; fi; " +
"if [ -z ""$ts"" ]; then ts=unknown; fi; " +
"if [ -s /data/faultmon/demo_stage2/logs/triggerd.log ]; then $BB mv /data/faultmon/demo_stage2/logs/triggerd.log /data/faultmon/demo_stage2/logs/triggerd.log.$ts 2>/dev/null; fi; " +
": > /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null; " +
"$BB rm -f /data/faultmon/state/trigger.active /data/faultmon/state/last_trigger_epoch /data/faultmon/state/last_trigger.json 2>/dev/null; " +
"echo CLEAN_OK"

# IMPORTANT: In PowerShell 5.1, backtick is escape char, but we're passing literal to device.
# We must prevent PowerShell from interpreting `$(...)` here, so we wrap the whole command in single quotes at invocation time:
# We'll send via Invoke-Hdc with a single-quoted literal string built by concatenation WITHOUT any PS $().
$prepCmd =
'BB=/data/local/tmp/busybox; ' +
'sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop >/dev/null 2>/dev/null; sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop >/dev/null 2>/dev/null; ' +
'sh /data/faultmon/demo_stage2/bin/triggerd.sh stop >/dev/null 2>/dev/null; ' +
'$BB mkdir -p /data/faultmon/demo_stage2/logs /data/faultmon/state >/dev/null 2>/dev/null; ' +
'ts=$($BB date +%Y%m%d_%H%M%S 2>/dev/null); ' +
'if [ -z "$ts" ]; then ts=$(date +%Y%m%d_%H%M%S 2>/dev/null); fi; ' +
'if [ -z "$ts" ]; then ts=unknown; fi; ' +
'if [ -s /data/faultmon/demo_stage2/logs/triggerd.log ]; then $BB mv /data/faultmon/demo_stage2/logs/triggerd.log /data/faultmon/demo_stage2/logs/triggerd.log.$ts 2>/dev/null; fi; ' +
': > /data/faultmon/demo_stage2/logs/triggerd.log 2>/dev/null; ' +
'$BB rm -f /data/faultmon/state/trigger.active /data/faultmon/state/last_trigger_epoch /data/faultmon/state/last_trigger.json 2>/dev/null; ' +
'echo CLEAN_OK'

# BUT: $() in the above is BASH and safe because this is a single-quoted PS string (no interpolation).
Write-Host (Invoke-Hdc $prepCmd)

Deploy-BoardScripts

Write-Host "[board] ensure device_id and start faultmon"
$null = Invoke-Hdc ("echo {0} > /data/faultmon/device_id" -f $DeviceId)
Write-Host (Invoke-Hdc "sh /data/faultmon/faultmon.sh start")

Write-Host "[board] start triggerd daemon"
$tdArgs = $TriggerdArgs.Trim()
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
  Write-Host '  hdc shell "/data/local/tmp/busybox tail -n 200 /data/faultmon/demo_stage2/logs/triggerd.log"'
  exit 1
}

$scriptStart = Now-Date
Write-Host "READY: run injection in another window (metrics will change until trigger)."

$stage1 = Wait-Stage1 -TimeoutSec $TimeoutStage1
if (-not $stage1) {
  Write-Host "FAIL: WAIT_STAGE1_TIMEOUT"
  Write-Host "HINT:"
  Write-Host '  hdc shell "/data/local/tmp/busybox tail -n 240 /data/faultmon/demo_stage2/logs/triggerd.log"'
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
  Write-Host ("FAIL: PIDSTAT_MISSING rid={0}" -f $rid)
  $lsProcs = Invoke-Ssh ("cd '{0}'; ls -la storage/runs/{1}/procs 2>/dev/null" -f $RepoDir, $rid) 20
  if ($lsProcs) { Write-Host $lsProcs }
  exit 1
}


$stage2 = Wait-Stage -Name "STAGE2" -TimeoutSec $TimeoutStage2 -Check { Test-Stage2Done $rid }
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

$stage3 = Wait-Stage -Name "STAGE3" -TimeoutSec $TimeoutStage3 -Check { Test-Stage3Done $rid }
if (-not $stage3) {
  Write-Host "FAIL: WAIT_STAGE3_TIMEOUT"
  Write-Host "HINT:"
  Write-Host ("  ssh {0}" -f $Server)
  Write-Host ("  cd {0}" -f $RepoDir)
  Write-Host ("  ls -la storage/runs/{0}/_action_result" -f $rid)
  exit 1
}
$stage3Sec = Sec $stage3.Start $stage3.Done
$total3 = Sec $scriptStart $stage3.Done
Write-Host ("STAGE3_DONE rid={0} stage3_sec={1} total_sec={2}" -f $rid, $stage3Sec, $total3)

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
if [ -f 'storage/runs/$rid/_server_out/actions_v2.json' ]; then
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
  foreach ($line in $actionLines) { Write-Host ("- {0}" -f $line) }
}

$outDir = Join-Path -Path (Split-Path -Parent $MyInvocation.MyCommand.Path) -ChildPath "out"
if (-not (Test-Path -Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$reportPath = Join-Path -Path $outDir -ChildPath ("demo_report_{0}.md" -f $rid)

$report = @()
$report += "# Stage2 Demo Report"
$report += ""
$report += ("RID: {0}" -f $rid)
$report += ("Stage1(trigger->upload): {0}s" -f $stage1Sec)
$report += ("Stage2(infer):           {0}s" -f $stage2Sec)
$report += ("Stage3(action):          {0}s" -f $stage3Sec)
$report += ("Total:                  {0}s" -f $total3)
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
if ($actionLines.Count -eq 0) {
  $report += "- (not found)"
} else {
  foreach ($x in $actionLines) { $report += ("- {0}" -f $x) }
}

$report | Set-Content -Path $reportPath -Encoding utf8
Write-Host ("REPORT: {0}" -f $reportPath)

Write-Host ""
Write-Host "NOTE: remember to stop injection manually in the other window:"
$mode = ""
if ($TriggerdArgs -match "(^|\s)--mode\s+(\S+)") { $mode = $Matches[2].Trim() }
if (-not $mode -and $TriggerdArgs -match "(^|\s)--mode=(\S+)") { $mode = $Matches[2].Trim() }

if ($mode -eq "mem") {
  Write-Host '  hdc shell "sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop"'
} elseif ($mode -eq "cpu") {
  Write-Host '  hdc shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop"'
} else {
  # Unknown / multi mode: print both to be safe.
  Write-Host '  hdc shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop"'
  Write-Host '  hdc shell "sh /data/faultmon/demo_stage2/bin/inject_mem.sh stop"'
}
