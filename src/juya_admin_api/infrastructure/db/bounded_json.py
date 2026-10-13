"""Read complete JSON via bounded UTF-8 character packets under one row lock.

Large JSON values remain complete in MySQL. Splitting SELECT response values avoids
asyncmy's receive-buffer export/resize failure on large Windows packets; no driver patch.
"""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Identifiers are exclusively server-owned constants. Values always use bind parameters.
JSON_COLUMNS = {
    "audit_event": frozenset({"after_summary"}),
    "content_import_item": frozenset({"candidate", "issues", "result"}),
    "scene_revision": frozenset({"content_snapshot"}),
    "short_reading_revision": frozenset({"content_snapshot"}),
    "content_work_item": frozenset({"source_snapshot", "historical_issues"}),
    "content_work_command": frozenset({"request", "result"}),
    "proofreading_set": frozenset({"candidates", "sample_ids"}),
    "content_import_batch": frozenset({"manifest"}),
    "publication_range": frozenset({"candidates", "results"}),
    "idempotency_record": frozenset({"response_body"}),
}
IDENTITY_COLUMNS = {
    "audit_event": frozenset({"public_id"}),
    "content_import_item": frozenset({"public_id"}),
    "scene_revision": frozenset({"public_id"}),
    "short_reading_revision": frozenset({"public_id"}),
    "content_work_item": frozenset({"public_id"}),
    "content_work_command": frozenset({"public_id"}),
    "proofreading_set": frozenset({"public_id"}),
    "content_import_batch": frozenset({"public_id"}),
    "publication_range": frozenset({"public_id"}),
    "idempotency_record": frozenset({"scope", "actor_id", "idempotency_key"}),
}
CHUNK_CHARS = 8192  # At most 32KiB UTF-8 payload, including four-byte characters.


async def read_json(
    session: AsyncSession, table: str, column: str, identity: Mapping[str, object]
) -> Any:
    if (
        table not in JSON_COLUMNS
        or column not in JSON_COLUMNS[table]
        or set(identity) != IDENTITY_COLUMNS[table]
    ):
        raise ValueError("Unsupported bounded JSON identity")
    params: dict[str, object] = {}
    conditions = []
    for index, (name, value) in enumerate(sorted(identity.items())):
        parameter = f"key_{index}"
        conditions.append(f"`{name}`=:{parameter}")
        params[parameter] = value
    where = " AND ".join(conditions)
    value = f"CAST(`{column}` AS CHAR CHARACTER SET utf8mb4)"
    # The current row stays shared-locked for the caller's transaction; writers cannot mix chunks.
    size = await session.scalar(
        text(f"SELECT CHAR_LENGTH({value}) FROM `{table}` WHERE {where} FOR SHARE"), params
    )
    if size is None:
        return None
    fragments: list[str] = []
    for offset in range(1, int(size) + 1, CHUNK_CHARS):
        part = await session.scalar(
            text(
                f"SELECT SUBSTRING({value},:offset,:length) FROM `{table}` WHERE {where} FOR SHARE"
            ),
            {**params, "offset": offset, "length": CHUNK_CHARS},
        )
        if not isinstance(part, str):
            raise ValueError("Bounded JSON fragment unavailable")
        fragments.append(part)
    return json.loads("".join(fragments))
