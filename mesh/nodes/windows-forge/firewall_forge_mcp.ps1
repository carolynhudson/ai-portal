# firewall_forge_mcp.ps1 - scope the image-forge MCP port to the portal box
# (self-elevates). forge_mcp.py has NO authentication, like ComfyUI: the
# firewall IS the security model. Inbound 8096 allowed ONLY from halo.local.
# NetSecurity cmdlets, NOT netsh (netsh context broken on this box).
param(
    [string]$PortalIp = "192.168.1.228",   # halo.local (static lease: verify)
    [int]$Port = 8096,
    [string]$RuleName = "ai-forge forge-mcp (portal only)"
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
