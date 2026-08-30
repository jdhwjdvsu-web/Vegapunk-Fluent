[CmdletBinding()]
param(
    [string]$BindAddress = "127.0.0.1",
    [int]$Port = 18001,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\Vegapunk\fluent-mcp",
    [string]$JobsRoot = "$env:LOCALAPPDATA\Vegapunk\fluent-jobs"
)

$ErrorActionPreference = "Stop"
$VenvPython = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..\..")).Path
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "PyFluent-MCP is not installed. Run install_pyfluent_mcp.ps1 first."
}

$env:PYTHONPATH = $RepositoryRoot
$env:VEGAPUNK_FLUENT_JOBS = $JobsRoot
Write-Host "Starting Vegapunk Fluent Job MCP at http://${BindAddress}:$Port/mcp"
& $VenvPython -m integrations.fluent.mcp_server --host $BindAddress --port $Port --jobs-root $JobsRoot
