# probe_agents.ps1 - list the agents the kit agent-server publishes.
# Proves the forge-ops lockdown agent is live for portal-driven sessions.
$ErrorActionPreference = "Stop"
$m = Get-Content (Join-Path $PSScriptRoot "agent_server.json") -Raw | ConvertFrom-Json
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($m.user + ":" + $m.password))
$h = @{ Authorization = ("Basic " + $b64) }
$info = Invoke-RestMethod ("http://127.0.0.1:" + $m.port + "/api/info") -Headers $h -TimeoutSec 10
Write-Output ("server v" + $info.version + " pid " + $info.pid)
$r = Invoke-WebRequest ("http://127.0.0.1:" + $m.port + "/api/agent") -Headers $h -UseBasicParsing -TimeoutSec 10
$j = $r.Content | ConvertFrom-Json
$list = if ($j.agents) { $j.agents } elseif ($j -is [array]) { $j } else { $j.data }
foreach ($a in $list) {
    Write-Output ("agent: " + $a.id + " mode=" + $a.mode + " model=" + $a.model)
}
