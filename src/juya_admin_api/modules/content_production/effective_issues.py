"""Original diagnostics remain evidence; applicability follows current source fields."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def issue_id(issue: Mapping[str, Any]) -> str:
    field = str(issue.get("field", ""))
    locations = [str(v) for v in issue.get("source_locations", [])]
    if field:
        locations.append(field)
    return str(
        issue.get("id")
        or "issue-"
        + digest(
            [issue.get("code", "CONTENT_REVIEW_REQUIRED"), field, locations, issue.get("row_no")]
        )[:32]
    )


def dependency_evidence(snapshot: dict[str, Any], issue: Mapping[str, Any]) -> dict[str, Any]:
    field = str(issue.get("field", ""))
    code = str(issue.get("code", ""))
    if field in {"title_en", "title_zh", "copyright", "source", "copyright_source"}:
        return {field: snapshot.get(field)}
    bilingual = {
        "dialogue": [
            {key: row.get(key) for key in ("id", "speaker", "english", "chinese")}
            for row in snapshot.get("dialogue", [])
        ],
        "paragraphs_en": snapshot.get("paragraphs_en"),
        "paragraph_ids": snapshot.get("paragraph_ids"),
        "translation_zh": snapshot.get("translation_zh"),
    }
    if code.startswith("BILINGUAL_") or field.startswith("dialogue"):
        return bilingual
    if (
        "LEXICON" in code
        or "ENTRY" in code
        or field.startswith(("entries", "vocabulary", "chunks"))
    ):
        fields = (
            "entry_id",
            "english",
            "chinese",
            "explanation",
            "source_id",
            "sense_id",
            "source_sentence",
            "source_sentence_ids",
            "source_locator",
        )
        return bilingual | {
            group: [{key: row.get(key) for key in fields} for row in snapshot.get(group, [])]
            for group in ("vocabulary", "chunks")
        }
    # Unknown diagnostics fail closed on substantive edits. Legitimate listening
    # metadata is deliberately absent from source-comparison dependencies.
    value = json.loads(json.dumps(snapshot))
    if isinstance(value.get("audio"), dict):
        for key in ("listening_confirmed", "confirmed_asset_id", "confirmed_version_id"):
            value["audio"].pop(key, None)
    return dict(value)


def resolved_issue(
    issue: Mapping[str, Any],
    snapshot: dict[str, Any],
    source_hash: str | None,
    resolutions: Sequence[Mapping[str, Any]],
) -> bool:
    expected = digest(dependency_evidence(snapshot, issue))
    return bool(source_hash) and any(
        row["issue_id"] == issue_id(issue)
        and row["source_hash"] == source_hash
        and row.get("dependency_hash") == expected
        for row in resolutions
    )


def effective_history(
    issues: list[dict[str, Any]],
    snapshot: dict[str, Any],
    source_hash: str | None,
    resolutions: Sequence[Mapping[str, Any]],
    *,
    imported: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for issue in issues:
        # Successful import already required explicit conflict/default acknowledgments.
        historical_only = imported and issue.get("severity") == "CONFIRM"
        historical_only |= issue.get("code") == "AUDIO_MISSING" and bool(snapshot.get("audio"))
        resolved = historical_only or resolved_issue(issue, snapshot, source_hash, resolutions)
        result.append(issue | {"id": issue_id(issue), "resolved": resolved})
    return result


async def current_issues_within(
    session: AsyncSession, content_id: str, revision_id: str, snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT public_id,import_item_id,source_hash,historical_issues "
                    "FROM content_work_item "
                    "WHERE content_id=:id FOR SHARE"
                ),
                {"id": content_id},
            )
        )
        .mappings()
        .all()
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        values = row["historical_issues"]
        values = json.loads(values) if isinstance(values, str) else values or []
        resolutions = (
            (
                await session.execute(
                    text(
                        "SELECT issue_id,source_hash,dependency_hash FROM "
                        "content_work_issue_resolution "
                        "WHERE work_item_id=:id FOR SHARE"
                    ),
                    {"id": row["public_id"]},
                )
            )
            .mappings()
            .all()
        )
        result.extend(
            i | {"origin_work_item_id": row["public_id"], "source_hash": row["source_hash"]}
            for i in effective_history(
                values, snapshot, row["source_hash"], [dict(r) for r in resolutions], imported=True
            )
            if not i["resolved"]
        )
    covered = {row["import_item_id"] for row in rows if row["import_item_id"]}
    legacy_rows = (
        (
            await session.execute(
                text(
                    "SELECT public_id,issues FROM content_import_item "
                    "WHERE revision_id=:id OR content_id=:content FOR SHARE"
                ),
                {"id": revision_id, "content": content_id},
            )
        )
        .mappings()
        .all()
    )
    for legacy in legacy_rows:
        if legacy["public_id"] in covered:
            continue
        value = legacy["issues"]
        decoded = json.loads(value) if isinstance(value, str) else value or []
        result.extend(
            i
            for i in effective_history(decoded, snapshot, None, [], imported=True)
            if not i["resolved"]
        )
    if snapshot.get("schema_version") == 2:
        from juya_admin_api.modules.content.production_store import decode
        from juya_admin_api.modules.content.schemas import SceneContent
        from juya_admin_api.modules.content.v15_candidates import dependency

        version = await session.scalar(
            text("SELECT edit_version FROM scene_revision WHERE public_id=:id"), {"id": revision_id}
        )
        actual = dependency(SceneContent.model_validate(snapshot), int(version or 1), "AI_LEXICON")
        candidates = (
            (
                await session.execute(
                    text(
                        "SELECT public_id,input_hash,candidate,status,dismissed_at "
                        "FROM lexicon_candidate WHERE revision_id=:id "
                        "AND JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.kind'))='AI_LEXICON' FOR SHARE"
                    ),
                    {"id": revision_id},
                )
            )
            .mappings()
            .all()
        )
        for candidate in candidates:
            if candidate["dismissed_at"] is not None:
                continue
            payload = decode(candidate["candidate"])
            if actual != (payload.get("decision_input_hash") or candidate["input_hash"]):
                continue
            pending = candidate["status"] in {
                "PENDING",
                "SUBMITTING",
                "SUBMITTED",
                "UNKNOWN",
                "FAILED",
            }
            pending |= any(
                row.get("status", "PENDING") == "PENDING" for row in payload.get("results", [])
            )
            if pending:
                result.append(
                    dict(
                        id=candidate["public_id"],
                        code="LEXICON_CANDIDATE_PENDING",
                        field="entries",
                        message="请采纳或拒绝本篇待处理的推荐词条",
                    )
                )
    return result
