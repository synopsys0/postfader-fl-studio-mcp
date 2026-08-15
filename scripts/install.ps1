[CmdletBinding()]
param(
    [string]$UserDataDir,
    [string]$Python,
    [switch]$DryRun,
    [switch]$AllowBridgeReplacementWhileFLStudioRunning,
    [int[]]$TestRunningFLStudioProcessId
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VenvRoot = Join-Path $RepositoryRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

if ($UserDataDir -and -not [IO.Path]::IsPathRooted($UserDataDir)) {
    throw "-UserDataDir must be an absolute FL Studio user-data path."
}
if ($TestRunningFLStudioProcessId -and -not $DryRun) {
    throw "-TestRunningFLStudioProcessId is test-only and requires -DryRun."
}

function Resolve-BootstrapPython {
    if ($Python) {
        if (-not [IO.Path]::IsPathRooted($Python)) {
            throw "-Python must be an absolute native Windows interpreter path."
        }
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw "Python interpreter was not found at: $Python"
        }
        return @([IO.Path]::GetFullPath($Python))
    }
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        return @($VenvPython)
    }
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher) {
        return @($Launcher.Source, "-3")
    }
    $NativePython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($NativePython) {
        return @($NativePython.Source)
    }
    throw "No native Windows Python was found. Install Python 3.10+ or pass -Python with an absolute path."
}

function Invoke-CheckedPython {
    param(
        [string]$Command,
        [string[]]$Prefix,
        [string[]]$Arguments
    )
    & $Command @Prefix @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

$Bootstrap = @(Resolve-BootstrapPython)
$BootstrapCommand = $Bootstrap[0]
$BootstrapPrefix = @($Bootstrap | Select-Object -Skip 1)
$GenerateConfigScript = Join-Path $RepositoryRoot "scripts\generate_mcp_config.py"
$GenerateConfigCommand = "& {0} {1} --help" -f @(
    (ConvertTo-PowerShellSingleQuotedLiteral $VenvPython),
    (ConvertTo-PowerShellSingleQuotedLiteral $GenerateConfigScript)
)
$SelectionCode = @'
import sys
from fl_studio_mcp.host_config import fl_studio_user_data_dir
value = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(fl_studio_user_data_dir(value))
'@

Push-Location $RepositoryRoot
try {
    $SelectionArguments = @("-c", $SelectionCode)
    if ($UserDataDir) {
        $SelectionArguments += $UserDataDir
    }
    $ResolvedUserData = (& $BootstrapCommand @BootstrapPrefix @SelectionArguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve the FL Studio user-data folder."
    }
    $ResolvedUserData = [string]$ResolvedUserData

    if ($DryRun) {
        if ($null -eq $TestRunningFLStudioProcessId) {
            $RunningFLStudioProcessIds = @()
        }
        else {
            $RunningFLStudioProcessIds = @($TestRunningFLStudioProcessId)
        }
    }
    else {
        $RunningFLStudioProcessIds = @(
            Get-Process -ErrorAction SilentlyContinue |
                Where-Object { $_.ProcessName -match '^(FL|FL64|FL Studio)$' } |
                Select-Object -ExpandProperty Id
        )
    }
    $ReplacementRefused = (
        $RunningFLStudioProcessIds.Count -gt 0 -and
        -not $AllowBridgeReplacementWhileFLStudioRunning
    )

    if ($DryRun) {
        [ordered]@{
            dry_run = $true
            repository_root = $RepositoryRoot
            virtual_environment = $VenvRoot
            bootstrap_python = $BootstrapCommand
            user_data_dir = $ResolvedUserData
            would_create_venv = -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)
            would_install_project = $true
            would_deploy_packaged_bridge = $true
            detected_fl_studio_process_ids = $RunningFLStudioProcessIds
            would_refuse_bridge_replacement = $ReplacementRefused
            running_fl_studio_override = [bool]$AllowBridgeReplacementWhileFLStudioRunning
            would_write_client_configuration = $false
            persistent_environment_changes = $false
            configuration_help_command = $GenerateConfigCommand
        } | ConvertTo-Json -Depth 3
        if ($ReplacementRefused) {
            exit 2
        }
        exit 0
    }

    if ($ReplacementRefused) {
        throw ((
            "FL Studio is running (PID(s): {0}). Refusing bridge replacement. " +
            "Close FL Studio, or deliberately pass " +
            "-AllowBridgeReplacementWhileFLStudioRunning."
        ) -f ($RunningFLStudioProcessIds -join ", "))
    }
    if ($RunningFLStudioProcessIds.Count -gt 0) {
        Write-Warning ((
            "FL Studio is running (PID(s): {0}); the narrowly scoped bridge " +
            "replacement override was explicitly supplied."
        ) -f ($RunningFLStudioProcessIds -join ", "))
    }

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Host "Creating native Windows virtual environment at $VenvRoot"
        Invoke-CheckedPython $BootstrapCommand $BootstrapPrefix @("-m", "venv", $VenvRoot)
    }

    Write-Host "Installing Postfader into the checkout virtual environment"
    Invoke-CheckedPython $VenvPython @() @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-CheckedPython $VenvPython @() @("-m", "pip", "install", "--editable", $RepositoryRoot)

    Write-Host "Deploying the packaged, stamped bridge"
    $DeployArguments = @("-m", "fl_studio_mcp.bridge_install")
    if ($UserDataDir) {
        $DeployArguments += @("--user-data-dir", $ResolvedUserData)
    }
    Invoke-CheckedPython $VenvPython @() $DeployArguments

    Write-Host ""
    Write-Host "Installation complete. No MCP client configuration was changed."
    Write-Host "Generate a Codex or Claude example explicitly with:"
    Write-Host "  $GenerateConfigCommand"
    Write-Host "Postfader never installs or configures a MIDI driver."
}
finally {
    Pop-Location
}
