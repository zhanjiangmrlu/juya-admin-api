"""Machine preparation of exact FULL resources, independent of human listening."""

import hashlib
import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.short_reading.schemas import (
    ReadingAudio,
    ReadingContent,
    publication_errors,
)


async def owned_full_chain(
    session: AsyncSession,
    revision_id: str,
    stable_key: str,
    audio: ReadingAudio,
) -> tuple[bool, list[dict[str, Any]]]:
    """Only exact immutable references carried through the recorded same-content source chain."""
    seen: set[str] = set()
    owner: str | None = None
    evidence: list[dict[str, Any]] = []
    fixed = {k: getattr(audio, k) for k in ("asset_id", "version_id", "target_id", "duration_ms")}
    current: str | None = revision_id
    while current and current not in seen:
        seen.add(current)
        row = (
            (
                await session.execute(
                    text(
                        "SELECT short_reading_id,source_revision_id FROM short_reading_revision "
                        "WHERE public_id=:id FOR UPDATE"
                    ),
                    {"id": current},
                )
            )
            .mappings()
            .first()
        )
        if row is None or (owner is not None and row["short_reading_id"] != owner):
            return False, evidence
        owner = str(row["short_reading_id"])
        if current != revision_id:
            source = await read_json(
                session, "short_reading_revision", "content_snapshot", {"public_id": current}
            )
            source_audio = source.get("audio") if isinstance(source, dict) else None
            if not isinstance(source_audio, dict) or any(
                source_audio.get(k) != v for k, v in fixed.items()
            ):
                return False, evidence
        evidence.append(dict(revision_id=current, content_id=owner, audio=fixed))
        if stable_key == f"short-reading:{owner}:{current}:FULL":
            return True, evidence
        current = row["source_revision_id"]
    return False, evidence


async def reading_preparation_facts(
    session: AsyncSession, revision_id: str, snapshot: dict[str, Any], *, require_review: bool
) -> dict[str, Any]:
    content = ReadingContent.model_validate(snapshot)
    errors = [code for code in publication_errors(content) if code != "AUDIO_LISTENING_REQUIRED"]
    evidence: list[dict[str, Any]] = []
    audio = content.audio
    if audio:
        fact = (
            (
                await session.execute(
                    text(
                        "SELECT v.status AS version_status,t.public_"
                        "id AS target_id,t.target_type,t.stable_key,"
                        "m.public_id AS asset_id,m.status,m.secu"
                        "rity_status,m.asset_type,m.duration_ms,"
                        "m.sha256,m.size_bytes,m.object_key FRO"
                        "M audio_version v JOIN audio_target t "
                        "ON t.id=v.target_id JOIN media_asset m ON m.id=v.asset_id "
                        "WHERE v.public_id=:id FOR UPDATE"
                    ),
                    {"id": audio.version_id},
                )
            )
            .mappings()
            .first()
        )
        # The importer intentionally deduplicates a FULL target by immutable file hash.
        # Its fixed reference is safe to reuse; a generic/foreign target is never equivalent.
        imported = bool(fact and fact["stable_key"] == "v15-import:FULL:" + str(fact["sha256"]))
        owned = False
        ownership_evidence: list[dict[str, Any]] = []
        if fact and not imported:
            owned, ownership_evidence = await owned_full_chain(
                session, revision_id, str(fact["stable_key"]), audio
            )
        if (
            not fact
            or fact["target_type"] != "FULL"
            or not (owned or imported)
            or fact["version_status"] not in {"READY", "CONFIRMED", "ACTIVE", "SUPERSEDED"}
            or fact["target_id"] != audio.target_id
            or fact["asset_id"] != audio.asset_id
            or fact["asset_type"] != "audio"
            or fact["status"] != "CONFIRMED"
            or fact["duration_ms"] != audio.duration_ms
            or not re.fullmatch(r"[0-9a-f]{64}", str(fact["sha256"] or ""))
            or not fact["size_bytes"]
            or not str(fact["object_key"]).startswith("sealed/media/")
            or not security_status_usable(fact["security_status"], require_review=require_review)
        ):
            errors.append("AUDIO_RESOURCE_INVALID")
        if fact:
            evidence.append(
                {k: v for k, v in fact.items() if k != "version_status"}
                | {"version_id": audio.version_id}
            )
            if ownership_evidence:
                evidence.append({"ownership_chain": ownership_evidence})
    if content.cover_asset_id:
        cover = (
            (
                await session.execute(
                    text(
                        "SELECT public_id,status,security_status,"
                        "asset_type,sha256,size_bytes,object_key "
                        "FROM media_asset WHERE public_id=:id FOR UPDATE"
                    ),
                    {"id": content.cover_asset_id},
                )
            )
            .mappings()
            .first()
        )
        if (
            not cover
            or cover["status"] != "CONFIRMED"
            or cover["asset_type"] != "images"
            or not cover["size_bytes"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(cover["sha256"] or ""))
            or not str(cover["object_key"]).startswith("sealed/media/")
            or not security_status_usable(cover["security_status"], require_review=require_review)
        ):
            errors.append("COVER_RESOURCE_INVALID")
        if cover:
            evidence.append(dict(cover))
    digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    from juya_admin_api.modules.content_production.preparation_phases import reading_checks

    return dict(
        ready=not errors,
        normal=not errors,
        has_audio=bool(audio),
        has_images=bool(content.cover_asset_id),
        error_codes=errors,
        audio_version_ids=[audio.version_id] if audio else [],
        slice_ids=[],
        preparation_hash=digest,
        phase_checks=reading_checks(errors),
    )
