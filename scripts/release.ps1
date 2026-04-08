<#
.SYNOPSIS
    Release script for mneme (PowerShell, uv-native).

.DESCRIPTION
    Builds the mneme sdist + wheel with `uv build`, validates with twine,
    smoke-tests the wheel in a clean uv venv, and optionally uploads to
    TestPyPI or PyPI.

.PARAMETER Target
    'check' - build + twine check + clean-venv install (default, no upload).
    'test'  - same as check, then upload to TestPyPI.
    'prod'  - same as check, then upload to real PyPI.

.EXAMPLE
    scripts\release.ps1 check
    scripts\release.ps1 test
    scripts\release.ps1 prod

.NOTES
    Prerequisites:
      - uv installed: https://docs.astral.sh/uv/
      - For uploads: ~/.pypirc with API tokens for [pypi] and [testpypi],
        or set $env:TWINE_USERNAME = '__token__' and
        $env:TWINE_PASSWORD = '<token>' in the shell.

    Build / check / install all run via uv. Twine is fetched on demand
    via `uv tool run twine` so no global pip install is required.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('check', 'test', 'prod')]
    [string]$Target = 'check'
)

$ErrorActionPreference = 'Stop'

# Move to repo root (parent of scripts/)
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is not installed. Install it from https://docs.astral.sh/uv/'
}

Write-Host '==> Cleaning previous build artifacts' -ForegroundColor Cyan
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
    'build', 'dist', 'mneme.egg-info'
Get-ChildItem -Filter '*.egg-info' -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

Write-Host '==> Building sdist + wheel with uv' -ForegroundColor Cyan
uv build
if ($LASTEXITCODE -ne 0) { throw 'uv build failed.' }

Write-Host '==> Verifying package metadata + README rendering' -ForegroundColor Cyan
$distFiles = Get-ChildItem dist\* | ForEach-Object { $_.FullName }
uv tool run twine check @distFiles
if ($LASTEXITCODE -ne 0) { throw 'twine check failed.' }

# Smoke-test the wheel in a clean uv venv
$wheel = (Get-ChildItem dist\*.whl | Select-Object -First 1).FullName
$checkVenv = Join-Path $env:TEMP 'mneme-release-check'

Write-Host "==> Smoke-testing wheel in a clean uv venv ($checkVenv)" -ForegroundColor Cyan
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $checkVenv
uv venv $checkVenv
if ($LASTEXITCODE -ne 0) { throw 'uv venv failed.' }

uv pip install --python $checkVenv $wheel
if ($LASTEXITCODE -ne 0) { throw 'wheel install failed.' }

$mnemeBin = Join-Path $checkVenv 'Scripts\mneme.exe'
if (-not (Test-Path $mnemeBin)) {
    throw "Could not locate mneme entry point at $mnemeBin"
}

& $mnemeBin --version
if ($LASTEXITCODE -ne 0) { throw 'mneme --version failed in the smoke-test venv.' }

switch ($Target) {
    'check' {
        Write-Host ''
        Write-Host "Pre-flight passed. Run 'scripts\release.ps1 test' to upload to TestPyPI." -ForegroundColor Green
        return
    }
    'test' {
        Write-Host '==> Uploading to TestPyPI' -ForegroundColor Cyan
        uv tool run twine upload --repository testpypi @distFiles
        if ($LASTEXITCODE -ne 0) { throw 'TestPyPI upload failed.' }

        Write-Host ''
        Write-Host 'Test install with:' -ForegroundColor Green
        Write-Host '  uv pip install --index-url https://test.pypi.org/simple/ `'
        Write-Host '      --extra-index-url https://pypi.org/simple/ mneme-cli'
    }
    'prod' {
        Write-Host '==> Uploading to PyPI (production)' -ForegroundColor Yellow
        $confirm = Read-Host 'Are you sure you want to publish to real PyPI? [y/N]'
        if ($confirm.ToLower() -notin @('y', 'yes')) {
            Write-Host 'Aborted.'
            return
        }
        uv tool run twine upload @distFiles
        if ($LASTEXITCODE -ne 0) { throw 'PyPI upload failed.' }

        Write-Host ''
        Write-Host 'Done. Verify at https://pypi.org/project/mneme-cli/' -ForegroundColor Green
    }
}
