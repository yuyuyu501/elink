param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -LiteralPath $PlanPath
$log = Join-Path $workspace 'update.log'
$movedOld = $false
$installedNew = $false
$newProcess = $null
$parentExited = $false

function Write-UpdateLog([string]$Message) {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}

function Start-Elink([string]$Directory) {
    # Start-Process resolves brackets as wildcard paths on Windows PowerShell 5.1.
    # ProcessStartInfo passes the executable and working directory literally.
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Join-Path $Directory 'Elink.exe'
    $start.WorkingDirectory = $Directory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    return [Diagnostics.Process]::Start($start)
}

try {
    $plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $workspace = [IO.Path]::GetFullPath($workspace).TrimEnd('\')
    $target = [IO.Path]::GetFullPath($plan.target).TrimEnd('\')
    $payload = Join-Path $workspace 'payload\Elink'
    $backup = Join-Path $workspace 'previous'
    $failed = Join-Path $workspace 'failed'
    $ready = Join-Path $workspace 'app-ready'
    if ($workspace -ne [IO.Path]::GetFullPath($plan.workspace).TrimEnd('\') -or
        (Split-Path -Parent $workspace) -ne (Split-Path -Parent $target) -or
        (Split-Path -Leaf $workspace) -notlike '.Elink-update-*' -or
        $target -eq $workspace) { throw 'Invalid update paths' }
    foreach ($directory in @($target, $workspace, $payload)) {
        $item = Get-Item -LiteralPath $directory
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Update directories cannot be links'
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $payload 'Elink.exe') -PathType Leaf) -or
        (Test-Path -LiteralPath $backup) -or (Test-Path -LiteralPath $failed)) {
        throw 'Invalid update payload or backup'
    }
    $parentProcess = [Diagnostics.Process]::GetProcessById([int]$plan.parent_pid)
    $null = $parentProcess.Handle # Hold the original process handle, not a reused PID.
    [IO.File]::WriteAllText((Join-Path $workspace 'helper-ready'), 'ready')
    if (-not $parentProcess.WaitForExit(60000)) { throw 'Elink did not exit; update abandoned' }
    if (-not (Test-Path -LiteralPath (Join-Path $workspace 'armed'))) { throw 'Update was not armed' }
    $parentExited = $true
    Write-UpdateLog 'Replacing portable application'
    # These are checked, absolute sibling paths. Never delete the active application.
    Move-Item -LiteralPath $target -Destination $backup
    $movedOld = $true
    Move-Item -LiteralPath $payload -Destination $target
    $installedNew = $true
    $env:ELINK_DATA_DIR = $plan.data_root
    $env:ELINK_UPDATE_READY = $ready
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    $newProcess = Start-Elink $target
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 200
        $newProcess.Refresh()
        if ($newProcess.HasExited) { throw 'New application exited before startup confirmation' }
        if (Test-Path -LiteralPath $ready) {
            Write-UpdateLog "Update successful; previous application retained at $backup"
            exit 0
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'New application did not confirm startup'
} catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
    try {
        if ($null -ne $newProcess -and -not $newProcess.HasExited) {
            $newProcess.Kill()
            $newProcess.WaitForExit()
        }
        if ($installedNew) { Move-Item -LiteralPath $target -Destination $failed }
        if ($movedOld) {
            Move-Item -LiteralPath $backup -Destination $target
            Write-UpdateLog 'Previous application restored'
        }
        if ($parentExited) {
            Remove-Item Env:ELINK_UPDATE_READY -ErrorAction SilentlyContinue
            $env:ELINK_DATA_DIR = $plan.data_root
            $env:ELINK_UPDATE_FAILURE = $log
            $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
            $null = Start-Elink $target
        }
    } catch {
        Write-UpdateLog "Rollback or restart could not finish: $($_.Exception.Message). Check $target and $backup"
    }
    exit 1
}
