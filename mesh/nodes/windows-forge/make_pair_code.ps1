# make_pair_code.ps1 - mint a one-time pairing code for the kit agent server.
# Hand the printed code to the portal harness: opencode --server
# http://RainbowBlock.local:24096 (redeem via GET /auth/connect/<code>).
$ErrorActionPreference = "Stop"
$m = Get-Content (Join-Path $PSScriptRoot "agent_server.json") -Raw | ConvertFrom-Json
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($m.user + ":" + $m.password))
$r = Invoke-RestMethod ("http://127.0.0.1:" + $m.port + "/api/pair") -Method Post `
    -Headers @{Authorization = ("Basic " + $b64)} -TimeoutSec 10
Write-Output ("pairing endpoint: " + $m.url)
Write-Output ("redeem: GET " + $m.url + "/auth/connect/<code>")
$r | ConvertTo-Json
