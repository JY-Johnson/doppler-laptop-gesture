$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Run: py -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt"
}
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name DopplerGesture (Join-Path $PSScriptRoot "app.py")
Write-Host "Built dist\DopplerGesture.exe"
