# Install and start a per-user Windows Task Scheduler task for codex-api-service.
# The task runs at user logon and does not require administrator privileges.

$ErrorActionPreference = "Stop"

# Resolve project paths relative to this script so the task can be installed from the repo root.
$TaskName = "CodexApiService"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$RunnerPath = Join-Path $ScriptDir "run_service.ps1"

# Build a PowerShell action that launches the project runner with local execution policy bypass.
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerPath`"" `
    -WorkingDirectory $ProjectRoot

# Run the service whenever the current user logs in.
$Trigger = New-ScheduledTaskTrigger -AtLogOn

# Keep the task resilient on laptops and restart it if the process exits.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# Register or replace the task, then start it immediately.
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Local Codex OpenAI-compatible API service" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

# Output useful URLs and log paths for the user.
Write-Host "Installed and started $TaskName"
Write-Host "Console: http://127.0.0.1:1219/ui"
Write-Host "API base: http://127.0.0.1:1219/v1"
Write-Host "Health check: curl http://127.0.0.1:1219/health"
Write-Host "Logs: logs\windows.out.log and logs\windows.err.log"
