# MeshPilot Windows Installer - GUI
# Collects an install folder, admin credentials, and a port via a form, then
# clones the repo, configures .env, and starts the stack via Docker Compose in
# the background while streaming progress into the log box.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# -- Form -----------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "MeshPilot Installer"
$form.Size = New-Object System.Drawing.Size(560, 700)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Text = "MeshPilot Installer"
$lblTitle.Font = New-Object System.Drawing.Font("Segoe UI", 14, [System.Drawing.FontStyle]::Bold)
$lblTitle.Location = New-Object System.Drawing.Point(20, 15)
$lblTitle.AutoSize = $true

$lblAdminNote = New-Object System.Windows.Forms.Label
$lblAdminNote.Location = New-Object System.Drawing.Point(20, 46)
$lblAdminNote.Size = New-Object System.Drawing.Size(500, 16)
$lblAdminNote.Font = New-Object System.Drawing.Font("Segoe UI", 8)
$lblAdminNote.ForeColor = [System.Drawing.Color]::DarkOrange
if (-not (Test-IsAdmin)) {
    $lblAdminNote.Text = "Not running as Administrator - only needed if Git/Docker still need installing."
}

$lblPrereq = New-Object System.Windows.Forms.Label
$lblPrereq.Text = "Prerequisites:"
$lblPrereq.Location = New-Object System.Drawing.Point(20, 72)
$lblPrereq.AutoSize = $true

$chkGit = New-Object System.Windows.Forms.CheckBox
$chkGit.Text = "Git"
$chkGit.Location = New-Object System.Drawing.Point(140, 70)
$chkGit.AutoSize = $true
$chkGit.Enabled = $false
$chkGit.Checked = [bool](Get-Command git -ErrorAction SilentlyContinue)

$chkDocker = New-Object System.Windows.Forms.CheckBox
$chkDocker.Text = "Docker"
$chkDocker.Location = New-Object System.Drawing.Point(260, 70)
$chkDocker.AutoSize = $true
$chkDocker.Enabled = $false
$chkDocker.Checked = [bool](Get-Command docker -ErrorAction SilentlyContinue)

$lblFolder = New-Object System.Windows.Forms.Label
$lblFolder.Text = "Install folder:"
$lblFolder.Location = New-Object System.Drawing.Point(20, 112)
$lblFolder.AutoSize = $true

$txtFolder = New-Object System.Windows.Forms.TextBox
$txtFolder.Location = New-Object System.Drawing.Point(20, 134)
$txtFolder.Size = New-Object System.Drawing.Size(400, 25)
$txtFolder.Text = Join-Path $HOME "meshpilot"

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = "Browse..."
$btnBrowse.Location = New-Object System.Drawing.Point(430, 133)
$btnBrowse.Size = New-Object System.Drawing.Size(90, 25)

$lblEmail = New-Object System.Windows.Forms.Label
$lblEmail.Text = "Admin email:"
$lblEmail.Location = New-Object System.Drawing.Point(20, 172)
$lblEmail.AutoSize = $true

$txtEmail = New-Object System.Windows.Forms.TextBox
$txtEmail.Location = New-Object System.Drawing.Point(20, 194)
$txtEmail.Size = New-Object System.Drawing.Size(500, 25)

$lblPass = New-Object System.Windows.Forms.Label
$lblPass.Text = "Admin password (min 8 characters):"
$lblPass.Location = New-Object System.Drawing.Point(20, 232)
$lblPass.AutoSize = $true

$txtPass = New-Object System.Windows.Forms.TextBox
$txtPass.Location = New-Object System.Drawing.Point(20, 254)
$txtPass.Size = New-Object System.Drawing.Size(500, 25)
$txtPass.UseSystemPasswordChar = $true

$lblPass2 = New-Object System.Windows.Forms.Label
$lblPass2.Text = "Confirm password:"
$lblPass2.Location = New-Object System.Drawing.Point(20, 290)
$lblPass2.AutoSize = $true

