param(
    [switch]$ServicesOnly,
    [string]$Model,
    [string]$KeepAlive = '30m',
    [string]$AppDir = (Join-Path $PSScriptRoot 'app'),
    [string]$RuntimeDir = $env:RST_DOTNET_ROOT,
    [string]$OcrPython = $env:RST_OCR_PYTHON,
    [string]$ProxyPython = $env:RST_PROXY_PYTHON
)

$ErrorActionPreference = 'Stop'
$appDir = [IO.Path]::GetFullPath($AppDir)
if (-not $RuntimeDir) {
    $dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
    if (-not $dotnet) { throw 'Install .NET 9 or pass -RuntimeDir.' }
    $RuntimeDir = Split-Path -Parent $dotnet.Source
}
$runtimeDir = [IO.Path]::GetFullPath($RuntimeDir)
if (-not $OcrPython) {
    $OcrPython = Join-Path $PSScriptRoot 'gpu_ocr\python\python.exe'
    if (-not (Test-Path -LiteralPath $OcrPython)) { $OcrPython = Join-Path $PSScriptRoot 'gpu_ocr\.venv\Scripts\python.exe' }
}
$python = [IO.Path]::GetFullPath($OcrPython)
if (-not $ProxyPython) { $ProxyPython = $python }
$proxyPython = [IO.Path]::GetFullPath($ProxyPython)
$proxyScript = Join-Path $PSScriptRoot 'ollama_proxy.py'
$rstExe = Join-Path $appDir 'rst.exe'
$logDir = Join-Path $PSScriptRoot 'gpu_ocr\logs'
$upstream = 'http://127.0.0.1:11438'
$proxyUrl = 'http://127.0.0.1:11434'
$mutex = [Threading.Mutex]::new($false, 'Local\RST_GPU_Startup_v1')
$locked = $false
$logStarted = $false

function Get-Api([string]$Uri) {
    try { return Invoke-RestMethod -Uri $Uri -TimeoutSec 3 -ErrorAction Stop }
    catch { return $null }
}

