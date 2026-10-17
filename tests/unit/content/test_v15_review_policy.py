"""Proofreading must respect the same environment review policy as media preparation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("require_review", "status", "expected"),
    [
        (False, "SKIPPED", True),
        (True, "SKIPPED", False),
        (True, "PASSED", True),
        (False, "BLOCKED", False),
        (False, "PENDING", False),
    ],
)
async def test_confirmed_material_uses_environment_review_policy(require_review, status, expected):
    # require_review 为环境审核开关,status 为真实审核事实,expected 为允许进入校对集合的结果
    asset = {
        "status": "CONFIRMED",
        "security_status": status,
        "asset_type": "images",
        "object_key": "sealed/media/images/cover.png",
    }
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: asset))
        )
    )
    store = ProofreadingStore(Mock())
    store.require_review = require_review
    facts = await store.default_facts(
        session,
        "dialogue",
        "revision",
        {
            "title_en": "Hello",
            "title_zh": "你好",
            "copyright": "Owned",
            "source": "Author",
            "cover_asset_id": "cover",
            "dialogue": [{"speaker": "A", "english": "Hi", "chinese": "你好"}],
        },
    )
    assert facts["ready"] is expected and facts["normal"] is expected
