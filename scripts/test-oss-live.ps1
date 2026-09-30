param(
    [Parameter(Mandatory = $true)]
    [string]$CredentialFile,
    [ValidateSet('admin', 'miniapp')]
    [string]$Target = 'admin'
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRepository = if ($Target -eq 'admin') { $taskRoot } else {
    Join-Path (Split-Path -Parent $taskRoot) 'juya-miniapp-api'
}
$taskEnvNames = @(
    'OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET', 'OSS_SESSION_TOKEN',
    'JUYA_OSS_ACCESS_KEY_ID', 'JUYA_OSS_ACCESS_KEY_SECRET', 'JUYA_OSS_SESSION_TOKEN',
    'JUYA_OSS_CREDENTIALS_EXPIRES_AT', 'JUYA_OSS_CREDENTIALS_MODE',
    'JUYA_ENVIRONMENT', 'JUYA_OSS_REGION', 'JUYA_OSS_BUCKET',
    'JUYA_OSS_EXPECTED_BUCKET', 'JUYA_OSS_ENDPOINT', 'JUYA_RUN_LIVE_OSS_TESTS'
)
$taskOriginal = @{}
foreach ($name in $taskEnvNames) {
    $taskOriginal[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $taskText = Get-Content -Raw -Encoding UTF8 -LiteralPath $CredentialFile
    $taskId = [regex]::Match($taskText, '(?im)^\s*AccessKey\s*ID\s*[:\uFF1A=]\s*([A-Za-z0-9]+)').Groups[1].Value
    $taskSecret = [regex]::Match($taskText, '(?im)^\s*AccessKey\s*Secret\s*[:\uFF1A=]\s*([A-Za-z0-9]+)').Groups[1].Value
    if (-not $taskId -or -not $taskSecret) {
        throw 'Credential file must contain AccessKey ID and AccessKey Secret labels'
    }
    foreach ($name in $taskEnvNames) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    $env:OSS_ACCESS_KEY_ID = $taskId
    $env:OSS_ACCESS_KEY_SECRET = $taskSecret
    $env:JUYA_ENVIRONMENT = 'test'
    $env:JUYA_OSS_REGION = 'cn-shenzhen'
    $env:JUYA_OSS_BUCKET = 'juya-test'
    $env:JUYA_OSS_EXPECTED_BUCKET = 'juya-test'
    $env:JUYA_OSS_ENDPOINT = 'https://oss-cn-shenzhen.aliyuncs.com'
    $env:JUYA_OSS_CREDENTIALS_MODE = 'environment'
    $env:JUYA_RUN_LIVE_OSS_TESTS = 'true'
    Push-Location -LiteralPath $taskRepository
    try {
        & uv run pytest tests/live -q --tb=short
        $taskExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    foreach ($name in $taskEnvNames) {
        if ($null -eq $taskOriginal[$name]) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $taskOriginal[$name], 'Process')
        }
    }
    $taskText = $null
    $taskId = $null
    $taskSecret = $null
}
exit $taskExitCode
