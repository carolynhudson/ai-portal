# router_smoke.ps1 - one honest completion against THIS box's router (8082).
# The box's router serves the forge catalog (flint, nemotron, Qwen3-*, gpt-oss);
# the agent brain in opencode.json is NOT here - its baseURL is the portal's own
# router (192.168.1.228:8080), so the resident-brain id is not in this catalog.
# The router runs --models-max 1, so each id asked for costs a load (and a swap
# if another is resident): ask for the ids you mean to test, few. Default flint
# (CPU lane, fast); nemotron exercises the CUDA sm_75 + MTP path (needs VRAM
# headroom - check node_health first). Never prints the token. ASCII, PS 5.1.
param(
    [string]$Base = 'http://127.0.0.1:8082',
    [string[]]$Ids = @('flint'),
    [int]$MaxTokens = 48,
    [string]$KeyFile = 'C:\forge\llama\subagent_key.txt'
)
$ErrorActionPreference = 'Stop'

# token: hub-first (ONE-TOKEN LAW), file fallback; value never printed
# -File callers cannot pass a real array, so accept "a,b,c" too.
$Ids = ($Ids -join ',') -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$key = ''
$meshctl = 'C:\forge\etc\mesh\meshctl.exe'
if (Test-Path $meshctl) { $key = (& $meshctl get subagent-key 2>$null | Out-String).Trim() }
if (-not $key -and (Test-Path $KeyFile)) { $key = (Get-Content $KeyFile -Raw).Trim() }
if (-not $key) { throw 'no token (hub and file both empty) - not printing anything' }
$hdr = @{ Authorization = ('Bearer ' + $key) }

$listed = (Invoke-RestMethod -Uri ($Base + '/v1/models') -Headers $hdr -TimeoutSec 30).data | ForEach-Object { $_.id }
'models listed: ' + $listed.Count + '   target ids: ' + ($Ids -join ', ')

foreach ($mid in $Ids) {
    if ($listed -notcontains $mid) { 'SKIP ' + $mid + ' - not in this router catalog'; continue }
    $body = @{
        model      = $mid
        messages   = @(@{ role = 'user'; content = 'Reply with exactly: READY' })
        max_tokens = $MaxTokens
    } | ConvertTo-Json -Depth 5
    $t0 = Get-Date
    try {
        $r = Invoke-RestMethod -Uri ($Base + '/v1/chat/completions') -Method Post -Headers $hdr `
            -ContentType 'application/json' -Body $body -TimeoutSec 900
    } catch {
        'FAIL ' + $mid + ': ' + $_.Exception.Message
        continue
    }
    $dt = ((Get-Date) - $t0).TotalSeconds
    $c = [string]$r.choices[0].message.content
    $c = ($c -replace '\s+', ' ')
    $toks = 0
    if ($r.usage) { $toks = $r.usage.completion_tokens }
    'OK ' + $mid + ': ' + ('{0:N1} s' -f $dt) + '  ' + $toks + ' tok  ' +
        ('{0:N1} tok/s' -f ($toks / $dt)) + '  reply: ' + $c.Substring(0, [Math]::Min(40, $c.Length))
}
