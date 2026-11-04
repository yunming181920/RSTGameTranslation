param([string]$Python = 'py', [switch]$SkipVerify)
$ErrorActionPreference = 'Stop'
$venv = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($Python -eq 'py') { & py -3.12 -m venv $venv }
    else { & $Python -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 virtual environment creation failed.' }
}
& $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'GPU OCR dependency installation failed.' }
if (-not $SkipVerify) {
    & $venvPython (Join-Path $PSScriptRoot 'verify.py')
    if ($LASTEXITCODE -ne 0) { throw 'DirectML verification failed.' }
}
Write-Host "OCR Python: $venvPython"
