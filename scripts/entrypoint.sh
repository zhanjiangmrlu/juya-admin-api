#!/bin/sh
set -eu

role="${JUYA_PROCESS_ROLE:-admin-api}"

case "$role" in
  admin-api)
    exec uvicorn juya_admin_api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  admin-worker-content)
    exec celery -A juya_admin_api.infrastructure.tasks.celery_app:celery_app worker \
      --loglevel "${JUYA_LOG_LEVEL:-INFO}" \
      --queues content.ocr,content.audio,content.assets,content.publish
    ;;
  admin-worker-domain)
    exec celery -A juya_admin_api.infrastructure.tasks.celery_app:celery_app worker \
      --loglevel "${JUYA_LOG_LEVEL:-INFO}" \
      --queues content.lifecycle,content.analytics,domain.messages
    ;;
  admin-beat)
    exec celery -A juya_admin_api.infrastructure.tasks.celery_app:celery_app beat \
      --loglevel "${JUYA_LOG_LEVEL:-INFO}" \
      --schedule /tmp/celerybeat-schedule
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    echo "Unknown JUYA_PROCESS_ROLE: $role" >&2
    exit 64
    ;;
esac
