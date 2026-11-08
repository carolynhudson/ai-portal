<#
parse_check.ps1 - preflight parser check for every .ps1 in this kit.

Run it on RainbowBlock.local BEFORE install_forge.ps1:

  powershell -ExecutionPolicy Bypass -File parse_check.ps1

Every script in this kit is parsed by the real PowerShell parser and
NOTHING from the files is executed. Each file gets PARSE OK, or every
parse error with its line and column.

Why this exists: the first runs of this kit on the target box died with
parse errors before doing any work (ANSI-codepage mojibake in a UTF-8
file; a PS7 ternary; a multi-line throw). All of that is visible here,
on every file, in seconds, before an installer touches the disk.

It also flags non-ASCII bytes: Windows PowerShell 5.1 reads scripts as
ANSI, so a UTF-8 em-dash arrives as three mojibake bytes, breaks string
quoting, and shows up as a parse error. See README 'File encoding'.
Exit code is the number of files that failed to parse.

Parser API note: the modern surface is
System.Management.Automation.Language.Parser::ParseInput (PowerShell 3+,
so 5.1). The legacy PSParser class has Tokenize only - calling
ParseInput on it is 'MethodNotFound' (observed on the target box), so
the modern call is tried first and Tokenize is the fallback.
#>

$ErrorActionPreference = 'Stop'

function Get-ParseProblems([string]$code) {
    # Returns one string per parse problem; an empty result means PARSE OK.
    $out = @()
    $tokens = $null
    $perrs = $null
    try {
        [void][System.Management.Automation.Language.Parser]::ParseInput($code, [ref]$tokens, [ref]$perrs)
        foreach ($e in $perrs) {
            $out += ("line " + $e.Extent.StartLineNumber + " col " + $e.Extent.StartColumnNumber + " : " + $e.Message)
        }
        return $out
    } catch {
        # Only reached on a box with no modern parser (v2-era): Tokenize
        # reports syntax problems too, in the legacy property shape.
        $toks = $null
        $terr = $null
        [void][System.Management.Automation.PSParser]::Tokenize($code, [ref]$terr)
        foreach ($e in $terr) {
            $out += ("line " + $e.Token.StartLine + " col " + $e.Token.StartColumn + " : " + $e.Message)
        }
        return $out
    }
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$files = @(Get-ChildItem -Path $here -Filter '*.ps1' -File |
    Sort-Object Name)

if ($files.Count -eq 0) {
    Write-Host "no .ps1 files found next to this script in: $here"
    exit 2
}

Write-Host "parse preflight: PowerShell $($PSVersionTable.PSVersion)"
Write-Host "folder: $here"

$bad = 0
foreach ($f in $files) {
    # ASCII audit: the encoding trap is what broke the earliest runs.
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hi = @($bytes | Where-Object { $_ -gt 127 })

    $code = Get-Content -Raw -LiteralPath $f.FullName
    $problems = Get-ParseProblems $code

    if ($problems.Count -gt 0) {
        $bad = $bad + 1
        Write-Host ''
        Write-Host "PARSE FAIL: $($f.Name)"
        foreach ($p in $problems) {
            Write-Host "    $p"
        }
        if ($hi.Count -gt 0) {
            Write-Host "    NOTE: $($hi.Count) non-ASCII byte(s) - see README 'File encoding'"
        }
    }
    elseif ($hi.Count -gt 0) {
        Write-Host "PARSE OK : $($f.Name)  (WARN: $($hi.Count) non-ASCII bytes)"
    }
    else {
        Write-Host "PARSE OK : $($f.Name)"
    }
}

Write-Host ''
if ($bad -eq 0) {
    Write-Host 'ALL FILES PARSE OK - safe to run install_forge.ps1'
    exit 0
}
Write-Host "$bad of $($files.Count) file(s) failed to parse - fix before installing"
exit 1
