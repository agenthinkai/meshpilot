# MeshPilot Windows Installer
# Prompts for an install folder and admin credentials, clones the repo, configures
# .env, and starts the full stack via Docker Compose.

$ErrorActionPreference = "Stop"

function New-RandomHex64 {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Read-PlainText([System.Security.SecureString]$secure) {
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

Write-Host "===== MeshPilot Installer =====" -ForegroundColor Cyan
Write-Host ""

# -- 1. Install folder --------------------------------------------------------
$defaultDir = Join-Path $HOME "meshpilot"
$installDir = Read-Host "Install folder [default: $defaultDir]"
if ([string]::IsNullOrWhiteSpace($installDir)) { $installDir = $defaultDir }
$installDir = [System.IO.Path]::GetFullPath($installDir)

# -- 2. Admin credentials ------------------------------------------------------
$adminEmail = ""
while ([string]::IsNullOrWhiteSpace($adminEmail)) {
    $adminEmail = Read-Host "Admin email (this logs you into the dashboard)"
}

$adminPassword = ""
while ($adminPassword.Length -lt 8) {
    $secure = Read-Host "Admin password (min 8 characters)" -AsSecureString
    $adminPassword = Read-PlainText $secure
    if ($adminPassword.Length -lt 8) {
        Write-Host "Password must be at least 8 characters." -ForegroundColor Yellow
    }
}

$portText = Read-Host "Local port [default: 8100]"
if ([string]::IsNullOrWhiteSpace($portText)) { $portText = "8100" }
$port = 0
while (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    $portText = Read-Host "Please enter a valid port number (1-65535)"
}

# -- 3. Git check ----------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git not found. Installing Git for Windows..."
    winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
    $gitCmdPath = "C:\Program Files\Git\cmd"
    if (Test-Path $gitCmdPath) { $env:PATH = "$gitCmdPath;$env:PATH" }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "Git installation did not complete. Please install Git manually from https://git-scm.com and run this installer again." -ForegroundColor Red
        exit 1
    }
    Write-Host "Git installed."
}

# -- 4. Docker check ------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Docker Desktop..."
    winget install Docker.DockerDesktop --silent --accept-package-agreements --accept-source-agreements
    Write-Host "Docker installed. Restart your computer and run this installer again."
    exit
}

# -- 5. Clone -------------------------------------------------------------------
$parent = Split-Path $installDir -Parent
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

if (-not (Test-Path $installDir)) {
    git clone https://github.com/agenthinkai/meshpilot.git $installDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git clone failed. Aborting." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Folder already exists: $installDir - skipping clone." -ForegroundColor Yellow
}

Set-Location $installDir

# -- 6. Configure .env ----------------------------------------------------------
Copy-Item .env.example .env -Force

$secretKey = New-RandomHex64
$envContent = Get-Content .env
$envContent = $envContent -replace '^SECRET_KEY=.*', "SECRET_KEY=$secretKey"
$envContent = $envContent -replace '^ADMIN_EMAIL=.*', "ADMIN_EMAIL=$adminEmail"
$envContent = $envContent -replace '^ADMIN_PASSWORD=.*', "ADMIN_PASSWORD=$adminPassword"
$envContent | Set-Content .env

# Docker Desktop on Windows needs a real host folder for the models bind mount -
# the repo's default (/opt/meshpilot/models) only exists on Linux deploy hosts.
$modelDir = Join-Path $installDir "models"
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null
$modelDirDocker = $modelDir -replace '\\', '/'
Add-Content .env ""
Add-Content .env "MODEL_DIR=$modelDirDocker"
Add-Content .env "MESHPILOT_PORT=$port"

# -- 7. Start ---------------------------------------------------------------
Write-Host ""
Write-Host "Building and starting MeshPilot (this can take several minutes on first run)..." -ForegroundColor Cyan
docker compose up -d --build

Write-Host ""
Write-Host "===== MeshPilot is running at http://localhost:$port =====" -ForegroundColor Green
Write-Host "Log in with: $adminEmail"
