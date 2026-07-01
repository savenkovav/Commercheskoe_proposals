#Requires -Version 5.1
param(
    [string]$ConfigPath = "",
    [switch]$SkipBuild,
    [switch]$TestConnectionOnly
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Read-DeployConfig {
    param([string]$Path)
    $cfg = @{
        VPS_HOST = "77.222.55.100"
        VPS_USER = "root"
        VPS_PORT = "22"
        VPS_APP_DIR = "/opt/comm-proposals"
        VPS_SSH_KEY = ""
        VPS_SSH_HOST = ""
        PUBLIC_BASE_URL = "http://77.222.55.100"
        DEPLOY_MODE = "standalone"
        EXPOSE_APP_PORT = "true"
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Config not found: $Path. Copy deploy\vps.deploy.env.example to deploy\vps.deploy.env"
    }
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { return }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        $cfg[$key] = $val
    }
    if ([string]::IsNullOrWhiteSpace($cfg.VPS_SSH_KEY)) {
        throw "Set VPS_SSH_KEY in $Path"
    }
    if (-not (Test-Path -LiteralPath $cfg.VPS_SSH_KEY)) {
        $keyInDir = Join-Path $cfg.VPS_SSH_KEY "id_rsa"
        if ((Test-Path -LiteralPath $cfg.VPS_SSH_KEY -PathType Container) -and (Test-Path -LiteralPath $keyInDir)) {
            $cfg.VPS_SSH_KEY = $keyInDir
        } else {
            throw "SSH key not found: $($cfg.VPS_SSH_KEY)"
        }
    }
    return $cfg
}

function Set-SshKeyPermissions {
    param([string]$KeyPath)
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $KeyPath /inheritance:r | Out-Null
    & icacls $KeyPath /grant:r "${user}:(R)" | Out-Null
}

function New-ProductionDotenv {
    param([string]$PublicBaseUrl)
    $source = Join-Path $Root "env.example"
    if (Test-Path (Join-Path $Root ".env")) {
        $source = Join-Path $Root ".env"
    }
    $lines = Get-Content -LiteralPath $source -Encoding UTF8
    $out = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($line in $lines) {
        $trim = $line.Trim()
        if ($trim -match '^WEB_HOST=') { $out.Add("WEB_HOST=0.0.0.0"); $seen.WEB_HOST = $true; continue }
        if ($trim -match '^WEB_BEHIND_PROXY=') { $out.Add("WEB_BEHIND_PROXY=true"); $seen.WEB_BEHIND_PROXY = $true; continue }
        if ($trim -match '^PUBLIC_BASE_URL=') { $out.Add("PUBLIC_BASE_URL=$PublicBaseUrl"); $seen.PUBLIC_BASE_URL = $true; continue }
        if ($trim -match '^PROCUREMENT_REPORT_PATH=\.\./') { $out.Add("PROCUREMENT_REPORT_PATH="); $seen.PROCUREMENT_REPORT_PATH = $true; continue }
        if ($trim -match '^MEILISEARCH_ENABLED=') { $out.Add("MEILISEARCH_ENABLED=true"); $seen.MEILISEARCH_ENABLED = $true; continue }
        if ($trim -match '^MEILISEARCH_HOST=') { $out.Add("MEILISEARCH_HOST=http://meilisearch:7700"); $seen.MEILISEARCH_HOST = $true; continue }
        if ($trim -match '^USE_AI_INTERNET_SEARCH=') { $out.Add("USE_AI_INTERNET_SEARCH=false"); $seen.USE_AI_INTERNET_SEARCH = $true; continue }
        $out.Add($line)
    }
    if (-not $seen.WEB_BEHIND_PROXY) { $out.Add("WEB_BEHIND_PROXY=true") }
    if (-not $seen.PUBLIC_BASE_URL) { $out.Add("PUBLIC_BASE_URL=$PublicBaseUrl") }
    if (-not $seen.AUTH_ENABLED) { $out.Add("AUTH_ENABLED=true") }
    if (-not $seen.USERS_DB_PATH) { $out.Add("USERS_DB_PATH=data/users.db") }
    if (-not $seen.MEILISEARCH_ENABLED) { $out.Add("MEILISEARCH_ENABLED=true") }
    if (-not $seen.MEILISEARCH_HOST) { $out.Add("MEILISEARCH_HOST=http://meilisearch:7700") }
    $tmp = Join-Path $env:TEMP "comm-proposals-deploy.env"
    [System.IO.File]::WriteAllLines($tmp, $out, [System.Text.UTF8Encoding]::new($false))
    return $tmp
}

function Invoke-Ssh {
    param([hashtable]$Cfg, [string]$RemoteCommand)
    $target = if (-not [string]::IsNullOrWhiteSpace($Cfg.VPS_SSH_HOST)) {
        $Cfg.VPS_SSH_HOST
    } else {
        "$($Cfg.VPS_USER)@$($Cfg.VPS_HOST)"
    }
    if (-not [string]::IsNullOrWhiteSpace($Cfg.VPS_SSH_HOST)) {
        $args = @(
            "-F", (Join-Path $env:USERPROFILE ".ssh\config"),
            "-p", $Cfg.VPS_PORT,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=20",
            $target,
            $RemoteCommand
        )
    } else {
        $args = @(
            "-F", "NUL",
            "-i", $Cfg.VPS_SSH_KEY,
            "-p", $Cfg.VPS_PORT,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=20",
            $target,
            $RemoteCommand
        )
    }
    & ssh @args
    if ($LASTEXITCODE -ne 0) { throw "SSH failed with exit code $LASTEXITCODE" }
}

