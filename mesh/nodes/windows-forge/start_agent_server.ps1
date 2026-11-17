# start_agent_server.ps1 - OpenCode LAN server for the forge kit (RainbowBlock.local)
#
# What: runs `opencode serve` bound to the LAN, working folder = this kit,
# so halo.local's agent can drive forge maintenance over the OpenCode HTTP API.
#
# Security model (audited 2026-11-12):
#   - inbound TCP 24096 allowed ONLY from the portal box 192.168.1.228
#   - MEASURED LAW: opencode-cli 2.2.7 'serve' enforces NO auth on ANY
#     endpoint (all 200 without credentials even with OPENCODE_SERVER_PASSWORD
#     set). The password is set from the shared subagent key for FORWARD
#     COMPATIBILITY (builds that enforce will not spring a random key on the
#     portal at boot); agent_server.json now records the STABLE credential
#     (user 'opencode', password = C:\forge\llama\subagent_key.txt) - no more
#     per-boot random passwords the portal can never have.
#   - working folder is C:\forge\kit; project opencode.json denies edits
#     outside the kit. NOTE: the bash tool is not OS-sandboxed - the
#     portal box is the trust boundary, exactly like the forge itself.
#   - model is the RESIDENT portal router model (generated global config):
#     remote prompts run on halo's brain, zero local llama load.
#
# Launcher contract (2026-11-07, same as start_comfyui.ps1): DETACHED via
# Start-Process with per-boot redirected logs; task process exits 0 at once;
# idempotent - exits 0 if the port already listens. The scheduled-task
# action must carry -ExecutionPolicy Bypass (machine policy is Restricted).
#
# Run via scheduled task at logon, or by hand:
#   powershell -ExecutionPolicy Bypass -File .\start_agent_server.ps1
param(
    [string]$Root = "C:\forge",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 24096
)
$ErrorActionPreference = "Stop"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Output ("agent server already listening on port " + $Port + " - no start needed")
    exit 0
}
$cliBase = Join-Path $env:APPDATA "ai.opencode.desktop\cli"
$cli = Get-ChildItem -Path $cliBase -Recurse -Filter "opencode-cli.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $cli) { throw "opencode-cli.exe not found under $cliBase - OpenCode desktop not installed?" }

$KeyFile = Join-Path $Root "llama\subagent_key.txt"
if (-not (Test-Path $KeyFile)) { throw ("shared subagent key missing: " + $KeyFile + " (start the router first)") }
$pw = (Get-Content $KeyFile -Raw).Trim()
$env:OPENCODE_SERVER_PASSWORD = $pw

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logOut = Join-Path $logDir ("agent-server-" + $stamp + ".out.log")
$logErr = Join-Path $logDir ("agent-server-" + $stamp + ".err.log")
$meta = Join-Path $PSScriptRoot "agent_server.json"

Start-Process -FilePath $cli.FullName -ArgumentList @("serve", "--hostname", $Listen, "--port", $Port) `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr

# STABLE credentials: same token as the llama router (one mesh token law).
(@{ url = ("http://RainbowBlock.local:{0}" -f $Port);
    user = "opencode"; password_file = $KeyFile; port = $Port;
    note = "password = contents of password_file; opencode-cli 2.2.7 enforces NO auth (firewall gates the port)" } |
    ConvertTo-Json) | Set-Content -Path $meta -Encoding ASCII
Write-Output ("launched agent server on " + $Listen + ":" + $Port + "; logs: " + $logOut + " / " + $logErr)
Write-Output ("stable credentials (shared subagent token) recorded in " + $meta)
