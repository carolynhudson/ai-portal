# register_forge_router_autostart.ps1 - start the router endpoint (8082) at
# logon. Replaces the direct-flint task of the SAME name (ai-forge-llama-subagent):
# registration Unregisters-then-Registers, so re-running this file is the
# supported path. Self-elevates; -ExecutionPolicy Bypass mandatory (machine
# policy Restricted, 2026-11-07 law).
param(
    [string]$Root = "C:\forge",
    [string]$TaskName = "ai-forge-llama-subagent"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -Root `"$Root`" -TaskName `"$TaskName`""
    exit
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_forge_router.ps1`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "router-mode LLM endpoint (flint + nemotron Lightning, llama.cpp router, models-max 1, port 8082, AI portal)" | Out-Null
Write-Host "task '$TaskName' now runs the ROUTER (flint + nemotron). Swap the old flint-direct task: done."
Write-Host "run now:  Stop-ScheduledTask -TaskName '$TaskName'; then .\start_forge_router.ps1"