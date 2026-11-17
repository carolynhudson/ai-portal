# register_autostart.ps1 - start the forge engine at logon (user-level task,
# no admin rights needed to RUN: ComfyUI needs the logged-in desktop session
# for the GPU anyway). Registration self-elevates: this box's tasks live in
# the root task folder and the machine execution policy is Restricted
# (all scopes Undefined), so the action MUST carry -ExecutionPolicy Bypass
# or every task fire dies with exit 1 before running the script (learned
# the expensive way, 2026-11-07).
param(
    [string]$Root = "C:\forge",
    [string]$TaskName = "ai-forge-comfyui"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -Root `"$Root`" -TaskName `"$TaskName`""
    exit
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_comfyui.ps1`" -Root `"$Root`""
# AtLogOn, run for whoever logs on (ComfyUI needs the desktop session's GPU):
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "image-forge ComfyUI backend (AI portal)" | Out-Null
Write-Host "task '$TaskName' registered (starts hidden at logon)."
Write-Host "run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "stop:     Stop-ScheduledTask -TaskName '$TaskName'; then kill the python process or POST /free + close."
