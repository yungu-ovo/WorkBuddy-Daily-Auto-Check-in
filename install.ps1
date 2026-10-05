<#
    install.ps1 -- register the Windows scheduled task that drives wb_signin.py.

    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads .ps1
    files as ANSI when there is no BOM, so non-ASCII text in here could be garbled
    on some systems. All user-facing Chinese messages come from wb_signin.py.

    ---------------------------------------------------------------------------
    Why the trigger strategy changed (2026-10-05)

    The old design used fixed clock times: a "main" task at 09:10 plus a fallback
    task at 12:00 / 15:00 / 18:00 / 21:00. That only works if the machine happens
    to be awake at those exact minutes.

    On a laptop that sleeps, every fixed time can be missed, and Windows'
    "start as soon as possible after a missed start" catch-up proved unreliable:
    it made up a missed 09:10 run on 10-04 (at 10:51), but silently skipped the
    same run on 10-03 and again on 10-05. On 10-05 both the 09:10 run and the
    12:00 fallback were missed, and no points were claimed all day.

    WakeToRun did not help either: this machine's power plan has
    "allow wake timers" set to "important wake timers only" on AC and "disabled"
    on battery, and a third-party scheduled task does not count as important.

    The new design registers ONE task with three triggers:

      1. Heartbeat      -- once, repeating every N minutes, forever.
                           While the machine is awake, the next run is at most
                           N minutes away, independent of when it woke up.
      2. At logon       -- run shortly after signing in.
      3. Session unlock -- run shortly after the screen is unlocked.

    Because the script is idempotent (it queries the status first and only calls
    the claim endpoint when today is still unsigned), the extra runs cost exactly
    one cheap HTTPS request each and can never double-claim.
    ---------------------------------------------------------------------------

    Examples:
        powershell -ExecutionPolicy Bypass -File .\install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -IntervalMinutes 60
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Pythonw "D:\miniconda\pythonw.exe"
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnableWake
#>
[CmdletBinding()]
param(
    [string] $Pythonw         = "",
    [int]    $IntervalMinutes = 30,
    [string] $StartAt         = "08:00",
    [string] $TaskPrefix      = "WB-SignIn",
    [switch] $EnableWake,
    [switch] $KeepOldTasks
)

$ErrorActionPreference = "Stop"

$ScriptDir    = $PSScriptRoot
$SigninScript = Join-Path $ScriptDir "wb_signin.py"

if (-not (Test-Path -LiteralPath $SigninScript)) {
    throw "wb_signin.py was not found next to install.ps1 (looked in: $SigninScript)"
}

