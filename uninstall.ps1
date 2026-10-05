<#
    uninstall.ps1 -- remove the scheduled task registered by install.ps1.

    ASCII-only on purpose (see the note in install.ps1).

    Also cleans up the legacy <prefix>-Main / <prefix>-Poll pair, which older
    versions of install.ps1 registered on a fixed clock-time schedule.

    Example:
        powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#>
[CmdletBinding()]
param(
    [string] $TaskPrefix = "WB-SignIn"
)

$ErrorActionPreference = "Stop"

# The current single task first, then the legacy pair.
$names = @("$TaskPrefix", "$TaskPrefix-Main", "$TaskPrefix-Poll")
$removed = 0

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed  : $name"
        $removed++
    }
    else {
        Write-Host "Not found: $name"
    }
}

Write-Host ""
Write-Host "Done. $removed task(s) removed."
Write-Host "Logs, state.json and config.json were left in place on purpose."
Write-Host "Nothing personal was deleted -- remove those files yourself if you want to."
