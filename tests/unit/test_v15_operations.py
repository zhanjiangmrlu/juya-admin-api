from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from juya_admin_api.modules.campaigns.domain import CampaignVersion
from juya_admin_api.modules.campaigns.router import CampaignSaveRequest, _save_fields
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    InMemoryFormalEntitlementRepository,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.shared.errors import AppError


def test_capacity_null_is_unlimited_and_missing_is_distinct():
    CampaignVersion("v", "c", "DRAFT", 3, 3, None, 5, ()).validate()
    assert "capacity" not in _save_fields(CampaignSaveRequest(name="活动"))
    assert _save_fields(CampaignSaveRequest(name="活动", capacity=None))["capacity"] is None
    with pytest.raises(ValidationError):
        CampaignSaveRequest(name="活动", capacity=0)


@pytest.mark.asyncio
async def test_preview_is_true_before_after_and_stale_replay_is_safe():
    service = FormalEntitlementService(InMemoryFormalEntitlementRepository())
    now = datetime(2026, 1, 31, tzinfo=UTC)
    command = FormalEntitlementCommand(
        "u", "p", EntitlementOperation.GRANT, EntitlementTerm.MONTH_1, expected_version=0
    )
    preview = await service.preview_details(command, now)
    assert preview["before"] is None
    assert preview["actual_operation"] == "GRANT"
    assert preview["expected_version"] == 0
    result = await service.apply_operation(command, "a", "first", now)
    assert await service.apply_operation(command, "a", "first", now) == result
    with pytest.raises(AppError) as caught:
        await service.apply_operation(command, "a", "other", now)
    assert caught.value.code == "ENTITLEMENT_VERSION_CONFLICT"
    renewal = FormalEntitlementCommand(
        "u", "p", EntitlementOperation.GRANT, EntitlementTerm.MONTH_1, expected_version=1
    )
    preview = await service.preview_details(renewal, now)
    assert preview["actual_operation"] == "RENEW"
    assert preview["before"]["version"] == 1
    assert preview["after"]["version"] == 2


def test_short_reading_counts_do_not_become_scene_completions():
    from juya_admin_api.modules.analytics.events import AnalyticsEvent, aggregate_events

    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = aggregate_events(
        [AnalyticsEvent("r", "SHORT_READING_COMPLETED", now, "ALL", {})], now.date()
    )
    assert result[("SHORT_READING_COMPLETIONS", "ALL")] == 1
    assert ("SCENE_COMPLETIONS", "ALL") not in result


def test_ocr_has_no_registered_task_or_dispatch_entry():
    from juya_admin_api.infrastructure.tasks.celery_app import create_celery_app
    from juya_admin_api.modules.media.tasks import (
        CeleryMediaTaskDispatcher,
        PersistentMediaTaskService,
    )

    assert not hasattr(CeleryMediaTaskDispatcher, "enqueue_ocr")
    assert not hasattr(PersistentMediaTaskService, "run_ocr")
    app = create_celery_app()
    assert "juya.content.ocr.process" not in app.tasks
    assert "content.ocr" not in {queue.name for queue in app.conf.task_queues}


@pytest.mark.asyncio
async def test_ocr_config_is_hidden_and_cannot_be_updated():
    from juya_admin_api.modules.system_config.service import (
        InMemorySystemConfigRepository,
        SystemConfigService,
    )

    service = SystemConfigService(
        InMemorySystemConfigRepository(
            {"ocr_provider": ({"value": "baidu"}, 1), "feedback_sla_hours": ({"value": 48}, 1)}
        )
    )
    assert [item.key for item in await service.list()] == ["feedback_sla_hours"]
    with pytest.raises(AppError) as caught:
        await service.update("ocr_provider", {"value": "baidu"}, 1, "a")
    assert caught.value.code == "OCR_RETIRED"


