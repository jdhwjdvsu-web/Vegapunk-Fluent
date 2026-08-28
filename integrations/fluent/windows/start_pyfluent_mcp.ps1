[CmdletBinding()]
param(
    [string]$BindAddress = "127.0.0.1",
    [int]$Port = 18000,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\Vegapunk\fluent-mcp"
)

$ErrorActionPreference = "Stop"
$McpExecutable = Join-Path $RuntimeRoot ".venv\Scripts\ansys-fluent-mcp.exe"
if (-not (Test-Path -LiteralPath $McpExecutable)) {
    throw "PyFluent-MCP is not installed. Run install_pyfluent_mcp.ps1 first."
}

Write-Host "Starting PyFluent-MCP at http://${BindAddress}:$Port/mcp"
& $McpExecutable --transport http --host $BindAddress --port $Port
