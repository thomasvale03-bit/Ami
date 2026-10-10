# Playmaker Jarvis setup: installs dependencies, schedules the daily check,
# adds the dashboard to Windows startup, then opens the one-time TAO sign-in.
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null
$ErrorLog = Join-Path $ProjectDir "logs\jarvis-error.log"
$TaskName = "Playmaker Jarvis TAO Check"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Yellow }

$exitCode = 0
try {
    Write-Host "PLAYMAKER JARVIS SETUP" -ForegroundColor DarkYellow
    Write-Host "Folder: $ProjectDir"
    if ($ProjectDir -match "OneDrive") {
        Write-Host "WARNING: This folder is inside OneDrive. OneDrive syncing can lock the Jarvis Edge profile." -ForegroundColor Red
        Write-Host "         Recommended: move the folder to C:\PlaymakerJarvis and run setup again." -ForegroundColor Red
    }

    Step "Checking Node.js"
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) {
        throw "Node.js is not installed. Install the LTS version from https://nodejs.org, restart the computer, then run Setup-Jarvis.bat again."
    }
    $nodeVersion = (& node.exe --version).Trim()
    if ([int]($nodeVersion.TrimStart("v").Split(".")[0]) -lt 20) {
        throw "Node.js $nodeVersion is too old. Install the current LTS version from https://nodejs.org, then run setup again."
    }
    Write-Host "Node.js $nodeVersion found at $($node.Source)"

    Step "Checking Microsoft Edge"
    $edgePaths = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
    )
    $edge = $edgePaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $edge) { throw "Microsoft Edge was not found. Install it from https://www.microsoft.com/edge and run setup again." }
    Write-Host "Edge found at $edge"

    Step "Installing Jarvis (npm install)"
    & npm.cmd install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit code $LASTEXITCODE). Check your internet connection and run setup again." }

    Step "Scheduling the daily check"
    $config = Get-Content (Join-Path $ProjectDir "config.json") -Raw | ConvertFrom-Json
    $time = [datetime]::ParseExact($config.scheduleTime, "HH:mm", $null)
    $bat = Join-Path $ProjectDir "Run-Scheduled-Check.bat"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $description = "Counts yesterday's TAO validations by event for the Playmaker Jarvis dashboard"
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
            -Description $description -Force | Out-Null
    } catch {
        Write-Host "Retrying task registration for the current user ($($_.Exception.Message))"
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Description $description -Force | Out-Null
    }
    $task = Get-ScheduledTask -TaskName $TaskName
    $next = ($task | Get-ScheduledTaskInfo).NextRunTime
    Write-Host "Scheduled task '$TaskName' created. Next run: $next"

    Step "Adding the dashboard to Windows startup"
    $startup = [Environment]::GetFolderPath("Startup")
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup "Playmaker Jarvis Dashboard.lnk"))
    $shortcut.TargetPath = Join-Path $ProjectDir "Start-Jarvis-Dashboard.bat"
    $shortcut.WorkingDirectory = $ProjectDir
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Host "The dashboard will open automatically when you sign in to Windows."

    Step "Opening the one-time TAO sign-in"
    & node.exe login.js
    if ($LASTEXITCODE -ne 0) { throw "The TAO sign-in helper did not finish. You can rerun it any time with Start-Jarvis-Login.bat." }

    Write-Host ""
    Write-Host "SETUP COMPLETE" -ForegroundColor Green
    Write-Host "Next: double-click Check-Now-Debug.bat to watch the first check."
}
catch {
    $exitCode = 1
    $message = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [ERROR] [setup] $($_.Exception.Message)"
    Add-Content -Path $ErrorLog -Value $message
    Write-Host ""
    Write-Host "SETUP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Saved to $ErrorLog"
}
finally {
    Write-Host ""
    Read-Host "Press ENTER to close this window"
}
exit $exitCode
