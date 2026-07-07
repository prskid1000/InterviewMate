#Requires -Version 5.1
<#
.SYNOPSIS
    InterviewMate Setup — real-time interview assistant overlay for Windows.
.DESCRIPTION
    Creates a single venv (.venv), installs the Python deps
    (PySide6 + PyAudioWPatch + faster-whisper + CUDA 12 runtime + fastapi + …),
    pre-downloads the local Whisper STT model (large-v3), seeds a .env
    template, and registers a scheduled task `InterviewMate` that auto-starts
    at logon (hidden, via pythonw).

    Transcription is 100% local (faster-whisper, no API key). It runs large-v3
    on an NVIDIA GPU (CUDA, float16) for large-v3 accuracy at ~13x realtime, and
    falls back to CPU int8 automatically if no GPU is present (functional but
    slow for large-v3 — set stt.model=small.en in config.yaml for CPU-only PCs).
    Only the AI answer needs a provider key (Gemini / Groq), which you add in
    the app's Settings -> AI Model, or in .env (GEMINI_API_KEY=...).
.PARAMETER InstallDir
    Repo / install root. Defaults to ~/.interview. Must contain copilot/.
.PARAMETER PreloadModel
    Download the Whisper large-v3 model now (~2.9 GB) instead of lazily on the
    first transcription. Default $true.
.PARAMETER RegisterTask
    Register + start the `InterviewMate` logon task. Default $true.
#>
param(
    [string]$InstallDir   = "$env:USERPROFILE\.interview",
    [bool]  $PreloadModel = $true,
    [bool]  $RegisterTask = $true
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [FAIL] $msg" -ForegroundColor Red; exit 1 }

Write-Host @"

  InterviewMate Setup
  Real-time interview assistant for Windows (Python / PySide6)
  ============================================================

"@ -ForegroundColor Magenta

# ─── Prerequisites ──────────────────────────────────────────────────

Step "Checking prerequisites"

# Find a working Python 3.10+
$pythonExe = $null
$candidates = @()
foreach ($name in @("python3.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
}
$pyenvRoot = "$env:USERPROFILE\.pyenv\pyenv-win\versions"
if (Test-Path $pyenvRoot) {
    Get-ChildItem $pyenvRoot -Directory | Sort-Object Name -Descending | ForEach-Object {
        $p = Join-Path $_.FullName "python.exe"
        if (Test-Path $p) { $candidates += $p }
    }
}
foreach ($p in @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
)) { if (Test-Path $p) { $candidates += $p } }

foreach ($c in $candidates) {
    try {
        $ver = & $c --version 2>&1
        if ($ver -match 'Python 3\.(1[0-9]|[2-9][0-9])') { $pythonExe = $c; break }
    } catch {}
}
if (-not $pythonExe) { Fail "Python 3.10+ not found. Install from https://python.org" }
Ok "Python: $(& $pythonExe --version 2>&1) ($pythonExe)"

# ─── Install dir ─────────────────────────────────────────────────────

Step "Install directory: $InstallDir"
if (-not (Test-Path (Join-Path $InstallDir "copilot\main.py"))) {
    Fail "copilot\ not found at $InstallDir — run setup.ps1 from the repo root, or pass -InstallDir."
}
Ok "Repo found"

# ─── venv + deps ─────────────────────────────────────────────────────

Step "Installing InterviewMate (single venv — UI + local STT + server)"
$venv      = Join-Path $InstallDir ".venv"
$venvPy    = Join-Path $venv "Scripts\python.exe"
$venvPip   = Join-Path $venv "Scripts\pip.exe"

if (-not (Test-Path $venvPy)) {
    & $pythonExe -m venv $venv
}
& $venvPy -m pip install --upgrade pip --no-cache-dir --quiet 2>&1 | Out-Null

Write-Host "    pip install -r requirements.txt (PySide6, faster-whisper, fastapi, …)..." -ForegroundColor DarkGray
& $venvPip install -r (Join-Path $InstallDir "requirements.txt") --no-cache-dir --quiet 2>&1 | Out-Null

if (-not (Test-Path "$venv\Lib\site-packages\PySide6"))     { Fail "PySide6 install failed (overlay UI)" }
if (-not (Test-Path "$venv\Lib\site-packages\faster_whisper")) { Fail "faster-whisper install failed (local STT)" }
if (-not (Test-Path "$venv\Lib\site-packages\pyaudiowpatch")) { Fail "PyAudioWPatch install failed (WASAPI capture)" }
if (-not (Test-Path "$venv\Lib\site-packages\fastapi"))     { Fail "fastapi install failed (server)" }
if (-not (Test-Path "$venv\Lib\site-packages\nvidia\cublas")) { Warn "nvidia-cublas-cu12 missing — GPU disabled, STT will use CPU (slow for large-v3)" }
Ok "Core deps installed (UI + local Whisper STT + capture + server)"

# ─── GPU check ───────────────────────────────────────────────────────

Step "Checking for an NVIDIA GPU (CUDA)"
$gpu = & $venvPy -c @"
try:
    import ctranslate2 as c
    n = c.get_cuda_device_count()
    print('cuda' if n > 0 else 'cpu')
except Exception:
    print('cpu')
"@ 2>&1
if ("$gpu".Trim() -eq 'cuda') { Ok "CUDA GPU detected — large-v3 will run on the GPU (float16)" }
else { Warn "No CUDA GPU — STT falls back to CPU. large-v3 is slow on CPU; consider stt.model=small.en in config.yaml" }

# ─── Pre-download the local STT model (large-v3) ─────────────────────

if ($PreloadModel) {
    Step "Pre-downloading local Whisper model (large-v3, ~2.9 GB)"
    # Sets a socket timeout so a stalled connection errors + retries instead of
    # hanging forever. Loads on CPU int8 only to cache the weights (fast); the
    # app loads them on GPU float16 at runtime.
    $rc = & $venvPy -c @"
import os, sys, time
os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '30')
from faster_whisper import WhisperModel
for attempt in range(1, 6):
    try:
        WhisperModel('large-v3', device='cpu', compute_type='int8')
        print('ok'); break
    except Exception as e:
        print('retry %d: %s' % (attempt, e), file=sys.stderr); time.sleep(3)
else:
    sys.exit(1)
"@ 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "Whisper large-v3 cached" }
    else { Warn "Model pre-download failed (downloads lazily on first use): $rc" }
}

