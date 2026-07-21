[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

Push-Location $projectRoot
try {
    if (-not (Test-Path $python)) {
        py -3.12 -m venv .venv
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }

    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    & $python -m pip install -r requirements.txt -r requirements-dev.txt
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
