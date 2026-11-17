# start_forge_mcp.ps1 - launch the image-forge MCP server (hidden, logged).
# Machine execution policy is Restricted: the caller's -ExecutionPolicy Bypass
# is inherited by the venv python's absence-of-policy; python needs no policy,
# only this file needs the Bypass (learned 2026-11-07).
param([string]$Root = "C:\forge")
$log = Join-Path $Root "logs\forge_mcp.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
$py = Join-Path $Root "venv\Scripts\python.exe"
"--- start $(Get-Date -Format 's') ---" | Add-Content $log
Start-Process -FilePath $py -ArgumentList "`"$Root\kit\tools\forge_mcp.py`"" `
    -WindowStyle Hidden -WorkingDirectory "$Root\kit" -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $Root "logs\forge_mcp.err.log")
Write-Host "forge-mcp started (log: $log)"
