# start_agent_server.ps1 - OpenCode LAN server for the forge kit (RainbowBlock.local)
#
# What: runs `opencode serve` bound to the LAN, working folder = this kit,
# so halo.local's agent can drive forge maintenance (update MCP service
# glue, fix kit scripts, run verify) over the OpenCode HTTP API.
#
# Security model (mirrors firewall_forge.ps1 for port 8188):
#   - inbound TCP 24096 allowed ONLY from the portal box 192.168.1.228
#   - auth: HTTP Basic, username 'opencode', password = the server's
#     startup password, printed to the log; agent_server.json records it
#     (same plaintext-on-disk model as ~/.config/opencode/service.json;
#     NTFS keeps it user-readable only)
#   - working folder is C:\forge\kit; project opencode.json denies edits
#     outside the kit. NOTE: the bash tool is not OS-sandboxed - the
#     portal box is the trust boundary, exactly like the forge itself.
#   - model is the RESIDENT router model (global config): remote prompts
#     share the portal's brain and KV cache; keep jobs discrete.
#
# Run via scheduled task at logon, or by hand:
#   powershell -ExecutionPolicy Bypass -File .\start_agent_server.ps1
param(
    [string]$Root = "C:\forge",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 24096
)
$ErrorActionPreference = "Stop"

$cliBase = Join-Path $env:APPDATA "ai.opencode.desktop\cli"
$cli = Get-ChildItem -Path $cliBase -Recurse -Filter "opencode-cli.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $cli) { throw "opencode-cli.exe not found under $cliBase - OpenCode desktop not installed?" }

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("agent-server-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$meta = Join-Path $PSScriptRoot "agent_server.json"

Write-Host "starting agent server on ${Listen}:$Port (log: $log)"
Push-Location $PSScriptRoot
& $cli.FullName serve --hostname $Listen --port $Port 2>&1 |
    Tee-Object -FilePath $log |
    ForEach-Object {
        $line = $_
        Write-Host $line
        if ($line -like "*server password *") {
            $pw = ($line -split "server password ")[1].Trim()
            (@{ url = ("http://RainbowBlock.local:{0}" -f $Port);
                user = "opencode"; password = $pw; port = $Port } |
                ConvertTo-Json) | Set-Content -Path $meta -Encoding ASCII
            Write-Host "credentials written to $meta"
        }
    }
Pop-Location
