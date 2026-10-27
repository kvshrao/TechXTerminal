# Builds the 64-bit Windows installer: dist\TechXTerminal-Setup-<version>-x64.exe
# Usage, from anywhere:   powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1
# Needs Inno Setup 6 (winget install JRSoftware.InnoSetup) and a 64-bit Python in .venv.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# The installer is 64-bit only, so the application inside it must be too.
$bits = & $python -c "import struct; print(struct.calcsize('P') * 8)"
if ($bits -ne "64") { Write-Host "A 64-bit Python is needed to build the 64-bit installer (this one is $bits-bit)"; exit 1 }

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { Write-Host "Inno Setup 6 is not installed: winget install JRSoftware.InnoSetup"; exit 1 }

# The installer carries the folder build, which starts faster than the single-file one.
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build.ps1")
if ($LASTEXITCODE -ne 0) { Write-Host "The application build failed"; exit 1 }

$version = & $python -c "import sys; sys.path.insert(0, r'$root'); import techxterminal; print(techxterminal.__version__)"
& $iscc /Q "/DAppVersion=$version" (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { Write-Host "Inno Setup failed"; exit 1 }

Write-Host ""
Write-Host "Built dist\TechXTerminal-Setup-$version-x64.exe"
