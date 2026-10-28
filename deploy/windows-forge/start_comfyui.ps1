# start_comfyui.ps1 - the forge engine launcher (RainbowBlock.local)
# Flags come from the portal's memory-efficiency contract (IMAGE_FORGE_PROPOSAL ?1):
#   --listen 0.0.0.0      LAN-reachable; the Windows firewall rule scopes 8188
#                         INBOUND TO THE PORTAL BOX ONLY (firewall_forge.ps1) -
#                         ComfyUI answers unauthenticated, the rule is the gate.
#   --disable-smart-memory  models leave VRAM/RAM when a job ends (the /free
#                         contract's steady-state twin; the forge MCP calls
#                         POST /free after every job).
#   --preview-method none   no extra preview-image memory; results come back
#                         through /view.
param(
    [string]$Root = "C:\forge",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 8188,
    [switch]$LowVram
)
$Comfy = Join-Path $Root "ComfyUI"
$VenvPy = Join-Path $Root "venv\Scripts\python.exe"
$log = Join-Path $Root ("logs\comfyui-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$extra = @()
if ($LowVram) { $extra += "--lowvram" }
Write-Host "starting ComfyUI on ${Listen}:${Port} (log: $log)"
Push-Location $Comfy
& $VenvPy main.py --listen $Listen --port $Port --disable-smart-memory --preview-method none @extra 2>&1 |
    Tee-Object -FilePath $log -Append
Pop-Location
