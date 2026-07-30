[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$manifests = 'requirements.txt', 'requirements-dev.txt'

function Get-DependencyFingerprint([string]$root) {
    foreach ($manifest in $manifests) {
        "$(Get-FileHash (Join-Path $root $manifest) | Select-Object -ExpandProperty Hash) $manifest"
    }
}

Push-Location $projectRoot
try {
    $gitDir = [IO.Path]::GetFullPath((git rev-parse --git-dir), $projectRoot)
    $commonGitDir = [IO.Path]::GetFullPath((git rev-parse --git-common-dir), $projectRoot)
    if ($gitDir -ne $commonGitDir) {
        $mainRoot = Split-Path -Parent $commonGitDir
        $sharedVenv = Join-Path $mainRoot '.venv'
        if (-not (Test-Path (Join-Path $sharedVenv 'Scripts\python.exe'))) {
            Write-Error 'Shared Python environment is missing; run scripts/setup-worktree.ps1 in the main repository first.'
        }
        foreach ($manifest in $manifests) {
            if ((Get-FileHash (Join-Path $projectRoot $manifest)).Hash -ne (Get-FileHash (Join-Path $mainRoot $manifest)).Hash) {
                Write-Error "Worktree and main repository dependency manifests differ: $manifest"
            }
        }
        $fingerprintPath = Join-Path $sharedVenv '.requirements.sha256'
        if (-not (Test-Path $fingerprintPath) -or (Compare-Object (Get-Content $fingerprintPath) (Get-DependencyFingerprint $mainRoot))) {
            Write-Error 'Shared Python environment is stale; run scripts/setup-worktree.ps1 in the main repository first.'
        }
        $worktreeVenv = Join-Path $projectRoot '.venv'
        if (Test-Path $worktreeVenv) {
            $existingVenv = Get-Item $worktreeVenv -Force
            if ($existingVenv.LinkType -eq 'Junction' -and [IO.Path]::GetFullPath([string]$existingVenv.Target) -eq [IO.Path]::GetFullPath($sharedVenv)) {
                exit 0
            }
            Write-Error 'Worktree .venv exists but is not linked to the main repository environment.'
        }
        New-Item -ItemType Junction -Path $worktreeVenv -Target $sharedVenv | Out-Null
        exit 0
    }

    if (-not (Test-Path $python)) {
        py -3.12 -m venv .venv
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }

    Remove-Item (Join-Path $projectRoot '.venv\.requirements.sha256') -Force -ErrorAction SilentlyContinue

    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    & $python -m pip install -r requirements.txt -r requirements-dev.txt
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    Get-DependencyFingerprint $projectRoot | Set-Content (Join-Path $projectRoot '.venv\.requirements.sha256') -Encoding ascii
    exit 0
}
finally {
    Pop-Location
}
