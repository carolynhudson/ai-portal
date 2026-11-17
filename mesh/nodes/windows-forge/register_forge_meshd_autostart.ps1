# register_forge_meshd_autostart.ps1 - start meshd (node status daemon) at logon.
# User-scope scheduled task per NODE_CONTRACT (Windows: task at logon, no admin
# service). Registration self-elevates; action MUST carry -ExecutionPolicy
# Bypass (machine policy Restricted, 2026-11-07 law). ASCII only.
param(
    [string]$Root = "C:\forge",
    [int]$Port = 8092,
    [string]$TaskName = "ai-forge-meshd"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -Root `"$Root`" -Port $Port -TaskName `"$TaskName`""
    exit
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_forge_meshd.ps1`" -Root `"$Root`" -Port $Port"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "meshd node daemon: mesh.status MCP on port $Port, bearer auth, firewall-gated to portal (AI portal)" | Out-Null
Write-Host "task '$TaskName' registered (starts hidden at logon)."
Write-Host "run now:  Start-ScheduledTask -TaskName '$TaskName'"
