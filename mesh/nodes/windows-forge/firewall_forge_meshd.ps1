# firewall_forge_meshd.ps1 - scope meshd port 8092 to the portal box
# (self-elevates). meshd HAS bearer auth, unlike ComfyUI/forge-mcp: defense in
# depth - firewall gates the network, the ONE-token gates the wire.
# Inbound 8092 allowed ONLY from the portal IP. NetSecurity cmdlets, NOT netsh
# (netsh context broken on this box). ASCII only.
param(
    [string]$PortalIp = "192.168.1.228",   # halo.local (static lease: verify)
    [int]$Port = 8092,
    [string]$RuleName = "ai-forge meshd (portal only)"
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
Write-Host "verify:  Get-NetFirewallRule -DisplayName `"$RuleName`" | Get-NetFirewallPortFilter"
Write-Host "         ... | Get-NetFirewallAddressFilter"
