# audit_forge.ps1 - full live audit of the AI services surface on this box:
# listeners, auth behavior, firewall exposure, idle cost. Pure ASCII, PS 5.1.
$ErrorActionPreference = "Continue"
Write-Output "=== listening ports (AI range) ==="
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 8188,8081,8082,8096,24096,8083 } |
    Sort-Object LocalPort | ForEach-Object {
        $p = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Write-Output ("  " + $_.LocalAddress + ":" + $_.LocalPort + "  pid=" + $_.OwningProcess + "  " + $p)
    }

Write-Output "=== firewall rules (forge/ai) ==="
Get-NetFirewallRule | Where-Object { $_.DisplayName -like "*ai-forge*" } | ForEach-Object {
    $pa = $_ | Get-NetFirewallPortFilter
    $ra = $_ | Get-NetFirewallAddressFilter
    Write-Output ("  [" + $_.Enabled + "] " + $_.DisplayName + "  port=" + ($pa.LocalPort -join ",") + "  remote=" + ($ra.RemoteAddress -join ","))
}

Write-Output "=== router 8082: NO key (what the portal sees if unconfigured) ==="
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8082/v1/models" -UseBasicParsing -TimeoutSec 10
    Write-Output ("  HTTP " + $r.StatusCode + " body=" + $r.Content.Substring(0, [Math]::Min(200, $r.Content.Length)))
} catch {
    Write-Output ("  " + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))
}

Write-Output "=== router 8082: WITH subagent key ==="
$key = (Get-Content "C:\forge\llama\subagent_key.txt" -Raw).Trim()
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8082/v1/models" -Headers @{ Authorization = ("Bearer " + $key) } -TimeoutSec 10
    foreach ($m in $r.data) {
        Write-Output ("  id=" + $m.id + "  state=" + $m.status.value + "  nctx=" + $m.n_ctx)
    }
} catch {
    Write-Output ("  " + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))
}

Write-Output "=== prose 8081: bind + key check ==="
try {
    $pk = (Get-Content "C:\forge\llama\api_key.txt" -Raw).Trim()
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8081/v1/models" -Headers @{ Authorization = ("Bearer " + $pk) } -TimeoutSec 10
    foreach ($m in $r.data) { Write-Output ("  8081 id=" + $m.id + "  object=" + $m.object) }
} catch {
    Write-Output ("  8081: " + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))
}

Write-Output "=== opencode 24096: no-auth vs agent_server.json creds ==="
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:24096/global/health" -UseBasicParsing -TimeoutSec 10 | Out-Null
    Write-Output "  no-auth: ACCEPTED (no gate!)"
} catch { Write-Output ("  no-auth: HTTP rejected (good): " + $_.Exception.Message.Substring(0, [Math]::Min(60, $_.Exception.Message.Length))) }
if (Test-Path "C:\forge\kit\agent_server.json") {
    $m = Get-Content "C:\forge\kit\agent_server.json" -Raw | ConvertFrom-Json
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($m.user + ":" + $m.password))
    try {
        $r = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $m.port + "/global/health") -Headers @{ Authorization = ("Basic " + $b64) } -TimeoutSec 10
        Write-Output ("  creds from agent_server.json: OK  " + ($r | ConvertTo-Json -Compress).Substring(0, 120))
    } catch {
        Write-Output ("  creds from agent_server.json: FAILED: " + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))
    }
    Write-Output ("  stored url=" + $m.url + " user=" + $m.user)
} else { Write-Output "  agent_server.json MISSING" }

Write-Output "=== forge-mcp 8096 health ==="
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8096/health" -TimeoutSec 10
    Write-Output ("  " + ($r | ConvertTo-Json -Compress))
} catch { Write-Output ("  FAILED: " + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length))) }

Write-Output "=== process cost (RAM GB, CPU s) ==="
foreach ($n in @("llama-server", "python", "opencode-cli", "node")) {
    $ps = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($ps) {
        $ram = ($ps | Measure-Object WorkingSet64 -Sum).Sum / 1GB
        $cpu = ($ps | Measure-Object CPU -Sum).Sum
        Write-Output ("  " + $n + " x" + $ps.Count + "  RAM " + [math]::Round($ram, 1) + " GB  CPU " + [math]::Round($cpu, 0) + " s")
    }
}
$os = Get-CimInstance Win32_OperatingSystem
Write-Output ("  free RAM: " + [math]::Round($os.FreePhysicalMemory / 1MB, 1) + " GB of " + [math]::Round($os.TotalVisibleMemorySize / 1MB, 1) + " GB")
Write-Output "=== kit sprawl (top-level tree) ==="
Get-ChildItem C:\forge -Directory | Select-Object -ExpandProperty Name
