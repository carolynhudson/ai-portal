# parse_one.ps1 - parse-check + ASCII check any ps1 file(s) given as args. ASCII only.
param([string[]]$Files)
$ErrorActionPreference = 'Stop'
foreach ($f in $Files) {
    $raw = Get-Content -Raw $f
    $tokens = $null; $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($raw, [ref]$tokens, [ref]$errors)
    if ($errors.Count -eq 0) { "PARSE OK : $f" } else { "FAIL : $f"; $errors | ForEach-Object { "   " + $_.Message + "  line " + $_.Extent.StartLineNumber } }
    $nonAscii = ([regex]::Matches($raw, '[^\x00-\x7F]')).Count
    "   non-ascii bytes: $nonAscii"
}
