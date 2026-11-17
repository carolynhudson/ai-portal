# firewall_forge.ps1 - scope ComfyUI's port to the portal box (RUN AS ADMIN)
#
# ComfyUI's API has NO authentication. The security model is the firewall:
# inbound 8188 is allowed ONLY from the portal (halo.local). Same trust domain
# the harness joined on Oct-25 (LAN-exposed opencode behind Basic auth); this
# rule is deliberately narrower.
param(
    [string]$PortalIp = "192.168.1.228",   # halo.local (static lease: verify)
    [int]$Port = 8188,
    [string]$RuleName = "ai-forge comfyui (portal only)"
)
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "elevating..."
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -PortalIp $PortalIp -Port $Port -RuleName `"$RuleName`""
    exit
}
# Idempotent: drop any old rule of the same display name, then add.
# NetSecurity cmdlets, NOT netsh: this box's netsh advfirewall context is
# broken (its UI context DLL loads degenerate - 'show add rule' prints
# nothing and every keyword is 'not a valid argument', even elevated).
# The cmdlet layer talks to the same MPS store and validates properly.
Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -RemoteAddress $PortalIp -Profile Any |
    Out-Null
Write-Host "inbound TCP $Port now allowed ONLY from $PortalIp"
Write-Host "verify:  Get-NetFirewallRule -DisplayName `"$RuleName`" | Get-NetFirewallPortFilter"
Write-Host "         ... | Get-NetFirewallAddressFilter"
Write-Host "NOTE: this scopes remote machines. The Windows Firewall applies to"
Write-Host "this box's own services; keep ComfyUI trusted-local use as-is."
