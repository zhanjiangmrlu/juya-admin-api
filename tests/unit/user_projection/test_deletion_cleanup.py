from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.user_projection.deletion_service import (
    DeletionCleanupService,
    InMemoryDeletionRepository,
)

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_deletion_cleanup_is_idempotent_and_removes_user_links() -> None:
    repository = InMemoryDeletionRepository()
    repository.seed_user(
        "user-1",
        formal_entitlements={"formal-1"},
        limited_entitlements={"limited-1"},
        feedback_ids={"feedback-1"},
        screenshot_keys={"feedback/user-1/shot.png"},
    )
    service = DeletionCleanupService(repository)

    first = await service.cleanup("event-1", "user-1", NOW)
    replay = await service.cleanup("event-1", "user-1", NOW)

    assert first is replay
    assert first.status == "COMPLETED"
    assert repository.formal_entitlements["formal-1"] == "REVOKED"
    assert repository.limited_entitlements["limited-1"] == "REVOKED"
    assert repository.feedback_users["feedback-1"] is None
    assert repository.screenshot_deletions == {"feedback/user-1/shot.png"}
    assert repository.audit_subjects["user-1"] == "ANONYMIZED"
