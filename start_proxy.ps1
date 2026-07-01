param(
    [switch]$Foreground,
    [switch]$OpenAdmin
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "config.ccswitch-9980.capture.json"
$ScriptPath = Join-Path $Root "capture_proxy.py"
$HealthUrl = "http://127.0.0.1:9980/health"
$AdminUrl = "http://127.0.0.1:9980/admin"

function Import-EnvIfMissing {
    param([string]$Name)

    if ([Environment]::GetEnvironmentVariable($Name, "Process")) {
        return
    }

    $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($userValue) {
        Set-Item -Path "Env:$Name" -Value $userValue
        return
    }

    $machineValue = [Environment]::GetEnvironmentVariable($Name, "Machine")
    if ($machineValue) {
        Set-Item -Path "Env:$Name" -Value $machineValue
    }
}

Import-EnvIfMissing "GUIJILIUDONG_API_KEY"
Import-EnvIfMissing "MODELSCOPE_API_KEY"

Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and $_.CommandLine -match "capture_proxy\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if ($Foreground) {
    Set-Location $Root
    python $ScriptPath --config $ConfigPath
    exit $LASTEXITCODE
}

$process = Start-Process `
    -FilePath "python" `
    -ArgumentList @($ScriptPath, "--config", $ConfigPath) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 1

try {
    Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3 | Out-Null
    Write-Host "vision proxy started: $HealthUrl"
    Write-Host "admin page: $AdminUrl"
    Write-Host "pid: $($process.Id)"
    if ($OpenAdmin) {
        Start-Process $AdminUrl | Out-Null
    }
} catch {
    Write-Host "vision proxy process started, but health check failed: $($_.Exception.Message)"
    Write-Host "pid: $($process.Id)"
    exit 1
}