@pytest.mark.asyncio
async def test_deletion_removes_snapshot_retention_and_short_reading_progress():
    from juya_admin_api.modules.user_projection.deletion_service import InMemoryDeletionRepository

    repository = InMemoryDeletionRepository()
    repository.user_favorites["u"] = {"f1"}
    repository.favorite_references = {"f1", "other"}
    repository.favorite_projections = {"f1", "other"}
    repository.short_reading_progress = {"u", "other"}
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await repository.cleanup("e", "u", now)
    assert repository.favorite_references == {"other"}
    assert repository.favorite_projections == {"other"}
    assert repository.short_reading_progress == {"other"}


@pytest.mark.asyncio
async def test_unlimited_capacity_grants_and_unique_count_survive_replay():
    from juya_admin_api.modules.limited_entitlements.repository import (
        InMemoryLimitedEntitlementRepository,
    )

    repository = InMemoryLimitedEntitlementRepository()
    repository.campaign_versions["v"] = CampaignVersion("v", "c", "OPEN", 3, 3, None, 0, ())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = await repository.grant("u", "v", "a", "k", now)
    assert await repository.grant("u", "v", "a", "k", now) == first
    await repository.grant("other", "v", "a", "k2", now)
    assert repository.campaign_versions["v"].granted_user_count == 2


@pytest.mark.asyncio
async def test_limited_remedy_preview_stale_version_and_replay_payload():
    from juya_admin_api.modules.limited_entitlements.domain import RemedyMode
    from juya_admin_api.modules.limited_entitlements.repository import (
        InMemoryLimitedEntitlementRepository,
    )
    from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService

    repository = InMemoryLimitedEntitlementRepository()
    repository.campaign_versions["v"] = CampaignVersion("v", "c", "OPEN", 3, 3, None, 0, ())
    service = LimitedEntitlementService(repository)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    current = await repository.grant("u", "v", "a", "g", now)
    mode = RemedyMode.EXTEND_START_DEADLINE
    preview = service.preview_details(current, "REMEDY", mode, now)
    assert preview["before"]["version"] == 1
    assert preview["after"]["version"] == 2
    applied = await service.apply_prepared(
        current.id, "REMEDY", "a", "k", now, expected_version=1, reason="启动补救", mode=mode
    )
    assert (
        await service.apply_prepared(
            current.id, "REMEDY", "a", "k", now, expected_version=1, reason="启动补救", mode=mode
        )
        == applied
    )
    with pytest.raises(AppError) as caught:
        await service.apply_prepared(
            current.id,
            "REMEDY",
            "a",
            "other",
            now,
            expected_version=1,
            reason="启动补救",
            mode=mode,
        )
    assert caught.value.code == "ENTITLEMENT_VERSION_CONFLICT"
    with pytest.raises(AppError) as caught:
        await service.apply_prepared(
            current.id,
            "REMEDY",
            "a",
            "k",
            now,
            expected_version=1,
            reason="启动补救",
            mode=RemedyMode.RESTORE_START_WINDOW,
        )
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_formal_available_operations_dry_run_keeps_reason_required_commands():
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock

    from juya_admin_api.modules.formal_entitlements.repository import (
        SQLAlchemyEntitlementQueryRepository,
    )

    now = datetime.now(UTC)
    session = AsyncMock()
    session.__aenter__.return_value = session
    row = {
        "id": "e",
        "user_id": "u",
        "package_id": "p",
        "package_name": "内容包",
        "status": "ACTIVE",
        "term": "month_1",
        "granted_at": now,
        "expires_at": now + timedelta(days=30),
        "version": 1,
    }
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    session.execute.return_value = result
    repository = SQLAlchemyEntitlementQueryRepository(MagicMock(return_value=session))
    detail = await repository.get_formal("e")
    assert detail is not None
    assert detail["available_operations"] == ["RENEW", "PAUSE", "REVOKE"]
    row["status"] = "PAUSED"
    detail = await repository.get_formal("e")
    assert detail is not None
    assert detail["available_operations"] == ["RENEW", "RESUME", "REVOKE"]
