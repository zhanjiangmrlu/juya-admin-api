"""Per-content stable identities; confirmed meanings and sources never merge by spelling."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content_import.parser import canonical_hash
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def details(entry: SceneEntry) -> dict[str, Any]:
    return entry.model_dump(exclude={"confirmed", "meaning_change"})


def source_identity(entry: SceneEntry) -> tuple[Any, ...]:
    locator = entry.source_locator or {}
    if isinstance(locator, str):
        locator = {"reference": locator}
    stable = {
        key: value
        for key, value in locator.items()
        if key
        in {
            "sentence_id",
            "document",
            "document_id",
            "document_path",
            "block",
            "block_id",
            "paragraph",
            "paragraph_id",
            "paragraph_index",
            "reference",
        }
    }
    return tuple(entry.source_sentence_ids), canonical_hash(stable)


async def save_entries_within(
    session: AsyncSession,
    content_id: str,
    revision_id: str,
    content: SceneContent,
    baseline_revision_id: str | None = None,
    import_keys: dict[tuple[str, int], str] | None = None,
    actor: str = "",
) -> SceneContent:
    kind = "word_card" if content.template_type in {"word_card", "vocabulary"} else "dialogue"
    result = content.model_copy(deep=True)
    now = datetime.now(UTC)
    for group, entry_type in ((result.vocabulary, "word"), (result.chunks, "chunk")):
        for index, entry in enumerate(group):
            previous = None
            latest_version = 0
            if entry.entry_id:
                row = (
                    await session.execute(
                        text(
                            "SELECT content_type,content_id,entry_type FROM content_entry "
                            "WHERE public_id=:id FOR UPDATE"
                        ),
                        {"id": entry.entry_id},
                    )
                ).first()
                if row is None or tuple(row) != (kind, content_id, entry_type):
                    raise AppError("ENTRY_IDENTITY_CONFLICT", "条目标识不属于当前内容及类型", 409)
                previous = (
                    await session.execute(
                        text(
                            "SELECT details,details_hash,confirmed_by,lexicon_entry_id,"
                            "lexicon_entry_version FROM content_entry_revision "
                            "WHERE entry_id=:id AND revision_id=:baseline LIMIT 1 "
                            "FOR UPDATE"
                        ),
                        {"id": entry.entry_id, "baseline": baseline_revision_id or revision_id},
                    )
                ).first()
                if previous is None:
                    raise AppError(
                        "ENTRY_BASELINE_CONFLICT", "条目不属于当前草稿或明确复制版本", 409
                    )
                latest_version = int(
                    await session.scalar(
                        text(
                            "SELECT COALESCE(MAX(entry_version),0) FROM content_entry_revision "
                            "WHERE entry_id=:id"
                        ),
                        {"id": entry.entry_id},
                    )
                    or 0
                )
            else:
                entry.entry_id = new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO content_entry(public_id,content_type,content_id,entry_type,"
                        "sense_id,source_id) VALUES(:id,:type,:content,:kind,'','')"
                    ),
                    {"id": entry.entry_id, "type": kind, "content": content_id, "kind": entry_type},
                )
            old = SceneEntry.model_validate(
                json.loads(previous.details)
                if previous and isinstance(previous.details, str)
                else previous.details
                if previous
                else {}
            )
            if previous and old.chinese != entry.chinese and entry.meaning_change is None:
                raise AppError(
                    "MEANING_CHANGE_CONFIRMATION_REQUIRED", "请选择措辞修订或新义项", 409
                )
            definition_changed = previous is not None and any(
                getattr(old, field) != getattr(entry, field)
                for field in ("english", "chinese", "explanation", "phonetic", "variants")
            )
            central = previous.lexicon_entry_id if previous and not definition_changed else None
            central_version = previous.lexicon_entry_version if previous and central else None
            replacement = previous is not None and (
                entry.meaning_change == "new_sense"
                or source_identity(old) != source_identity(entry)
            )
            replacement_reason = (
                "new_sense" if entry.meaning_change == "new_sense" else "source_target"
            )
            if replacement:
                entry.entry_id = new_ulid(now)
                entry.sense_id = entry.source_id = ""
                entry.pronunciation_target_id = None
                await session.execute(
                    text(
                        "INSERT INTO content_entry(public_id,content_type,content_id,entry_type,"
                        "sense_id,source_id) "
                        "VALUES(:id,:type,:content,:kind,'','')"
                    ),
                    dict(id=entry.entry_id, type=kind, content=content_id, kind=entry_type),
                )
                previous, latest_version = None, 0
                central, central_version = None, None
            new_sense = not previous or entry.meaning_change == "new_sense"
            if new_sense:
                entry.sense_id = new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO content_sense(public_id,entry_id,meaning_zh,confirmed_by,"
                        "confirmed_at) VALUES(:id,:entry,:meaning,'',NULL)"
                    ),
                    {"id": entry.sense_id, "entry": entry.entry_id, "meaning": entry.chinese},
                )
            else:
                if entry.sense_id and entry.sense_id != old.sense_id:
                    raise AppError("SENSE_IDENTITY_CONFLICT", "义项标识不能直接替换", 409)
                entry.sense_id = old.sense_id
            source_hash = canonical_hash(
                {
                    "locator": entry.source_locator,
                    "sentence": entry.source_sentence,
                    "sentences": entry.source_sentence_ids,
                }
            )
            if not previous:
                entry.source_id = new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO content_source(public_id,content_type,content_id,entry_id,"
                        "source_fingerprint,locator) "
                        "VALUES(:id,:type,:content,:entry,:hash,:locator)"
                    ),
                    {
                        "id": entry.source_id,
                        "type": kind,
                        "content": content_id,
                        "entry": entry.entry_id,
                        "hash": source_hash,
                        "locator": json.dumps(entry.source_locator or {}, ensure_ascii=False),
                    },
                )
            else:
                if entry.source_id and entry.source_id != old.source_id:
                    raise AppError("SOURCE_IDENTITY_CONFLICT", "来源标识不能直接替换", 409)
                entry.source_id = old.source_id
            # Control fields supplied by clients cannot manufacture confirmation.
            entry.entry_version = old.entry_version if previous else 1
            same = previous is not None and details(entry) == details(old)
            if previous and not same:
                entry.entry_version = latest_version + 1
                entry.pronunciation_target_id = None
            entry.confirmed = bool(same and previous and previous.confirmed_by)
            entry.meaning_change = None
            digest = canonical_hash(details(entry))
            await session.execute(
                text(
                    "INSERT INTO "
                    "content_entry_revision(public_id,entry_id,revision_id,entry_version,"
                    "sense_id,source_id,details,details_hash,confirmed_by,confirmed_at,"
                    "lexicon_entry_id,lexicon_entry_version) "
                    "VALUES(:id,:entry,:revision,:version,:sense,:source,:details,:hash,:actor,"
                    "IF(:actor='',NULL,UTC_TIMESTAMP(6)),:central,:central_version) ON DUPLICATE "
                    "KEY UPDATE "
                    "entry_version=VALUES(entry_version),sense_id=VALUES(sense_id),"
                    "source_id=VALUES(source_id),details=VALUES(details),"
                    "details_hash=VALUES(details_hash),"
                    "confirmed_by=VALUES(confirmed_by),confirmed_at=VALUES(confirmed_at),"
                    "lexicon_entry_id=VALUES(lexicon_entry_id),"
                    "lexicon_entry_version=VALUES(lexicon_entry_version)"
                ),
                {
                    "central": central,
                    "central_version": central_version,
                    "id": new_ulid(now),
                    "entry": entry.entry_id,
                    "revision": revision_id,
                    "version": entry.entry_version,
                    "sense": entry.sense_id,
                    "source": entry.source_id,
                    "details": json.dumps(details(entry), ensure_ascii=False),
                    "hash": digest,
                    "actor": previous.confirmed_by if entry.confirmed and previous else "",
                },
            )
            await session.execute(
                text(
                    "UPDATE content_entry SET sense_id=:sense,source_id=:source,"
                    "lexicon_entry_id=:central,"
                    "lexicon_entry_version=:central_version WHERE public_id=:id"
                ),
                {
                    "sense": entry.sense_id,
                    "source": entry.source_id,
                    "id": entry.entry_id,
                    "central": central,
                    "central_version": central_version,
                },
            )
            if not previous:
                external = (import_keys or {}).get((entry_type, index)) if not replacement else None
                external = external or new_ulid(now)
                provenance = (
                    "replacement" if replacement else "imported" if import_keys else "generated"
                )
                await session.execute(
                    text(
                        "UPDATE content_entry SET import_namespace='juya-content-library',"
                        "import_entry_id=:external,identity_provenance=:provenance WHERE "
                        "public_id=:id"
                    ),
                    dict(external=external, provenance=provenance, id=entry.entry_id),
                )
                await session.execute(
                    text(
                        "INSERT INTO import_entry_mapping(namespace,content_type,content_id,"
                        "import_entry_id,"
                        "entry_id,sense_id,source_id,entry_type,source_fingerprint,input_hash) "
                        "VALUES('juya-content-library',:type,:content,:external,:entry,:sense,"
                        ":source,:kind,:fingerprint,:hash)"
                    ),
                    dict(
                        type=kind,
                        content=content_id,
                        external=external,
                        entry=entry.entry_id,
                        sense=entry.sense_id,
                        source=entry.source_id,
                        kind=entry_type,
                        fingerprint=canonical_hash([entry.source_locator, entry.source_sentence]),
                        hash=digest,
                    ),
                )
                if replacement:
                    from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore

                    await ProofreadingStore(async_sessionmaker(session.bind)).audit(
                        session,
                        revision_id,
                        actor,
                        "entry.identity.replace",
                        dict(
                            reason=replacement_reason,
                            old_entry_version=old.entry_version,
                            new_entry_version=entry.entry_version,
                            old_entry_id=old.entry_id,
                            new_entry_id=entry.entry_id,
                            old_sense_id=old.sense_id,
                            new_sense_id=entry.sense_id,
                            old_source_id=old.source_id,
                            new_source_id=entry.source_id,
                            namespace="juya-content-library",
                            import_entry_id=external,
                        ),
                    )
            group[index] = entry
    return result


async def entry_check_within(
    session: AsyncSession, revision_id: str, content: SceneContent
) -> bool:
    for entry in [*content.vocabulary, *content.chunks]:
        row = (
            await session.execute(
                text(
                    "SELECT details_hash,confirmed_by,sense_id,source_id FROM "
                    "content_entry_revision "
                    "WHERE entry_id=:entry AND revision_id=:revision FOR SHARE"
                ),
                {"entry": entry.entry_id, "revision": revision_id},
            )
        ).first()
        if (
            not row
            or not row.confirmed_by
            or not entry.confirmed
            or row.details_hash != canonical_hash(details(entry))
            or (row.sense_id, row.source_id) != (entry.sense_id, entry.source_id)
        ):
            return False
    return True
