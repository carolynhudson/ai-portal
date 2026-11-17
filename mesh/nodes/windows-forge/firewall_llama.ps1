# firewall_llama.ps1 - scope the prose endpoint (8081) to the portal box (RUN AS ADMIN)
# Same law as firewall_forge.ps1: NetSecurity cmdlets (netsh advfirewall context is
# broken on this box), portal IP only, keep the IP not a name. Self-elevates.
param(
    [string]$PortalIp = "192.168.1.228",   # halo.local (static lease: verify)
    [int]$Port = 8081,
    [string]$RuleName = "ai-forge llama prose (portal only)"
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
Write-Host "inbound TCP $Port now allowed ONLY from $PortalIp (plus the API key gate)"
Write-Host "verify:  Get-NetFirewallRule -DisplayName `"$RuleName`" | Get-NetFirewallPortFilter"
