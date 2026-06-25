# qwen3_server_helpers.ps1
# Purpose: provide SSH/SCP helpers + closed-loop runner for Qwen3 server
# PowerShell 5.1 compatible

$script:Qwen3ServerConfigHelper = Join-Path $PSScriptRoot "tools\\server_config.ps1"
if (Test-Path -LiteralPath $script:Qwen3ServerConfigHelper) {
  . $script:Qwen3ServerConfigHelper
}
$script:HdcTargetHelper = Join-Path $PSScriptRoot "tools\\hdc_target.ps1"
if (Test-Path -LiteralPath $script:HdcTargetHelper) {
  . $script:HdcTargetHelper
}

# ------------------ fallback helpers (only define if missing) ------------------
if (-not (Get-Command Ensure-Dir -ErrorAction SilentlyContinue)) {
  function Ensure-Dir([string]$dir) {
    if ($null -eq $dir -or $dir -eq "") { return }
    if (-not (Test-Path -LiteralPath $dir)) {
      New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
  }
}

if (-not (Get-Command Test-PathSafe -ErrorAction SilentlyContinue)) {
  function Test-PathSafe([string]$p) {
    try { return (Test-Path -LiteralPath $p) } catch { return $false }
  }
}

if (-not (Get-Command Convert-ToBoolFromEnv -ErrorAction SilentlyContinue)) {
  function Convert-ToBoolFromEnv([string]$s, [bool]$defaultValue=$false) {
    if ($null -eq $s -or $s -eq "") { return $defaultValue }
    $t = $s.Trim().ToLowerInvariant()
    return @("1","true","yes","y","on") -contains $t
  }
}

# ------------------ config init ------------------
function Initialize-Qwen3ClosedLoopConfig {
  # Enable by env: WK_ENABLE_QWEN3_CLOSED_LOOP=1
  $script:ENABLE_QWEN3_CLOSED_LOOP = Convert-ToBoolFromEnv $env:WK_ENABLE_QWEN3_CLOSED_LOOP $false

  $sshHost = ""
  if (Get-Command Get-Qwen3SshHost -ErrorAction SilentlyContinue) {
    try { $sshHost = Get-Qwen3SshHost } catch { $sshHost = "" }
  }
  if (-not $sshHost -and $env:QWEN3_SSH_HOST -and $env:QWEN3_SSH_HOST.Trim() -ne "") {
    $sshHost = $env:QWEN3_SSH_HOST.Trim()
  }
  if (-not $sshHost -and $env:QWEN3_SERVER_HOST -and $env:QWEN3_SERVER_HOST.Trim() -ne "") {
    $sshHost = $env:QWEN3_SERVER_HOST.Trim()
  }
  if (-not $sshHost) { $sshHost = "183.56.183.131" }

  # legacy compatibility: WK_QWEN3_SERVER still overrides unified host
  $script:QWEN3_SERVER = if ($env:WK_QWEN3_SERVER -and $env:WK_QWEN3_SERVER.Trim() -ne "") { $env:WK_QWEN3_SERVER } else { $sshHost }

  $script:QWEN3_SSH_KEY = if ($env:WK_QWEN3_SSH_KEY -and $env:WK_QWEN3_SSH_KEY.Trim() -ne "") {
    $env:WK_QWEN3_SSH_KEY
  } else {
    Join-Path $env:USERPROFILE ".ssh\qwen3_server_ed25519"
  }

  $script:QWEN3_REMOTE_ROOT = if ($env:WK_QWEN3_REMOTE_ROOT -and $env:WK_QWEN3_REMOTE_ROOT.Trim() -ne "") {
    $env:WK_QWEN3_REMOTE_ROOT
  } else {
    "/home/xrh/qwen3_os_fault"
  }

  $script:QWEN3_REMOTE_INBOX = if ($env:WK_QWEN3_REMOTE_INBOX -and $env:WK_QWEN3_REMOTE_INBOX.Trim() -ne "") {
    $env:WK_QWEN3_REMOTE_INBOX
  } else {
    ($script:QWEN3_REMOTE_ROOT + "/inbox/win_runs")
  }

  $script:QWEN3_REMOTE_TOOL = if ($env:WK_QWEN3_REMOTE_TOOL -and $env:WK_QWEN3_REMOTE_TOOL.Trim() -ne "") {
    $env:WK_QWEN3_REMOTE_TOOL
  } else {
    "/home/xrh/qwen3_os_fault/closed_loop_demo/server/src/closed_loop_infer_run.py"
  }
    # export to global for run script compatibility
  $global:ENABLE_QWEN3_CLOSED_LOOP = $script:ENABLE_QWEN3_CLOSED_LOOP

}

# ------------------ ssh/scp helpers ------------------
function Qwen3-NormalizeBashCmd([string]$s) {
  if ($null -eq $s) { return "" }
  return ($s -replace "`r`n","; " -replace "`n","; " -replace "`r","; ")
}

function Qwen3-EscapeBashSingleQuotes([string]$s) {
  if ($null -eq $s) { return "" }
  # bash single quote escape:  '  ->  '"'"'
  $repl = "'" + '"' + "'" + '"' + "'"
  return ($s -replace "'", $repl)
}

function Invoke-Qwen3ServerSsh {
  param(
    [Parameter(Mandatory=$true)][string]$BashCmd,
    [string]$Tag = "qwen3-ssh"
  )

  if (-not (Test-PathSafe $script:QWEN3_SSH_KEY)) {
    return @{ exit=2; stdout=""; stderr=("missing ssh key: " + $script:QWEN3_SSH_KEY) }
  }

  # Normalize to avoid CRLF / multiline surprises
  $cmd1 = Qwen3-NormalizeBashCmd $BashCmd
  $cmd1 = $cmd1 -replace "`r",""

  # Base64 encode to avoid all quoting/pipe/regex issues over SSH
  $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($cmd1))

  # Remote execution strategy:
  # - write decoded script to /tmp
  # - run it with bash
  # - preserve exit code
  # IMPORTANT: wrap the inner script with SINGLE QUOTES so outer /bin/sh won't expand $tmp/$?
  $remote =
    'bash -lc ''tmp=/tmp/wk_cmd_$$.sh; ' +
    'echo ' + $b64 + ' | base64 -d > "$tmp"; ' +
    'bash "$tmp"; rc=$?; rm -f "$tmp"; exit $rc'''

  $sshArgs = @(
    "-T",
    "-i", $script:QWEN3_SSH_KEY,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
    $script:QWEN3_SERVER,
    $remote
  )

  $out = (& ssh @sshArgs 2>&1 | Out-String)
  $ec  = $LASTEXITCODE
  return @{ exit=$ec; stdout=$out; stderr="" }
}




