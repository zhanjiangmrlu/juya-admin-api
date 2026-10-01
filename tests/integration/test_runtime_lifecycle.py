import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.repository import SQLAlchemyMediaAdminRepository
from juya_admin_api.modules.media.service import MediaAdminService
from juya_admin_api.modules.user_projection.deletion_service import SQLAlchemyDeletionRepository
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def database_url() -> str:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    return url.replace("mysql+pymysql://", "mysql+asyncmy://", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket_status", ["PROCESSING", "RESOLVED"])
async def test_cleanup_persists_callback_retries_and_only_releases_completed_user_screenshots(
    monkeypatch: pytest.MonkeyPatch,
    ticket_status: str,
) -> None:
    url = database_url()
    engine = create_engine(url)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    user, request, event, ticket, other = [new_ulid(now) for _ in range(5)]
    key = f"feedback/{ticket}/deleted.png"
    deleted = []
    calls = []

    class CallbackClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def record_deletion_cleanup_result(
            self, user_id: str, request_id: str, event_id: str
        ) -> None:
            calls.append((user_id, request_id, event_id))
            if len(calls) == 1:
                raise RuntimeError("temporary callback failure")
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE account_deletion_request SET status='DELETED',completed_at=:now "
                        "WHERE public_id=:id"
                    ),
                    {"now": now, "id": request_id},
                )
                await session.execute(
                    text("UPDATE user_account SET status='DELETED' WHERE public_id=:id"),
                    {"id": user_id},
                )

        async def aclose(self) -> None:
            pass

    class Oss:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def delete_object(self, object_key: str) -> None:
            deleted.append(object_key)

    monkeypatch.setattr(maintenance, "MiniappApiClient", CallbackClient)
    monkeypatch.setattr(maintenance, "AliyunOssProvider", Oss)
    settings = Settings(
        environment="test",
        database_url=url,
        internal_hmac_secret="test-secret",
        oss_region="cn-shenzhen",
        oss_bucket="juya-test",
        oss_expected_bucket="juya-test",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
    )
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status) "
                    "VALUES(:id,:number,'DELETING')"
                ),
                {"id": user, "number": "JY" + user[-12:]},
            )
            pk = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO "
                    "account_deletion_request(public_id,user_id,requested_at,effective_at,"
                    "status) VALUES(:id,:user,:now,:now,'DELETING')"
                ),
                {"id": request, "user": pk, "now": now},
            )
            for ticket_id, user_pk in [(ticket, pk), (other, None)]:
                await session.execute(
                    text(
                        "INSERT INTO "
                        "feedback_ticket(public_id,user_id,category,description,source,status,"
                        "sla_hours,create_idempotency_key,created_at,updated_at) "
                        "VALUES(:id,:user,'FUNCTION','test',JSON_OBJECT(),:status,48,:id,"
                        ":now,:now)"
                    ),
                    {
                        "id": ticket_id,
                        "user": user_pk,
                        "now": now,
                        "status": ticket_status if ticket_id == ticket else "PROCESSING",
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO "
                        "feedback_screenshot(ticket_id,object_key,security_status,delete_after) "
                        "SELECT id,:key,'PASSED',:now FROM feedback_ticket WHERE public_id=:id"
                    ),
                    {
                        "id": ticket_id,
                        "key": key if ticket_id == ticket else f"feedback/{other}/ordinary.png",
                        "now": now,
                    },
                )
        repo = SQLAlchemyDeletionRepository(factory)
        first = await repo.cleanup(event, user, now, deletion_request_id=request)
        assert first == await repo.cleanup(event, user, now, deletion_request_id=request)
        with pytest.raises(AppError, match="注销"):
            await repo.cleanup(event, user, now, deletion_request_id=new_ulid(now))
        async with factory() as session:
            rows = (
                await session.execute(
                    text("SELECT payload,status FROM admin_outbox WHERE event_id=:id"),
                    {"id": event},
                )
            ).all()
            assert len(rows) == 1
            payload = json.loads(rows[0].payload)
            assert payload["deletion_request_id"] == request and len(payload["screenshot_ids"]) == 1
            assert rows[0].status == "PENDING"
        assert await maintenance._dispatch_outbox(settings) == {"delivered": 0, "failed": 1}
        assert await maintenance._cleanup_feedback_screenshots(settings) == {
            "deleted": 0,
            "failed": 0,
        }
        assert deleted == []
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT next_attempt_at,attempt_count FROM admin_outbox WHERE event_id=:id"
                    ),
                    {"id": event},
                )
            ).one()
            assert row.attempt_count == 1 and row.next_attempt_at > now.replace(tzinfo=None)
            await session.execute(
                text("UPDATE admin_outbox SET next_attempt_at=:due WHERE event_id=:id"),
                {"id": event, "due": now - timedelta(minutes=1)},
            )
            await session.execute(
                text("UPDATE feedback_screenshot SET delete_after=:due WHERE object_key=:key"),
                {"key": key, "due": now},
            )
        assert await maintenance._dispatch_outbox(settings) == {"delivered": 1, "failed": 0}
        assert await maintenance._cleanup_feedback_screenshots(settings) == {
            "deleted": 1,
            "failed": 0,
        }
        assert deleted == [key]
        assert await maintenance._dispatch_outbox(settings) == {"delivered": 0, "failed": 0}
        assert await maintenance._cleanup_feedback_screenshots(settings) == {
            "deleted": 0,
            "failed": 0,
        }
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM admin_outbox WHERE event_id=:id"), {"id": event}
            )
            await session.execute(
                text("DELETE FROM deletion_cleanup_event WHERE event_id=:id"), {"id": event}
            )
            await session.execute(
                text("DELETE FROM feedback_ticket WHERE public_id IN(:id,:other)"),
                {"id": ticket, "other": other},
            )
            await session.execute(
                text("DELETE FROM audit_event WHERE object_public_id IN(:id,:other)"),
                {"id": ticket, "other": other},
            )
            await session.execute(
                text("DELETE FROM account_deletion_request WHERE public_id=:id"), {"id": request}
            )
            await session.execute(
                text("DELETE FROM user_account WHERE public_id=:id"), {"id": user}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_due_draft_recycle_checks_status_references_and_redelivery() -> None:
    url = database_url()
    engine = create_engine(url)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    store = ProductionStore(factory, require_review=False)
    service = ContentService(SQLAlchemyContentRepository(factory, require_review=False))
    series = await store.create_series("lifecycle test", new_ulid(now), None)
    scenes = []
    trash_ids = []
    try:
        for case in ("expired", "early", "restored", "referenced"):
            scene = await store.create_scene(series["id"], "dialogue")
            scenes.append(scene)
            revision = await service.create_revision(scene, None, "test", now)
            trash = new_ulid(now)
            trash_ids.append(trash)
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO "
                        "draft_trash(public_id,scene_public_id,revision_public_id,status,"
                        "trashed_by,trashed_at,retention_until) "
                        "VALUES(:id,:scene,:revision,:status,'test',:now,:until)"
                    ),
                    {
                        "id": trash,
                        "scene": scene,
                        "revision": revision.id,
                        "status": "RESTORED" if case == "restored" else "TRASHED",
                        "now": now - timedelta(days=31),
                        "until": now + timedelta(days=1)
                        if case == "early"
                        else now - timedelta(days=1),
                    },
                )
                if case == "referenced":
                    await session.execute(
                        text(
                            "INSERT INTO "
                            "preview_config(series_id,scene_id,position,updated_by) SELECT "
                            "series_id,id,1,'test' FROM scene WHERE public_id=:scene"
                        ),
                        {"scene": scene},
                    )
        settings = Settings(environment="test", database_url=url)
        assert await maintenance._cleanup_expired_drafts(settings) == {"cleaned": 1, "protected": 1}
        assert await maintenance._cleanup_expired_drafts(settings) == {"cleaned": 0, "protected": 1}
        media = MediaAdminService(SQLAlchemyMediaAdminRepository(factory))
        with pytest.raises(AppError) as terminal:
            await media.restore_draft(trash_ids[0], actor_id="test", now=now)
        assert terminal.value.code == "TRASH_ENTRY_NOT_ACTIVE"
        async with factory() as session:
            states = (
                (
                    await session.execute(
                        text(
                            "SELECT status FROM draft_trash WHERE scene_public_id "
                            "IN(:s0,:s1,:s2,:s3) "
                            "ORDER BY id"
                        ),
                        dict(zip(("s0", "s1", "s2", "s3"), scenes, strict=True)),
                    )
                )
                .scalars()
                .all()
            )
            assert states == ["CLEANED", "TRASHED", "RESTORED", "TRASHED"]
    finally:
        async with factory() as session, session.begin():
            for scene in scenes:
                await session.execute(
                    text(
                        "DELETE oi FROM preview_config oi JOIN scene s ON s.id=oi.scene_id WHERE "
                        "s.public_id=:id"
                    ),
                    {"id": scene},
                )
                await session.execute(
                    text("DELETE FROM draft_trash WHERE scene_public_id=:id"), {"id": scene}
                )
                await session.execute(
                    text(
                        "UPDATE scene SET draft_revision_id=NULL,published_revision_id=NULL WHERE "
                        "public_id=:id"
                    ),
                    {"id": scene},
                )
                await session.execute(
                    text(
                        "DELETE r FROM scene_revision r JOIN scene s ON s.id=r.scene_id WHERE "
                        "s.public_id=:id"
                    ),
                    {"id": scene},
                )
                await session.execute(text("DELETE FROM scene WHERE public_id=:id"), {"id": scene})
            await session.execute(
                text("DELETE FROM content_series WHERE public_id=:id"), {"id": series["id"]}
            )
            for trash in trash_ids:
                await session.execute(
                    text("DELETE FROM audit_event WHERE object_public_id=:id"), {"id": trash}
                )
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_first", [True, False])
async def test_restore_and_cleanup_race_has_one_consistent_terminal_state(
    restore_first: bool,
) -> None:
    engine = create_engine(database_url())
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    store = ProductionStore(factory, require_review=False)
    content = ContentService(SQLAlchemyContentRepository(factory, require_review=False))
    media = MediaAdminService(SQLAlchemyMediaAdminRepository(factory))
    series = await store.create_series("restore race", new_ulid(now), None)
    scene = await store.create_scene(series["id"], "dialogue")
    revision = await content.create_revision(scene, None, "test", now)
    trash = await media.trash_draft(
        scene, revision.id, actor_id="test", now=now - timedelta(days=31)
    )
    try:
        actions = [media.restore_draft, media.cleanup_draft]
        if not restore_first:
            actions.reverse()
        results = await asyncio.gather(
            *(action(trash.id, actor_id="test", now=now) for action in actions),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert all(
            not isinstance(result, Exception) or isinstance(result, AppError) for result in results
        )
        async with factory() as session:
            state = await session.scalar(
                text("SELECT status FROM draft_trash WHERE public_id=:id"), {"id": trash.id}
            )
            exists = await session.scalar(
                text("SELECT COUNT(*) FROM scene_revision WHERE public_id=:id"), {"id": revision.id}
            )
            assert (state, exists) in {("RESTORED", 1), ("CLEANED", 0)}
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM draft_trash WHERE public_id=:id"), {"id": trash.id}
            )
            await session.execute(
                text("UPDATE scene SET draft_revision_id=NULL WHERE public_id=:id"), {"id": scene}
            )
            await session.execute(
                text("DELETE FROM scene_revision WHERE public_id=:id"), {"id": revision.id}
            )
            await session.execute(text("DELETE FROM scene WHERE public_id=:id"), {"id": scene})
            await session.execute(
                text("DELETE FROM content_series WHERE public_id=:id"), {"id": series["id"]}
            )
            await session.execute(
                text("DELETE FROM audit_event WHERE object_public_id=:id"), {"id": trash.id}
            )
        await engine.dispose()