# ─── Seed .env template (LLM key) ────────────────────────────────────

$envFile = Join-Path $InstallDir ".env"
if (-not (Test-Path $envFile)) {
    @"
# InterviewMate — API key for the AI ANSWER (transcription is local, no key).
# Free Gemini key: https://aistudio.google.com/apikey  (starts with AIza...)
GEMINI_API_KEY=
# Optional Groq key (starts with gsk_...): https://console.groq.com/keys
# GROQ_API_KEY=
"@ | Set-Content -Path $envFile -Encoding UTF8
    Ok "Seeded .env template — add your GEMINI_API_KEY (or use Settings -> AI Model)"
} else {
    Ok ".env already present (left untouched)"
}

# config.yaml is auto-generated on first run — nothing to seed.

# ─── Scheduled task ──────────────────────────────────────────────────

if ($RegisterTask) {
    Step "Registering scheduled task: InterviewMate (auto-start at logon)"
    $pythonwExe = $venvPy -replace 'python\.exe$', 'pythonw.exe'
    if (-not (Test-Path $pythonwExe)) {
        Warn "pythonw.exe not found — falling back to python.exe (a console window will show)"
        $pythonwExe = $venvPy
    }

    $taskName = 'InterviewMate'
    $username = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | ForEach-Object {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }

    $action    = New-ScheduledTaskAction -Execute $pythonwExe -Argument "-m copilot.main" -WorkingDirectory $InstallDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $username
    $settings  = New-ScheduledTaskSettingsSet `
                    -MultipleInstances IgnoreNew `
                    -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries `
                    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
                    -RestartCount 3 `
                    -RestartInterval (New-TimeSpan -Minutes 1) `
                    -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $username -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Ok "Scheduled task registered"

    Step "Starting InterviewMate"
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Ok "Running"
}

# ─── Done ────────────────────────────────────────────────────────────

Write-Host @"

  ============================================================
  Setup complete!
  ============================================================

  InterviewMate is running as a frameless overlay (hidden from screen
  recording / share by default). It auto-starts at every logon.

  - Transcription: local Whisper large-v3 (GPU/CUDA, CPU fallback) — offline, no API key.
  - AI answers: add a Gemini/Groq key in Settings, or .env (GEMINI_API_KEY).

  Hotkeys (global):
    Ctrl+Space    send the current turn to the AI (manual mode)
    Ctrl+Alt+C    open Settings
    Ctrl+Alt+H    show / hide the answer panel
    Ctrl+Alt+B    show / hide the HUD
    Ctrl+Alt+Q    quit

  Config + state live in:
    $InstallDir\config.yaml   (auto-generated; gitignored)
    $InstallDir\overlay_state.json

  Uninstall anytime:  .\uninstall.ps1

"@ -ForegroundColor Green