function Invoke-Qwen3ServerScpUploadDir {
  param(
    [Parameter(Mandatory=$true)][string]$LocalDir,
    [Parameter(Mandatory=$true)][string]$RemoteParentDir
  )

  if (-not (Test-PathSafe $script:QWEN3_SSH_KEY)) { return 2 }
  if (-not (Test-Path -LiteralPath $LocalDir)) { return 3 }

  $scpArgs = @(
    "-r",
    "-i", $script:QWEN3_SSH_KEY,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    $LocalDir,
    ($script:QWEN3_SERVER + ":" + $RemoteParentDir + "/")
  )
  & scp @scpArgs 2>&1 | Out-Null
  return $LASTEXITCODE
}

function Invoke-Qwen3ServerScpDownloadDir {
  param(
    [Parameter(Mandatory=$true)][string]$RemoteDir,
    [Parameter(Mandatory=$true)][string]$LocalDestDir
  )

  if (-not (Test-PathSafe $script:QWEN3_SSH_KEY)) { return 2 }

  # 寮哄埗锛氭竻绌哄苟閲嶅缓鐩爣鐩綍锛屼繚璇佽鐩?
  Remove-Item -LiteralPath $LocalDestDir -Recurse -Force -ErrorAction SilentlyContinue
  Ensure-Dir $LocalDestDir

  # 鍏抽敭锛氬鍒垛€滅洰褰曞唴瀹光€濊€屼笉鏄€滅洰褰曟湰韬€濓紝閬垮厤 _server_out\_server_out 宓屽
  $remote = ($script:QWEN3_SERVER + ":" + $RemoteDir + "/.")
  $scpArgs = @(
    "-r",
    "-i", $script:QWEN3_SSH_KEY,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    $remote,
    $LocalDestDir
  )
  & scp @scpArgs 2>&1 | Out-Null
  return $LASTEXITCODE
}
function Invoke-Qwen3UploadLocalFileToRemote {
  param(
    [Parameter(Mandatory=$true)][string]$LocalPath,
    [Parameter(Mandatory=$true)][string]$RemotePath
  )
  if (-not (Test-Path -LiteralPath $LocalPath)) { return 2 }
  if (-not (Test-PathSafe $script:QWEN3_SSH_KEY)) { return 2 }

  $scpArgs = @(
    "-i", $script:QWEN3_SSH_KEY,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    $LocalPath,
    ($script:QWEN3_SERVER + ":" + $RemotePath)
  )
  & scp @scpArgs 2>&1 | Out-Null
  return $LASTEXITCODE
}


