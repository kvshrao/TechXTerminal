# Builds everything that is released, 64-bit Windows only:
#   dist\TechXTerminal-<version>-x64-portable.exe   one file, runs without installing
#   dist\TechXTerminal-Setup-<version>-x64.exe      the installer
#   dist\SHA256SUMS.txt                             checksums of both, to verify a download
# Usage, from anywhere:   powershell -ExecutionPolicy Bypass -File packaging\build_release.ps1
#
# There is no 32-bit (x86) build: Qt 6, which the application is built on, does not support
# 32-bit Windows.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$dist = Join-Path $root "dist"

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$bits = & $python -c "import struct; print(struct.calcsize('P') * 8)"
if ($bits -ne "64") { Write-Host "A 64-bit Python is needed (this one is $bits-bit)"; exit 1 }
$version = & $python -c "import sys; sys.path.insert(0, r'$root'); import techxterminal; print(techxterminal.__version__)"

# The portable single file.
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build.ps1") -OneFile
if ($LASTEXITCODE -ne 0) { Write-Host "The single-file build failed"; exit 1 }
$portable = Join-Path $dist "TechXTerminal-$version-x64-portable.exe"
Move-Item -Force (Join-Path $dist "TechXTerminal.exe") $portable

# The installer (it builds the folder version it carries).
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_installer.ps1")
if ($LASTEXITCODE -ne 0) { Write-Host "The installer build failed"; exit 1 }
$setup = Join-Path $dist "TechXTerminal-Setup-$version-x64.exe"
# The folder version was only the installer's input; leave dist\ with just what is handed out.
Remove-Item -Recurse -Force (Join-Path $dist "TechXTerminal") -ErrorAction SilentlyContinue

$sums = foreach ($file in $portable, $setup) {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower(), (Split-Path -Leaf $file)
}
Set-Content -Encoding ascii (Join-Path $dist "SHA256SUMS.txt") $sums

Write-Host ""
Write-Host "Built, in dist\:"
foreach ($file in $portable, $setup) {
    "  {0}  ({1:N1} MB)" -f (Split-Path -Leaf $file), ((Get-Item $file).Length / 1MB)
}
Write-Host "  SHA256SUMS.txt"
