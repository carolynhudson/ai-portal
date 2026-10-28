# install_forge.ps1 - image-forge remote backend installer (RainbowBlock.local)
#
# What this is: the Windows-side half of the "image forge" MCP plan
# (docs/IMAGE_FORGE_PROPOSAL.md on the Halo box). ComfyUI + the ComfyUI-GGUF
# node + the Qwen-Image-2.1 pipeline (gen + edit + native RGBA) + the
# zero-download z-image bring-up set. Reads model_files.json (same folder) as
# its inventory, so the list of files is ONE artifact, versioned on the portal.
#
# Run as the normal desktop user (no admin):  Set-ExecutionPolicy -Scope Process Bypass -Force; .\install_forge.ps1
# Idempotent: safe to re-run; every step checks before acting.
# Requires internet. ~35 GB of downloads (see model_files.json); the
# copy_from_halo rows arrive over SFTP from halo.local instead.

param(
    [string]$Root = "C:\forge",
    [switch]$IncludeGGUF,          # + unsloth Qwen-Image-2.1 Q4_K_M (~4.6 GB RAM tier)
    [switch]$SkipModels            # install the runtime only, download nothing
)

$ErrorActionPreference = "Stop"
$RootDir = $Root
$Comfy = Join-Path $RootDir "ComfyUI"
$VenvPy = Join-Path $RootDir "venv\Scripts\python.exe"
$LogFile = Join-Path $RootDir "logs\install.log"
New-Item -ItemType Directory -Force -Path (Join-Path $RootDir "logs") | Out-Null

function Say([string]$m) {
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

# ---------------------------------------------------------------- preflight
Say "image-forge install -> $RootDir"
$disk = Get-PSDrive ($RootDir.Substring(0,1))
$freeGB = [math]::Round($disk.Free / 1GB, 1)
Say ("disk free on {0}: {1} GB" -f $RootDir.Substring(0,1), $freeGB)
if ($freeGB -lt 80) { Say "WARNING: <80 GB free; models + venv need the room. Continuing." }

$gpu = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like "*NVIDIA*" }
if (-not $gpu) { throw "No NVIDIA GPU visible - this box is the CUDA backend; check drivers first." }
Say ("GPU: {0} ({1} GB)" -f $gpu[0].Name, [math]::Round($gpu[0].AdapterRAM / 1GB, 0))
# 2080 Ti is sm_75 (Turing): int8 tensor cores yes, fp8/bf16 compute no. The
# int8_convrot files from Comfy-Org are the fast tier; the gate on the portal
# side reads this box's /system_stats, not this line.

# ------------------------------------------------------------------- git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Say "git missing -> winget install Git.Git (re-run the installer after it finishes)"
    winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
    throw "git installed; re-run install_forge.ps1 so PATH refreshes."
}
Say ("git: " + (git --version))

# ----------------------------------------------------------------- python
function Find-Python {
    foreach ($probe in @("3.13","3.12","3.11","3.10")) {
        $ok = & py "-$probe" -c "import sys;print(sys.version)" 2>$null
        if ($ok) { return @("-$probe", $ok) }
    }
    $ok = & python -c "import sys;print(sys.version)" 2>$null
    if ($ok) { return @("", $ok) }
    return $null
}
$py = Find-Python
if (-not $py) {
    Say "python >=3.10 not found -> winget install Python.Python.3.12 (re-run after)"
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    throw "python installed; re-run install_forge.ps1."
}
$pyArgs = $py[0]
Say ("python: " + ($py[1] -split "`n")[0])

# --------------------------------------------------------------- ComfyUI
if (-not (Test-Path (Join-Path $Comfy "main.py"))) {
    Say "cloning ComfyUI (master)"
    git clone --depth 1 https://github.com/comfy-org/comfyui $Comfy
} else { Say "ComfyUI present (not auto-updated; update deliberately: git -C $Comfy pull)" }

