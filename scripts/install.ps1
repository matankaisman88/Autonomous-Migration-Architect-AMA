[CmdletBinding()]
param(
    [switch]$Embed,
    [switch]$Viz,
    [switch]$All,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

try {
    Step "Using Python: $Python"
    & $Python --version
}
catch {
    Write-Error "Python was not found via '$Python'. Install Python 3.11+ and retry."
    exit 1
}

Step "Creating virtual environment (.venv)"
& $Python -m venv .venv

$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Could not locate Python in .venv at $venvPython"
    exit 1
}

Step "Upgrading pip"
& $venvPython -m pip install --upgrade pip

Step "Installing AMA (editable)"
& $venvPython -m pip install -e .

$extras = @()
if ($All -or $Embed) { $extras += "embed" }
if ($All -or $Viz) { $extras += "viz" }
$extras = $extras | Select-Object -Unique

if ($extras.Count -gt 0) {
    $extraList = ($extras -join ",")
    Step "Installing extras: $extraList"
    & $venvPython -m pip install -e ".[${extraList}]"
}

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Activate the environment:"
Write-Host "  .venv\Scripts\Activate.ps1"
Write-Host "Then run:"
Write-Host "  ama-ingest --help"
