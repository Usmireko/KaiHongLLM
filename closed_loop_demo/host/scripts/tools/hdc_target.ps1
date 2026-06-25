# tools/hdc_target.ps1
# Centralized HDC target resolver for PowerShell scripts.

if (-not (Get-Command Resolve-HdcTarget -ErrorAction SilentlyContinue)) {
  function Resolve-HdcTarget {
    param(
      [AllowEmptyString()][string]$Target = ""
    )

    $resolved = ""
    if (-not [string]::IsNullOrWhiteSpace($Target)) {
      $resolved = $Target.Trim()
    }
    if ([string]::IsNullOrWhiteSpace($resolved) -and -not [string]::IsNullOrWhiteSpace($env:HDC_TARGET)) {
      $resolved = $env:HDC_TARGET.Trim()
    }
    if ([string]::IsNullOrWhiteSpace($resolved) -and -not [string]::IsNullOrWhiteSpace($env:WK_DEVICE_TARGET)) {
      $resolved = $env:WK_DEVICE_TARGET.Trim()
    }
    if ([string]::IsNullOrWhiteSpace($resolved)) {
      $resolved = "192.168.3.28:8711"
    }

    $env:HDC_TARGET = $resolved
    return $resolved
  }
}

if (-not (Get-Command Get-HdcTargetArgs -ErrorAction SilentlyContinue)) {
  function Get-HdcTargetArgs {
    param(
      [AllowEmptyString()][string]$Target = ""
    )
    $resolved = Resolve-HdcTarget -Target $Target
    return @("-t", $resolved)
  }
}
