<#
check_agent_link.ps1 - proves this box can reach the Halo llama.cpp router.

Run from the agent folder on RainbowBlock.local:

  powershell -ExecutionPolicy Bypass -File .\check_agent_link.ps1

Checks TCP 192.168.1.228:8080 and lists the models the router serves.
Exit 0 = the opencode provider in opencode.json is reachable.
#>

$ErrorActionPreference = 'Stop'

$portal = '192.168.1.228'
$port = 8080

Write-Host "checking the link to the Halo router at ${portal}:$port"

$tcp = Test-NetConnection -ComputerName $portal -Port $port -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    Write-Host "TCP $port refused - check the portal Settings router bind (open to LAN) and this box's LAN."
    exit 1
}

try {
    $m = Invoke-RestMethod -Uri "http://${portal}:$port/v1/models" -TimeoutSec 20
    $ids = @($m.data | ForEach-Object { $_.id })
    Write-Host "router reachable, models visible:"
    foreach ($id in $ids) {
        Write-Host "  $id"
    }
} catch {
    Write-Host "the models call failed: $($_.Exception.Message)"
    exit 1
}

exit 0
