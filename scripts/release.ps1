<#
.SYNOPSIS
    Release script for mneme (PowerShell).

.DESCRIPTION
    Builds the mneme sdist + wheel, validates with twine check, and uploads to
    TestPyPI or PyPI.

.PARAMETER Target
    'test' uploads to TestPyPI, 'prod' uploads to real PyPI.

.EXAMPLE
    scripts\release.ps1 test
    scripts\release.ps1 prod

.NOTES
    Prerequisites:
      pip install -e ".[release]"
      ~/.pypirc configured with API tokens for [pypi] and [testpypi],
      or set $env:TWINE_USERNAME = '__token__' and $env:TWINE_PASSWORD = '<token>'.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('test', 'prod')]
    [string]$Target = 'test'
)

$ErrorActionPreference = 'Stop'

# Move to repo root (parent of scripts/)
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host '==> Cleaning previous build artifacts' -ForegroundColor Cyan
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
    'build', 'dist', 'mneme.egg-info'
Get-ChildItem -Filter '*.egg-info' -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

Write-Host '==> Building sdist + wheel' -ForegroundColor Cyan
python -m build
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }

Write-Host '==> Verifying package metadata + README rendering' -ForegroundColor Cyan
twine check (Get-ChildItem dist\* | ForEach-Object { $_.FullName })
if ($LASTEXITCODE -ne 0) { throw 'twine check failed.' }

switch ($Target) {
    'test' {
        Write-Host '==> Uploading to TestPyPI' -ForegroundColor Cyan
        twine upload --repository testpypi (Get-ChildItem dist\* | ForEach-Object { $_.FullName })
        if ($LASTEXITCODE -ne 0) { throw 'TestPyPI upload failed.' }

        Write-Host ''
        Write-Host 'Test install with:' -ForegroundColor Green
        Write-Host '  pip install --index-url https://test.pypi.org/simple/ `'
        Write-Host '      --extra-index-url https://pypi.org/simple/ mneme-cli'
    }
    'prod' {
        Write-Host '==> Uploading to PyPI (production)' -ForegroundColor Yellow
        $confirm = Read-Host 'Are you sure you want to publish to real PyPI? [y/N]'
        if ($confirm.ToLower() -notin @('y', 'yes')) {
            Write-Host 'Aborted.'
            exit 0
        }
        twine upload (Get-ChildItem dist\* | ForEach-Object { $_.FullName })
        if ($LASTEXITCODE -ne 0) { throw 'PyPI upload failed.' }

        Write-Host ''
        Write-Host 'Done. Verify at https://pypi.org/project/mneme-cli/' -ForegroundColor Green
    }
}
