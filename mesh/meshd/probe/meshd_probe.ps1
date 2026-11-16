# meshd_probe.ps1 - smoke test the meshd phase-1 status server (port 8092).
# ASCII only (PS 5.1). Run: powershell -ExecutionPolicy Bypass -File .\meshd_probe.ps1
$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:8092'
$tok = (Get-Content 'C:\forge\llama\subagent_key.txt' -Raw).Trim()

function Invoke-Mcp($body, $headers) {
    $json = $body | ConvertTo-Json -Depth 10 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $r = Invoke-WebRequest -Uri "$base/mcp" -Method Post -Body $bytes -ContentType 'application/json' -Headers $headers -UseBasicParsing
    # SSE body: first data: line is an empty priming event; take first payload line
    $txt = $r.Content
    $dataLine = ($txt -split "`n" | Where-Object { $_ -match '^data:\s*\{' } | Select-Object -First 1)
    if ($dataLine) { return @{ json = ($dataLine.Substring(5).Trim() | ConvertFrom-Json); headers = $r.Headers } }
    return @{ json = ($txt | ConvertFrom-Json); headers = $r.Headers }
}

"== 1. GET /healthz (must be OPEN, no token)"
$h = Invoke-RestMethod -Uri "$base/healthz" -UseBasicParsing
"   healthz OK: node=" + $h.node + " v" + $h.version + " ram_used_mib=" + $h.ram.used_mib + " vram_used=" + $h.vram_mib.used

"== 2. POST /mcp WITHOUT token (must be 401)"
try {
    Invoke-WebRequest -Uri "$base/mcp" -Method Post -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' -ContentType 'application/json' -Headers @{ 'Accept' = 'application/json, text/event-stream' } -UseBasicParsing | Out-Null
    throw 'expected 401, got 200'
} catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode.value__ -eq 401) { '   401 as expected' }
    elseif ($_.Exception.Message -like 'expected 401*') { throw $_ }
    else { '   non-401 error: ' + $_.Exception.Message; throw }
}

"== 3. initialize with Bearer"
$hdr = @{ 'Accept' = 'application/json, text/event-stream'; 'Authorization' = "Bearer $tok" }
$init = Invoke-Mcp @{ jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{ protocolVersion = '2025-03-26'; capabilities = @{}; clientInfo = @{ name = 'meshd-probe'; version = '1.0' } } } $hdr
$sid = $init.headers['mcp-session-id']
if (-not $sid) { $sid = $init.headers['Mcp-Session-Id'] }
"   server=" + $init.json.result.serverInfo.name + ' v' + $init.json.result.serverInfo.version + '  session=' + $sid

"== 4. notifications/initialized"
$hdr2 = $hdr.Clone(); $hdr2['mcp-session-id'] = $sid
$json = (@{ jsonrpc = '2.0'; method = 'notifications/initialized' } | ConvertTo-Json -Compress)
Invoke-WebRequest -Uri "$base/mcp" -Method Post -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -ContentType 'application/json' -Headers $hdr2 -UseBasicParsing | Out-Null
"   sent"

"== 5. tools/list"
$tl = Invoke-Mcp @{ jsonrpc = '2.0'; id = 2; method = 'tools/list'; params = @{} } $hdr2
"   tools: " + (($tl.json.result.tools | ForEach-Object { $_.name }) -join ', ')

"== 6. tools/call node_health"
$ch = Invoke-Mcp @{ jsonrpc = '2.0'; id = 3; method = 'tools/call'; params = @{ name = 'node_health'; arguments = @{} } } $hdr2
$payload = ($ch.json.result.content[0].text | ConvertFrom-Json)
"   node_health: host=" + $payload.host + " vram_used=" + $payload.vram_mib.used + '/' + $payload.vram_mib.total + ' MiB  isError=' + $ch.json.result.isError

"== 7. tools/call node_services (compact)"
$sv = Invoke-Mcp @{ jsonrpc = '2.0'; id = 4; method = 'tools/call'; params = @{ name = 'node_services'; arguments = @{} } } $hdr2
$pl = ($sv.json.result.content[0].text | ConvertFrom-Json)
foreach ($s in $pl.services) { "   svc " + $s.id + ' port ' + $s.port + ' -> ' + $s.observed.state }
"   llm ids: " + (($pl.llm.models | ForEach-Object { $_.id }) -join ', ')

