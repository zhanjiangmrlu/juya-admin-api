param(
    [string]$DockerCommand = 'docker',
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$DatabasePort = 3306,
    [ValidateRange(1, 65535)]
    [int]$RedisPort = 6379,
    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$composeFile = Join-Path $projectRoot 'docker-compose.dev.yml'

if ($null -eq (Get-Command $DockerCommand -ErrorAction SilentlyContinue)) {
    Write-Error '未找到 Docker 命令，请安装并启动 Docker Desktop'
    exit 1
}

& $DockerCommand compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Docker Compose 不可用，请安装并启动 Docker Desktop'
    exit 1
}

& $DockerCommand info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Docker Engine 不可用，请安装并启动 Docker Desktop'
    exit 1
}

foreach ($port in @($ApiPort, $DatabasePort, $RedisPort)) {
    $portProbe = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $port
    )
    try {
        $portProbe.Start()
    }
    catch {
        Write-Error "端口 $port 已被占用，请释放端口后重试"
        exit 1
    }
    finally {
        $portProbe.Stop()
    }
}

Push-Location $projectRoot
try {
    & $DockerCommand compose -f $composeFile up --build -d
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Docker Compose 启动失败'
        exit 1
    }

    $readyUrl = "http://127.0.0.1:$ApiPort/health/ready"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $ready = $false
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }

    if (-not $ready) {
        Write-Output "管理接口在 $TimeoutSeconds 秒内未就绪"
        & $DockerCommand compose -f $composeFile ps
        & $DockerCommand compose -f $composeFile logs --tail 100 admin-api
        exit 1
    }

    $username = if ($env:JUYA_LOCAL_ADMIN_USERNAME) {
        $env:JUYA_LOCAL_ADMIN_USERNAME
    }
    else {
        'admin'
    }
    Write-Output "juya-admin-api 已就绪: $readyUrl"
    Write-Output "本地管理员账号: $username"
    Write-Output '本地管理员密码见 README 的本地开发章节'
}
finally {
    Pop-Location
}
