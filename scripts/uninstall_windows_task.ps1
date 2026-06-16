# Stop and remove the per-user Windows Task Scheduler task for codex-api-service.

$ErrorActionPreference = "Stop"

# Task name must match install_windows_task.ps1.
$TaskName = "CodexApiService"

# Stop the task if it is currently running.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Output confirmation for scripts and humans.
Write-Host "Uninstalled $TaskName"
