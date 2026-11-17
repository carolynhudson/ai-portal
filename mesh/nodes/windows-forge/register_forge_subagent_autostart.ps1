# register_forge_subagent_autostart.ps1 - start the agentic sub-agent endpoint
# at logon (user-level task; registration self-elevates; action MUST carry
# -ExecutionPolicy Bypass: machine policy is Restricted, 2026-11-07 law).
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
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_forge_subagent.ps1`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "agentic sub-agent endpoint (Flint 40B-A2.5B, llama.cpp CPU lane, port 8082, AI portal)" | Out-Null
Write-Host "task '$TaskName' registered (starts hidden at logon)."
Write-Host "run now:  Start-ScheduledTask -TaskName '$TaskName'"
