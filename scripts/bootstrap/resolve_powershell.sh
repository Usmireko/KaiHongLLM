#!/bin/sh
if command -v pwsh >/dev/null 2>&1; then
  echo pwsh
elif command -v powershell >/dev/null 2>&1; then
  echo powershell
elif command -v powershell.exe >/dev/null 2>&1; then
  echo powershell.exe
else
  echo "ERROR: pwsh/powershell not found" >&2
  exit 127
fi
