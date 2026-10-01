import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from juya_admin_api.local_admin import (
    LocalAdminConfig,
    SQLAlchemyLocalAdminRepository,
    seed_local_admin,
)
from juya_admin_api.shared.ids import new_ulid

ROOT = Path(__file__).parents[2]


def test_real_http_session_analytics_config_and_sql_celery_batch(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    redis_url = os.getenv("JUYA_TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip(
            "isolated MySQL and Redis URLs are required for socket HTTP + worker acceptance"
        )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    username = f"batch6-{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(24)
    engine = create_engine(database_url)
    asset_id = new_ulid(datetime.now(UTC))
    user_id = new_ulid(datetime.now(UTC))
    ticket_id = new_ulid(datetime.now(UTC))
    processes: list[subprocess.Popen[bytes]] = []
    batch_id: str | None = None
    request.addfinalizer(
        lambda: _cleanup_acceptance(
            engine, username, asset_id, user_id, ticket_id, processes, batch_id
        )
    )
    admin_repository = SQLAlchemyLocalAdminRepository(database_url)
    try:
        seed_local_admin(
            LocalAdminConfig("test", database_url, username, password),
            admin_repository,
            datetime.now(UTC),
        )
    finally:
        admin_repository.close()

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM analytics_daily WHERE metric_day = '2034-01-02'"))
        connection.execute(
            text(
                "INSERT INTO analytics_daily "
                "(metric_day, metric, dimension, metric_value, generated_at) "
                "VALUES ('2034-01-02','NEW_USERS','ALL',7,UTC_TIMESTAMP(6)), "
                "('2034-01-02','FEEDBACK_SLA','NUMERATOR',4,UTC_TIMESTAMP(6)), "
                "('2034-01-02','FEEDBACK_SLA','DENOMINATOR',5,UTC_TIMESTAMP(6))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_account (public_id, juya_number, status) "
                "VALUES (:id, :number, 'ACTIVE')"
            ),
            {"id": user_id, "number": username},
        )
        connection.execute(
            text(
                "INSERT INTO feedback_ticket "
                "(public_id, user_id, category, description, source, status, sla_hours, "
                "deadline_at, create_idempotency_key, created_at, updated_at) "
                "SELECT :ticket, id, 'FUNCTION', 'acceptance fixture', JSON_OBJECT(), "
                "'PROCESSING', 24, DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 HOUR), "
                ":key, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6) FROM user_account WHERE public_id=:user"
            ),
            {"ticket": ticket_id, "key": username, "user": user_id},
        )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "JUYA_ENVIRONMENT": "test",
        "JUYA_DATABASE_URL": database_url,
        "JUYA_REDIS_URL": redis_url,
        "JUYA_INTERNAL_HMAC_SECRET": secrets.token_urlsafe(32),
        "JUYA_OSS_REGION": "oss-cn-test",
        "JUYA_OSS_BUCKET": "acceptance-local",
        "JUYA_OSS_EXPECTED_BUCKET": "acceptance-local",
        "OSS_ACCESS_KEY_ID": "acceptance-placeholder",
        "OSS_ACCESS_KEY_SECRET": "acceptance-placeholder",
        "JUYA_OCR_PROVIDER": "disabled",
    }
    with (
        (tmp_path / "api.log").open("wb") as api_log,
        (tmp_path / "worker.log").open("wb") as worker_log,
    ):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "juya_admin_api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdout=api_log,
                stderr=subprocess.STDOUT,
            )
        )
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "-A",
                    "juya_admin_api.infrastructure.tasks.celery_app:celery_app",
                    "worker",
                    "--pool=solo",
                    "--concurrency=1",
                    "--queues=content.publish",
                    "--loglevel=INFO",
                    "--hostname",
                    f"{username}@acceptance",
                ],
                cwd=ROOT,
                env=environment,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
            )
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                assert time.monotonic() < deadline, "acceptance API did not become ready"
                time.sleep(0.1)
            assert (
                client.get("/api/v1/admin/analytics?start=2034-01-02&end=2034-01-02").status_code
                == 401
            )
            login = client.post(
                "/api/v1/admin/session", json={"username": username, "password": password}
            )
            assert login.status_code == 200
            # Production cookies stay Secure. Only this loopback HTTP harness
            # copies the returned token into its jar; no server policy is weakened.
            token = login.cookies["juya_admin_session"]
            client.cookies.clear()
            client.cookies.set(
                "juya_admin_session", token, domain="127.0.0.1", path="/api/v1/admin"
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            for period in ("day", "week", "month"):
                response = client.get(
                    "/api/v1/admin/analytics",
                    params={"start": "2034-01-02", "end": "2034-01-02", "period": period},
                )
                assert response.status_code == 200
                assert response.json()["ratios"][0]["rate"] == 0.8
                assert any(
                    row["metric"] == "NEW_USERS" and row["value"] == 7
                    for row in response.json()["rows"]
                )
            for path in (
                "dashboard",
                "work-items",
                "settings",
                "content/scenes",
                "media/batch-jobs",
                "media/trash",
            ):
                assert client.get(f"/api/v1/admin/{path}").status_code == 200, path
            before = client.get("/api/v1/admin/dashboard").json()
            before_items = client.get("/api/v1/admin/work-items").json()
            work_key = f"feedback:{ticket_id}"
            assert any(item["key"] == work_key for item in before_items)
            resolved = client.post(
                f"/api/v1/admin/feedback/{ticket_id}/commands/resolve",
                json={"template": "RESOLVED", "note": "acceptance"},
                headers={**headers, "X-Idempotency-Key": username},
            )
            assert resolved.status_code == 200
            after = client.get("/api/v1/admin/dashboard").json()
            assert after["open_feedback"] == before["open_feedback"] - 1
            assert after["overdue_feedback"] == before["overdue_feedback"] - 1
            assert all(
                item["key"] != work_key for item in client.get("/api/v1/admin/work-items").json()
            )
            settings = client.get("/api/v1/admin/settings").json()["items"]
            setting = next(item for item in settings if item["key"] == "feedback_sla_hours")
            key_path = "/api/v1/admin/settings/feedback_sla_hours"
            assert (
                client.patch(
                    key_path,
                    json={"expected_version": setting["version"], "value": setting["value"]},
                ).status_code
                == 403
            )
            saved = client.patch(
                key_path,
                json={"expected_version": setting["version"], "value": setting["value"]},
                headers=headers,
            )
            assert saved.status_code == 200
            assert (
                client.patch(
                    key_path,
                    json={"expected_version": setting["version"], "value": setting["value"]},
                    headers=headers,
                ).status_code
                == 409
            )
            # This socket acceptance uses a real SQL/Celery content operation.
            # OCR defaults off; no fabricated OCR candidate or paid provider call.
            series = client.post(
                "/api/v1/admin/content/series",
                json={"title": "Acceptance synthetic content", "slug": username},
                headers={**headers, "X-Idempotency-Key": username + "-series"},
            )
            assert series.status_code == 201
            replay_series = client.post(
                "/api/v1/admin/content/series",
                json={"title": "Acceptance synthetic content", "slug": username},
                headers={**headers, "X-Idempotency-Key": username + "-series"},
            )
            assert replay_series.status_code == 201
            assert replay_series.json() == series.json()
            scene = client.post(
                "/api/v1/admin/content/scenes",
                json={"series_id": series.json()["id"], "template_type": "dialogue"},
                headers={**headers, "X-Idempotency-Key": username + "-scene"},
            )
            assert scene.status_code == 201
            replay_scene = client.post(
                "/api/v1/admin/content/scenes",
                json={"series_id": series.json()["id"], "template_type": "dialogue"},
                headers={**headers, "X-Idempotency-Key": username + "-scene"},
            )
            assert replay_scene.status_code == 201
            assert replay_scene.json()["id"] == scene.json()["id"]
            assert replay_scene.json()["draft_revision_id"] == scene.json()["draft_revision_id"]
            scene_id = scene.json()["id"]
            revision_id = scene.json()["draft_revision_id"]
            created = client.post(
                "/api/v1/admin/media/batch-jobs",
                json={
                    "job_type": "TAGS",
                    "target_ids": [scene_id],
                    "input_payload": {"tags": ["real-http-worker"]},
                },
                headers={**headers, "X-Idempotency-Key": username},
            )
            assert created.status_code == 201
            batch_id = created.json()["id"]
            deadline = time.monotonic() + 30
            while True:
                batch = client.get(f"/api/v1/admin/media/batch-jobs/{batch_id}").json()
                if batch["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
                    break
                assert time.monotonic() < deadline, "SQL batch worker did not finish"
                time.sleep(0.1)
            assert batch["status"] == "COMPLETED", batch["result_payload"]
            assert batch["items"][0]["attempt_count"] == 1
            draft = client.get(f"/api/v1/admin/content/revisions/{revision_id}")
            assert draft.status_code == 200
            assert draft.json()["content"]["tags"] == ["real-http-worker"]


def _cleanup_acceptance(
    engine: Engine,
    username: str,
    asset_id: str,
    user_id: str,
    ticket_id: str,
    processes: list[subprocess.Popen[bytes]],
    batch_id: str | None,
) -> None:
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    with engine.begin() as connection:
        if batch_id:
            connection.execute(
                text("DELETE FROM batch_job WHERE public_id = :id"), {"id": batch_id}
            )
        connection.execute(
            text(
                "UPDATE scene s JOIN content_series c ON c.id=s.series_id "
                "SET s.draft_revision_id=NULL,s.published_revision_id=NULL WHERE c.slug=:slug"
            ),
            {"slug": username},
        )
        connection.execute(
            text(
                "DELETE s FROM scene s JOIN content_series c ON c.id=s.series_id WHERE c.slug=:slug"
            ),
            {"slug": username},
        )
        connection.execute(text("DELETE FROM content_series WHERE slug=:slug"), {"slug": username})
        connection.execute(text("DELETE FROM media_asset WHERE public_id = :id"), {"id": asset_id})
        connection.execute(text("DELETE FROM analytics_daily WHERE metric_day = '2034-01-02'"))
        connection.execute(
            text("DELETE FROM feedback_ticket WHERE public_id = :id"), {"id": ticket_id}
        )
        connection.execute(text("DELETE FROM user_account WHERE public_id = :id"), {"id": user_id})
        connection.execute(
            text("DELETE FROM admin_outbox WHERE aggregate_public_id = :id"),
            {"id": ticket_id},
        )
        connection.execute(
            text(
                "DELETE FROM admin_session WHERE admin_user_id = "
                "(SELECT id FROM admin_user WHERE username = :name)"
            ),
            {"name": username},
        )
        connection.execute(
            text("DELETE FROM admin_user WHERE username = :name"), {"name": username}
        )
    engine.dispose()