"== 8. mesh_lease: 2048 MiB vram for 120 s (expect granted)"
$lz = Invoke-Mcp @{ jsonrpc = '2.0'; id = 5; method = 'tools/call'; params = @{ name = 'mesh_lease'; arguments = @{ vram_mib = 2048; ttl_s = 120; job = 'meshd-probe' } } } $hdr2
$lzpl = ($lz.json.result.content[0].text | ConvertFrom-Json)
if ($lzpl.decision -ne 'granted') { throw ('lease not granted: ' + ($lzpl | ConvertTo-Json -Compress)) }
$leaseId = $lzpl.lease.id
"   granted id=" + $leaseId + ' expires_in=' + ($lzpl.lease.expires_unix - $lzpl.lease.granted_unix) + 's free_vram=' + $lzpl.free_vram_mib

"== 9. mesh_lease: 999999 MiB (expect denied)"
$dz = Invoke-Mcp @{ jsonrpc = '2.0'; id = 6; method = 'tools/call'; params = @{ name = 'mesh_lease'; arguments = @{ vram_mib = 999999; ttl_s = 60; job = 'meshd-probe-overcommit' } } } $hdr2
$dzpl = ($dz.json.result.content[0].text | ConvertFrom-Json)
if ($dzpl.decision -ne 'denied') { throw 'overcommit was not denied' }
"   denied: " + $dzpl.reason

"== 10. lease_release (expect released)"
$rl = Invoke-Mcp @{ jsonrpc = '2.0'; id = 7; method = 'tools/call'; params = @{ name = 'lease_release'; arguments = @{ id = $leaseId } } } $hdr2
$rlpl = ($rl.json.result.content[0].text | ConvertFrom-Json)
if ($rlpl.decision -ne 'released') { throw ('release failed: ' + ($rlpl | ConvertTo-Json -Compress)) }
"   released " + $rlpl.id

"== 11. mesh_service status comfyui (expect running)"
$sc = Invoke-Mcp @{ jsonrpc = '2.0'; id = 8; method = 'tools/call'; params = @{ name = 'mesh_service'; arguments = @{ id = 'comfyui'; action = 'status' } } } $hdr2
$scpl = ($sc.json.result.content[0].text | ConvertFrom-Json)
"   comfyui: " + $scpl.state

"== 12. mesh_service stop llama-router (expect refused - deny-list)"
$dc = Invoke-Mcp @{ jsonrpc = '2.0'; id = 9; method = 'tools/call'; params = @{ name = 'mesh_service'; arguments = @{ id = 'llama-router'; action = 'stop' } } } $hdr2
$dcpl = ($dc.json.result.content[0].text | ConvertFrom-Json)
if ($dcpl.state -ne 'refused') { throw 'deny-list failed - stop was NOT refused' }
"   refused as designed: " + $dcpl.reason

"== 13. node_caps service cards (schema v0)"
$cp = Invoke-Mcp @{ jsonrpc = '2.0'; id = 10; method = 'tools/call'; params = @{ name = 'node_caps'; arguments = @{} } } $hdr2
$cpl = ($cp.json.result.content[0].text | ConvertFrom-Json)
$img = @($cpl.cards | Where-Object { $_.kind -eq 'image-forge' })
$llm = @($cpl.cards | Where-Object { $_.kind -eq 'llm-router' })
"   cards: " + $cpl.cards.Count + " (image=" + $img.Count + ' llm=' + $llm.Count + ')'
$need = @('node','kind','service','tier','fills','requires','limits','idle_policy','state','measured','pack')
$miss = @()
foreach ($k in $need) { if (-not $cpl.cards[0].PSObject.Properties.Name -contains $k) { $miss += $k } }
if ($miss.Count -gt 0) { throw ('card schema missing keys: ' + ($miss -join ',')) }
$sample = $img | Where-Object { $_.service -eq 'qwen21_t2i_lightning4' } | Select-Object -First 1
"   schema v0 keys OK; sample: " + $sample.service + ' vram_gb_min=' + $sample.requires.vram_gb_min + ' state=' + $sample.state

"`nALL PROBES PASS (phase 2b + cards)"
