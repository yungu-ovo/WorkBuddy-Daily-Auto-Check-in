<#
    install.ps1 -- register the Windows scheduled tasks that drive wb_signin.py.

    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads .ps1
    files as ANSI when there is no BOM, so non-ASCII text in here could be garbled
    on some systems. All user-facing Chinese messages come from wb_signin.py.

    Two tasks are registered:

      <prefix>-Main   once a day, at $MainTime
                      query the check-in status, claim only when not yet claimed today

      <prefix>-Poll   a few times a day, at $PollTimes
                      cheap re-check; claims only when today is still unsigned, so a
                      powered-off / sleeping machine still gets another chance today.
                      The claim endpoint is idempotent, so this can never double-claim.

    Both tasks run through pythonw.exe (no console window), and both are set to
    "run as soon as possible after a missed start" so the machine catches up on the
    next boot instead of silently skipping the day.

    Examples:
        powershell -ExecutionPolicy Bypass -File .\install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -MainTime "08:30"
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Pythonw "D:\miniconda\pythonw.exe"
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoWake
#>
[CmdletBinding()]
param(
    [string]   $Pythonw    = "",
    [string]   $MainTime   = "09:10",
    [string[]] $PollTimes  = @("12:00", "15:00", "18:00", "21:00"),
    [string]   $TaskPrefix = "WB-SignIn",
    [switch]   $NoWake
)

$ErrorActionPreference = "Stop"

$ScriptDir    = $PSScriptRoot
$SigninScript = Join-Path $ScriptDir "wb_signin.py"

if (-not (Test-Path -LiteralPath $SigninScript)) {
    throw "wb_signin.py was not found next to install.ps1 (looked in: $SigninScript)"
}


function Resolve-PythonwPath {
    param([string] $Explicit)

    $candidates = New-Object System.Collections.Generic.List[string]

    if ($Explicit) {
        $candidates.Add($Explicit)
    }
    elseif ($env:WORKBUDDY_PYTHONW) {
        $candidates.Add($env:WORKBUDDY_PYTHONW)
    }
    else {
        $found = @()
        try { $found = @(where.exe pythonw 2>$null) } catch { $found = @() }

        # Prefer a Python runtime that is NOT bundled with the WorkBuddy desktop app.
        # The bundled one lives in a version-numbered folder and may be moved or
        # removed when WorkBuddy updates, which would silently break the task.
        $stable  = @($found | Where-Object { $_ -and ($_ -notmatch '\.workbuddy\\binaries\\python') })
        $bundled = @($found | Where-Object { $_ -and ($_     -match '\.workbuddy\\binaries\\python') })
        foreach ($p in $stable)  { $candidates.Add($p) }
        foreach ($p in $bundled) { $candidates.Add($p) }

        # Last resort: pythonw.exe sitting next to whatever "python" resolves to.
        try {
            foreach ($p in @(where.exe python 2>$null)) {
                if ($p) {
                    $candidates.Add((Join-Path (Split-Path -Parent $p) "pythonw.exe"))
                }
            }
        }
        catch { }
    }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }

    throw @'
Could not locate pythonw.exe automatically.

Re-run with an explicit path, for example:
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Pythonw "C:\Some\Python\pythonw.exe"

Any Python 3 pythonw.exe works: wb_signin.py uses the standard library only.
'@
}


$PythonwPath = Resolve-PythonwPath -Explicit $Pythonw

$commonSettings = @{
    StartWhenAvailable         = $true
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    MultipleInstances          = "IgnoreNew"
}
if (-not $NoWake) { $commonSettings["WakeToRun"] = $true }


function New-SigninTask {
    param(
        [string] $Name,
        [string] $Source,
        [int]    $TimeLimitMinutes,
        [object] $Triggers,
        [string] $Description
    )

    $action = New-ScheduledTaskAction `
        -Execute $PythonwPath `
        -Argument "`"$SigninScript`" auto --quiet --source $Source" `
        -WorkingDirectory $ScriptDir

    $settings = New-ScheduledTaskSettingsSet @commonSettings `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes)

    Register-ScheduledTask `
        -TaskName    $Name `
        -Action      $action `
        -Trigger     $Triggers `
        -Settings    $settings `
        -Description $Description `
        -Force | Out-Null
}


Write-Host "Script     : $SigninScript"
Write-Host "Interpreter: $PythonwPath"
Write-Host "Working dir: $ScriptDir"
Write-Host ""

$mainName = "$TaskPrefix-Main"
$pollName = "$TaskPrefix-Poll"

$mainTimeSpan = [datetime]::ParseExact($MainTime, "HH:mm", $null)
$mainTask = New-SigninTask `
    -Name $mainName `
    -Source "main" `
    -TimeLimitMinutes 10 `
    -Triggers (New-ScheduledTaskTrigger -Daily -At $mainTimeSpan) `
    -Description "WorkBuddy daily check-in (main run). Idempotent: skips when already claimed today."

# One task, several trigger times -- deliberately not one task per time slot.
$pollTriggers = @()
foreach ($t in $PollTimes) {
    $pollTriggers += New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($t, "HH:mm", $null))
}

$pollTask = New-SigninTask `
    -Name $pollName `
    -Source "poll" `
    -TimeLimitMinutes 5 `
    -Triggers $pollTriggers `
    -Description "WorkBuddy check-in fallback. Only claims when today is still unsigned."

Write-Host "Registered tasks:"
foreach ($n in @($mainName, $pollName)) {
    $task = Get-ScheduledTask -TaskName $n
    $info = Get-ScheduledTaskInfo -TaskName $n
    Write-Host ("  {0,-22} state={1,-8} nextRun={2}" -f $n, $task.State, $info.NextRunTime)
}

Write-Host ""
Write-Host "Verify with:"
Write-Host "  schtasks /query /tn `"$mainName`" /fo LIST /v"
Write-Host "  powershell -Command `"Get-Content '$ScriptDir\signin.log' -Tail 5`""
Write-Host ""
Write-Host "Remove with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\$((Split-Path -Leaf $PSCommandPath) -replace 'install','uninstall')"