function Invoke-Scp {
    param([hashtable]$Cfg, [string]$LocalPath, [string]$RemotePath)
    if (-not [string]::IsNullOrWhiteSpace($Cfg.VPS_SSH_HOST)) {
        $target = "$($Cfg.VPS_SSH_HOST):$RemotePath"
        $args = @(
            "-F", (Join-Path $env:USERPROFILE ".ssh\config"),
            "-P", $Cfg.VPS_PORT,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=20",
            $LocalPath,
            $target
        )
    } else {
        $target = "$($Cfg.VPS_USER)@$($Cfg.VPS_HOST):$RemotePath"
        $args = @(
            "-F", "NUL",
            "-i", $Cfg.VPS_SSH_KEY,
            "-P", $Cfg.VPS_PORT,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=20",
            $LocalPath,
            $target
        )
    }
    & scp @args
    if ($LASTEXITCODE -ne 0) { throw "SCP failed with exit code $LASTEXITCODE" }
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $Root "deploy\vps.deploy.env"
}
$cfg = Read-DeployConfig -Path $ConfigPath
Set-SshKeyPermissions -KeyPath $cfg.VPS_SSH_KEY

Write-Host "==> VPS: $($cfg.VPS_USER)@$($cfg.VPS_HOST):$($cfg.VPS_PORT)"
Write-Host "==> App dir: $($cfg.VPS_APP_DIR)"
Write-Host "==> URL: $($cfg.PUBLIC_BASE_URL)"

if ($TestConnectionOnly) {
    Invoke-Ssh -Cfg $cfg -RemoteCommand "echo SSH_OK && uname -a && (docker --version || echo NO_DOCKER)"
    Write-Host "SSH connection OK."
    exit 0
}

$archive = Join-Path $env:TEMP "comm-proposals-release.tgz"
$dotenv = New-ProductionDotenv -PublicBaseUrl $cfg.PUBLIC_BASE_URL

if (-not $SkipBuild) {
    Write-Host "==> Building release archive..."
    if (Test-Path $archive) { Remove-Item -Force $archive }
    Push-Location $Root
    try {
        & tar -czf $archive `
            --exclude=".git" `
            --exclude=".venv" `
            --exclude="venv" `
            --exclude="kp_meilisearch/.venv" `
            --exclude="output" `
            --exclude="__pycache__" `
            --exclude=".env" `
            --exclude=".DS_Store" `
            --exclude=".cursor" `
            --exclude="deploy/vps.deploy.env" `
            --exclude="deploy/github-vps-dotenv.secret" `
            --exclude="id_rsa" `
            .
        if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

Write-Host "==> Checking SSH..."
Invoke-Ssh -Cfg $cfg -RemoteCommand "mkdir -p $($cfg.VPS_APP_DIR)"

Write-Host "==> Uploading files..."
Invoke-Scp -Cfg $cfg -LocalPath $archive -RemotePath "/tmp/comm-proposals-release.tgz"
Invoke-Scp -Cfg $cfg -LocalPath $dotenv -RemotePath "$($cfg.VPS_APP_DIR)/.env"

$appDir = $cfg.VPS_APP_DIR
$remoteSetup = "mkdir -p '$appDir' && tar xzf /tmp/comm-proposals-release.tgz -C '$appDir' && rm -f /tmp/comm-proposals-release.tgz && find '$appDir' -name '*.sh' -exec sed -i 's/\r$//' {} + && APP_DIR='$appDir' PUBLIC_BASE_URL='$($cfg.PUBLIC_BASE_URL)' DEPLOY_MODE='$($cfg.DEPLOY_MODE)' EXPOSE_APP_PORT='$($cfg.EXPOSE_APP_PORT)' bash '$appDir/scripts/vps_remote_setup.sh'"

Write-Host "==> Running remote setup (Docker + app)..."
Invoke-Ssh -Cfg $cfg -RemoteCommand $remoteSetup

Write-Host ""
Write-Host "Deploy complete."
Write-Host "  URL: $($cfg.PUBLIC_BASE_URL)"
if ($cfg.EXPOSE_APP_PORT -eq "true") {
    Write-Host "  Direct: http://$($cfg.VPS_HOST):8080"
}
Write-Host "  Logs: ssh -i `"$($cfg.VPS_SSH_KEY)`" $($cfg.VPS_USER)@$($cfg.VPS_HOST) 'docker compose -f $($cfg.VPS_APP_DIR)/docker-compose.prod.yml logs -f kp-web'"

if ((Get-Content -LiteralPath $dotenv -Raw) -match 'PROXYAPI_API_KEY=your_proxyapi_key') {
    Write-Host ""
    Write-Host "WARNING: set PROXYAPI_API_KEY on the server:"
    Write-Host "  ssh -i `"$($cfg.VPS_SSH_KEY)`" $($cfg.VPS_USER)@$($cfg.VPS_HOST) 'nano $($cfg.VPS_APP_DIR)/.env'"
}
