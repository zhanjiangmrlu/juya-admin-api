"""Read active central definitions without replacing supplied content or local identity."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.content.production_store import decode, encode
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def normalized(word: str) -> str:
    return " ".join(word.split()).casefold()


def provided_hash(content: SceneContent) -> str:
    # Confirmation/ASR completion must not recreate already reviewed lexicon choices.
    return snapshot_hash(
        {
            group: [
                {
                    k: row.model_dump()[k]
                    for k in (
                        "entry_id",
                        "english",
                        "chinese",
                        "explanation",
                        "phonetic",
                        "variants",
                        "source_id",
                        "sense_id",
                        "source_sentence_ids",
                        "source_sentence",
                        "source_locator",
                    )
                }
                for row in getattr(content, group)
            ]
            for group in ("vocabulary", "chunks")
        }
        | {
            "dialogue": [
                {
                    "id": line.id,
                    "english": line.english,
                    "chinese": line.chinese,
                    "speaker": line.speaker,
                }
                for line in content.dialogue
            ]
        }
    )


async def active_options(
    session: AsyncSession, entry: SceneEntry, kind: str
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT e.public_id,a.version,a.sense_key,v.content FROM lexicon_entry e "
                    "JOIN lexicon_active_sense a ON a.entry_id=e.public_id JOIN "
                    "lexicon_entry_version v "
                    "ON v.entry_id=a.entry_id AND v.version=a.version WHERE e.entry_type=:kind "
                    "ORDER BY e.public_id,a.version"
                ),
                {"kind": kind},
            )
        )
        .mappings()
        .all()
    )
    result = []
    for row in rows:
        definition = SceneEntry.model_validate(decode(row["content"]))
        exact = normalized(definition.english) == normalized(entry.english)
        if not exact and normalized(entry.english) not in {
            normalized(v) for v in definition.variants
        }:
            continue
        result.append(
            dict(
                lexicon_entry_id=row["public_id"],
                definition_version=row["version"],
                sense_key=row["sense_key"],
                match="exact" if exact else "variant",
                content=definition.model_dump(mode="json"),
                meaning_differs=definition.chinese != entry.chinese,
            )
        )
    return result


async def organize_within(
    session: AsyncSession, revision_id: str, content_id: str, content: SceneContent, actor: str
) -> str | None:
    if content.schema_version != 2 or not (content.vocabulary or content.chunks):
        return None
    digest = provided_hash(content)
    existing = (
        (
            await session.execute(
                text(
                    "SELECT public_id,candidate FROM lexicon_candidate "
                    "WHERE revision_id=:revision AND "
                    "status<>'SUPERSEDED' AND "
                    "(input_hash=:hash OR "
                    "JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.decision_input_hash'))=:hash) "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.kind'))='PROVIDED_LEXICON' "
                    "ORDER BY id DESC LIMIT 1 FOR "
                    "UPDATE"
                ),
                {"revision": revision_id, "hash": digest},
            )
        )
        .mappings()
        .first()
    )
    carried: dict[str, Any] = {}
    options: dict[str, list[dict[str, Any]]] = {}
    if existing:
        carried = {r["entry_id"]: r for r in decode(existing["candidate"])["results"]}
        refresh = False
        for entries, kind in ((content.vocabulary, "VOCABULARY"), (content.chunks, "PHRASE")):
            for entry in entries:
                previous = carried.get(entry.entry_id or "", {})
                if previous.get("status") != "PENDING":
                    continue
                options[entry.entry_id or ""] = await active_options(session, entry, kind)
                refresh |= options[entry.entry_id or ""] != previous.get("central_options", [])
        if not refresh:
            return str(existing["public_id"])
        await session.execute(
            text("UPDATE lexicon_candidate SET status='SUPERSEDED' WHERE public_id=:id"),
            {"id": existing["public_id"]},
        )
    identifier = new_ulid(datetime.now(UTC))
    results = []
    for entries, kind in ((content.vocabulary, "VOCABULARY"), (content.chunks, "PHRASE")):
        for entry in entries:
            previous = carried.get(entry.entry_id or "")
            if previous and previous["status"] != "PENDING":
                results.append(
                    previous
                    | dict(
                        result_id="provided-" + snapshot_hash([identifier, entry.entry_id])[:32],
                        carried_from_candidate=existing["public_id"] if existing else None,
                        carried_from_result=previous["result_id"],
                    )
                )
                continue
            current_options = options.get(entry.entry_id or "")
            results.append(
                dict(
                    result_id="provided-" + snapshot_hash([identifier, entry.entry_id])[:32],
                    status="ADOPTED" if entry.confirmed else "PENDING",
                    entry_id=entry.entry_id,
                    entry_type=kind,
                    english=entry.english,
                    chinese=entry.chinese,
                    provided=entry.model_dump(mode="json"),
                    central_options=current_options
                    if current_options is not None
                    else await active_options(session, entry, kind),
                )
            )
    await session.execute(
        text(
            "INSERT INTO "
            "lexicon_candidate(public_id,content_type,content_id,revision_id,input_hash,"
            "candidate,status) VALUES(:id,'SCENE',:content,:revision,:hash,:payload,'READY')"
        ),
        dict(
            id=identifier,
            content=content_id,
            revision=revision_id,
            hash=digest,
            payload=encode(dict(kind="PROVIDED_LEXICON", results=results, actor=actor)),
        ),
    )
    return identifier


async def activate_legacy_definition(
    session: AsyncSession,
    entry_id: str,
    version: int,
    previous_version: int | None,
    actor: str,
    new_sense: bool = False,
) -> None:
    sense = (
        await session.scalar(
            text(
                "SELECT sense_key FROM lexicon_active_sense WHERE entry_id=:id AND "
                "version=:version FOR UPDATE"
            ),
            {"id": entry_id, "version": previous_version},
        )
        if previous_version
        else None
    )
    sense = new_ulid(datetime.now(UTC)) if new_sense else sense or "legacy-" + entry_id
    await session.execute(
        text(
            "INSERT INTO lexicon_active_sense(entry_id,sense_key,version,updated_by,updated_at) "
            "VALUES(:id,:sense,:version,:actor,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE "
            "version=:version,updated_by=:actor,updated_at=UTC_TIMESTAMP(6)"
        ),
        dict(id=entry_id, sense=sense, version=version, actor=actor),
    )


async def add_definition(
    session: AsyncSession,
    entry: SceneEntry,
    kind: str,
    actor: str,
    action: str,
    option: dict[str, Any] | None,
) -> tuple[str, int]:
    if action not in {"new_sense", "wording"}:
        raise AppError("CENTRAL_ACTION_REQUIRED", "请明确新增义项或修订指定义项", 422)
    root = (
        await session.execute(
            text(
                "SELECT public_id,current_version FROM lexicon_entry WHERE entry_type=:kind AND "
                "(public_id=:id OR normalized_english=:english) FOR UPDATE"
            ),
            dict(
                kind=kind,
                id=option["lexicon_entry_id"] if option else "",
                english=normalized(entry.english),
            ),
        )
    ).first()
    if action == "wording" and not option:
        raise AppError("CENTRAL_SENSE_REQUIRED", "请明确选择要修订的当前义项", 422)
    if root:
        identity, version = root.public_id, root.current_version + 1
    else:
        identity, version = new_ulid(datetime.now(UTC)), 1
        await session.execute(
            text(
                "INSERT INTO lexicon_entry(public_id,entry_type,normalized_english,current_version,"
                "created_by,created_at) VALUES(:id,:kind,:english,1,:actor,UTC_TIMESTAMP(6))"
            ),
            dict(id=identity, kind=kind, english=normalized(entry.english), actor=actor),
        )
    if option:
        valid = await session.scalar(
            text(
                "SELECT sense_key FROM lexicon_active_sense WHERE entry_id=:id AND "
                "version=:version FOR UPDATE"
            ),
            {"id": option["lexicon_entry_id"], "version": option["definition_version"]},
        )
        if not valid or identity != option["lexicon_entry_id"]:
            raise AppError("CENTRAL_DEFINITION_STALE", "中央义项已变化,请重新整理", 409)
    definition = SceneEntry(
        entry_id=identity,
        entry_version=version,
        english=entry.english,
        chinese=entry.chinese,
        phonetic=entry.phonetic,
        explanation=entry.explanation,
        variants=entry.variants,
    )
    await session.execute(
        text(
            "INSERT INTO lexicon_entry_version(entry_id,version,content,created_by,created_at) "
            "VALUES(:id,:version,:content,:actor,UTC_TIMESTAMP(6))"
        ),
        dict(
            id=identity,
            version=version,
            content=encode(definition.model_dump(mode="json")),
            actor=actor,
        ),
    )
    await session.execute(
        text("UPDATE lexicon_entry SET current_version=:version WHERE public_id=:id"),
        dict(id=identity, version=version),
    )
    sense = option["sense_key"] if action == "wording" and option else new_ulid(datetime.now(UTC))
    await session.execute(
        text(
            "INSERT INTO lexicon_active_sense(entry_id,sense_key,version,updated_by,updated_at) "
            "VALUES(:id,:sense,:version,:actor,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE "
            "version=:version,updated_by=:actor,updated_at=UTC_TIMESTAMP(6)"
        ),
        dict(id=identity, sense=sense, version=version, actor=actor),
    )
    return identity, version
