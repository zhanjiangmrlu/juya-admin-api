$ErrorActionPreference = 'Stop'
$role = if ($env:JUYA_PROCESS_ROLE) { $env:JUYA_PROCESS_ROLE } else { 'admin-api' }
$logLevel = if ($env:JUYA_LOG_LEVEL) { $env:JUYA_LOG_LEVEL } else { 'INFO' }
$port = if ($env:PORT) { $env:PORT } else { '8000' }

switch ($role) {
    'admin-api' {
        & uvicorn 'juya_admin_api.main:app' '--host' '0.0.0.0' '--port' $port
    }
    'admin-worker-content' {
        & celery '-A' 'juya_admin_api.infrastructure.tasks.celery_app:celery_app' 'worker' `
            '--loglevel' $logLevel `
            '--queues' 'content.ocr,content.audio,content.assets,content.publish'
    }
    'admin-worker-domain' {
        & celery '-A' 'juya_admin_api.infrastructure.tasks.celery_app:celery_app' 'worker' `
            '--loglevel' $logLevel `
            '--queues' 'content.lifecycle,content.analytics,domain.messages'
    }
    'admin-beat' {
        $beatSchedule = Join-Path ([System.IO.Path]::GetTempPath()) 'celerybeat-schedule'
        & celery '-A' 'juya_admin_api.infrastructure.tasks.celery_app:celery_app' 'beat' `
            '--loglevel' $logLevel `
            '--schedule' $beatSchedule
    }
    'migrate' {
        & alembic 'upgrade' 'head'
    }
    default {
        Write-Error "Unknown JUYA_PROCESS_ROLE: $role"
        exit 64
    }
}
exit $LASTEXITCODE
