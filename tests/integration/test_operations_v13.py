import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.modules.dashboard.service import SQLAlchemyDashboardRepository
from juya_admin_api.modules.feedback.repository import SQLAlchemyFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyEntitlementQueryRepository,
    SQLAlchemyFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.modules.system_config.service import (
    SQLAlchemySystemConfigRepository,
    SystemConfigService,
)
from juya_admin_api.modules.user_projection.repository import SQLAlchemyUserProjectionRepository

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_operations_filter_before_pagination_and_use_canonical_records() -> None:
    url = os.environ.get("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    assert url.rsplit("/", 1)[-1] == "juya_v13_ops_20261001"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "heads")
    engine = create_engine(url.replace("mysql+pymysql", "mysql+asyncmy"))
    sessions = create_session_factory(engine)
    now = datetime.now(UTC)
    ids = [f"OPS{index:023d}" for index in range(125)]
    try:
        async with sessions() as session, session.begin():
            for index, public_id in enumerate(ids):
                await session.execute(
                    text(
                        "INSERT INTO user_account(public_id,juya_number,status,created_at,"
                        "last_active_at) "
                        "VALUES (:id,:number,'ACTIVE',:now,:active)"
                    ),
                    {
                        "id": public_id,
                        "number": f"OPS{index}",
                        "now": now,
                        "active": now - timedelta(minutes=index),
                    },
                )
                uid = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                await session.execute(
                    text(
                        "INSERT INTO user_profile(user_id,nickname,avatar_object_key) "
                        "VALUES (:uid,:nickname,:avatar)"
                    ),
                    {"uid": uid, "nickname": f"学习者{index}", "avatar": "avatar/test.png"},
                )
                if index >= 120:
                    await session.execute(
                        text(
                            "INSERT INTO user_contact(user_id,contact_status,change_pending) "
                            "VALUES (:uid,'PENDING',1)"
                        ),
                        {"uid": uid},
                    )
        repo = SQLAlchemyUserProjectionRepository(sessions)
        page = await repo.search(None, contact_status="PENDING", page=1, page_size=2)
        assert [user.user_id for user in page] == ids[120:122]
        assert page[0].nickname == "学习者120"
        assert page[0].juya_number == "OPS120"
        assert page[0].change_pending is True
        assert [
            u.user_id
            for u in await repo.search(None, contact_status="PENDING", page=2, page_size=2)
        ] == ids[122:124]
        assert [u.user_id for u in await repo.search("学习者124")] == [ids[124]]
        assert [u.user_id for u in await repo.search("OPS124")] == [ids[124]]
        assert len(await repo.search(None, profile_completeness="COMPLETE", page_size=100)) == 100
        snapshot = await SQLAlchemyDashboardRepository(sessions).snapshot()
        assert snapshot.new_users_today == 125
        assert snapshot.pending_contacts == 5
        async with sessions() as session, session.begin():
            uid = await session.scalar(
                text("SELECT id FROM user_account WHERE public_id=:id"), {"id": ids[124]}
            )
            await session.execute(
                text(
                    "INSERT INTO content_package(public_id,name,status) "
                    "VALUES ('OPS_PACKAGE','运营测试包','ACTIVE')"
                )
            )
            package = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO formal_entitlement(public_id,user_id,package_id,term,"
                    "status,granted_at,expires_at,updated_at) VALUES "
                    "('OPS_FORMAL',:uid,:package,'month_1','ACTIVE',:now,:end,:now)"
                ),
                {"uid": uid, "package": package, "now": now, "end": now + timedelta(days=15)},
            )
            await session.execute(
                text(
                    "INSERT INTO limited_campaign(public_id,name) "
                    "VALUES ('OPS_CAMPAIGN','运营测试活动')"
                )
            )
            campaign = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO limited_campaign_version(public_id,campaign_id,versio"
                    "n_no,duration_days,activation_window_days,capacity) VALUES "
                    "('OPS_CAMPAIGN_VERSION',:campaign,2,3,7,100)"
                ),
                {"campaign": campaign},
            )
            version = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            for status, offset in (("ACTIVE", 124), ("PENDING", 123)):
                await session.execute(
                    text(
                        "INSERT INTO limited_entitlement(public_id,user_id,campaign_versio"
                        "n_id,status,granted_at,start_deadline,activated_at,expires_at,upd"
                        "ated_at) SELECT :id,id,:version,:status,:now,:end,:activated,:exp"
                        "ires,:now FROM user_account WHERE public_id=:user"
                    ),
                    {
                        "id": f"OPS_LIMITED_{status}",
                        "version": version,
                        "status": status,
                        "now": now,
                        "end": now + timedelta(hours=12),
                        "activated": now if status == "ACTIVE" else None,
                        "expires": now + timedelta(hours=12) if status == "ACTIVE" else None,
                        "user": ids[offset],
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO contact_status_history(user_id,status,actor_type,acto"
                    "r_id,occurred_at,note) VALUES "
                    "(:uid,'PENDING','USER',:actor,:now,'CONTACT_CHANGED')"
                ),
                {"uid": uid, "actor": ids[124], "now": now},
            )
            await session.execute(
                text(
                    "INSERT INTO account_deletion_request(public_id,user_id,requested_"
                    "at,effective_at,status) VALUES "
                    "('OPS_DELETE',:uid,:now,:end,'PENDING')"
                ),
                {"uid": uid, "now": now, "end": now + timedelta(days=7)},
            )
            await session.execute(
                text(
                    "INSERT INTO audit_event(public_id,action,object_type,object_publi"
                    "c_id,request_id,after_summary) VALUES "
                    "('OPS_AUDIT','entitlement.grant','formal_entitlement','OPS_FORMAL"
                    "','ops-test',JSON_OBJECT('user_id',:user))"
                ),
                {"user": ids[124]},
            )
        entitlements = SQLAlchemyEntitlementQueryRepository(sessions)
        formal = await entitlements.list_entitlements({"expiry": "EXPIRING"}, 1, 20)
        assert formal["total"] == 1
        assert formal["items"][0]["term"] == "month_1"
        assert formal["items"][0]["juya_number"] == "OPS124"
        for expiry, status in (("ENDING", "ACTIVE"), ("START_EXPIRING", "PENDING")):
            limited = await entitlements.list_entitlements(
                {"expiry": expiry, "campaign_version_id": "OPS_CAMPAIGN_VERSION"}, 1, 20
            )
            assert limited["total"] == 1
            assert limited["items"][0]["status"] == status
            assert limited["items"][0]["campaign_version_no"] == 2
        assert [
            u.user_id
            for u in await repo.search(None, entitlement_type="FORMAL", entitlement_status="ACTIVE")
        ] == [ids[124]]
        assert [
            u.user_id
            for u in await repo.search(
                None, entitlement_type="LIMITED", entitlement_status="PENDING"
            )
        ] == [ids[123]]
        user = await repo.get(ids[124])
        assert user is not None and user.contact_changed_at is not None
        config_service = SystemConfigService(SQLAlchemySystemConfigRepository(sessions))
        assert await config_service.feedback_sla_hours() == 48
        assert await config_service.entitlement_warning_days() == 30
        feedback = FeedbackService(
            SQLAlchemyFeedbackRepository(sessions),
            sla_hours_provider=config_service.feedback_sla_hours,
        )
        ticket = await feedback.create(
            ids[124],
            "CONTENT",
            "字幕问题",
            {"page": "scene", "scene_id": "OPS_SCENE"},
            ["ops-test/screenshot.png"],
            "ops-feedback",
            now,
        )
        await feedback.start_processing(ticket.id, "admin", "ops-start", now)
        await feedback.request_supplement(ticket.id, "复现步骤", "admin", "ops-more", now)
        await feedback.supply(
            ticket.id, "补充步骤", ids[124], "ops-supply", now + timedelta(hours=1)
        )
        page = await feedback.list_admin({"sla": "URGENT"}, 1, 20, now + timedelta(hours=1))
        assert page.total == 1 and page.items[0].source["page"] == "scene"
        assert page.items[0].screenshot_status == "PASSED"
        assert page.items[0].supplied_at == now + timedelta(hours=1)
        records = await repo.records(ids[124])
        assert len(records["formal_entitlements"]) == 1
        assert len(records["limited_entitlements"]) == 1
        assert len(records["feedback"]) == 1
        assert len(records["deletions"]) == 1
        formal_service = FormalEntitlementService(SQLAlchemyFormalEntitlementRepository(sessions))
        for operation in (EntitlementOperation.PAUSE, EntitlementOperation.RESUME):
            await formal_service.apply_operation(
                FormalEntitlementCommand(ids[124], "OPS_PACKAGE", operation, reason="运营回归"),
                "OPS_ADMIN",
                f"ops-{operation}",
                now,
            )
        records = await repo.records(ids[124])
        assert {row["action"] for row in records["audit"]} >= {
            "formal.PAUSE",
            "formal.RESUME",
            "entitlement.grant",
        }
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE formal_entitlement SET status='PAUSED',expires_at=:end "
                    "WHERE public_id='OPS_FORMAL'"
                ),
                {"end": now - timedelta(seconds=1)},
            )
            await session.execute(
                text(
                    "UPDATE limited_entitlement SET expires_at=:end "
                    "WHERE public_id='OPS_LIMITED_ACTIVE'"
                ),
                {"end": now - timedelta(seconds=1)},
            )
        records = await repo.records(ids[124])
        assert records["formal_entitlements"][0]["status"] == "EXPIRED"
        assert records["limited_entitlements"][0]["status"] == "ENDED"
        assert [
            u.user_id
            for u in await repo.search(
                None, entitlement_type="FORMAL", entitlement_status="EXPIRED"
            )
        ] == [ids[124]]
        assert (await entitlements.list_entitlements({"status": "EXPIRED"}, 1, 20))["total"] == 1
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE formal_entitlement SET status='ACTIVE',expires_at=:end "
                    "WHERE public_id='OPS_FORMAL'"
                ),
                {"end": now + timedelta(days=15)},
            )
            await session.execute(
                text(
                    "UPDATE limited_entitlement SET expires_at=:end "
                    "WHERE public_id='OPS_LIMITED_ACTIVE'"
                ),
                {"end": now + timedelta(hours=12)},
            )
        warning = next(
            item
            for item in await config_service.list()
            if item.key == "entitlement_expiry_warning_days"
        )
        warning = await config_service.update(warning.key, {"value": 1}, warning.version, "admin")
        assert (await entitlements.list_entitlements({"expiry": "EXPIRING"}, 1, 20))["total"] == 0
        await config_service.update(warning.key, {"value": 30}, warning.version, "admin")
        today = (now + timedelta(hours=8)).strftime("%Y-%m-%d")
        assert (
            await entitlements.list_entitlements({"date_from": today, "date_to": today}, 1, 20)
        )["total"] == 3
        assert (
            await entitlements.list_entitlements(
                {"date_from": "2025-01-01", "date_to": "2025-01-01"}, 1, 20
            )
        )["total"] == 0
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO content_series(public_id,slug,title) VALUES "
                    "('OPS_SERIES','ops-series','运营测试系列')"
                )
            )
            series = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO content_template(template_type,version,required_modul"
                    "es,validation_rules) VALUES "
                    "('ops-test',777,JSON_OBJECT(),JSON_OBJECT())"
                )
            )
            template = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO open_scene_config(version,activated_at,actor_public_i"
                    "d) VALUES (1,:now,'OPS_ACTOR')"
                ),
                {"now": now},
            )
            config_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            uid = await session.scalar(
                text("SELECT id FROM user_account WHERE public_id=:id"), {"id": ids[124]}
            )
            for index in range(1, 4):
                scene_id = f"OPS_SCENE_{index}"
                await session.execute(
                    text(
                        "INSERT INTO scene(public_id,series_id,template_id,title) VALUES "
                        "(:id,:series,:template,'运营测试场景')"
                    ),
                    {"id": scene_id, "series": series, "template": template},
                )
                sid = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                await session.execute(
                    text(
                        "INSERT INTO open_scene_item(config_id,scene_id,position) VALUES "
                        "(:config,:scene,:position)"
                    ),
                    {"config": config_id, "scene": sid, "position": index},
                )
                await session.execute(
                    text(
                        "INSERT INTO learning_progress(user_id,scene_id,source_type,positi"
                        "on,started_at,completed_at,last_learned_at) VALUES "
                        "(:uid,:scene,'SCENE',JSON_OBJECT(),:now,:now,:now)"
                    ),
                    {"uid": uid, "scene": scene_id, "now": now},
                )
        assert [u.user_id for u in await repo.search(None, cohort="OPEN_WITHOUT_CONTACT")] == [
            ids[124]
        ]
        snapshot = await SQLAlchemyDashboardRepository(sessions).snapshot()
        assert snapshot.open_completed_without_contact == 1
        assert (
            snapshot.limited_pending == snapshot.limited_learning == snapshot.urgent_feedback == 1
        )
        overdue = await feedback.create(
            ids[124], "CONTENT", "更早的超时反馈", {}, [], "ops-overdue", now - timedelta(days=3)
        )
        for index in range(21):
            await feedback.create(
                ids[124],
                "CONTENT",
                "近期将超时反馈",
                {},
                [],
                f"ops-due-{index}",
                now - timedelta(hours=40),
            )
        urgent = await feedback.list_admin({"sla": "URGENT"}, 1, 20, now)
        assert urgent.total == 23
        assert urgent.items[0].id == overdue.id
    finally:
        async with sessions() as session, session.begin():
            for kind in ("formal", "limited"):
                await session.execute(
                    text(
                        f"DELETE o FROM {kind}_entitlement_operation o "
                        f"JOIN {kind}_entitlement e ON e.id=o.entitlement_id "
                        "JOIN user_account u ON u.id=e.user_id WHERE u.public_id LIKE 'OPS%'"
                    )
                )
            for table in (
                "feedback_ticket",
                "limited_entitlement",
                "formal_entitlement",
                "account_deletion_request",
            ):
                await session.execute(
                    text(
                        f"DELETE FROM {table} WHERE user_id IN "
                        "(SELECT id FROM user_account WHERE public_id LIKE 'OPS%')"
                    )
                )
            await session.execute(text("DELETE FROM audit_event WHERE public_id='OPS_AUDIT'"))
            await session.execute(text("DELETE FROM content_package WHERE public_id='OPS_PACKAGE'"))
            await session.execute(
                text("DELETE FROM limited_campaign_version WHERE public_id='OPS_CAMPAIGN_VERSION'")
            )
            await session.execute(
                text("DELETE FROM limited_campaign WHERE public_id='OPS_CAMPAIGN'")
            )
            await session.execute(
                text("DELETE FROM open_scene_config WHERE actor_public_id='OPS_ACTOR'")
            )
            await session.execute(
                text(
                    "DELETE FROM scene WHERE public_id IN "
                    "('OPS_SCENE_1','OPS_SCENE_2','OPS_SCENE_3')"
                )
            )
            await session.execute(text("DELETE FROM content_series WHERE public_id='OPS_SERIES'"))
            await session.execute(
                text("DELETE FROM content_template WHERE template_type='ops-test' AND version=777")
            )
            for public_id in ids:
                await session.execute(
                    text("DELETE FROM user_account WHERE public_id=:id"), {"id": public_id}
                )
        await engine.dispose()
