# probe_agent.ps1 - prove the agent server is up and credentials work.
# Reads agent_server.json (written by start_agent_server.ps1) and calls
# /api/info with the Basic auth the server expects (user 'opencode').
$ErrorActionPreference = "Stop"
$meta = Join-Path $PSScriptRoot "agent_server.json"
if (-not (Test-Path $meta)) {
    Write-Host "no agent_server.json - start the server first: .\start_agent_server.ps1"
    exit 1
}
$m = Get-Content $meta -Raw | ConvertFrom-Json
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($m.user + ":" + $m.password))
try {
    $r = Invoke-RestMethod ("http://127.0.0.1:" + $m.port + "/api/info") `
        -Headers @{Authorization = ("Basic " + $b64)} -TimeoutSec 10
    Write-Host ("AGENT_SERVER_UP v" + $r.version + " pid " + $r.pid + " host " + $r.hostname)
    Write-Host ("url: " + $m.url + " (portal-side check: from halo.local GET " + $m.url + "/api/info with Basic auth)")
    exit 0
} catch {
    Write-Host ("FAILED: " + $_.Exception.Message)
    exit 1
}
