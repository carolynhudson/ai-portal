# firewall_forge.ps1 — scope ComfyUI's port to the portal box (RUN AS ADMIN)
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
    Write-Host "elevating…"
    Start-Process powershell -Verb RunAs -ArgumentList "-File `"$PSCommandPath`" -PortalIp $PortalIp -Port $Port -RuleName `"$RuleName`""
    exit
}
# Idempotent: drop any old rule of the same name, then add.
netsh advfirewall firewall delete rule name="$RuleName" | Out-Null
netsh advfirewall firewall add rule name="$RuleName" dir=in action=allow protocol=TCP `
    localport=$Port remoteaddress=$PortalIp
Write-Host "inbound TCP $Port now allowed ONLY from $PortalIp"
Write-Host "verify:  netsh advfirewall firewall show rule name=`"$RuleName`""
Write-Host "NOTE: this scopes remote machines. The Windows Firewall applies to"
Write-Host "this box's own services; keep ComfyUI trusted-local use as-is."