$txtPass2 = New-Object System.Windows.Forms.TextBox
$txtPass2.Location = New-Object System.Drawing.Point(20, 312)
$txtPass2.Size = New-Object System.Drawing.Size(500, 25)
$txtPass2.UseSystemPasswordChar = $true

$lblPort = New-Object System.Windows.Forms.Label
$lblPort.Text = "Local port (default 8100):"
$lblPort.Location = New-Object System.Drawing.Point(20, 348)
$lblPort.AutoSize = $true

$txtPort = New-Object System.Windows.Forms.TextBox
$txtPort.Location = New-Object System.Drawing.Point(20, 370)
$txtPort.Size = New-Object System.Drawing.Size(100, 25)
$txtPort.Text = "8100"

$lblPortHint = New-Object System.Windows.Forms.Label
$lblPortHint.Text = "MeshPilot will open at http://localhost:<port>"
$lblPortHint.Location = New-Object System.Drawing.Point(130, 374)
$lblPortHint.AutoSize = $true
$lblPortHint.ForeColor = [System.Drawing.Color]::Gray

$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = "Install"
$btnInstall.Location = New-Object System.Drawing.Point(20, 408)
$btnInstall.Size = New-Object System.Drawing.Size(120, 36)
$btnInstall.BackColor = [System.Drawing.Color]::FromArgb(37, 99, 235)
$btnInstall.ForeColor = [System.Drawing.Color]::White
$btnInstall.FlatStyle = "Flat"

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = "Cancel"
$btnCancel.Location = New-Object System.Drawing.Point(150, 408)
$btnCancel.Size = New-Object System.Drawing.Size(100, 36)
$btnCancel.Enabled = $false

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Text = ""
$lblStatus.Location = New-Object System.Drawing.Point(265, 418)
$lblStatus.AutoSize = $true

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Style = "Blocks"
$progress.Minimum = 0
$progress.Maximum = 100
$progress.Value = 0
$progress.Location = New-Object System.Drawing.Point(20, 456)
$progress.Size = New-Object System.Drawing.Size(500, 20)

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Multiline = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.ReadOnly = $true
$txtLog.Location = New-Object System.Drawing.Point(20, 486)
$txtLog.Size = New-Object System.Drawing.Size(500, 150)
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$txtLog.BackColor = [System.Drawing.Color]::FromArgb(30, 30, 30)
$txtLog.ForeColor = [System.Drawing.Color]::FromArgb(220, 220, 220)

$form.Controls.AddRange(@(
    $lblTitle, $lblAdminNote, $lblPrereq, $chkGit, $chkDocker,
    $lblFolder, $txtFolder, $btnBrowse,
    $lblEmail, $txtEmail, $lblPass, $txtPass, $lblPass2, $txtPass2,
    $lblPort, $txtPort, $lblPortHint,
    $btnInstall, $btnCancel, $lblStatus, $progress, $txtLog
))

# -- Helpers ----------------------------------------------------------------------
function Write-Log([string]$text) {
    $txtLog.AppendText("$text`r`n")
    $txtLog.SelectionStart = $txtLog.Text.Length
    $txtLog.ScrollToCaret()
}

function Set-Status([string]$text) {
    $lblStatus.Text = $text
}

function Set-FormEnabled([bool]$enabled) {
    $txtFolder.Enabled = $enabled
    $btnBrowse.Enabled = $enabled
    $txtEmail.Enabled = $enabled
    $txtPass.Enabled = $enabled
    $txtPass2.Enabled = $enabled
    $txtPort.Enabled = $enabled
    $btnInstall.Enabled = $enabled
    $btnCancel.Enabled = -not $enabled
}

$btnBrowse.Add_Click({
    $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
    $fbd.Description = "Choose a parent folder for the meshpilot install"
    if ($fbd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $txtFolder.Text = Join-Path $fbd.SelectedPath "meshpilot"
    }
})

