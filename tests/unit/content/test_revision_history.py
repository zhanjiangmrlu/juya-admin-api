from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.content.domain import Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.service import ContentService


@pytest.mark.asyncio
async def test_complete_history_is_paginated_and_copy_keeps_entire_snapshot_isolated():
    # 功能:验证完整历史分页且复制保留整个独立快照。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repo = InMemoryContentRepository()
    repo.scenes["s"] = Scene("s", "series", published_revision_id="old")
    for n in range(55):
        repo.revisions[str(n)] = SceneRevision(
            str(n), "s", None, status="SUPERSEDED", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    old = SceneRevision(
        "old",
        "s",
        None,
        status="PUBLISHED",
        content={
            "title_en": "Old",
            "dialogue": [{"id": "stable", "english": "Hello"}],
            "audio": {"version_id": "a1"},
        },
    )
    repo.revisions["old"] = old
    service = ContentService(repo)
    history = await service.list_revision_history("s", page=2, page_size=20)
    assert history["total"] == 56
    assert len(history["items"]) == 20
    draft = await service.create_revision("s", "old", "admin", datetime.now(UTC))
    draft.content["dialogue"][0]["english"] = "Edited"
    assert old.content["dialogue"][0]["english"] == "Hello"
    assert draft.content["audio"] == {"version_id": "a1"}
    assert repo.scenes["s"].published_revision_id == "old"
