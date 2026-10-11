"""Conservative pre-backfill for BFF-owned per-source favorite snapshots."""

import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.ids import new_ulid

SOURCE_SQL = """
SELECT fs.id AS legacy_source_id, fs.favorite_id AS legacy_favorite_id,
 f.user_id,f.entry_type,f.entry_stable_id,f.favorited_at,f.last_reviewed_at,
 fs.scene_id,fs.sentence_snapshot,fs.source_locator,fs.revision_id,
 fs.entry_snapshot, r.version_no,r.content_snapshot,
 e.normalized_english,e.phonetic,e.chinese_text,e.explanation,u.status AS user_status
FROM favorite_source fs JOIN favorite_entry f ON f.id=fs.favorite_id
JOIN user_account u ON u.id=f.user_id
LEFT JOIN scene s ON s.public_id=fs.scene_id
LEFT JOIN scene_revision r ON r.public_id=fs.revision_id AND r.scene_id=s.id
LEFT JOIN scene_entry e ON e.revision_id=r.id AND e.stable_id=f.entry_stable_id
LEFT JOIN favorite_legacy_map m ON m.legacy_source_id=fs.id
WHERE fs.id>:after AND m.id IS NULL ORDER BY fs.id LIMIT :limit
"""


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def build_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """Use saved source data or its exact archived revision, never current text identity."""
    snapshot = _object(row.get("entry_snapshot"))
    content = _object(row.get("content_snapshot"))
    missing: list[str] = ["source_id"]

    def saved(name: str, fallback: str, *aliases: str) -> str:
        for key in (name, *aliases):
            value = snapshot.get(key)
            if isinstance(value, str) and value:
                return value
        value = row.get(fallback)
        if isinstance(value, str) and value:
            return value
        missing.append(name)
        return ""

    english = saved("english", "normalized_english")
    meaning = saved("meaning_zh", "chinese_text", "chinese")
    phonetic = saved("phonetic", "phonetic")
    explanation = saved("explanation", "explanation")
    sentence = row.get("sentence_snapshot") or ""
    if not sentence:
        missing.append("source_sentence")
    revision = row.get("revision_id") if row.get("version_no") is not None else ""
    version = int(row["version_no"]) if revision else None
    if not revision:
        missing.extend(["revision_id", "saved_content_version"])
    locator = None
    raw = str(row.get("source_locator") or "").split("|", 1)[-1]
    match = re.fullmatch(r"sentence:([^:]+):entry:([^:]+)", raw)
    dialogue = content.get("dialogue", [])
    if (
        match
        and match.group(2) == row.get("entry_stable_id")
        and any(isinstance(part, dict) and part.get("id") == match.group(1) for part in dialogue)
    ):
        locator = {"sentence_id": match.group(1), "entry_id": match.group(2)}
    else:
        missing.append("source_locator")
    source_id = f"legacy-source:{row['legacy_source_id']}"
    sense = snapshot.get("sense_id")
    if not isinstance(sense, str) or not sense:
        missing.append("sense_id")
    # Only an exact, proven archive plus stable entry identity can fill absent resources.
    archived = []
    if revision:
        archived = [
            part
            for group in ("vocabulary", "chunks")
            for part in (content.get(group) or [])
            if isinstance(part, dict)
            and (part.get("entry_id") or part.get("stable_id")) == row["entry_stable_id"]
        ]
    historical = archived[0] if len(archived) == 1 else {}

    def fixed(value: Any) -> str | None:
        return value if isinstance(value, str) and 0 < len(value) <= 64 else None

    image = fixed(snapshot.get("image_asset_id") or snapshot.get("icon_asset_id"))
    if not image:
        image = fixed(historical.get("image_asset_id") or historical.get("icon_asset_id"))
    # Audio target/version are one historical binding, never joined across snapshots.
    audio_source = (
        snapshot
        if any(
            snapshot.get(key)
            for key in (
                "pronunciation_target_id",
                "resource_version",
                "pronunciation",
                "audio_target_id",
                "audio_version_id",
            )
        )
        else historical
    )
    reference = _object(audio_source.get("pronunciation"))
    pronunciation = fixed(audio_source.get("pronunciation_target_id") or reference.get("target_id"))
    resource_version = fixed(
        audio_source.get("resource_version")
        or reference.get("resource_version")
        or (audio_source.get("audio_version_id") if pronunciation else None)
    )
    resources = {
        "image_asset_id": image,
        "pronunciation_target_id": pronunciation,
        "resource_version": resource_version,
    }
    missing.extend(name for name, value in resources.items() if value is None)
    return {
        **resources,
        "entry_id": row["entry_stable_id"],
        "sense_id": sense if isinstance(sense, str) and sense else source_id,
        "source_id": source_id,
        "content_type": "SCENE",
        "content_id": row["scene_id"],
        "saved_content_version": version,
        "revision_id": revision or None,
        "english": english,
        "meaning_zh": meaning,
        "phonetic": phonetic,
        "explanation": explanation,
        "source_sentence": sentence,
        "source_locator": locator,
        "safe_title_en": str(content.get("title_en") or "")[:200],
        "safe_title_zh": str(content.get("title_zh") or "")[:200],
        "legacy_incomplete": bool(missing),
        "missing_fields": missing,
    }


