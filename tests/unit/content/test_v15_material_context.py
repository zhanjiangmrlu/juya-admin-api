from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.content.v15_materials import LegacyMaterialService


@pytest.mark.asyncio
async def test_preview_restores_context_without_inventing_old_marker():
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    service = LegacyMaterialService(SimpleNamespace(sessions=session, stages=None), None, None)
    service._batch = AsyncMock(
        return_value={
            "status": "PREVIEW_READY",
            "input_hash": "hash",
            "series_id": "SERIES-ACTUAL",
            "content_type": "short_reading",
            "manifest": '{"resources":[],"vocabulary_rows":[]}',
        }
    )
    preview = await service.preview("old-batch")
    assert preview["series_id"] == "SERIES-ACTUAL"
    assert preview["content_type"] == "short_reading"
    assert "has_vocabulary_table" not in preview["manifest"]
