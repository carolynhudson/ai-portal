# firewall_forge_coder.ps1 - scope the coder-lane port (8083) to the portal box
# (self-elevates). NetSecurity cmdlets, NOT netsh (netsh context broken here).
param(
    [string]$PortalIp = "192.168.1.228",   # halo.local (static lease: verify)
    [int]$Port = 8083,
    [string]$RuleName = "ai-forge llama-coder (portal only)"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -PortalIp $PortalIp -Port $Port -RuleName `"$RuleName`""
    exit
}
Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -RemoteAddress $PortalIp -Profile Any |
    Out-Null
Write-Host "inbound TCP $Port now allowed ONLY from $PortalIp"
