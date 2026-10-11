from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.short_reading.store import ShortReadingStore


class Sessions:
    def __init__(self, session):
        self.session = session

    @asynccontextmanager
    async def __call__(self):
        yield self.session


class Session:
    @asynccontextmanager
    async def begin(self):
        yield self


@pytest.mark.asyncio
async def test_replay_returns_original_before_any_current_version_or_media_checks():
    store = ShortReadingStore(Sessions(Session()))
    result = {"content_id": "reading", "revision_id": "revision", "version": 2}
    store.commands.creation_replay = AsyncMock(return_value=result)
    store.row = AsyncMock(side_effect=AssertionError("must not read changed current state"))
    assert (
        await store.command("publish", "reading", "actor", "replay", {"expected_version": 1})
        == result
    )
    store.row.assert_not_awaited()


@pytest.mark.asyncio
async def test_proofreading_fail_closed_and_immutable_hash_is_passed():
    store = ShortReadingStore(Sessions(Session()))
    content = dict(
        title_en="Read",
        title_zh="读",
        category="daily",
        topic="life",
        level="A1",
        safe_intro="safe",
        paragraphs_en=["English"],
        translation_zh="中文",
        copyright_status="CONFIRMED",
        copyright_source="Owned",
    )
    revision = {"content_snapshot": content, "public_id": "r1", "content_hash": "h1"}
    assert await store.inspect(Session(), revision) == ["PROOFREADING_REQUIRED"]
    confirmation = AsyncMock(return_value=True)
    store.check_confirmation = confirmation
    assert await store.inspect(Session(), revision) == []
    confirmation.assert_awaited_once_with(confirmation.call_args.args[0], "r1", "h1")
