$ErrorActionPreference = 'Stop'
$taskScript = Join-Path $PSScriptRoot '..\..\scripts\start-local-oss.ps1'
$taskFixture = Join-Path ([IO.Path]::GetTempPath()) "juya-oss-credentials-$PID.txt"
$taskCalls = [Collections.Generic.List[object]]::new()
$taskDockerFails = $false

function TestDocker {
    $taskCalls.Add(@{
        Arguments = @($args)
        Bucket = $env:JUYA_OSS_BUCKET
        ExpectedBucket = $env:JUYA_OSS_EXPECTED_BUCKET
        Region = $env:JUYA_OSS_REGION
        Endpoint = $env:JUYA_OSS_ENDPOINT
        Id = $env:OSS_ACCESS_KEY_ID
        Secret = $env:OSS_ACCESS_KEY_SECRET
        PrimaryId = $env:JUYA_OSS_ACCESS_KEY_ID
        Token = $env:OSS_SESSION_TOKEN
    })
    $global:LASTEXITCODE = if ($taskDockerFails) { 1 } else { 0 }
}
function Invoke-RestMethod { return @{ status = 'ready' } }

$taskSaved = $env:JUYA_OSS_BUCKET
try {
    Set-Content -LiteralPath $taskFixture -Encoding ascii -Value "AccessKey ID: fixtureId`nAccessKey Secret: fixtureSecret"
    $env:JUYA_OSS_BUCKET = 'original-bucket'
    & $taskScript -CredentialFile $taskFixture -DockerCommand TestDocker
    $taskUp = @($taskCalls | Where-Object { $_.Arguments -contains 'up' })
    if ($taskUp.Count -ne 1) { throw 'Must recreate the running application services once' }
    $taskInvocation = $taskUp[0]
    if ($taskInvocation.Bucket -ne 'juya-test' -or $taskInvocation.ExpectedBucket -ne 'juya-test' -or
        $taskInvocation.Region -ne 'cn-shenzhen' -or
        $taskInvocation.Endpoint -ne 'https://oss-cn-shenzhen.aliyuncs.com' -or
        $taskInvocation.Id -ne 'fixtureId' -or $taskInvocation.Secret -ne 'fixtureSecret' -or
        $taskInvocation.PrimaryId -or $taskInvocation.Token) {
        throw 'Compose must receive the complete test credentials and matching test bucket'
    }
    if ($taskInvocation.Arguments -notcontains '--no-deps' -or
        $taskInvocation.Arguments -notcontains 'admin-api' -or
        $taskInvocation.Arguments -notcontains 'admin-worker-content' -or
        $taskInvocation.Arguments -notcontains 'admin-worker-domain' -or
        $taskInvocation.Arguments -notcontains 'admin-beat') {
        throw 'Only application containers should be recreated; preserve database and login initialization'
    }
    if ($env:JUYA_OSS_BUCKET -ne 'original-bucket') { throw 'Caller environment was not restored' }

    $taskDockerFails = $true
    $taskRejected = $false
    try { & $taskScript -CredentialFile $taskFixture -DockerCommand TestDocker }
    catch { $taskRejected = $true }
    if (-not $taskRejected -or $env:JUYA_OSS_BUCKET -ne 'original-bucket') {
        throw 'Docker failure must propagate and restore the caller environment'
    }
    $taskDockerFails = $false

    $taskCalls.Clear()
    Set-Content -LiteralPath $taskFixture -Encoding ascii -Value 'AccessKey ID: incomplete'
    $taskRejected = $false
    try { & $taskScript -CredentialFile $taskFixture -DockerCommand TestDocker }
    catch { $taskRejected = $true }
    if (-not $taskRejected -or $taskCalls.Count -ne 0) {
        throw 'Incomplete credentials must be rejected before invoking Docker'
    }
}
finally {
    $env:JUYA_OSS_BUCKET = $taskSaved
    Remove-Item -LiteralPath $taskFixture -Force -ErrorAction SilentlyContinue
}
Write-Output 'start-local-oss tests passed'
