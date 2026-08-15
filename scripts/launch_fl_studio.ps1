[CmdletBinding()]
param(
    [string]$Executable,
    [switch]$EnableWrites,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Resolve-FlStudioExecutable {
    param([string]$Requested)

    if ($Requested) {
        if (-not [IO.Path]::IsPathRooted($Requested)) {
            throw "-Executable must be an absolute FL Studio 2026 executable path."
        }
        $Resolved = [IO.Path]::GetFullPath($Requested)
        if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
            throw "FL Studio executable was not found at: $Resolved"
        }
        return $Resolved
    }

    $Candidates = @()
    if (${env:ProgramFiles}) {
        $Candidates += Join-Path ${env:ProgramFiles} "Image-Line\FL Studio 2026\FL64.exe"
        $Candidates += Join-Path ${env:ProgramFiles} "Image-Line\FL Studio 2026\FL.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Image-Line\FL Studio 2026\FL64.exe"
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Image-Line\FL Studio 2026\FL.exe"
    }
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($Candidate)
        }
    }
    throw "FL Studio 2026 was not found in the standard Image-Line folders. Pass -Executable with its absolute path."
}

function Get-RunningFlStudio {
    $Names = @("FL", "FL64", "FL Studio", "FL Studio 2026")
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $Names -contains $_.ProcessName
    })
}

$FlExecutable = Resolve-FlStudioExecutable $Executable
$Running = @(Get-RunningFlStudio)
if ($Running.Count -gt 0 -and -not $DryRun) {
    $Ids = ($Running | ForEach-Object { $_.Id }) -join ", "
    throw "FL Studio is already running (PID $Ids). Quit it manually first: FL_BRIDGE_ENABLE_WRITES is read at process and script startup. Postfader will not kill or restart it."
}

$WriteMode = if ($EnableWrites) { "1" } else { $null }
if ($DryRun) {
    [ordered]@{
        dry_run = $true
        executable = $FlExecutable
        arguments = @()
        child_write_mode = if ($EnableWrites) { "enabled" } else { "read-only" }
        child_environment = [ordered]@{
            FL_BRIDGE_ENABLE_WRITES = if ($EnableWrites) { "1" } else { $null }
        }
        persistent_environment_changes = $false
        running_fl_studio_processes = @($Running | ForEach-Object { $_.Id })
        would_refuse_real_launch_while_running = ($Running.Count -gt 0)
        would_kill_or_restart_existing_process = $false
    } | ConvertTo-Json -Depth 4
    exit 0
}

$HadWriteVariable = Test-Path Env:\FL_BRIDGE_ENABLE_WRITES
$PreviousWriteValue = $env:FL_BRIDGE_ENABLE_WRITES
try {
    if ($EnableWrites) {
        $env:FL_BRIDGE_ENABLE_WRITES = "1"
    }
    else {
        Remove-Item Env:\FL_BRIDGE_ENABLE_WRITES -ErrorAction SilentlyContinue
    }
    $Process = Start-Process -FilePath $FlExecutable -PassThru
}
finally {
    if ($HadWriteVariable) {
        $env:FL_BRIDGE_ENABLE_WRITES = $PreviousWriteValue
    }
    else {
        Remove-Item Env:\FL_BRIDGE_ENABLE_WRITES -ErrorAction SilentlyContinue
    }
}

Write-Host "Started FL Studio PID $($Process.Id) in $(if ($EnableWrites) { 'explicit write-test' } else { 'read-only' }) mode."
