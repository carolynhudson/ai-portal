# firewall_forge_subagent.ps1 - scope the agentic sub-agent port (8082) to the
# portal box (self-elevates). OpenAI API is key-protected AND firewall-gated.
# NetSecurity cmdlets, NOT netsh (netsh context broken on this box).
param(
    [string]$PortalIp = "192.168.1.228",   # halo.local (static lease: verify)
    [int]$Port = 8082,
    [string]$RuleName = "ai-forge llama-subagent (portal only)"
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
