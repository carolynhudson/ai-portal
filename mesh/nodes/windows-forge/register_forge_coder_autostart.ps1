# register_forge_coder_autostart.ps1 - start the coder-lane endpoint at logon
# (user-level task; self-elevates; -ExecutionPolicy Bypass is mandatory on this
# box: machine policy Restricted, 2026-11-07 law).
param(
    [string]$Root = "C:\forge",
    [string]$TaskName = "ai-forge-llama-coder"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -Root `"$Root`" -TaskName `"$TaskName`""
    exit
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_forge_coder.ps1`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "coder lane endpoint (Qwen3.8-Flash-Next Coder IQ1_M, llama.cpp CPU lazy-mode, port 8083, AI portal)" | Out-Null
Write-Host "task '$TaskName' registered (starts hidden at logon)."
Write-Host "run now:  Start-ScheduledTask -TaskName '$TaskName'"
