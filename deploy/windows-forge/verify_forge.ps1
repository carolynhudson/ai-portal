# verify_forge.ps1 - run on the Windows box after everything is installed.
param([int]$Port = 8188, [string]$Root = "C:\forge")
$fail = 0
function Check([string]$label, [bool]$ok, [string]$detail = "") {
    if ($ok) { Write-Host "PASS  $label $detail" -ForegroundColor Green }
    else     { Write-Host "FAIL  $label $detail" -ForegroundColor Red; $script:fail++ }
}
# 1. torch sees the GPU (run in the venv; CUDA wheel must be the cu124 line)
$torch = & (Join-Path $Root "venv\Scripts\python.exe") -c "import torch,sys;print(torch.cuda.is_available(), torch.cuda.get_device_name(0))" 2>&1
Check "torch CUDA" ($torch -like "True*") $torch
# 2. the engine answers
try {
    $r = Invoke-RestMethod "http://127.0.0.1:$Port/system_stats" -TimeoutSec 5
    Check "ComfyUI /system_stats" $true ("version " + $r.system.comfyui_version + " | torch " + $r.system.pytorch_version)
    # schema (verified against ComfyUI 0.39): system.comfyui_version, devices[] with
    # name/type/vram_total - there is no system.cuda object
    $g = @($r.devices | Where-Object { $_.type -eq "cuda" })[0]
    Check "GPU visible" ($g -and [double]$g.vram_total -gt 10GB) ("{0} | total {1:N1} GB" -f $g.name, ($g.vram_total/1GB))
} catch { Check "ComfyUI /system_stats" $false $_.Exception.Message }
# 3. model inventory on disk (from model_files.json)
$Inv = Get-Content (Join-Path $PSScriptRoot "model_files.json") -Raw | ConvertFrom-Json
foreach ($row in ($Inv.hf + $Inv.copy_from_halo)) {
    $f = if ($row.file) { $row.file } else { (Split-Path $row.from -Leaf) }
    $p = Join-Path (Join-Path $Root "ComfyUI\models") $f
    Check ("model: " + $f) (Test-Path $p)
}
# 4. from the Halo box (printed reminder; run there):
Write-Host ""
Write-Host "portal-side check (run on halo.local):" -ForegroundColor Cyan
Write-Host "  curl -s http://RainbowBlock.local:$Port/system_stats"
Write-Host "  Test-NetConnection RainbowBlock.local -Port $Port   (run on halo.local, if enabled)"
exit $fail