# ------------------ closed-loop runner ------------------
function Invoke-Qwen3ClosedLoopForRun {
  param(
    [Parameter(Mandatory=$true)][string]$RunDir
  )

  $rid = Split-Path $RunDir -Leaf
  $remoteParent = $script:QWEN3_REMOTE_INBOX
  $remoteRunDir = ($remoteParent + "/" + $rid)
  $remoteOutDir = ($remoteRunDir + "/_server_out")

  # 1) remote prepare: always create _server_out so download never fails due to missing dir
  $prepCmd = "mkdir -p '$remoteParent'; rm -rf '$remoteRunDir'; mkdir -p '$remoteRunDir'; mkdir -p '$remoteOutDir';"
  $r1 = Invoke-Qwen3ServerSsh -BashCmd $prepCmd -Tag "prep"
  if ($r1.exit -ne 0) {
    return @{ ok=$false; step="prep"; exit=$r1.exit; msg=$r1.stdout; remote_run=$remoteRunDir; remote_out=$remoteOutDir }
  }

  # 2) upload run dir
  $up = Invoke-Qwen3ServerScpUploadDir -LocalDir $RunDir -RemoteParentDir $remoteParent
  if ($up -ne 0) {
    # still try to download (out dir exists) so caller can see prep artifacts
    return @{ ok=$false; step="upload"; exit=$up; msg="scp upload failed"; remote_run=$remoteRunDir; remote_out=$remoteOutDir }
  }

    # 3) infer on server (NO single quotes in command to avoid quote-escape hell)
  $tool = $script:QWEN3_REMOTE_TOOL
  $venvActivate = ($script:QWEN3_REMOTE_ROOT + "/.venv_qwen3/bin/activate")
# ---- pass low-vram policy to server via CLI args (do NOT rely on env passthrough) ----
$policy = $env:WK_QWEN3_LOW_VRAM_POLICY
if ([string]::IsNullOrWhiteSpace($policy)) { $policy = "skip" }
$policy = $policy.Trim().Trim('"').Trim("'").ToLower()
if ($policy -notin @("skip","try","wait")) { $policy = "skip" }

$minFree = $env:WK_QWEN3_MIN_FREE_MIB
$pollSec = $env:WK_QWEN3_WAIT_POLL_SEC
$maxSec  = $env:WK_QWEN3_WAIT_MAX_SEC
$waitSec = $env:WK_QWEN3_LOW_VRAM_WAIT_SEC

$extraArgs = "--low_vram_policy $policy"
if ($minFree -match '^\d+$') { $extraArgs += " --min_free_mib $minFree" }
if ($waitSec -match '^\d+$') { $extraArgs += " --low_vram_wait_sec $waitSec" }
if ($pollSec -match '^\d+$') { $extraArgs += " --wait_poll_sec $pollSec" }
if ($maxSec  -match '^\d+$') { $extraArgs += " --wait_max_sec $maxSec" }

  $inferCmd = @(
    "set +e",
    "mkdir -p $remoteOutDir",
    "date > $remoteOutDir/started.txt",
    "echo [meta] run_dir=$remoteRunDir > $remoteOutDir/infer.log",
    "echo [meta] tool=$tool >> $remoteOutDir/infer.log",
    "echo [meta] venv=$venvActivate >> $remoteOutDir/infer.log",
    "ls -la $remoteRunDir >> $remoteOutDir/infer.log 2>&1",

    "cd $script:QWEN3_REMOTE_ROOT",
    ". .venv_qwen3/bin/activate",

    "echo [meta] low_vram_args=$extraArgs >> $remoteOutDir/infer.log",
    "python $tool --run_dir $remoteRunDir --out_dir $remoteOutDir $extraArgs >> $remoteOutDir/infer.log 2>&1",
    "echo `$? > $remoteOutDir/infer_ec.txt",

    "ls -la $remoteOutDir >> $remoteOutDir/infer.log 2>&1"
  ) -join "; "

  $r2 = Invoke-Qwen3ServerSsh -BashCmd $inferCmd -Tag "infer"

  # ignore r2.exit (we force logging); proceed to download

  # 4) download _server_out into local run dir
  $localOut = Join-Path $RunDir "_server_out"
  $localOut = Join-Path $RunDir "_server_out"
  $down = Invoke-Qwen3ServerScpDownloadDir -RemoteDir $remoteOutDir -LocalDestDir $localOut
if ($down -ne 0) {
  $ls = Invoke-Qwen3ServerSsh -BashCmd ("ls -la '$remoteRunDir' 2>&1; echo '---'; ls -la '$remoteOutDir' 2>&1;") -Tag "ls"
  return @{
    ok=$false; step="download"; exit=$down; msg="scp download failed";
    remote_run=$remoteRunDir; remote_out=$remoteOutDir;
    infer_out=$r2.stdout; remote_ls=$ls.stdout
  }
}

if (-not (Test-Path -LiteralPath (Join-Path $localOut "diagnosis.json"))) {
  return @{ ok=$false; step="verify"; exit=4; msg="diagnosis.json missing after download"; local_out=$localOut; remote_out=$remoteOutDir }
}


  return @{
    ok=$true; step="done"; exit=0;
    remote_run=$remoteRunDir; remote_out=$remoteOutDir;
    local_out=(Join-Path $RunDir "_server_out")
  }
}


