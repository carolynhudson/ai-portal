# register_autostart.ps1 — start the forge engine at logon (user-level task,
# no admin: ComfyUI needs the logged-in desktop session for the GPU anyway).
param(
    [string]$Root = "C:\forge",
    [string]$TaskName = "ai-forge-comfyui"
)
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$PSScriptRoot\start_comfyui.ps1`" -Root `"$Root`""
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