class FavoriteBackfill:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def run(self, *, execute: bool = False, batch_size: int = 100) -> dict[str, Any]:
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        after = 0
        counts: Counter[str] = Counter()
        missing: Counter[str] = Counter()
        while True:
            async with self.sessions() as session, session.begin():
                rows = (
                    (await session.execute(text(SOURCE_SQL), {"after": after, "limit": batch_size}))
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                for row in rows:
                    after = int(row["legacy_source_id"])
                    counts["scanned"] += 1
                    if row["user_status"] != "ACTIVE":
                        counts["inactive_user_skipped"] += 1
                        continue
                    saved = build_snapshot(dict(row))
                    missing.update(saved["missing_fields"])
                    counts["incomplete" if saved["legacy_incomplete"] else "complete"] += 1
                    if execute:
                        if await self._insert(session, dict(row), saved):
                            counts["inserted"] += 1
                        else:
                            counts["concurrent_replay"] += 1
            # Each bounded page has committed independently; retry safely starts at zero.
        return {"execute": execute, "counts": dict(counts), "missing_fields": dict(missing)}

    async def _insert(
        self, session: AsyncSession, row: Mapping[str, Any], saved: dict[str, Any]
    ) -> bool:
        # Serialize with BFF source cancellation; recheck existence after taking its lock.
        source = await session.scalar(
            text("SELECT id FROM favorite_source WHERE id=:id FOR UPDATE"),
            {"id": row["legacy_source_id"]},
        )
        if source is None:
            return False
        mapped = await session.scalar(
            text("SELECT id FROM favorite_legacy_map WHERE legacy_source_id=:id FOR UPDATE"),
            {"id": source},
        )
        if mapped is not None:
            return False
        now = datetime.now(UTC)
        favorite_public = new_ulid(now)
        revision_public = new_ulid(now)
        params = {
            **saved,
            "public": favorite_public,
            "user": row["user_id"],
            "type": row["entry_type"],
            "at": row["favorited_at"],
            "reviewed": row["last_reviewed_at"],
        }
        await session.execute(
            text(
                "INSERT INTO favorite_snapshot(public_id,user_id,entry_type,entry_id,"
                "sense_id,source_id,favorited_at,last_reviewed_at) VALUES(:public,"
                ":user,:type,:entry_id,:sense_id,:source_id,:at,:reviewed)"
            ),
            params,
        )
        favorite_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
        params.update(
            {
                "favorite": favorite_id,
                "revision": revision_public,
                "locator": json.dumps(saved["source_locator"]) if saved["source_locator"] else None,
                "missing": json.dumps(saved["missing_fields"]),
            }
        )
        await session.execute(
            text(
                "INSERT INTO favorite_snapshot_revision(public_id,favorite_id,"
                "snapshot_version,content_type,content_id,saved_content_version,"
                "revision_id,english,phonetic,meaning_zh,explanation,source_sentence,"
                "source_locator,safe_title_en,safe_title_zh,legacy_incomplete,"
                "missing_fields,image_asset_id,pronunciation_target_id,resource_version) "
                "VALUES(:revision,:favorite,1,:content_type,"
                ":content_id,:saved_content_version,:revision_id,:english,:phonetic,"
                ":meaning_zh,:explanation,:source_sentence,:locator,:safe_title_en,"
                ":safe_title_zh,:legacy_incomplete,:missing,:image_asset_id,"
                ":pronunciation_target_id,:resource_version)"
            ),
            params,
        )
        await session.execute(
            text(
                "UPDATE favorite_snapshot SET current_snapshot_revision_id=:revision "
                "WHERE id=:favorite"
            ),
            params,
        )
        await session.execute(
            text(
                "INSERT INTO favorite_legacy_map(legacy_favorite_id,legacy_source_id,"
                "favorite_id,snapshot_revision_id,migration_status) VALUES(:legacy,"
                ":source,:favorite,:revision,:status)"
            ),
            {
                "legacy": row["legacy_favorite_id"],
                "source": source,
                "favorite": favorite_id,
                "revision": revision_public,
                "status": "INCOMPLETE" if saved["legacy_incomplete"] else "COMPLETE",
            },
        )
        return True