if ($IntervalMinutes -lt 15) {
    Write-Warning "IntervalMinutes=$IntervalMinutes is very aggressive. Each run is one HTTPS request; 15 minutes or more is recommended."
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


function Show-WakeTimerAdvice {
    # WakeToRun only works when the active power plan allows wake timers. Reading
    # the setting does not need admin rights, so just report it and let the user
    # decide. powercfg output is localized, so never match on English key names:
    # pull out every 0x.. number and take the last two (AC index, then DC index).
    $raw = ""
    try { $raw = (powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE 2>&1 | Out-String) }
    catch { return }

    $hex = @([regex]::Matches($raw, '0x[0-9a-fA-F]+') | ForEach-Object { $_.Value })
    if ($hex.Count -lt 2) {
        Write-Host "Wake timers  : could not read the RTCWAKE setting (skipped)"
        return
    }

    $ac = [Convert]::ToInt32($hex[$hex.Count - 2], 16)
    $dc = [Convert]::ToInt32($hex[$hex.Count - 1], 16)
    $desc = @{ 0 = "disabled"; 1 = "enabled"; 2 = "important wake timers only" }
    $acText = if ($desc.ContainsKey($ac)) { $desc[$ac] } else { "index $ac" }
    $dcText = if ($desc.ContainsKey($dc)) { $desc[$dc] } else { "index $dc" }

    Write-Host "Wake timers  : AC=$acText, DC=$dcText"

    if ($ac -ne 1 -or $dc -ne 1) {
        Write-Warning @"
Wake timers are not fully enabled, so this task CANNOT wake a sleeping machine.
That is fine -- it will simply run at the next heartbeat, at logon, or on unlock.
If you want it to wake the machine too, run this from an elevated prompt:
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    powercfg /setactive SCHEME_CURRENT
"@
    }
}


$PythonwPath = Resolve-PythonwPath -Explicit $Pythonw

# A plain user account cannot create a trigger that applies to "any user", so the
# logon and unlock triggers must be pinned to this account explicitly.
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$commonSettings = @{
    StartWhenAvailable         = $true
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    MultipleInstances          = "IgnoreNew"
}
if ($EnableWake) { $commonSettings["WakeToRun"] = $true }


function Remove-LegacyTask {
    param([string] $Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed legacy task: $Name"
    }
}


Write-Host "Script       : $SigninScript"
Write-Host "Interpreter  : $PythonwPath"
Write-Host "Working dir  : $ScriptDir"
Write-Host "Task name    : $TaskPrefix"
Write-Host ""

Show-WakeTimerAdvice
Write-Host ""

# --- triggers -------------------------------------------------------------

# 1) Heartbeat: a one-off trigger that repeats forever.
#    Deliberately NO -RepetitionDuration: passing [TimeSpan]::MaxValue serializes
#    to "P99999999DT23H59M59S" and Register-ScheduledTask rejects it outright with
#    "The task XML contains a value which is incorrectly formatted or out of range".
#    Leaving Duration unset is what actually means "repeat indefinitely".
$interval  = New-TimeSpan -Minutes $IntervalMinutes
$startTime = [datetime]::ParseExact($StartAt, "HH:mm", $null)
$startWhen = (Get-Date).Date.Add($startTime.TimeOfDay)

$tHeartbeat = New-ScheduledTaskTrigger -Once -At $startWhen -RepetitionInterval $interval
$tHeartbeat.Repetition.StopAtDurationEnd = $false

# 2) At logon, with a short delay so the network and the desktop client are ready.
#    -User is MANDATORY here, not cosmetic: a logon trigger with no user means
#    "any user", and creating that requires administrator rights. Without it,
#    Register-ScheduledTask fails with "Access is denied" (0x80070005) -- which
#    is exactly what happened the first time this was written.
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$tLogon.Delay = "PT30S"

# 3) On session unlock. New-ScheduledTaskTrigger has no parameter for this, so the
#    trigger has to be built from the CIM class directly.
#    StateChange 8 = TASK_SESSION_UNLOCK (7 would be TASK_SESSION_LOCK).
#    UserId is mandatory for the same reason as above.
$tUnlock = New-CimInstance -ClientOnly `
    -CimClass (Get-CimClass -Namespace ROOT\Microsoft\Windows\TaskScheduler `
                            -ClassName MSFT_TaskSessionStateChangeTrigger) `
    -Property @{ StateChange = 8; UserId = $currentUser; Delay = "PT10S" }

$triggers = @($tHeartbeat, $tLogon, $tUnlock)

# --- action / settings ----------------------------------------------------

$action = New-ScheduledTaskAction `
    -Execute $PythonwPath `
    -Argument "`"$SigninScript`" auto --quiet --source heartbeat" `
    -WorkingDirectory $ScriptDir

$settings = New-ScheduledTaskSettingsSet @commonSettings `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName    $TaskPrefix `
    -Action      $action `
    -Trigger     $triggers `
    -Settings    $settings `
    -Description "WorkBuddy daily check-in (heartbeat). Idempotent: skips when already claimed today." `
    -Force -ErrorAction Stop | Out-Null

# Do not touch the old tasks unless the new one really exists -- otherwise a
# failed registration would leave the machine with no check-in task at all.
if (-not (Get-ScheduledTask -TaskName $TaskPrefix -ErrorAction SilentlyContinue)) {
    throw "Registration reported success but task '$TaskPrefix' is not there. Existing tasks were left untouched."
}

# The old layout used two tasks (<prefix>-Main and <prefix>-Poll) driven by fixed
# clock times. Leaving them behind would keep firing on the broken schedule.
if (-not $KeepOldTasks) {
    Remove-LegacyTask -Name "$TaskPrefix-Main"
    Remove-LegacyTask -Name "$TaskPrefix-Poll"
}

# --- report ---------------------------------------------------------------

$task = Get-ScheduledTask -TaskName $TaskPrefix
$info = Get-ScheduledTaskInfo -TaskName $TaskPrefix

Write-Host "Registered task:"
Write-Host ("  {0,-22} state={1,-8} triggers={2} nextRun={3}" -f $TaskPrefix, $task.State, $task.Triggers.Count, $info.NextRunTime)

$expected = 3
if ($task.Triggers.Count -ne $expected) {
    Write-Warning "Expected $expected triggers but found $($task.Triggers.Count). Re-run this script and check the errors above."
}
Write-Host ""
Write-Host "Triggers:"
Write-Host ("  heartbeat : every {0} min, starting {1}" -f $IntervalMinutes, $startWhen.ToString("yyyy-MM-dd HH:mm"))
Write-Host  "  logon     : at sign-in (+30s)"
Write-Host  "  unlock    : when the screen is unlocked"
if ($EnableWake) { Write-Host "  wake      : WakeToRun is set (only effective if the power plan allows wake timers)" }
Write-Host ""

Write-Host "Verify with:"
Write-Host "  schtasks /query /tn `"$TaskPrefix`" /fo LIST /v"
Write-Host "  Start-ScheduledTask -TaskName `"$TaskPrefix`"      # run it once right now"
Write-Host "  powershell -Command `"Get-Content '$ScriptDir\signin.log' -Tail 5`""
Write-Host ""
Write-Host "Remove with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1"
