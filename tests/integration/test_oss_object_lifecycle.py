import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["none", "feedback", "media", "cover", "active"])
async def test_cleanup_protects_references_and_audits_without_object_urls(
    reference: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_engine(url)
    now = datetime.now(UTC)
    ticket_id, other_id, asset_id, series_id = [new_ulid(now) for _ in range(4)]
    key = f"feedback/{ticket_id}/image.png"
    deleted: list[str] = []

    class FakeOss:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def delete_object(self, object_key: str) -> None:
            deleted.append(object_key)

    monkeypatch.setattr(maintenance, "AliyunOssProvider", FakeOss)
    settings = Settings(
        environment="test",
        database_url=url,
        oss_region="cn-shenzhen",
        oss_bucket="juya-test",
        oss_expected_bucket="juya-test",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
    )
    try:
        with engine.begin() as connection:
            for public_id in [ticket_id] + ([other_id] if reference == "feedback" else []):
                connection.execute(
                    text(
                        "INSERT INTO feedback_ticket (public_id,user_id,category,description,"
                        "source,"
                        "status,sla_hours,create_idempotency_key,created_at,updated_at) VALUES "
                        "(:id,NULL,'FUNCTION','test',JSON_OBJECT(),:status,48,:id,:now,:now)"
                    ),
                    {
                        "id": public_id,
                        "status": "PROCESSING" if reference == "active" else "RESOLVED",
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO feedback_screenshot "
                        "(ticket_id,object_key,security_status,delete_after)"
                        " SELECT id,:key,'PASSED',:due FROM feedback_ticket WHERE public_id=:id"
                    ),
                    {"id": public_id, "key": key, "due": None if public_id == other_id else now},
                )
            if reference == "media":
                connection.execute(
                    text(
                        "INSERT INTO media_asset (public_id,object_key,asset_type,content_type,"
                        "size_bytes,"
                        "sha256,status,security_status,created_by,created_at) VALUES "
                        "(:id,:key,'images','image/png',10,:hash,'CONFIRMED','PASSED','system',:now)"
                    ),
                    {"id": asset_id, "key": key, "hash": ticket_id.ljust(64, "0"), "now": now},
                )
            if reference == "cover":
                connection.execute(
                    text(
                        "INSERT INTO content_series (public_id,slug,title,cover_object_key,status) "
                        "VALUES (:id,:id,'test',:key,'PUBLISHED')"
                    ),
                    {"id": series_id, "key": key},
                )
        result = await maintenance._cleanup_feedback_screenshots(settings)
        assert result == {"deleted": int(reference == "none"), "failed": 0}
        assert deleted == ([key] if reference == "none" else [])
        with engine.connect() as connection:
            audit = connection.execute(
                text(
                    "SELECT after_summary FROM audit_event WHERE object_type='feedback_screenshot' "
                    "AND object_public_id=:id ORDER BY id DESC LIMIT 1"
                ),
                {"id": ticket_id},
            ).scalar()
        assert audit is not None
        summary = json.loads(audit) if isinstance(audit, str) else audit
        assert summary["outcome"] == ("DELETED" if reference == "none" else "PROTECTED")
        assert key not in str(summary)
        assert "secret" not in str(summary)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM feedback_ticket WHERE public_id IN (:id,:other)"),
                {"id": ticket_id, "other": other_id},
            )
            connection.execute(
                text("DELETE FROM media_asset WHERE public_id=:id"), {"id": asset_id}
            )
            connection.execute(
                text("DELETE FROM content_series WHERE public_id=:id"), {"id": series_id}
            )
            connection.execute(
                text("DELETE FROM audit_event WHERE object_public_id=:id"), {"id": ticket_id}
            )
        engine.dispose()
