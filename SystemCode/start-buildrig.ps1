# One-step (re)start of BuildRig on Windows: API (8000), Pi worker (8090) and web app (5173).
#
#   .\start-buildrig.bat            restart everything and open the web app
#   .\start-buildrig.bat -Stop      stop the three services
#   .\start-buildrig.bat -NoBrowser restart without opening the browser
#
# Each service runs in its own console window so its log stays visible. Running the script
# again closes the old windows first. When .env selects the neo4j_milvus backend the script
# also makes sure Docker Desktop and the Neo4j / Milvus containers are up.
param([switch]$Stop, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '..\.venv\Scripts\python.exe'
$pidFile = Join-Path $root 'runtime\buildrig-services.json'
$services = @(
    @{ Name = 'API';       Port = 8000; Dir = $root;                         Command = "`"$python`" -m uvicorn main:app --port 8000" },
    @{ Name = 'Pi worker'; Port = 8090; Dir = (Join-Path $root 'pi-worker'); Command = 'node --env-file=../.env dist/server.js' },
    @{ Name = 'Web app';   Port = 5173; Dir = (Join-Path $root 'frontend');  Command = 'npm run dev' }
)

# A terminal opened before Docker or Ollama was installed does not have them on PATH yet.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

function Test-Port($port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Port($port, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# Runs a native command silently. Windows PowerShell turns redirected stderr lines into errors,
# so the preference is relaxed for the call; the exit code is left in $LASTEXITCODE.
function Invoke-Quiet([scriptblock]$command) {
    $ErrorActionPreference = 'Continue'
    & $command *> $null
}

function Stop-Services {
    # Windows started by an earlier run of this script.
    if (Test-Path $pidFile) {
        $entries = Get-Content $pidFile -Raw | ConvertFrom-Json   # assigned first: 5.1 emits the array as one object
        foreach ($entry in $entries) {
            $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -eq 'cmd') { Invoke-Quiet { taskkill.exe /PID $entry.pid /T /F } }
        }
        Remove-Item $pidFile -Force
    }
    # Anything else still listening, e.g. services started by hand in a terminal.
    foreach ($service in $services) {
        $owners = Get-NetTCPConnection -LocalPort $service.Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($owner in $owners) {
            $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
            if ($process) {
                Say "  stopping $($service.Name) (port $($service.Port), $($process.ProcessName) pid $owner)"
                Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
            }
        }
    }
    foreach ($service in $services) {
        $deadline = (Get-Date).AddSeconds(15)
        while ((Test-Port $service.Port) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
        if (Test-Port $service.Port) { throw "Port $($service.Port) is still in use; close the program that holds it and try again." }
    }
}

function Start-Infrastructure {
    $backend = 'local'
    $line = Select-String -Path (Join-Path $root '.env') -Pattern '^\s*BUILDRIG_RETRIEVAL_BACKEND\s*=\s*(\S+)' | Select-Object -First 1
    if ($line) { $backend = $line.Matches[0].Groups[1].Value }
    Say "Retrieval backend: $backend"

    if ($backend -eq 'neo4j_milvus') {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is not installed, but .env selects the neo4j_milvus backend.' }
        Invoke-Quiet { docker info }
        if ($LASTEXITCODE -ne 0) {
            $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
            if (-not (Test-Path $desktop)) { throw 'The Docker engine is not running. Start Docker Desktop and run this script again.' }
            Say 'Starting Docker Desktop (this can take a minute)...'
            Start-Process $desktop
            $deadline = (Get-Date).AddSeconds(180)
            do { Start-Sleep -Seconds 3; Invoke-Quiet { docker info } } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
            if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop did not become ready within 3 minutes.' }
        }
        Say 'Starting Neo4j and Milvus containers...'
        Invoke-Quiet { docker compose --project-directory $root up -d neo4j milvus }
        if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed. Run "docker compose up -d neo4j milvus" in SystemCode to see the error.' }
        foreach ($database in @(@{ Name = 'Neo4j'; Port = 7687 }, @{ Name = 'Milvus'; Port = 19530 })) {
            if (-not (Wait-Port $database.Port 120)) { throw "$($database.Name) did not open port $($database.Port)." }
        }
        # The ports open before the databases accept queries.
        $deadline = (Get-Date).AddSeconds(180)
        do {
            $health = & docker compose --project-directory $root ps --format '{{.Health}}' neo4j milvus
            $waiting = @($health | Where-Object { $_ -ne 'healthy' }).Count
            if ($waiting) { Start-Sleep -Seconds 3 }
        } while ($waiting -and (Get-Date) -lt $deadline)
        if ($waiting) { Say '  warning: a database container is not healthy yet; the API may report "degraded" at first.' 'Yellow' }
    }

    # A native Ollama must serve the context length the backend asks for (BUILDRIG_OLLAMA_NUM_CTX): the Pi
    # worker cannot set one per request, and a different server default reloads the model on every switch.
    $context = '8192'
    $line = Select-String -Path (Join-Path $root '.env') -Pattern '^\s*BUILDRIG_OLLAMA_NUM_CTX\s*=\s*(\d+)' | Select-Object -First 1
    if ($line) { $context = $line.Matches[0].Groups[1].Value }
    $native = Get-Command ollama -ErrorAction SilentlyContinue
    $owner = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess
    $servedNatively = $owner -and (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName -like 'ollama*'
    $configured = [Environment]::GetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH', 'User')
    if ($native -and $configured -ne $context -and ($servedNatively -or -not $owner)) {
        Say "Setting Ollama's default context length to $context (was '$configured') and restarting Ollama..."
        [Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH', $context, 'User')
        Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(15)
        while ((Test-Port 11434) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
    }
    if (-not (Test-Port 11434)) {
        if ($native) {
            Say 'Starting Ollama...'
            $env:OLLAMA_CONTEXT_LENGTH = $context
            Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden
            if (-not (Wait-Port 11434 30)) { Say '  warning: Ollama did not start; recommendations will fail until it runs.' 'Yellow' }
        } else {
            Say '  warning: Ollama is not running on port 11434; recommendations will fail until it runs.' 'Yellow'
        }
    }
}

Say 'Stopping running BuildRig services...' 'Cyan'
Stop-Services
if ($Stop) { Say 'Stopped.' 'Green'; return }

if (-not (Test-Path (Join-Path $root '.env'))) { throw 'SystemCode\.env is missing. Copy .env.example (or .env.local.example) to .env first.' }
if (-not (Test-Path $python)) { throw 'The virtual environment is missing. Run: python -m venv ..\.venv, then pip install -r requirements.txt' }
if (-not (Test-Path (Join-Path $root 'frontend\node_modules'))) { throw 'Front-end packages are missing. Run "npm install" in SystemCode\frontend.' }
if (-not (Test-Path (Join-Path $root 'pi-worker\node_modules'))) { throw 'Pi worker packages are missing. Run "npm install" in SystemCode\pi-worker.' }
if (-not (Test-Path (Join-Path $root 'pi-worker\dist\server.js'))) {
    Say 'Building the Pi worker...'
    Push-Location (Join-Path $root 'pi-worker'); & npx tsc -p tsconfig.json; $code = $LASTEXITCODE; Pop-Location
    if ($code -ne 0) { throw 'The Pi worker failed to build.' }
}

Say 'Checking infrastructure...' 'Cyan'
Start-Infrastructure

Say 'Starting services...' 'Cyan'
New-Item -ItemType Directory -Force (Split-Path $pidFile) | Out-Null
$started = @()
foreach ($service in $services) {
    $title = "BuildRig $($service.Name)"
    $line = "/c title $title & $($service.Command) & echo. & echo [$title stopped] & pause"
    $process = Start-Process cmd.exe -ArgumentList $line -WorkingDirectory $service.Dir -PassThru
    $started += @{ name = $service.Name; pid = $process.Id }
}
ConvertTo-Json $started | Set-Content $pidFile -Encoding utf8

$failed = $false
foreach ($service in $services) {
    if (Wait-Port $service.Port 90) { Say "  $($service.Name) is up on port $($service.Port)" 'Green' }
    else { Say "  $($service.Name) did not start; see its window for the error" 'Red'; $failed = $true }
}

try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health' -TimeoutSec 60
    $color = 'Yellow'; if ($health.status -eq 'ok') { $color = 'Green' }
    Say "API health: $($health.status) (retrieval backend: $($health.retrieval_backend))" $color
    if ($health.detail) { Say "  $($health.detail)" $color }
} catch {
    Say "API health check failed: $($_.Exception.Message)" 'Red'; $failed = $true
}

if ($failed) { exit 1 }
Say 'BuildRig is running at http://localhost:5173' 'Green'
if (-not $NoBrowser) { Start-Process 'http://localhost:5173' }
