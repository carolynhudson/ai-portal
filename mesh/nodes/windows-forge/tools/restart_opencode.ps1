# restart_opencode.ps1 - cycle the opencode agent server onto the stable-token
# launcher. Pure ASCII, PS 5.1.
$ErrorActionPreference = "Continue"
Get-NetTCPConnection -State Listen -LocalPort 24096 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName ai-forge-opencode-agent
Start-Sleep -Seconds 10
$n = (Get-NetTCPConnection -State Listen -LocalPort 24096 -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output ($n.ToString() + " listener(s) on 24096")
if (Test-Path "C:\forge\kit\agent_server.json") {
    Get-Content "C:\forge\kit\agent_server.json" -Raw
}
