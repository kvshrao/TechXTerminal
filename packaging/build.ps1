# Builds a standalone Windows application in dist\TechXTerminal\ (no Python needed to run it).
# Usage, from anywhere:   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Add -OneFile for a single dist\TechXTerminal.exe instead: easier to pass around, slower to start,
# because it unpacks itself to a temporary folder each time it runs.
param([switch]$OneFile)

# Python tools write progress to stderr, so only exit codes decide success here.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -m pip install --quiet --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) { Write-Host "Could not install PyInstaller"; exit 1 }

& $python (Join-Path $root "packaging\make_icon.py")
if ($LASTEXITCODE -ne 0) { Write-Host "Could not create the icon"; exit 1 }

# What Windows shows in the properties of the .exe, and the licences of the libraries that go with it.
& $python (Join-Path $root "packaging\make_version_file.py")
if ($LASTEXITCODE -ne 0) { Write-Host "Could not create the version resource"; exit 1 }
& $python (Join-Path $root "packaging\make_notices.py")
if ($LASTEXITCODE -ne 0) { Write-Host "Could not gather the third-party notices"; exit 1 }

# Absolute paths: PyInstaller resolves data files relative to the spec file, not this folder.
$fonts = Join-Path $root "techxterminal\fonts"
$licence = Join-Path $root "LICENSE"
$notice = Join-Path $root "NOTICE"
$thirdParty = Join-Path $root "packaging\THIRD_PARTY_NOTICES.txt"
$changes = Join-Path $root "CHANGELOG.md"
$layout = if ($OneFile) { "--onefile" } else { "--onedir" }
# Since Python 3.15, extensions built for the stable ABI (cryptography's, which SSH needs) load
# python3t.dll. Python keeps it in a folder of its own that PyInstaller does not search, and
# without it the built program stops at start-up, so it is added by hand when there is one.
$extra = @()
$abi3t = & $python -c "import os, sys; print(os.path.join(sys.base_prefix, 'abi3t-compat', 'python3t.dll'))"
if (Test-Path $abi3t) { $extra += @("--add-binary", "$abi3t;.") }
& $python -m PyInstaller --noconfirm --clean --windowed $layout @extra `
    --name TechXTerminal `
    --exclude-module invoke `
    --collect-all winpty `
    --icon (Join-Path $root "packaging\techx.ico") `
    --version-file (Join-Path $root "packaging\version_info.txt") `
    --add-data "$fonts;techxterminal\fonts" `
    --add-data "$licence;." `
    --add-data "$notice;." `
    --add-data "$thirdParty;." `
    --add-data "$changes;." `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    (Join-Path $root "main.py")
if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller failed"; exit 1 }

Write-Host ""
if ($OneFile) { Write-Host "Built dist\TechXTerminal.exe" } else { Write-Host "Built dist\TechXTerminal\TechXTerminal.exe" }
