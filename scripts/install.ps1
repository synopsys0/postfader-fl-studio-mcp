[CmdletBinding()]
param(
    [string]$UserDataDir,
    [string]$Python,
    [switch]$DryRun,
    [switch]$SkipBridgeDeployment,
    [switch]$AllowBridgeReplacementWhileFLStudioRunning,
    [int[]]$TestRunningFLStudioProcessId
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VenvRoot = Join-Path $RepositoryRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$SupportedPythonCode = "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)"

if ($UserDataDir -and -not [IO.Path]::IsPathRooted($UserDataDir)) {
    throw "-UserDataDir must be an absolute FL Studio user-data path."
}
if ($TestRunningFLStudioProcessId -and -not $DryRun) {
    throw "-TestRunningFLStudioProcessId is test-only and requires -DryRun."
}

function Test-PythonCandidate {
    param(
        [string]$Command,
        [string[]]$Prefix
    )
    try {
        & $Command @Prefix -c $SupportedPythonCode *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
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
        foreach ($Version in @("-3.14", "-3.13", "-3.12", "-3.11", "-3.10")) {
            if (Test-PythonCandidate $Launcher.Source @($Version)) {
                return @($Launcher.Source, $Version)
            }
        }
    }
    $NativePython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($NativePython -and (Test-PythonCandidate $NativePython.Source @())) {
        return @($NativePython.Source)
    }
    throw "No compatible native Windows Python was found. Install Python 3.10 through 3.14 or pass -Python with an absolute path."
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
& $BootstrapCommand @BootstrapPrefix -c $SupportedPythonCode
if ($LASTEXITCODE -ne 0) {
    if ([IO.Path]::GetFullPath($BootstrapCommand) -eq [IO.Path]::GetFullPath($VenvPython)) {
        throw "The existing $VenvRoot uses an unsupported Python. Remove that .venv, install Python 3.10 through 3.14, and rerun this installer."
    }
    throw "Python 3.10 through 3.14 is required."
}
$PostFaderCommand = Join-Path $VenvRoot "Scripts\postfader.exe"
$GuidedSetupCommand = "& {0} setup" -f (
    ConvertTo-PowerShellSingleQuotedLiteral $PostFaderCommand
)
$SelectionCode = @'
import sys
from fl_studio_mcp.host_config import fl_studio_user_data_dir
value = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(fl_studio_user_data_dir(value))
'@

Push-Location $RepositoryRoot
try {
    $ResolvedUserData = $null
    if (-not $SkipBridgeDeployment) {
        $SelectionArguments = @("-c", $SelectionCode)
        if ($UserDataDir) {
            $SelectionArguments += $UserDataDir
        }
        $ResolvedUserData = (& $BootstrapCommand @BootstrapPrefix @SelectionArguments)
        if ($LASTEXITCODE -ne 0) {
            throw "Could not resolve the FL Studio user-data folder."
        }
        $ResolvedUserData = [string]$ResolvedUserData
    }

    if ($SkipBridgeDeployment) {
        $RunningFLStudioProcessIds = @()
    }
    elseif ($DryRun) {
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
        -not $SkipBridgeDeployment -and
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
            would_deploy_packaged_bridge = -not [bool]$SkipBridgeDeployment
            detected_fl_studio_process_ids = $RunningFLStudioProcessIds
            would_refuse_bridge_replacement = $ReplacementRefused
            running_fl_studio_override = [bool]$AllowBridgeReplacementWhileFLStudioRunning
            would_write_client_configuration = $false
            persistent_environment_changes = $false
            guided_setup_command = $GuidedSetupCommand
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

    Write-Host "Installing PostFader into the checkout virtual environment"
    Invoke-CheckedPython $VenvPython @() @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-CheckedPython $VenvPython @() @("-m", "pip", "install", "--editable", $RepositoryRoot)

    if ($SkipBridgeDeployment) {
        Write-Host "Bridge deployment deferred to guided setup."
    }
    else {
        Write-Host "Deploying the packaged, stamped bridge"
        $DeployArguments = @("-m", "fl_studio_mcp.bridge_install")
        if ($UserDataDir) {
            $DeployArguments += @("--user-data-dir", $ResolvedUserData)
        }
        Invoke-CheckedPython $VenvPython @() $DeployArguments
    }

    Write-Host ""
    Write-Host "Installation complete. No MCP client configuration was changed."
    Write-Host "Continue with the guided first-time setup:"
    Write-Host "  $GuidedSetupCommand"
    Write-Host "PostFader never installs or configures a MIDI driver."
}
finally {
    Pop-Location
}
