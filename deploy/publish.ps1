# 本地统一发布入口；参数直接传给 Python，不拼接命令字符串
param(
    [ValidateSet('all','juya-admin','juya-admin-api','juya-miniapp-h5','juya-miniapp-api')]
    [string]$Project,
    [ValidateSet('test','production')][string]$Environment,
    [ValidateSet('publish','rollback','doctor','plan')][string]$Action = 'publish',
    [string]$Config,
    [switch]$BuildOnly
)
$ErrorActionPreference = 'Stop'
$taskWorkspace = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$taskPython = Join-Path $taskWorkspace 'juya-admin-api/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '请先在 juya-admin-api 运行 uv sync --frozen，准备本地 Python 环境'
}
$taskConfig = if ($Config) { $Config } else { Join-Path $taskWorkspace '.local-release/config.json' }
$taskArguments = @((Join-Path $PSScriptRoot 'local-release/publish.py'), '--workspace', $taskWorkspace, '--action', $Action, '--config', $taskConfig)
if ($Project) { $taskArguments += @('--project', $Project) }
if ($Environment) { $taskArguments += @('--environment', $Environment) }
if ($BuildOnly) { $taskArguments += '--build-only' }
$taskOriginalPath = $env:PATH
$taskOriginalEncoding = $env:PYTHONIOENCODING
try {
    if (Test-Path -LiteralPath $taskConfig) {
        $taskSettings = Get-Content -LiteralPath $taskConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        $taskMediaBin = $taskSettings.local_tools.media_bin
        if ($taskMediaBin) {
            foreach ($taskBinary in @('ffmpeg.exe','ffprobe.exe')) {
                if (-not (Test-Path -LiteralPath (Join-Path $taskMediaBin $taskBinary))) {
                    throw 'local_tools.media_bin 需要包含 ffmpeg.exe 和 ffprobe.exe'
                }
            }
            $env:PATH = $taskMediaBin + [IO.Path]::PathSeparator + $env:PATH
        }
    }
    $env:PYTHONIOENCODING = 'utf-8'
    & $taskPython @taskArguments
    $taskExitCode = $LASTEXITCODE
} finally {
    $env:PATH = $taskOriginalPath
    $env:PYTHONIOENCODING = $taskOriginalEncoding
}
exit $taskExitCode