# -- Background install job ------------------------------------------------------
$jobScript = {
    param($installDir, $email, $pass, $port)

    function New-RandomHex64 {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        -join ($bytes | ForEach-Object { $_.ToString("x2") })
    }

    function Test-IsAdmin {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }

    Write-Output "PROGRESS:5"

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (-not (Test-IsAdmin)) {
            throw "Git needs to be installed, which requires Administrator rights. Close this installer, right-click install.bat, choose 'Run as administrator', and try again."
        }
        Write-Output "Git not found. Installing Git for Windows..."
        winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
        $gitCmdPath = "C:\Program Files\Git\cmd"
        if (Test-Path $gitCmdPath) { $env:PATH = "$gitCmdPath;$env:PATH" }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            throw "Git installation did not complete. Please install Git manually from https://git-scm.com and run this installer again."
        }
        Write-Output "Git installed."
    } else {
        Write-Output "Git is already installed."
    }
    Write-Output "GIT_READY"
    Write-Output "PROGRESS:15"

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        if (-not (Test-IsAdmin)) {
            throw "Docker needs to be installed, which requires Administrator rights. Close this installer, right-click install.bat, choose 'Run as administrator', and try again."
        }
        Write-Output "Docker not found. Installing Docker Desktop..."
        winget install Docker.DockerDesktop --silent --accept-package-agreements --accept-source-agreements
        Write-Output "DOCKER_INSTALLED_NEEDS_RESTART"
        return
    }
    Write-Output "Docker is already installed."
    Write-Output "DOCKER_READY"
    Write-Output "PROGRESS:30"

    $parent = Split-Path $installDir -Parent
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    if (-not (Test-Path $installDir)) {
        Write-Output "Cloning repository..."
        git clone https://github.com/agenthinkai/meshpilot.git $installDir 2>&1 | ForEach-Object { Write-Output "$_" }
        if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE" }
    } else {
        Write-Output "Folder already exists: $installDir - skipping clone."
    }
    Write-Output "PROGRESS:55"

    Set-Location $installDir
    Copy-Item ".env.example" ".env" -Force

    $secretKey = New-RandomHex64
    $envContent = Get-Content ".env"
    $envContent = $envContent -replace '^SECRET_KEY=.*', "SECRET_KEY=$secretKey"
    $envContent = $envContent -replace '^ADMIN_EMAIL=.*', "ADMIN_EMAIL=$email"
    $envContent = $envContent -replace '^ADMIN_PASSWORD=.*', "ADMIN_PASSWORD=$pass"
    $envContent | Set-Content ".env"

    $modelDir = Join-Path $installDir "models"
    New-Item -ItemType Directory -Path $modelDir -Force | Out-Null
    $modelDirDocker = $modelDir -replace '\\', '/'
    Add-Content ".env" ""
    Add-Content ".env" "MODEL_DIR=$modelDirDocker"
    Add-Content ".env" "MESHPILOT_PORT=$port"
    Write-Output "Environment configured."
    Write-Output "PROGRESS:70"

    Write-Output "Building and starting MeshPilot (this can take several minutes)..."
    docker compose up -d --build 2>&1 | ForEach-Object { Write-Output "$_" }
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }

    Write-Output "PROGRESS:100"
    Write-Output "INSTALL_COMPLETE"
}

$script:job = $null
$script:needsRestart = $false
$script:chosenPort = "8100"
$script:installDirForCancel = ""

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 300