$ggufNode = Join-Path $Comfy "custom_nodes\ComfyUI-GGUF"
if (-not (Test-Path $ggufNode)) {
    Say "cloning ComfyUI-GGUF (city96) - the Q4/Q8 loader, the one accepted custom node"
    git clone --depth 1 https://github.com/city96/ComfyUI-GGUF $ggufNode
}

# ------------------------------------------------------------------ venv
if (-not (Test-Path $VenvPy)) {
    Say "creating venv"
    & py $pyArgs -m venv (Join-Path $RootDir "venv")
}
Say "installing torch (CUDA 12.4 wheel line - sm_75 supported) + ComfyUI requirements"
function Guard([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw ("{0} failed (exit {1}) - see {2}" -f $what, $LASTEXITCODE, $LogFile) }
}
& $VenvPy -m pip install --upgrade pip | Out-Null
& $VenvPy -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
Guard "pip torch (cu124)"
& $VenvPy -m pip install -r (Join-Path $Comfy "requirements.txt")
Guard "pip ComfyUI requirements"
& $VenvPy -m pip install "huggingface_hub[cli]"
Guard "pip huggingface_hub[cli]"
Say "torch CUDA check:"
& $VenvPy -c "import torch; print('cuda:', torch.cuda.is_available(), '| device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"

# ---------------------------------------------------------------- models
$Inv = Get-Content (Join-Path $PSScriptRoot "model_files.json") -Raw | ConvertFrom-Json
$Hf = Join-Path $RootDir "venv\Scripts\hf.exe"
function Get-Hf($row) {
    # PS 5.1: no ternary operator (that is PS7+); plain if/else assignments only
    $rel = if ($row.dest) { $row.dest } else { $row.file }
    $target = Join-Path (Join-Path $Comfy "models") $rel
    if (Test-Path $target) { Say ("have: " + $rel); return }
    Say ("download: " + $row.repo + " :: " + $row.file)
    if (-not (Test-Path $Hf)) { $Hf = $VenvPy }   # fall back to the module entry point
    if ($Hf -eq $VenvPy) {
        & $VenvPy -m huggingface_hub.cli download $row.repo $row.file --local-dir (Join-Path $Comfy "models")
    } else {
        & $Hf download $row.repo $row.file --local-dir (Join-Path $Comfy "models")
    }
    if ($LASTEXITCODE -ne 0) {
        # one line, single-quoted path: 5.1 chokes on multi-line + continuation
        # inside throw(...) and on backtick-escaped quotes in long strings
        $hfCmd = Join-Path $RootDir "venv\Scripts\hf.exe"
        throw ("download failed for " + $row.file + " (exit " + $LASTEXITCODE + "). If the repo is license-gated (qwen-research): sign in at huggingface.co, accept the license, then run:  & '" + $hfCmd + "' auth login  - and re-run this installer.")
    }
    if ($row.dest) {
        $landed = Join-Path (Join-Path $Comfy "models") $row.file
        $dest = Join-Path (Join-Path $Comfy "models") $row.dest
        New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
        if ((Test-Path $landed) -and ($landed -ne $dest)) { Move-Item -Force $landed $dest }
    }
}
if (-not $SkipModels) {
    foreach ($row in $Inv.hf) { Get-Hf $row }
    if ($IncludeGGUF) { foreach ($row in $Inv.hf_optional_gguf) { Get-Hf $row } }
}
Say "copy-from-halo list (SFTP from halo.local; the installer cannot reach the Halo box):"
foreach ($row in $Inv.copy_from_halo) { Say ("  {0}  ->  ComfyUI\models\{1}" -f $row.from, $row.to) }

# ---------------------------------------------------------------- summary
Say "done. Next steps (see README.md):"
Say "  1. firewall (admin):  .\firewall_forge.ps1"
Say "  2. autostart:         .\register_autostart.ps1"
Say "  3. start now:         .\start_comfyui.ps1"
Say "  4. verify:            .\verify_forge.ps1"
Say "  5. from the Halo box: curl http://RainbowBlock.local:8188/system_stats"
