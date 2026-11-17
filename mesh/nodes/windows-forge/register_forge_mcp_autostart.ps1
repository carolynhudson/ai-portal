# register_forge_mcp_autostart.ps1 - start the image-forge MCP server at logon
# (user-level task; registration self-elevates; action MUST carry
# -ExecutionPolicy Bypass: machine policy is Restricted, see 2026-11-07 law).
param(
    [string]$Root = "C:\forge",
    [string]$TaskName = "ai-forge-forge-mcp"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -Root `"$Root`" -TaskName `"$TaskName`""
    exit
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start_forge_mcp.ps1`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "image-forge MCP server, port 8096, firewall-gated to portal (AI portal)" | Out-Null
Write-Host "task '$TaskName' registered (starts hidden at logon)."
Write-Host "run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "restart:  .\restart_forge_mcp.ps1 (no admin needed)"
