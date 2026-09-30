param(
    [Parameter(Mandatory = $true)]
    [string]$CredentialFile,
    [string]$DockerCommand = 'docker',
    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 60
)

# Reconfigure an existing local Compose stack for the private test bucket.
# Credentials stay in the process and container environment, never in a repo file.
$ErrorActionPreference = 'Stop'
$taskText = Get-Content -Raw -Encoding UTF8 -LiteralPath $CredentialFile
$taskId = [regex]::Match($taskText, '(?im)^\s*AccessKey\s*ID\s*[:\uFF1A=]\s*([A-Za-z0-9]+)').Groups[1].Value
$taskSecret = [regex]::Match($taskText, '(?im)^\s*AccessKey\s*Secret\s*[:\uFF1A=]\s*([A-Za-z0-9]+)').Groups[1].Value
if (-not $taskId -or -not $taskSecret) {
    throw 'Credential file must contain AccessKey ID and AccessKey Secret labels'
}
$taskEnvNames = @(
    'OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET', 'OSS_SESSION_TOKEN',
    'JUYA_OSS_ACCESS_KEY_ID', 'JUYA_OSS_ACCESS_KEY_SECRET', 'JUYA_OSS_SESSION_TOKEN',
    'JUYA_OSS_CREDENTIALS_EXPIRES_AT', 'JUYA_OSS_CREDENTIALS_MODE',
    'JUYA_OSS_REGION', 'JUYA_OSS_BUCKET', 'JUYA_OSS_EXPECTED_BUCKET', 'JUYA_OSS_ENDPOINT'
)
$taskOriginal = @{}
foreach ($taskName in $taskEnvNames) {
    $taskOriginal[$taskName] = [Environment]::GetEnvironmentVariable($taskName, 'Process')
}
try {
    foreach ($taskName in $taskEnvNames) {
        Remove-Item -LiteralPath "Env:$taskName" -ErrorAction SilentlyContinue
    }
    $env:OSS_ACCESS_KEY_ID = $taskId
    $env:OSS_ACCESS_KEY_SECRET = $taskSecret
    $env:JUYA_OSS_CREDENTIALS_MODE = 'environment'
    $env:JUYA_OSS_REGION = 'cn-shenzhen'
    $env:JUYA_OSS_BUCKET = 'juya-test'
    $env:JUYA_OSS_EXPECTED_BUCKET = 'juya-test'
    $env:JUYA_OSS_ENDPOINT = 'https://oss-cn-shenzhen.aliyuncs.com'
    $taskCompose = Join-Path $PSScriptRoot '..\docker-compose.dev.yml'
    & $DockerCommand compose -f $taskCompose up -d --no-deps `
        admin-api admin-worker-content admin-worker-domain admin-beat
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose OSS reconfiguration failed' }

    $taskReadyUrl = 'http://127.0.0.1:8000/health/ready'
    $taskDeadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $taskReady = $false
    while ([DateTimeOffset]::UtcNow -lt $taskDeadline) {
        try {
            $taskResponse = Invoke-RestMethod -Uri $taskReadyUrl -TimeoutSec 3
            if ($taskResponse.status -eq 'ready') {
                $taskReady = $true
                break
            }
        } catch {
            # Wait for the recreated API without printing request or credential details.
        }
        Start-Sleep -Seconds 2
    }
    if (-not $taskReady) { throw 'API did not become ready after OSS reconfiguration' }
    Write-Output "API ready: $taskReadyUrl; OSS bucket: juya-test (cn-shenzhen)"
    Write-Output 'Browser uploads also require bucket CORS for the actual frontend origin.'
} finally {
    foreach ($taskName in $taskEnvNames) {
        [Environment]::SetEnvironmentVariable($taskName, $taskOriginal[$taskName], 'Process')
    }
    $taskText = $null
    $taskId = $null
    $taskSecret = $null
}
