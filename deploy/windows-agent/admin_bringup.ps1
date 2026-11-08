# admin_bringup.ps1 - one UAC elevation for the admin steps of the kit:
#   1. firewall_forge.ps1      (inbound 8188 from 192.168.1.228 only)
#   2. register_autostart.ps1  (logon task; -AtLogOn for any user needs admin)
#   3. agent server: firewall 24096 from 192.168.1.228 only + logon task
# Self-elevates: run as normal user; a UAC prompt appears, click Yes once.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating (click Yes on the UAC prompt)..."
    Start-Process powershell -Verb RunAs -Wait -ArgumentList `
        "-ExecutionPolicy Bypass -File `"$here\admin_bringup.ps1`""
    exit $LASTEXITCODE
}

$log = "C:\forge\logs\admin_bringup.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
& "$here\firewall_forge.ps1" 2>&1 | Tee-Object -FilePath $log -Append
& "$here\register_autostart.ps1" 2>&1 | Tee-Object -FilePath $log -Append

# agent server: LAN gate for the OpenCode agent (portal IP only), + logon task
Remove-NetFirewallRule -DisplayName "ai-forge agent (portal only)" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "ai-forge agent (portal only)" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 24096 -RemoteAddress 192.168.1.228 -Profile Any | Out-Null
$agentAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$here\start_agent_server.ps1`""
$agentTrigger = New-ScheduledTaskTrigger -AtLogOn
$agentSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName "ai-forge-opencode-agent" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "ai-forge-opencode-agent" -Action $agentAction -Trigger $agentTrigger `
    -Settings $agentSettings -Description "image-forge OpenCode agent server (AI portal)" | Out-Null

Write-Host ""
Write-Host "=== evidence ==="
Get-NetFirewallRule -DisplayName "ai-forge comfyui (portal only)" -ErrorAction SilentlyContinue |
    Get-NetFirewallPortFilter |
    Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue |
    Format-List Action, Direction, Enabled, LocalPort, RemoteAddress |
    Tee-Object -FilePath $log -Append
Get-NetFirewallRule -DisplayName "ai-forge agent (portal only)" -ErrorAction SilentlyContinue |
    Get-NetFirewallPortFilter |
    Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue |
    Format-List Action, Direction, Enabled, LocalPort, RemoteAddress |
    Tee-Object -FilePath $log -Append
Get-ScheduledTask -TaskName "ai-forge-comfyui", "ai-forge-opencode-agent" -ErrorAction SilentlyContinue |
    Select-Object TaskName, State | Format-Table -AutoSize | Out-String -Width 200 |
    Tee-Object -FilePath $log -Append
Write-Host "log: $log"
Read-Host "press Enter to close"