function Get-Listener([int]$Port) {
    return Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Wait-Api([string]$Uri, [string]$Property, [int]$Seconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $response = Get-Api $Uri
        if ($null -ne $response -and $null -ne $response.PSObject.Properties[$Property]) { return $response }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Service did not become ready: $Uri. Check logs in $logDir"
}

try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { Write-Host 'RST startup is already in progress.'; exit 0 }
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Start-Transcript -Path (Join-Path $logDir 'startup.log') -Append | Out-Null
    $logStarted = $true
    foreach ($file in @($python, $proxyPython, $proxyScript, $rstExe, (Join-Path $runtimeDir 'dotnet.exe'))) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file missing: $file" }
    }
    if (-not $Model) {
        $line = $null
        $configPath = Join-Path $appDir 'config.txt'
        if (Test-Path -LiteralPath $configPath -PathType Leaf) {
            $line = Get-Content -LiteralPath $configPath -Encoding UTF8 |
                Where-Object { $_ -like 'ollama_model|*' } | Select-Object -First 1
        }
        if ($line) { $Model = $line.Split('|')[1] }
        if (-not $Model) { $Model = 'index-translate-2b' }
    }

    Write-Host '[1/4] Checking Ollama on port 11438...'
    $tags = Get-Api "$upstream/api/tags"
    if ($null -eq $tags -or $null -eq $tags.PSObject.Properties['models']) {
        if (Get-Listener 11438) { throw 'Port 11438 is occupied but is not a healthy Ollama server.' }
        $ollama = (Get-Command ollama.exe -ErrorAction SilentlyContinue).Source
        if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
        if (-not (Test-Path -LiteralPath $ollama)) { throw 'Ollama is not installed.' }
        # Environment is set only for the child; no persistent system changes.
        $savedHost = $env:OLLAMA_HOST
        try {
            $env:OLLAMA_HOST = '127.0.0.1:11438'
            Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $logDir 'ollama.out.log') `
                -RedirectStandardError (Join-Path $logDir 'ollama.err.log') | Out-Null
        } finally { $env:OLLAMA_HOST = $savedHost }
        $tags = Wait-Api "$upstream/api/tags" 'models'
    }
    $canonicalModel = $Model
    if ($canonicalModel -notmatch ':') { $canonicalModel += ':latest' }
    if (-not @($tags.models | Where-Object { $_.name -eq $Model -or $_.name -eq $canonicalModel }).Count) {
        throw "Model is not installed on port 11438: $Model"
    }

    Write-Host "[2/4] Loading $Model (keep_alive=$KeepAlive)..."
    $body = @{ model = $Model; prompt = ''; stream = $false; keep_alive = $KeepAlive } | ConvertTo-Json
    $loaded = Invoke-RestMethod -Uri "$upstream/api/generate" -Method Post -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 180
    if ($loaded.error) { throw $loaded.error }
    $running = Get-Api "$upstream/api/ps"
    if (-not @($running.models | Where-Object { $_.name -eq $Model -or $_.name -eq $canonicalModel }).Count) {
        throw 'Ollama answered the load request but the model is not resident.'
    }
    Write-Host "Model is loaded: $canonicalModel"

    Write-Host '[3/4] Checking RST translation proxy on port 11434...'
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $sourceHash = [BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($proxyScript))).Replace('-', '').ToLowerInvariant()
    } finally { $hasher.Dispose() }
    $health = Get-Api "$proxyUrl/rst/health"
    if ($null -eq $health -or $health.service -ne 'rst-ollama-proxy' -or $health.source_sha256 -ne $sourceHash) {
        $listener = Get-Listener 11434
        if ($listener) {
            # Only restart the process running this exact RST proxy script.
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if ($owner.Name -notmatch '^python(w)?\.exe$' -or
                $owner.CommandLine -notmatch [regex]::Escape($proxyScript)) {
                throw "Port 11434 belongs to another application (PID $($listener.OwningProcess))."
            }
            Write-Host "Updating existing RST proxy (PID $($owner.ProcessId))..."
            $oldProxy = Get-Process -Id $owner.ProcessId
            Stop-Process -Id $oldProxy.Id
            $oldProxy.WaitForExit()
            $oldProxy.Dispose()
        }
        Start-Process -FilePath $proxyPython -ArgumentList @('-u', ('"' + $proxyScript + '"')) `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logDir 'proxy.out.log') `
            -RedirectStandardError (Join-Path $logDir 'proxy.err.log') | Out-Null
        $health = Wait-Api "$proxyUrl/rst/health" 'source_sha256'
        if ($health.source_sha256 -ne $sourceHash) { throw 'Proxy source version mismatch.' }
    }

    if ($ServicesOnly) { Write-Host 'Services are ready. RST launch skipped by -ServicesOnly.'; exit 0 }
    Write-Host '[4/4] Starting RST GPU test executable...'
    $existing = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'rst.exe' -and $_.ExecutablePath -eq $rstExe) -or
        ($_.Name -eq 'dotnet.exe' -and $_.CommandLine -match [regex]::Escape((Join-Path $appDir 'rst.dll')))
    } | Select-Object -First 1
    if ($existing) { Write-Host "RST is already running (PID $($existing.ProcessId))."; exit 0 }
    $savedRoot = $env:DOTNET_ROOT
    $savedRootX64 = $env:DOTNET_ROOT_X64
    $savedOcrPython = $env:RST_OCR_PYTHON
    $savedOcrScript = $env:RST_OCR_SCRIPT
    try {
        $env:DOTNET_ROOT = $runtimeDir
        $env:DOTNET_ROOT_X64 = $runtimeDir
        $env:RST_OCR_PYTHON = $python
        $env:RST_OCR_SCRIPT = Join-Path $PSScriptRoot 'gpu_ocr\backend.py'
        $rst = Start-Process -FilePath $rstExe -WorkingDirectory $appDir -PassThru -WindowStyle Normal
    } finally {
        $env:DOTNET_ROOT = $savedRoot
        $env:DOTNET_ROOT_X64 = $savedRootX64
        $env:RST_OCR_PYTHON = $savedOcrPython
        $env:RST_OCR_SCRIPT = $savedOcrScript
    }
    # A native .NET error dialog also keeps rst.exe alive; require CoreCLR to load.
    $startupTimer = [Diagnostics.Stopwatch]::StartNew()
    $runtimeReady = $false
    do {
        Start-Sleep -Milliseconds 250
        $rst.Refresh()
        if ($rst.HasExited) { throw "RST exited during startup (exit code $($rst.ExitCode))." }
        $runtimeReady = @($rst.Modules | Where-Object { $_.ModuleName -eq 'coreclr.dll' }).Count -gt 0
    } while (-not $runtimeReady -and $startupTimer.Elapsed.TotalSeconds -lt 20)
    if (-not $runtimeReady) { throw 'RST could not load the .NET runtime. Check the application window.' }
    Write-Host "Ready. RST PID $($rst.Id)."
} catch {
    Write-Host "STARTUP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Logs: $logDir"
    exit 1
} finally {
    if ($logStarted) { Stop-Transcript | Out-Null }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
