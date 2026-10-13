"""Ordered human anomaly evidence across a work order's immutable cohorts."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.infrastructure.db.bounded_json import read_json

CandidateIdentity = tuple[str, str, str, str]


def candidate_identity(candidate: dict[str, Any]) -> CandidateIdentity:
    return (
        candidate["content_type"],
        candidate["content_id"],
        candidate["revision_id"],
        candidate["candidate_hash"],
    )


async def unresolved_within(
    session: AsyncSession, work_order_id: str
) -> dict[CandidateIdentity, dict[str, str]]:
    # A reservation alone is not evidence: require its committed set and audit effect.
    # Do not filter command status: the delegate can commit before its reply is lost.
    events = (
        await session.execute(
            text(
                "SELECT a.public_id,a.object_public_id,a.action FROM audit_event a "
                "JOIN proofreading_set s ON s.public_id=a.object_public_id "
                "WHERE a.object_type='proofreading_set' AND "
                "a.action IN ('proofreading.fail','proofreading.confirm') AND EXISTS "
                "(SELECT 1 FROM content_work_command c WHERE c.work_order_id=:order "
                "AND c.action='sampling' AND "
                "JSON_UNQUOTE(JSON_EXTRACT(c.request,'$.delegate_id'))=s.public_id) "
                "ORDER BY a.id"
            ),
            {"order": work_order_id},
        )
    ).mappings()
    unresolved: dict[CandidateIdentity, dict[str, str]] = {}
    legacy_candidates: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        summary = await read_json(
            session, "audit_event", "after_summary", {"public_id": event["public_id"]}
        )
        candidates = summary.get("candidates")
        if candidates is None:
            # Older fail audits contain only revision IDs. Recover only an extant
            # frozen failure; never invent a hash for an already resolved marker.
            set_id = event["object_public_id"]
            if set_id not in legacy_candidates:
                legacy_candidates[set_id] = await read_json(
                    session, "proofreading_set", "candidates", {"public_id": set_id}
                )
            if event["action"] == "proofreading.fail":
                candidates = [
                    c
                    for c in legacy_candidates[set_id]
                    if c["revision_id"] in summary["affected"] and c.get("failure")
                ]
            else:
                candidates = [
                    c | {"candidate_hash": summary["candidate_hash"]}
                    for c in legacy_candidates[set_id]
                    if c["revision_id"] == summary["revision_id"]
                ]
        for candidate in candidates:
            identity = candidate_identity(candidate)
            if event["action"] == "proofreading.confirm":
                unresolved.pop(identity, None)
            else:
                unresolved[identity] = {
                    "error_code": summary["error_code"],
                    "reported_revision": summary.get("reported_revision")
                    or candidate.get("failure", {}).get("reported_revision")
                    or candidate["revision_id"],
                }
    return unresolved
