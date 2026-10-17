import json
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.infrastructure.db.bounded_json import CHUNK_CHARS, read_json


@pytest.mark.asyncio
async def test_complete_unicode_json_is_reassembled_in_bounded_fragments():
    value = {"items": [{"id": i, "text": "学习😀𠀀" * 50} for i in range(1000)]}
    encoded = json.dumps(value, ensure_ascii=False)
    session = AsyncMock()
    session.scalar.side_effect = [len(encoded)] + [
        encoded[index : index + CHUNK_CHARS] for index in range(0, len(encoded), CHUNK_CHARS)
    ]
    assert await read_json(session, "publication_range", "results", {"public_id": "r"}) == value
    for call in session.scalar.call_args_list:
        assert "FOR SHARE" in str(call.args[0])
        if "length" in call.args[1]:
            assert call.args[1]["length"] == CHUNK_CHARS


@pytest.mark.asyncio
async def test_unsupported_identifier_or_identity_never_reaches_database():
    session = AsyncMock()
    for table, column, identity in [
        ("publication_range;DROP TABLE user_account", "results", {"public_id": "r"}),
        ("publication_range", "secret", {"public_id": "r"}),
        ("publication_range", "results", {"actor_id": "x"}),
    ]:
        with pytest.raises(ValueError):
            await read_json(session, table, column, identity)
    session.scalar.assert_not_called()
