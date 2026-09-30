import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.main import create_app
from juya_admin_api.modules.admin_auth.service import hash_password
from juya_admin_api.shared.ids import new_ulid

TEST_DATABASE_URL = os.getenv("JUYA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="JUYA_TEST_DATABASE_URL must point to an isolated MySQL 8.4 database",
)


def test_sql_runtime_login_publish_and_entitlement_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    assert TEST_DATABASE_URL is not None
    now = datetime.now(UTC)
    admin_public_id = new_ulid(now)
    user_public_id = new_ulid(now)
    series_public_id = new_ulid(now)
    scene_public_id = new_ulid(now)
    package_public_id = new_ulid(now)
    campaign_public_id = new_ulid(now)
    campaign_version_public_id = new_ulid(now)
    feedback_public_id = new_ulid(now)
    username = f"admin-{admin_public_id[-8:]}"
    password = "Strong-Test-Password-1"
    legacy_totp_placeholder = b"PASSWORD_ONLY_LOGIN"
    sync_engine = create_engine(TEST_DATABASE_URL)
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO admin_user "
                "(public_id, username, password_hash, totp_secret_ciphertext, status, "
                "created_at, updated_at) VALUES "
                "(:public_id, :username, :password_hash, :totp_secret, 'ACTIVE', :now, :now)"
            ),
            {
                "public_id": admin_public_id,
                "username": username,
                "password_hash": hash_password(password),
                "totp_secret": legacy_totp_placeholder,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO user_account (public_id, juya_number, status, created_at) "
                "VALUES (:public_id, :juya_number, 'ACTIVE', :now)"
            ),
            {
                "public_id": user_public_id,
                "juya_number": f"JY{user_public_id[-12:]}",
                "now": now,
            },
        )
        user_id = connection.scalar(
            text("SELECT id FROM user_account WHERE public_id = :public_id"),
            {"public_id": user_public_id},
        )
        connection.execute(
            text(
                "INSERT INTO feedback_ticket "
                "(public_id, user_id, category, description, source, status, sla_hours, "
                "deadline_at, create_idempotency_key, created_at, updated_at) VALUES "
                "(:public_id, :user_id, 'CONTENT', 'E2E feedback', JSON_OBJECT(), "
                "'PROCESSING', 48, :deadline_at, :idempotency_key, :now, :now)"
            ),
            {
                "public_id": feedback_public_id,
                "user_id": user_id,
                "deadline_at": now + timedelta(hours=48),
                "idempotency_key": f"create-{feedback_public_id}",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO content_series "
                "(public_id, slug, title, status, created_at) "
                "VALUES (:public_id, :slug, 'E2E Series', 'PUBLISHED', :now)"
            ),
            {
                "public_id": series_public_id,
                "slug": f"e2e-{series_public_id[-8:].lower()}",
                "now": now,
            },
        )
        series_id = connection.scalar(
            text("SELECT id FROM content_series WHERE public_id = :id"),
            {"id": series_public_id},
        )
        connection.execute(
            text(
                "INSERT INTO content_template "
                "(template_type, version, required_modules, validation_rules, enabled, "
                "created_at) VALUES (:type, 1, JSON_ARRAY(), JSON_OBJECT(), 1, :now)"
            ),
            {"type": f"e2e-{series_public_id[-8:]}", "now": now},
        )
        template_id = connection.scalar(text("SELECT LAST_INSERT_ID()"))
        connection.execute(
            text(
                "INSERT INTO scene "
                "(public_id, series_id, template_id, title, status, created_at, updated_at) "
                "VALUES (:public_id, :series_id, :template_id, 'E2E Scene', 'DRAFT', "
                ":now, :now)"
            ),
            {
                "public_id": scene_public_id,
                "series_id": series_id,
                "template_id": template_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO content_package "
                "(public_id, name, status, created_at, updated_at) "
                "VALUES (:public_id, 'E2E Package', 'ACTIVE', :now, :now)"
            ),
            {"public_id": package_public_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO limited_campaign (public_id, name, status, created_at) "
                "VALUES (:public_id, 'E2E Campaign', 'OPEN', :now)"
            ),
            {"public_id": campaign_public_id, "now": now},
        )
        campaign_id = connection.scalar(text("SELECT LAST_INSERT_ID()"))
        connection.execute(
            text(
                "INSERT INTO limited_campaign_version "
                "(public_id, campaign_id, version_no, status, duration_days, "
                "activation_window_days, capacity, granted_user_count, grant_starts_at, "
                "grant_ends_at, created_at) VALUES "
                "(:public_id, :campaign_id, 1, 'OPEN', 3, 7, 10, 0, :now, :ends_at, :now)"
            ),
            {
                "public_id": campaign_version_public_id,
                "campaign_id": campaign_id,
                "now": now,
                "ends_at": now + timedelta(days=7),
            },
        )
        campaign_version_id = connection.scalar(text("SELECT LAST_INSERT_ID()"))
        connection.execute(
            text("UPDATE limited_campaign SET current_version_id = :id WHERE id = :campaign_id"),
            {"id": campaign_version_id, "campaign_id": campaign_id},
        )

    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "e2e-access-key")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "e2e-access-secret")
    settings = Settings(
        database_url=TEST_DATABASE_URL.replace("mysql+pymysql://", "mysql+asyncmy://", 1),
        redis_url="redis://127.0.0.1:6399/15",
        internal_hmac_secret="e2e-internal-secret",
        oss_region="cn-hangzhou",
        oss_bucket="e2e-private-bucket",
        oss_expected_bucket="e2e-private-bucket",
    )
    app = create_app(settings)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            password_response = client.post(
                "/api/v1/admin/session",
                json={"username": username, "password": password},
            )
            assert password_response.status_code == 200
            csrf = password_response.json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}

            revision_response = client.post(
                f"/api/v1/admin/content/scenes/{scene_public_id}/revisions",
                json={},
                headers=headers,
            )
            assert revision_response.status_code == 201
            revision_id = revision_response.json()["id"]
            publish_response = client.post(
                f"/api/v1/admin/content/revisions/{revision_id}/commands/publish",
                json={"acknowledged_warning_codes": []},
                headers={**headers, "X-Idempotency-Key": "e2e-publish-1"},
            )
            assert publish_response.status_code == 200

            formal_response = client.post(
                "/api/v1/admin/formal-entitlements/commands/GRANT",
                json={
                    "user_id": user_public_id,
                    "package_id": package_public_id,
                    "term": "MONTH_1",
                },
                headers={**headers, "X-Idempotency-Key": "e2e-formal-1"},
            )
            assert formal_response.status_code == 200
            limited_response = client.post(
                "/api/v1/admin/limited-entitlements/commands/grant",
                json={
                    "user_id": user_public_id,
                    "campaign_version_id": campaign_version_public_id,
                },
                headers={**headers, "X-Idempotency-Key": "e2e-limited-1"},
            )
            assert limited_response.status_code == 201

            feedback_list = client.get(f"/api/v1/admin/feedback?keyword={feedback_public_id}")
            assert feedback_list.status_code == 200
            feedback_note = client.post(
                f"/api/v1/admin/feedback/{feedback_public_id}/internal-notes",
                json={"content": "E2E internal note"},
                headers={**headers, "X-Idempotency-Key": "e2e-feedback-note-1"},
            )
            assert feedback_note.status_code == 200
            audit_response = client.get("/api/v1/admin/audit-events?limit=200")
            assert audit_response.status_code == 200
            feedback_audits = [
                item
                for item in audit_response.json()["items"]
                if item["object_public_id"] in {"list", feedback_public_id}
                and item["action"].startswith("feedback.")
            ]
            assert any(
                item["action"] == "feedback.list" and item["after_summary"]["count"] == 1
                for item in feedback_audits
            )
            assert any(
                item["action"] == "feedback.internal_note.add"
                and item["after_summary"] == {"note_id": feedback_note.json()["id"]}
                for item in feedback_audits
            )

        with sync_engine.connect() as connection:
            summaries = connection.execute(
                text(
                    "SELECT after_summary FROM audit_event "
                    "WHERE object_public_id = :feedback_id ORDER BY id"
                ),
                {"feedback_id": feedback_public_id},
            ).scalars()
            persisted_summaries = list(summaries)
            assert [json.loads(summary) for summary in persisted_summaries] == [
                {"note_id": feedback_note.json()["id"]}
            ]
    finally:
        with sync_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM audit_event WHERE object_public_id = :feedback_id"),
                {"feedback_id": feedback_public_id},
            )
            connection.execute(
                text(
                    "DELETE n FROM feedback_internal_note n JOIN feedback_ticket f "
                    "ON f.id = n.ticket_id WHERE f.public_id = :feedback_id"
                ),
                {"feedback_id": feedback_public_id},
            )
            connection.execute(
                text("DELETE FROM feedback_ticket WHERE public_id = :feedback_id"),
                {"feedback_id": feedback_public_id},
            )
            connection.execute(
                text(
                    "DELETE FROM limited_entitlement_operation WHERE operator_id IN "
                    "(SELECT CAST(id AS CHAR) FROM admin_user WHERE public_id = :admin_id)"
                ),
                {"admin_id": admin_public_id},
            )
            connection.execute(
                text(
                    "DELETE le FROM limited_entitlement le JOIN user_account u "
                    "ON u.id = le.user_id WHERE u.public_id = :user_id"
                ),
                {"user_id": user_public_id},
            )
            connection.execute(
                text(
                    "UPDATE limited_campaign SET current_version_id = NULL "
                    "WHERE public_id = :campaign_id"
                ),
                {"campaign_id": campaign_public_id},
            )
            connection.execute(
                text("DELETE FROM limited_campaign_version WHERE public_id = :version_id"),
                {"version_id": campaign_version_public_id},
            )
            connection.execute(
                text("DELETE FROM limited_campaign WHERE public_id = :campaign_id"),
                {"campaign_id": campaign_public_id},
            )
            connection.execute(
                text(
                    "DELETE FROM formal_entitlement_operation WHERE operator_id IN "
                    "(SELECT CAST(id AS CHAR) FROM admin_user WHERE public_id = :admin_id)"
                ),
                {"admin_id": admin_public_id},
            )
            connection.execute(
                text(
                    "DELETE fe FROM formal_entitlement fe JOIN user_account u "
                    "ON u.id = fe.user_id WHERE u.public_id = :user_id"
                ),
                {"user_id": user_public_id},
            )
            connection.execute(
                text("DELETE FROM content_package WHERE public_id = :package_id"),
                {"package_id": package_public_id},
            )
            connection.execute(
                text("DELETE FROM idempotency_record WHERE idempotency_key = 'e2e-publish-1'")
            )
            connection.execute(
                text(
                    "UPDATE scene SET draft_revision_id = NULL, published_revision_id = NULL "
                    "WHERE public_id = :scene_id"
                ),
                {"scene_id": scene_public_id},
            )
            connection.execute(
                text(
                    "DELETE r FROM scene_revision r JOIN scene s ON s.id = r.scene_id "
                    "WHERE s.public_id = :scene_id"
                ),
                {"scene_id": scene_public_id},
            )
            connection.execute(
                text("DELETE FROM scene WHERE public_id = :scene_id"),
                {"scene_id": scene_public_id},
            )
            connection.execute(
                text("DELETE FROM content_template WHERE template_type = :type"),
                {"type": f"e2e-{series_public_id[-8:]}"},
            )
            connection.execute(
                text("DELETE FROM content_series WHERE public_id = :series_id"),
                {"series_id": series_public_id},
            )
            connection.execute(
                text(
                    "DELETE FROM admin_session WHERE admin_user_id IN "
                    "(SELECT id FROM admin_user WHERE public_id = :admin_id)"
                ),
                {"admin_id": admin_public_id},
            )
            connection.execute(
                text(
                    "DELETE FROM admin_auth_challenge WHERE admin_user_id IN "
                    "(SELECT id FROM admin_user WHERE public_id = :admin_id)"
                ),
                {"admin_id": admin_public_id},
            )
            connection.execute(
                text(
                    "DELETE FROM audit_event WHERE actor_public_id IN "
                    "(SELECT CAST(id AS CHAR) FROM admin_user WHERE public_id = :admin_id)"
                ),
                {"admin_id": admin_public_id},
            )
            connection.execute(
                text("DELETE FROM admin_user WHERE public_id = :admin_id"),
                {"admin_id": admin_public_id},
            )
            connection.execute(
                text("DELETE FROM user_account WHERE public_id = :user_id"),
                {"user_id": user_public_id},
            )
        sync_engine.dispose()
