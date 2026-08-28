[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\Vegapunk\fluent-mcp"
)

$ErrorActionPreference = "Stop"

if (-not $PythonExecutable) {
    $PythonExecutable = (& py -3.13 -c "import sys; print(sys.executable)" 2>$null)
}
if (-not $PythonExecutable) {
    $PythonExecutable = (& python -c "import sys; assert sys.version_info >= (3, 12); print(sys.executable)" 2>$null)
}
if (-not $PythonExecutable -or -not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "Python 3.12+ was not found. Install Python 3.13 or pass -PythonExecutable."
}
& $PythonExecutable -c "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"

$VenvPath = Join-Path $RuntimeRoot ".venv"
$RequirementsPath = Join-Path $PSScriptRoot "requirements.txt"
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
& $PythonExecutable -m venv $VenvPath
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r $RequirementsPath

Write-Host "PyFluent-MCP installed at $VenvPath"
Write-Host "Start it with: .\start_pyfluent_mcp.ps1"