# initialize on import
Initialize-Qwen3ClosedLoopConfig
function Test-WkBoolEnv([string]$Name, [bool]$Default=$false) {
  $v = (Get-Item -Path ("Env:" + $Name) -ErrorAction SilentlyContinue).Value
  if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
  $t = $v.Trim().ToLowerInvariant()
  return @("1","true","yes","y","on") -contains $t
}


function Invoke-WkApplyActionsFromServerOut {
  param(
    [Parameter(Mandatory=$true)][string]$RunDir,
    [AllowEmptyString()][string]$DeviceTarget
  )

  # ---- resolve DeviceTarget to unified HDC target ----
  if (Get-Command Resolve-HdcTarget -ErrorAction SilentlyContinue) {
    $DeviceTarget = Resolve-HdcTarget -Target $DeviceTarget
  } elseif ([string]::IsNullOrWhiteSpace($DeviceTarget)) {
    $DeviceTarget = "192.168.3.28:8711"
  }

  $apply = Test-WkBoolEnv "WK_APPLY_ACTIONS" $false
  Write-Host ("[actions] WK_APPLY_ACTIONS=" + ($(if ($apply) { "1" } else { "0" })))

  if (-not $apply) {
    Write-Host "[actions] skip execution."
    return
  }

  $serverOut = Join-Path $RunDir "_server_out"
  $execLog   = Join-Path $serverOut "actions_exec.log"
  $resultPath= Join-Path $serverOut "actions_exec_result.json"

  Ensure-Dir $serverOut

  function Write-ExecResult([string]$note, $results, [string]$family, [string]$sourceActions) {
    $payload = [pscustomobject]@{
      ts      = (Get-Date).ToString("s")
      run_id  = (Split-Path $RunDir -Leaf)
      device  = $DeviceTarget
      applied = $true
      family  = $family
      actions_source = $sourceActions
      note    = $note
      count   = ($results | Measure-Object).Count
      results = $results
    }
    try {
      ($payload | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $resultPath -Encoding utf8 -ErrorAction Stop
      Write-Host "[actions] wrote $resultPath"
    } catch {
      Write-Host ("[actions] ERROR: failed to write result: " + $_.Exception.Message)
    }
  }

  function Get-JsonPathAny([string[]]$candidates, [int]$waitSec) {
    $p = $null
    for ($i=0; $i -lt $waitSec; $i++) {
      foreach ($x in $candidates) {
        if (Test-Path -LiteralPath $x) { $p = $x; break }
      }
      if ($p) { break }
      Start-Sleep -Seconds 1
    }
    if (-not $p) {
      foreach ($x in $candidates) {
        $base = Split-Path -Parent $x
        if (Test-Path -LiteralPath $base) {
          $found = Get-ChildItem -LiteralPath $base -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -in @("actions_v2.json","actions.json","diagnosis_v2.json","diagnosis.json") } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
          if ($found) { $p = $found.FullName; break }
        }
      }
    }
    return $p
  }

  function Read-FamilyFromDiagnosis([string]$serverOutDir) {
    $cand = @(
      (Join-Path $serverOutDir "diagnosis_v2.json"),
      (Join-Path (Join-Path $serverOutDir "_server_out") "diagnosis_v2.json"),
      (Join-Path $serverOutDir "diagnosis.json"),
      (Join-Path (Join-Path $serverOutDir "_server_out") "diagnosis.json")
    )
    foreach ($p in $cand) {
      if (Test-Path -LiteralPath $p) {
        try {
          $t = Get-Content -LiteralPath $p -Raw -Encoding utf8
          $o = $t | ConvertFrom-Json
          if ($o -and $o.family) { return [string]$o.family }
        } catch { }
      }
    }
    return ""
  }

  function Sha1Hex([string]$s) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($s)
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    $hash = $sha1.ComputeHash($bytes)
    return ([System.BitConverter]::ToString($hash) -replace "-", "").ToLowerInvariant()
  }

  # ---- only allow cpu/mem families (per your requirement) ----
  $allowFamilies = @("cpu","mem")
  if ($env:WK_ACTIONS_FAMILY_ALLOW -and $env:WK_ACTIONS_FAMILY_ALLOW.Trim() -ne "") {
    $allowFamilies = @($env:WK_ACTIONS_FAMILY_ALLOW.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne "" })
  }

  $family = (Read-FamilyFromDiagnosis $serverOut)
  if ([string]::IsNullOrWhiteSpace($family)) { $family = "unknown" }
  $familyNorm = $family.Trim().ToLowerInvariant()

  if (-not ($allowFamilies -contains $familyNorm)) {
    Write-Host ("[actions] skip: family=" + $familyNorm + " not in {" + ($allowFamilies -join ",") + "}")
    Write-ExecResult ("skip_family_" + $familyNorm) @() $familyNorm ""
    return
  }

  # ---- wait + choose actions file (prefer v2) ----
  $waitSec = 60
  if ($env:WK_ACTIONS_WAIT_SEC) { try { $waitSec = [int]$env:WK_ACTIONS_WAIT_SEC } catch { } }

  $candidatesActions = @(
    (Join-Path $serverOut "actions_v2.json"),
    (Join-Path (Join-Path $serverOut "_server_out") "actions_v2.json"),
    (Join-Path $serverOut "actions.json"),
    (Join-Path (Join-Path $serverOut "_server_out") "actions.json")
  )
  $actionsPath = Get-JsonPathAny $candidatesActions $waitSec
  if (-not $actionsPath) {
    Write-Host "[actions] actions_v2.json/actions.json not found under: $serverOut"
    Write-ExecResult "actions_json_missing" @() $familyNorm ""
    return
  }
  Write-Host "[actions] using actions: $actionsPath"

  $jsonText = Get-Content -LiteralPath $actionsPath -Raw -Encoding utf8
  if ([string]::IsNullOrWhiteSpace($jsonText)) {
    Write-Host "[actions] actions json empty"
    Write-ExecResult "actions_json_empty" @() $familyNorm $actionsPath
    return
  }

  try { $obj = $jsonText | ConvertFrom-Json } catch {
    Write-Host "[actions] failed to parse actions json"
    Write-ExecResult "actions_json_parse_failed" @() $familyNorm $actionsPath
    return
  }

  $actions = @()
  if ($obj.actions) { $actions = @($obj.actions) }

  # ---- whitelist (collect only by default) ----
  $safeCmdPrefixes = @(
    "dmesg",
    "logcat",
    "hilog",
    "hilogctl",
    "ps",
    "top",
    "cat /proc/",
    "ls ",
    "echo "
  )

  # ---- idempotency ----
  $idempotent = Test-WkBoolEnv "WK_ACTIONS_IDEMPOTENT" $true
  if ($idempotent -and (Test-Path -LiteralPath $execLog)) {
    # ok
  } else {
    # make sure file exists so Select-String doesn't fail later
    "" | Out-File -LiteralPath $execLog -Encoding utf8 -Append
  }

  $results = @()

  foreach ($a in $actions) {
    $atype  = [string]$a.type
    $target = [string]$a.target
    $cmd    = [string]$a.cmd

    if ([string]::IsNullOrWhiteSpace($cmd)) { continue }

    # only device target
    if ($target -ne "device") {
      $results += [pscustomobject]@{ type=$atype; target=$target; cmd=$cmd; executed=$false; ok=$false; note="skip_non_device_target" }
      continue
    }

    # only collect (keep ultra-safe)
    if ($atype -ne "collect") {
      $results += [pscustomobject]@{ type=$atype; target=$target; cmd=$cmd; executed=$false; ok=$false; note="skip_non_collect_type" }
      continue
    }

    $allowed = $false
    foreach ($p in $safeCmdPrefixes) {
      if ($cmd.StartsWith($p)) { $allowed = $true; break }
    }
    if (-not $allowed) {
      Add-Content -LiteralPath $execLog -Encoding utf8 -Value ("BLOCK: $atype/$target :: $cmd")
      $results += [pscustomobject]@{ type=$atype; target=$target; cmd=$cmd; executed=$false; ok=$false; note="blocked_by_whitelist" }
      continue
    }

    # action key for idempotency
    $keyRaw = ($atype + "|" + $target + "|" + $cmd)
    $key = Sha1Hex $keyRaw

    if ($idempotent) {
      $already = $false
      try {
        $m = Select-String -LiteralPath $execLog -Pattern ("DONE\[" + $key + "\]") -SimpleMatch -ErrorAction SilentlyContinue
        if ($m) { $already = $true }
      } catch { }
      if ($already) {
        $results += [pscustomobject]@{ type=$atype; target=$target; cmd=$cmd; executed=$false; ok=$true; note=("skip_already_done:" + $key) }
        continue
      }
    }

    Add-Content -LiteralPath $execLog -Encoding utf8 -Value ("BEGIN[" + $key + "]: $atype/$target :: $cmd")
    $out = (& hdc -t $DeviceTarget shell $cmd 2>&1 | Out-String)
    $ec = $LASTEXITCODE
    Add-Content -LiteralPath $execLog -Encoding utf8 -Value $out
    Add-Content -LiteralPath $execLog -Encoding utf8 -Value ("DONE[" + $key + "]: exit=" + $ec)

    $results += [pscustomobject]@{ type=$atype; target=$target; cmd=$cmd; executed=$true; ok=($ec -eq 0); exit=$ec; key=$key }
  }

  Write-ExecResult "ok" $results $familyNorm $actionsPath

  # ---- upload action outputs back to server (best-effort, NO re-run closed loop) ----
  if ($env:WK_UPLOAD_ACTION_OUTPUTS -and $env:WK_UPLOAD_ACTION_OUTPUTS -ne "0") {
    try {
      $rid = Split-Path $RunDir -Leaf
      $remoteRunDir = ($script:QWEN3_REMOTE_INBOX + "/" + $rid)
      $remoteOutDir = ($remoteRunDir + "/_server_out")
      Invoke-Qwen3UploadLocalFileToRemote -LocalPath $execLog -RemotePath ($remoteOutDir + "/actions_exec.log") | Out-Null
      Invoke-Qwen3UploadLocalFileToRemote -LocalPath $resultPath -RemotePath ($remoteOutDir + "/actions_exec_result.json") | Out-Null
      Write-Host "[actions] uploaded exec outputs back to server (best-effort)"
    } catch {
      Write-Host ("[actions] upload back to server failed: " + $_.Exception.Message)
    }
  }
}





