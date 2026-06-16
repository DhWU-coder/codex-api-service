# Windows PowerShell runner for codex-api-service.
# It prefers the project virtual environment and falls back to global Python with a warning.

$ErrorActionPreference = "Stop"

# Resolve project paths relative to this script so it works from any current directory.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"
$OutLog = Join-Path $LogDir "windows.out.log"
$ErrLog = Join-Path $LogDir "windows.err.log"

# Keep output unbuffered so logs show the startup banner quickly.
$env:PYTHONUNBUFFERED = "1"

# Ensure the log directory exists before Start-Process redirects output.
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Prefer the project virtual environment; warn clearly before falling back.
if (Test-Path $VenvPython) {
    $PythonBin = $VenvPython
} else {
    Write-Warning "WARNING: $VenvPython not found; falling back to global Python."
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "No Python interpreter found. Create .venv or install global python."
    }
    $PythonBin = $PythonCommand.Source
}

# Run from the project root so config.yaml and relative usage paths resolve consistently.
Set-Location $ProjectRoot

# Start the API process and write logs to files that are ignored by Git.
Start-Process `
    -FilePath $PythonBin `
    -ArgumentList @("-m", "codex_api_service.app") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -NoNewWindow `
    -Wait
