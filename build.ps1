param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectDirectory = $PSScriptRoot
$buildPython = Join-Path $projectDirectory ".venv-build\Scripts\python.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Set-Location -LiteralPath $projectDirectory

if (-not (Test-Path -LiteralPath $buildPython)) {
    Invoke-Checked {
        python -m venv (Join-Path $projectDirectory ".venv-build")
    } "Creating build environment"
}

if (-not $SkipInstall) {
    Invoke-Checked {
        & $buildPython -m pip install --upgrade pip
    } "Upgrading pip"
    Invoke-Checked {
        & $buildPython -m pip install -r (Join-Path $projectDirectory "requirements-build.txt")
    } "Installing build dependencies"
}

Invoke-Checked {
    & $buildPython -m unittest discover -s tests -v
} "Tests"
# Avoid collecting incompatible DLLs from unrelated apps on PATH.
$previousBuildPath = $env:PATH
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$(Split-Path $buildPython)"
try {
Invoke-Checked {
    & $buildPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --runtime-tmpdir "%SZU_GUARDIAN_CACHE%" `
        --icon "szu_guardian/assets/app.ico" `
        --add-data "szu_guardian/assets;szu_guardian/assets" `
        --hidden-import PySide6.QtSvg `
        --name "SZU-Guardian-Glass" `
        (Join-Path $projectDirectory "main.py")
} "PyInstaller build"
} finally { $env:PATH = $previousBuildPath }

Write-Host ""
Write-Host "Build complete: $projectDirectory\dist\SZU-Guardian-Glass.exe"
