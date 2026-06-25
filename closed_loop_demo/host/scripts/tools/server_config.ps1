# Unified server endpoint configuration for Windows scripts.
# Priority: environment > tools/config.local.ps1 > defaults.

$script:Qwen3ConfigDefaults = @{
  Host        = "183.56.183.131"
  SshUser     = "xrh"
  SshPort     = 1122
  IngestPort  = 18080
  ActionsPort = 28081
}
$script:Qwen3ConfigLoaded = $false

function Import-Qwen3LocalConfig {
  if ($script:Qwen3ConfigLoaded) { return }
  $script:Qwen3ConfigLoaded = $true

  $cfgPath = Join-Path $PSScriptRoot "config.local.ps1"
  if (Test-Path -LiteralPath $cfgPath) {
    . $cfgPath
  }
}

function Get-Qwen3LocalValue {
  param(
    [string[]]$VariableNames,
    [string[]]$FunctionNames
  )

  foreach ($fn in $FunctionNames) {
    if (Get-Command -Name $fn -ErrorAction SilentlyContinue) {
      try {
        $v = & $fn
        if ($null -ne $v -and [string]$v -ne "") { return [string]$v }
      } catch {}
    }
  }

  foreach ($vn in $VariableNames) {
    if (Get-Variable -Name $vn -Scope Script -ErrorAction SilentlyContinue) {
      $v = Get-Variable -Name $vn -Scope Script -ValueOnly -ErrorAction SilentlyContinue
      if ($null -ne $v -and [string]$v -ne "") { return [string]$v }
    }
    if (Get-Variable -Name $vn -Scope Global -ErrorAction SilentlyContinue) {
      $v = Get-Variable -Name $vn -Scope Global -ValueOnly -ErrorAction SilentlyContinue
      if ($null -ne $v -and [string]$v -ne "") { return [string]$v }
    }
  }
  return ""
}

function Get-Qwen3ServerHost {
  Import-Qwen3LocalConfig
  if ($env:QWEN3_SERVER_HOST -and $env:QWEN3_SERVER_HOST.Trim() -ne "") {
    return $env:QWEN3_SERVER_HOST.Trim()
  }
  $local = Get-Qwen3LocalValue -VariableNames @("QWEN3_SERVER_HOST", "Qwen3ServerHost") -FunctionNames @("Get-LocalQwen3ServerHost")
  if ($local -and $local.Trim() -ne "") { return $local.Trim() }
  return $script:Qwen3ConfigDefaults.Host
}

function Get-Qwen3IngestPort {
  Import-Qwen3LocalConfig
  $raw = ""
  if ($env:QWEN3_INGEST_PORT -and $env:QWEN3_INGEST_PORT.Trim() -ne "") {
    $raw = $env:QWEN3_INGEST_PORT.Trim()
  } else {
    $raw = Get-Qwen3LocalValue -VariableNames @("QWEN3_INGEST_PORT", "Qwen3IngestPort") -FunctionNames @("Get-LocalQwen3IngestPort")
  }
  $p = 0
  if ([int]::TryParse([string]$raw, [ref]$p) -and $p -gt 0 -and $p -le 65535) { return $p }
  return [int]$script:Qwen3ConfigDefaults.IngestPort
}

function Get-Qwen3SshUser {
  Import-Qwen3LocalConfig
  if ($env:QWEN3_SSH_USER -and $env:QWEN3_SSH_USER.Trim() -ne "") {
    return $env:QWEN3_SSH_USER.Trim()
  }
  $local = Get-Qwen3LocalValue -VariableNames @("QWEN3_SSH_USER", "Qwen3SshUser") -FunctionNames @("Get-LocalQwen3SshUser")
  if ($local -and $local.Trim() -ne "") { return $local.Trim() }
  return $script:Qwen3ConfigDefaults.SshUser
}

function Get-Qwen3SshPort {
  Import-Qwen3LocalConfig
  $raw = ""
  if ($env:QWEN3_SSH_PORT -and $env:QWEN3_SSH_PORT.Trim() -ne "") {
    $raw = $env:QWEN3_SSH_PORT.Trim()
  } else {
    $raw = Get-Qwen3LocalValue -VariableNames @("QWEN3_SSH_PORT", "Qwen3SshPort") -FunctionNames @("Get-LocalQwen3SshPort")
  }
  $p = 0
  if ([int]::TryParse([string]$raw, [ref]$p) -and $p -gt 0 -and $p -le 65535) { return $p }
  return [int]$script:Qwen3ConfigDefaults.SshPort
}

function Get-Qwen3ActionsPort {
  Import-Qwen3LocalConfig
  $raw = ""
  if ($env:QWEN3_ACTIONS_PORT -and $env:QWEN3_ACTIONS_PORT.Trim() -ne "") {
    $raw = $env:QWEN3_ACTIONS_PORT.Trim()
  } else {
    $raw = Get-Qwen3LocalValue -VariableNames @("QWEN3_ACTIONS_PORT", "Qwen3ActionsPort") -FunctionNames @("Get-LocalQwen3ActionsPort")
  }
  $p = 0
  if ([int]::TryParse([string]$raw, [ref]$p) -and $p -gt 0 -and $p -le 65535) { return $p }
  return [int]$script:Qwen3ConfigDefaults.ActionsPort
}

function Get-Qwen3SshHost {
  Import-Qwen3LocalConfig
  if ($env:QWEN3_SSH_HOST -and $env:QWEN3_SSH_HOST.Trim() -ne "") {
    return $env:QWEN3_SSH_HOST.Trim()
  }
  $local = Get-Qwen3LocalValue -VariableNames @("QWEN3_SSH_HOST", "Qwen3SshHost") -FunctionNames @("Get-LocalQwen3SshHost")
  if ($local -and $local.Trim() -ne "") { return $local.Trim() }
  return Get-Qwen3ServerHost
}

function Get-Qwen3SshTarget {
  $user = Get-Qwen3SshUser
  $sshHost = Get-Qwen3SshHost
  if ($sshHost -match "@") { return $sshHost }
  if ($user -and $user.Trim() -ne "") { return ($user + "@" + $sshHost) }
  return $sshHost
}

function Get-Qwen3ServerConfig {
  return [pscustomobject]@{
    host         = Get-Qwen3ServerHost
    ssh_user     = Get-Qwen3SshUser
    ssh_target   = Get-Qwen3SshTarget
    ssh_port     = Get-Qwen3SshPort
    ingest_port  = Get-Qwen3IngestPort
    actions_port = Get-Qwen3ActionsPort
    ssh_host     = Get-Qwen3SshHost
  }
}
