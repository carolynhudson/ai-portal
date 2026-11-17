# start_comfyui.ps1 - the forge engine launcher (RainbowBlock.local)
# Flags come from the portal's memory-efficiency contract (IMAGE_FORGE_PROPOSAL sec 1):
#   --listen 0.0.0.0      LAN-reachable; the Windows firewall rule scopes 8188
#                         INBOUND TO THE PORTAL BOX ONLY (firewall_forge.ps1) -
#                         ComfyUI answers unauthenticated, the rule is the gate.
#   --disable-smart-memory  models leave VRAM/RAM when a job ends (the /free
#                         contract's steady-state twin; the forge MCP calls
#                         POST /free after every job).
#   --preview-method none   no extra preview-image memory; results come back
#                         through /view.
# Launcher contract (2026-11-07): the engine is launched DETACHED via
# Start-Process with stdout/stderr redirected to per-boot log files. The task
# process exits 0 immediately; the engine survives the task session closing,
# and no Tee-Object pipe/file lock can kill it. Re-running (task at logon +
# manual start) is idempotent: exits 0 if the port already listens.
param(
    [string]$Root = "C:\forge",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 8188,
    [switch]$LowVram
)
$ErrorActionPreference = "Stop"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Output ("engine already listening on port " + $Port + " - no start needed")
    exit 0
}
$Comfy = Join-Path $Root "ComfyUI"
$VenvPy = Join-Path $Root "venv\Scripts\python.exe"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logOut = Join-Path $Root ("logs\comfyui-" + $stamp + ".out.log")
$logErr = Join-Path $Root ("logs\comfyui-" + $stamp + ".err.log")
$pyArgs = @("main.py", "--listen", $Listen, "--port", $Port,
            "--disable-smart-memory", "--preview-method", "none")
if ($LowVram) { $pyArgs += "--lowvram" }
Start-Process -FilePath $VenvPy -ArgumentList $pyArgs -WorkingDirectory $Comfy `
    -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
Write-Output ("launched ComfyUI on ${Listen}:${Port}; logs: " + $logOut + " / " + $logErr)
