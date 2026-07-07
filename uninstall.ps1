#Requires -Version 5.1
<#
.SYNOPSIS
    Uninstall InterviewMate — stops the app, removes the scheduled task, and
    optionally removes the venv and local data (config, state).
.PARAMETER InstallDir
    Repo / install root. Defaults to ~/.interview.
#>
param(
    [string]$InstallDir = "$env:USERPROFILE\.interview"
)

function Ok($msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

Write-Host "`n  InterviewMate Uninstaller`n" -ForegroundColor Cyan

# 1. Remove the scheduled task
$tasks = @('InterviewMate')
foreach ($t in $tasks) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask       -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
        Ok "Removed task: $t"
    }
}

# 2. Kill any running InterviewMate processes under this install dir
foreach ($p in @('pythonw', 'python')) {
    Get-Process -Name $p -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($InstallDir, [System.StringComparison]::OrdinalIgnoreCase)
    } | ForEach-Object {
        try { $_ | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
    }
}
Ok "Stopped any running InterviewMate processes under $InstallDir"

# 3. venv
$venv = Join-Path $InstallDir ".venv"
if (Test-Path $venv) {
    $confirm = Read-Host "  Delete the virtualenv at $venv (deps, ~1 GB)? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $venv -ErrorAction SilentlyContinue
        Ok "Removed $venv"
    }
}

# 4. Local config + state (settings, window geometry) — keeps your API keys!
$dataFiles = @(
    (Join-Path $InstallDir "config.yaml"),
    (Join-Path $InstallDir "overlay_state.json"),
    (Join-Path $InstallDir ".env")
)
$present = $dataFiles | Where-Object { Test-Path $_ }
if ($present) {
    $confirm = Read-Host "  Delete local config/state (config.yaml, overlay_state.json, .env — includes your API keys)? (y/N)"
    if ($confirm -eq 'y') {
        foreach ($f in $present) { Remove-Item -Force $f -ErrorAction SilentlyContinue }
        Ok "Removed local config/state"
    } else {
        Warn "Kept config/state — re-run setup.ps1 anytime to reinstall."
    }
}

# 5. Cached Whisper model (shared HF cache — only offer if present)
$hub = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
$whisper = Join-Path $hub "models--Systran--faster-whisper-large-v3"
if (Test-Path $whisper) {
    $confirm = Read-Host "  Delete the cached Whisper model at $whisper (~2.9 GB)? (y/N)"
    if ($confirm -eq 'y') {
        Remove-Item -Recurse -Force $whisper -ErrorAction SilentlyContinue
        Ok "Removed cached model"
    }
}

Write-Host "`n  Uninstall complete. (The repo files themselves were left in place.)`n" -ForegroundColor Green
