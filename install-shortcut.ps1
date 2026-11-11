# Vanta IDE — shortcut installer.
#
#   powershell -ExecutionPolicy Bypass -File install-shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File install-shortcut.ps1 -Remove
#
# Creates "Vanta IDE" shortcuts on the Desktop and in the Start Menu, pointing
# at pythonw.exe + desktop.py with the app icon. No registry writes, no
# PATH changes — delete the two .lnk files and it never happened.

[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Admin = $true,
    [switch]$Standard
)

$ErrorActionPreference = 'Stop'

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$icon   = Join-Path $here 'assets\vanta.ico'
$script = Join-Path $here 'desktop.py'

$pyw = Join-Path $here '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) {
    $pyw = Join-Path (Split-Path -Parent $here) 'airllm\.venv\Scripts\pythonw.exe'
}
if (-not (Test-Path $pyw)) {
    $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pyw) { throw 'pythonw.exe not found — create the .venv first.' }
if (-not (Test-Path $script)) { throw "desktop.py missing at $script" }

$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Vanta IDE.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Vanta IDE.lnk')
)

if ($Remove) {
    $targets | Where-Object { Test-Path $_ } | ForEach-Object {
        Remove-Item $_ -Force
        Write-Host "removed  $_"
    }
    return
}

$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath       = $pyw
    $s.Arguments        = '"' + $script + '"'
    $s.WorkingDirectory = $here
    $s.Description      = 'Vanta IDE — local workspace'
    $s.IconLocation     = $icon + ',0'
    $s.WindowStyle      = 1
    $s.Save()
    if ($Admin -and -not $Standard) {
        $bytes = [System.IO.File]::ReadAllBytes($lnk)
        $bytes[0x15] = $bytes[0x15] -bor 0x20
        [System.IO.File]::WriteAllBytes($lnk, $bytes)
        Write-Host "created  $lnk (Run as administrator enabled)"
    } else {
        Write-Host "created  $lnk"
    }
}