$timer.Add_Tick({
    if (-not $script:job) { return }

    $lines = Receive-Job -Job $script:job -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        if ($line -eq "DOCKER_INSTALLED_NEEDS_RESTART") { $script:needsRestart = $true; continue }
        if ($line -eq "INSTALL_COMPLETE") { continue }
        if ($line -eq "GIT_READY") { $chkGit.Checked = $true; continue }
        if ($line -eq "DOCKER_READY") { $chkDocker.Checked = $true; continue }
        if ($line -match "^PROGRESS:(\d+)$") { $progress.Value = [int]$Matches[1]; continue }
        Write-Log "$line"
    }

    if ($script:job.State -in @("Completed", "Failed")) {
        $timer.Stop()
        $wasFailed = ($script:job.State -eq "Failed")
        $jobError = $script:job.ChildJobs[0].JobStateInfo.Reason
        Remove-Job -Job $script:job -Force
        $script:job = $null

        Set-FormEnabled $true

        if ($wasFailed) {
            Set-Status "Failed."
            [System.Windows.Forms.MessageBox]::Show(
                "Installation failed: $jobError`r`n`r`nSee the log above for details.",
                "MeshPilot Installer", "OK", "Error") | Out-Null
        } elseif ($script:needsRestart) {
            Set-Status "Docker installed."
            [System.Windows.Forms.MessageBox]::Show(
                "Docker Desktop was installed. Please restart your computer and run this installer again.",
                "MeshPilot Installer") | Out-Null
            $form.Close()
        } else {
            Set-Status "Done!"
            [System.Windows.Forms.MessageBox]::Show(
                "MeshPilot is running at http://localhost:$($script:chosenPort)`r`nLog in with: $($txtEmail.Text)",
                "MeshPilot Installer") | Out-Null
            Start-Process "http://localhost:$($script:chosenPort)"
        }
    }
})

$btnInstall.Add_Click({
    $installDir = $txtFolder.Text.Trim()
    $email = $txtEmail.Text.Trim()
    $pass = $txtPass.Text
    $pass2 = $txtPass2.Text
    $portText = $txtPort.Text.Trim()

    if (-not $installDir) {
        [System.Windows.Forms.MessageBox]::Show("Please choose an install folder.", "MeshPilot Installer") | Out-Null
        return
    }
    if (-not $email) {
        [System.Windows.Forms.MessageBox]::Show("Please enter an admin email.", "MeshPilot Installer") | Out-Null
        return
    }
    if ($pass.Length -lt 8) {
        [System.Windows.Forms.MessageBox]::Show("Password must be at least 8 characters.", "MeshPilot Installer") | Out-Null
        return
    }
    if ($pass -ne $pass2) {
        [System.Windows.Forms.MessageBox]::Show("Passwords do not match.", "MeshPilot Installer") | Out-Null
        return
    }
    $portNum = 0
    if (-not [int]::TryParse($portText, [ref]$portNum) -or $portNum -lt 1 -or $portNum -gt 65535) {
        [System.Windows.Forms.MessageBox]::Show("Please enter a valid port number (1-65535).", "MeshPilot Installer") | Out-Null
        return
    }

    $installDir = [System.IO.Path]::GetFullPath($installDir)
    $script:chosenPort = $portNum
    $script:installDirForCancel = $installDir

    Set-FormEnabled $false
    $progress.Value = 0
    $txtLog.Clear()
    Set-Status "Starting install..."
    $script:needsRestart = $false

    $script:job = Start-Job -ScriptBlock $jobScript -ArgumentList $installDir, $email, $pass, $portNum
    $timer.Start()
})

$btnCancel.Add_Click({
    $timer.Stop()
    Set-Status "Cancelling..."
    Write-Log "Cancelling installation..."

    if ($script:job) {
        Stop-Job -Job $script:job -ErrorAction SilentlyContinue
        Remove-Job -Job $script:job -Force -ErrorAction SilentlyContinue
        $script:job = $null
    }

    if ($script:installDirForCancel -and (Test-Path (Join-Path $script:installDirForCancel "docker-compose.yml"))) {
        Write-Log "Cleaning up any containers that were started..."
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = "docker"
            $psi.Arguments = "compose down"
            $psi.WorkingDirectory = $script:installDirForCancel
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $p = [System.Diagnostics.Process]::Start($psi)
            $p.WaitForExit()
        } catch {
            Write-Log "Cleanup step failed (this is usually harmless): $_"
        }
    }

    $progress.Value = 0
    Set-Status "Cancelled."
    Write-Log "Installation cancelled."
    Set-FormEnabled $true
})

[void]$form.ShowDialog()
