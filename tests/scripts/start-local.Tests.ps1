$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$startScript = Join-Path $projectRoot 'scripts\start-local.ps1'

if (-not (Test-Path -LiteralPath $startScript)) {
    throw 'scripts/start-local.ps1 does not exist'
}

$null = [scriptblock]::Create((Get-Content -Raw -Encoding utf8 -LiteralPath $startScript))

$ErrorActionPreference = 'Continue'
$missingOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript `
    -DockerCommand '__juya_missing_docker__' 2>&1 | Out-String
$missingExitCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($missingExitCode -eq 0) {
    throw 'missing Docker must return a non-zero exit code'
}
if ($missingOutput -notmatch '安装并启动 Docker Desktop') {
    throw 'missing Docker output must explain how to install and start Docker Desktop'
}

$temporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "juya-start-local-$PID"
$fakeDocker = Join-Path $temporaryDirectory 'docker.cmd'
$listener = $null
try {
    New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
    Set-Content -LiteralPath $fakeDocker -Value '@exit /b 0' -Encoding ascii
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    $occupiedPort = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port

    $ErrorActionPreference = 'Continue'
    $portOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript `
        -DockerCommand $fakeDocker -ApiPort $occupiedPort 2>&1 | Out-String
    $portExitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($portExitCode -eq 0) {
        throw 'occupied API port must return a non-zero exit code'
    }
    if ($portOutput -notmatch "端口 $occupiedPort 已被占用") {
        throw 'occupied port output must identify the conflicting port'
    }

    $ErrorActionPreference = 'Continue'
    $databasePortOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript `
        -DockerCommand $fakeDocker -DatabasePort $occupiedPort 2>&1 | Out-String
    $databasePortExitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($databasePortExitCode -eq 0) {
        throw 'occupied database port must return a non-zero exit code'
    }
    if ($databasePortOutput -notmatch "端口 $occupiedPort 已被占用") {
        throw 'occupied database port output must identify the conflicting port'
    }
}
finally {
    if ($null -ne $listener) {
        $listener.Stop()
    }
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

Write-Output 'start-local tests passed'
