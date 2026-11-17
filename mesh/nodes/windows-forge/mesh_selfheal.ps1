# mesh_selfheal.ps1 - node-cert self-heal at launch (bench #41/#42, docs/mesh-forge-enroll.md
# "Re-enroll (self-heal)"). The portal mints a fresh forge leaf nightly and stages it at
# cortana@halo.local:~/ai/state/mesh/forge-bundle/{cert.crt,cert.key} (0600, 24 h leaf;
# expiry = revocation, so a stale bundle is refused). Rule: if cert.crt notAfter < 6 h,
# pull, install, KEEP identity-forge.key untouched (grants ride the age pubkey), verify
# meshctl nodes + get subagent-key (length only - the key is NEVER printed).
# Failure-tolerant by design: meshctl/router/meshd keep working on the file fallback.
# ASCII only, PS 5.1.
param(
    [int]$MinHoursLeft = 6,
    [switch]$Force
)
$ErrorActionPreference = 'Continue'
$meshdir = Join-Path $env:USERPROFILE '.mesh'
$meshctl = 'C:\forge\etc\mesh\meshctl.exe'
$log = 'C:\forge\logs\mesh_selfheal.log'
$staged = 'cortana@halo.local:/home/cortana/ai/state/mesh/forge-bundle'

function Log($msg) {
    $line = (Get-Date -Format 's') + '  ' + $msg
    Add-Content -Path $log -Value $line
    Write-Host $line
}

if (-not (Test-Path $meshdir)) { Log 'MESHDIR MISSING - nothing to heal'; exit 0 }

# 1. current leaf state
$needHeal = $true
$crtPath = Join-Path $meshdir 'cert.crt'
if (Test-Path $crtPath) {
    try {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $crtPath
        $hoursLeft = ($cert.NotAfter - (Get-Date)).TotalHours
        if ($hoursLeft -ge $MinHoursLeft -and -not $Force) {
            Log ('cert fresh: ' + [math]::Round($hoursLeft, 1) + ' h left (need ' + $MinHoursLeft + ') - no heal')
            exit 0
        }
        Log ('cert low: ' + [math]::Round($hoursLeft, 1) + ' h left - healing')
        $needHeal = $true
        $cert.Dispose()   # Windows: release the handle before Move-Item/enroll touch cert.crt
    } catch {
        Log ('cert unreadable: ' + $_.Exception.Message + ' - healing')
    }
} else {
    Log 'no cert.crt - healing'
}

# 2. pull staged bundle to temp (BatchMode: fail fast, no prompts)
$newCrt = Join-Path $meshdir 'cert.new.crt'
$newKey = Join-Path $meshdir 'cert.new.key'
Remove-Item $newCrt, $newKey -Force -ErrorAction SilentlyContinue
scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$staged/cert.crt" $newCrt 2>&1 | Out-Null
scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$staged/cert.key" $newKey 2>&1 | Out-Null
if (-not ((Test-Path $newCrt) -and (Test-Path $newKey))) {
    Remove-Item $newCrt, $newKey -Force -ErrorAction SilentlyContinue
    Log 'PULL FAILED (halo offline or bundle absent) - continuing on file fallback'
    exit 0
}

# 3. refuse stale bundles (expiry = revocation)
try {
    $newCert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $newCrt
    $newHours = ($newCert.NotAfter - (Get-Date)).TotalHours
    $newCert.Dispose()
} catch {
    Remove-Item $newCrt, $newKey -Force -ErrorAction SilentlyContinue
    Log 'BUNDLE UNPARSEABLE - refused'
    exit 1
}
if ($newHours -lt 1) {
    Remove-Item $newCrt, $newKey -Force -ErrorAction SilentlyContinue
    Log ('BUNDLE STALE (notAfter ' + [math]::Round($newHours, 1) + ' h) - refused; ask portal to re-mint')
    exit 1
}

# 4. enroll from the TEMP copies (meshctl installs them into %USERPROFILE%\.mesh
# itself - source must NOT be the destination file: os error 32). The enroll
# handshake registers the new fingerprint at the hub (hub migrates the 'forge'
# record name -> new fingerprint; copying files alone = 401). identity-forge.key
# is NEVER touched. Same lane as the original enrollment (forge_enroll.ps1).
$rootCa = 'C:\forge\etc\mesh\root_ca.crt'
if (-not (Test-Path $rootCa)) {
    Remove-Item $newCrt, $newKey -Force -ErrorAction SilentlyContinue
    Log 'root_ca.crt missing - cannot re-enroll; file fallback stays valid'
    exit 0
}
& $meshctl enroll --name forge --cert-file $newCrt --key-file $newKey `
    --root-ca $rootCa --roles forge --hub pihole.home.lan:9443,pihole2.home.lan:9443 *> $null
$enrollOk = ($LASTEXITCODE -eq 0)
Remove-Item $newCrt, $newKey -Force -ErrorAction SilentlyContinue
if (-not $enrollOk) {
    Log ('re-enroll FAILED exit ' + $LASTEXITCODE + ' - file fallback stays valid; ask portal to re-mint')
    exit 1
}
Log ('bundle enrolled: new leaf valid ' + [math]::Round($newHours, 1) + ' h registered at hub; identity-forge.key untouched; temps purged')

# 5. verify hub lane (lengths only - no secrets in logs)
& $meshctl nodes *> $null
$nodesOk = ($LASTEXITCODE -eq 0)
$keyLen = 0
$keyOut = & $meshctl get subagent-key 2>$null
if ($LASTEXITCODE -eq 0 -and $keyOut) { $keyLen = (($keyOut | Out-String).Trim()).Length }
if ($nodesOk -and $keyLen -ge 16) {
    Log ('HEAL PASS: meshctl nodes OK, subagent-key grant readable (' + $keyLen + ' chars, not printed)')
    exit 0
}
Log ('HEAL PARTIAL: nodes exit ' + $LASTEXITCODE + ' ok=' + $nodesOk + ' keylen=' + $keyLen + ' - file fallback stays valid')
exit 0
