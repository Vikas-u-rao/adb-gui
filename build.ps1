# Build a standalone Windows executable in dist\ADB-GUI.exe
# Usage: .\build.ps1
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    python -m pip install --disable-pip-version-check -r requirements.txt
    python -m pip install --disable-pip-version-check pyinstaller
    python -m PyInstaller --noconfirm --clean adb-gui.spec
    Write-Host ""
    Write-Host "Built: $PSScriptRoot\dist\ADB-GUI.exe"
} finally {
    Pop-Location
}
