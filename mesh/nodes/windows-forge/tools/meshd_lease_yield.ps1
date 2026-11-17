# meshd_lease_yield.ps1 - E2E phase 2b proof: a granted meshd VRAM lease cuts
# the forge-mcp supervisor idle timer (empty queue + committed lease -> /free NOW).
# Flow: grant lease -> z-image-turbo render -> poll supervisor -> expect
# last_free_result with 'lease ... committed'. ASCII only, PS 5.1.
$ErrorActionPreference = 'Stop'
$tok = (Get-Content 'C:\forge\llama\subagent_key.txt' -Raw).Trim()

function Invoke-McpServer($base, $body, $headers) {
    $json = $body | ConvertTo-Json -Depth 12 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $r = Invoke-WebRequest -Uri "$base/mcp" -Method Post -Body $bytes -ContentType 'application/json' -Headers $headers -UseBasicParsing
    $txt = $r.Content
    $dataLine = ($txt -split "`n" | Where-Object { $_ -match '^data:\s*\{' } | Select-Object -First 1)
    if ($dataLine) { $payload = $dataLine.Substring(5).Trim() } else { $payload = $txt }
    if ($payload -match '^\{') { return @{ json = ($payload | ConvertFrom-Json); headers = $r.Headers } }
    # forge-mcp answers plain JSON (no SSE)
    return @{ json = ($payload | ConvertFrom-Json); headers = $r.Headers }
}

function New-Session($base, $name) {
    $hdr = @{ 'Accept' = 'application/json, text/event-stream'; 'Authorization' = "Bearer $tok" }
    $init = Invoke-McpServer $base @{ jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{ protocolVersion = '2025-03-26'; capabilities = @{}; clientInfo = @{ name = $name; version = '1.0' } } } $hdr
    $sid = $init.headers['mcp-session-id']; if (-not $sid) { $sid = $init.headers['Mcp-Session-Id'] }
    if ($sid) {
        $hdr2 = $hdr.Clone(); $hdr2['mcp-session-id'] = $sid
        $json = (@{ jsonrpc = '2.0'; method = 'notifications/initialized' } | ConvertTo-Json -Compress)
        Invoke-WebRequest -Uri "$base/mcp" -Method Post -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -ContentType 'application/json' -Headers $hdr2 -UseBasicParsing | Out-Null
        return $hdr2
    }
    return $hdr   # forge-mcp is sessionless
}

function Call-Tool($base, $hdr, $id, $name, $toolArgs) {
    $r = Invoke-McpServer $base @{ jsonrpc = '2.0'; id = $id; method = 'tools/call'; params = @{ name = $name; arguments = $toolArgs } } $hdr
    $t = $r.json.result.content[0].text
    return ($t | ConvertFrom-Json)
}

$mcpd = 'http://127.0.0.1:8092'
$mcpf = 'http://127.0.0.1:8096'

"== 0. forge health baseline (supervisor leased_vram_mib present + 0)"
$h0 = (Invoke-RestMethod -Uri "$mcpf/health" -UseBasicParsing).health
if ($null -eq $h0.supervisor.leased_vram_mib) { throw 'supervisor.leased_vram_mib missing - new forge_mcp not live?' }
"   leased_vram_mib=" + $h0.supervisor.leased_vram_mib + "  frees=" + $h0.supervisor.frees

"== 1. grant 4096 MiB lease, ttl 300 s, via meshd"
$hd = New-Session $mcpd 'lease-yield'
$lz = Call-Tool $mcpd $hd 2 'mesh_lease' @{ vram_mib = 4096; ttl_s = 300; job = 'phase2b-yield-test' }
if ($lz.decision -ne 'granted') { throw ('lease failed: ' + ($lz | ConvertTo-Json -Compress)) }
$leaseId = $lz.lease.id
"   granted " + $leaseId

"== 2. render z_image_turbo_fast (512x512, 8 steps) via forge-mcp (seed TOP-level: forge_render law)"
$hf = New-Session $mcpf 'lease-yield-f'
$rn = Call-Tool $mcpf $hf 3 'forge_render' @{ contract = 'z_image_turbo_fast'; seed = 42; job = @{ text = 'a small red cube on white, studio'; width = 512; height = 512; steps = 8 } }
if (-not $rn.ok) { throw ('render failed: ' + ($rn | ConvertTo-Json -Compress -Depth 6)) }
"   render ok=" + $rn.ok + "  results=" + $rn.results.Count

"== 3. supervisor must /free for the lease within ~3 ticks (180 s)"
$deadline = (Get-Date).AddSeconds(300)
$seen = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    $h1 = (Invoke-RestMethod -Uri "$mcpf/health" -UseBasicParsing).health
    if ($h1.supervisor.last_free_result -match 'lease .* committed') { $seen = $h1.supervisor.last_free_result; break }
}
if (-not $seen) { throw ('no lease-reason /free within 300 s; last_free_result=' + $h1.supervisor.last_free_result) }
"   supervisor: " + $seen

"== 4. release lease early + confirm ledger"
$rl = Call-Tool $mcpd $hd 4 'lease_release' @{ id = $leaseId }
"   release: " + $rl.decision
$st = Call-Tool $mcpd $hd 5 'mesh_status' @{}
"   ledger committed_vram_mib=" + $st.leases.committed_vram_mib

"`nLEASE YIELD E2E PASS"
