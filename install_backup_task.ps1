$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$backupScript = Join-Path $projectRoot "database_backup.py"
$taskName = "CampusShare Daily Database Backup"
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $pythonPath)) {
    throw "Project Python environment not found: $pythonPath"
}

$startTime = (Get-Date).Date.AddHours(2)
if ($startTime -le (Get-Date)) {
    $startTime = $startTime.AddDays(1)
}

$startBoundary = $startTime.ToString("s")
$escapedPythonPath = [System.Security.SecurityElement]::Escape($pythonPath)
$escapedBackupScript = [System.Security.SecurityElement]::Escape($backupScript)
$escapedProjectRoot = [System.Security.SecurityElement]::Escape($projectRoot)
$escapedTaskUser = [System.Security.SecurityElement]::Escape($taskUser)
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Creates a daily SQLite backup for CampusShare.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedTaskUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escapedPythonPath</Command>
      <Arguments>"$escapedBackupScript"</Arguments>
      <WorkingDirectory>$escapedProjectRoot</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = Join-Path $env:TEMP "campusshare-backup-task.xml"
try {
    [System.IO.File]::WriteAllText(
        $xmlPath,
        $taskXml,
        [System.Text.Encoding]::Unicode
    )
    $taskOutput = & schtasks.exe /Create /TN $taskName /XML $xmlPath /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the scheduled backup task: $taskOutput"
    }
}
finally {
    if (Test-Path $xmlPath) {
        Remove-Item -LiteralPath $xmlPath -Force
    }
}

Write-Output "Daily CampusShare backup task installed for 2:00 AM."
Write-Output $taskOutput
