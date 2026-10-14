"""Only real attempts associated with this order; no global totals or invented costs."""

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, text

from juya_admin_api.integrations.content_ai.aliyun import input_token_upper_bound
from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content.v15_auto_audio import needs_alignment
from juya_admin_api.modules.content.v15_candidates import missing_sources
from juya_admin_api.modules.content_production.schemas import (
    PlannedUsage,
    UsageAttempt,
    WorkOrderUsage,
)

from .list_prices import estimate, provider_region

if TYPE_CHECKING:
    from juya_admin_api.modules.content_production.service import WorkOrderService


def project_plan(
    identifier: str, item_id: str, payload: dict[str, Any], provider: Any, output_cap: int
) -> PlannedUsage:
    """Estimate one already planned current candidate, with explicit conservative coverage."""
    ai = payload.get("kind") == "AI_LEXICON"
    sources = payload.get("sources")
    tokens = (
        input_token_upper_bound(sources) if ai and isinstance(sources, dict) and sources else None
    )
    duration = payload.get("audio", {}).get("duration_ms") if not ai else None
    model = getattr(provider, "model", None)
    region = provider_region(getattr(provider, "base_url", None))
    price = estimate(
        model=model,
        region=region,
        input_tokens=tokens,
        output_tokens=output_cap if ai else None,
        audio_ms=duration,
    )
    price.assumptions.append("仅已规划候选一次调用的保守上界,不是精确Token或全批预计;未计重试")
    return PlannedUsage(
        candidate_id=identifier,
        item_id=item_id,
        kind=payload["kind"],
        model=model,
        provider_region=region,
        input_tokens_upper_bound=tokens,
        output_tokens_upper_bound=output_cap if ai else None,
        measured_audio_ms=duration,
        list_price_estimate=price,
    )


async def usage(service: "WorkOrderService", identity: str) -> WorkOrderUsage:
    async with service.sessions() as session:
        order = await service.row(session, identity)
    items = await service.projection.all(order)
    async with service.sessions() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT DISTINCT a.* FROM content_cloud_attempt a JOIN "
                        "content_work_item_stage s "
                        "ON s.stage_id=a.stage_id JOIN content_work_item i ON "
                        "i.public_id=s.work_item_id "
                        "WHERE i.work_order_id=:id ORDER BY a.created_at,a.public_id"
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .all()
        )
        candidates = (
            (
                await session.execute(
                    text(
                        "SELECT c.public_id,c.candidate FROM lexicon_candidate c "
                        "JOIN content_work_item i ON i.content_id=c.content_id "
                        "WHERE i.work_order_id=:id AND c.status='PENDING' "
                        "AND c.dismissed_at IS NULL ORDER BY c.id"
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .all()
        )
        imported = (
            (
                await session.execute(
                    text(
                        "SELECT c.public_id,c.candidate FROM content_import_item c "
                        "JOIN content_work_item i "
                        "ON i.import_item_id=c.public_id WHERE i.work_order_id=:id"
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .all()
        )
        audio_ids = {
            item.current.content.audio.asset_id
            for item in items
            if item.current and item.current.content.audio
        }
        media_rows = (
            (
                (
                    await session.execute(
                        text(
                            "SELECT public_id,duration_ms,status FROM media_asset "
                            "WHERE public_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": list(audio_ids)},
                    )
                )
                .mappings()
                .all()
            )
            if audio_ids
            else []
        )
    attempts = []
    for row in rows:
        evidence = decode(row["usage"])
        attempts.append(
            UsageAttempt(
                attempt_id=row["public_id"],
                stage_id=row["stage_id"],
                kind=row["kind"],
                attempt_number=row["attempt_number"],
                status=row["status"],
                reserved_units=row["reserved_units"],
                actual_units=row["actual_units"],
                input_tokens=evidence.get("prompt_tokens"),
                output_tokens=evidence.get("completion_tokens"),
                measured_audio_ms=evidence.get("measured_audio_duration_ms"),
                model=evidence.get("model"),
                created_at=row["created_at"],
                provider_region=evidence.get("provider_region"),
                list_price_estimate=estimate(
                    model=evidence.get("model"),
                    region=evidence.get("provider_region"),
                    input_tokens=evidence.get("prompt_tokens"),
                    output_tokens=evidence.get("completion_tokens"),
                    audio_ms=row["actual_units"] if row["kind"] == "ASR_MILLISECONDS" else None,
                ),
            )
        )
    owned = {
        task.stage_id: item.item_id
        for item in items
        for task in item.tasks
        if task.current
        and task.stage in {"AI_LEXICON", "ASR_BOUNDARY"}
        and task.status in {"PENDING", "QUEUED", "RETRY"}
    }
    planned = []
    attempted = {attempt.stage_id for attempt in attempts}
    for row in candidates:
        if row["public_id"] not in owned or row["public_id"] in attempted:
            continue
        payload = decode(row["candidate"])
        if payload.get("kind") not in {"AI_LEXICON", "ASR_BOUNDARY"}:
            continue
        provider = (
            service.candidates.ai if payload["kind"] == "AI_LEXICON" else service.candidates.asr
        )
        planned.append(
            project_plan(
                row["public_id"],
                owned[row["public_id"]],
                payload,
                provider,
                service.candidates.content_ai_max_output_tokens,
            )
        )
    prices = [
        a.list_price_estimate.amount_cny
        for a in attempts
        if a.list_price_estimate and a.list_price_estimate.amount_cny is not None
    ]
    raw_imports = {row["public_id"]: decode(row["candidate"]) for row in imported}
    originals = {
        identifier: value.get("candidate", {}) for identifier, value in raw_imports.items()
    }
    audio_facts = {row["public_id"]: row for row in media_rows}
    source_bytes, source_count, audio_unknown, audio_refs = 0, 0, 0, 0
    durations: dict[str, int] = {}
    potential = []
    for item in items:
        if item.production_status == "cancelled":
            continue
        snapshot = (
            item.current.content.model_dump(mode="json")
            if item.current
            else originals.get(item.import_item_id, {})
        )
        lines = snapshot.get("dialogue", [])
        texts = (
            [str(row.get(field, "")) for row in lines for field in ("english", "chinese")]
            if lines
            else [*snapshot.get("paragraphs_en", []), snapshot.get("translation_zh", "")]
        )
        if any(texts):
            source_count += 1
            source_bytes += sum(len(str(value).encode()) for value in texts)
        audio = item.current.content.audio if item.current else None
        if audio:
            audio_refs += 1
            actual = audio_facts.get(audio.asset_id)
            if (
                actual
                and actual["status"] == "CONFIRMED"
                and actual["duration_ms"] == audio.duration_ms
                and audio.duration_ms > 0
            ):
                durations[audio.asset_id] = audio.duration_ms
            else:
                audio_unknown += 1
        elif not item.current and raw_imports.get(item.import_item_id, {}).get("resources", {}).get(
            "audio_path"
        ):
            audio_refs += 1
            audio_unknown += 1
        if not item.current or item.current.template == "short_reading" or not item.current_draft:
            continue
        body = item.current.content
        active_kinds = {t.stage for t in item.tasks if t.current}
        kinds: list[tuple[str, dict[str, Any], Any]] = []
        if not body.vocabulary and not body.chunks and "AI_LEXICON" not in active_kinds:
            sources = missing_sources(body)
            if sources:
                kinds.append(("AI_LEXICON", {"sources": sources}, service.candidates.ai))
        if needs_alignment(body) and not active_kinds & {"ASR_BOUNDARY", "ASR_POLL"}:
            duration = durations.get(body.audio.asset_id) if body.audio else None
            kinds.append(
                ("ASR_BOUNDARY", {"audio": {"duration_ms": duration}}, service.candidates.asr)
            )
        for kind, payload, provider in kinds:
            estimate_row = project_plan(
                "potential-" + item.item_id + "-" + kind,
                item.item_id,
                payload | {"kind": kind},
                provider,
                service.candidates.content_ai_max_output_tokens,
            )
            estimate_row.candidate_id = None
            estimate_row.planning_status = "potential"
            estimate_row.list_price_estimate.assumptions.append(
                "当前源内容条件的潜在一次调用上界,尚未创建云任务;词条推荐仍需管理员选择"
            )
            potential.append(estimate_row)
    return WorkOrderUsage(
        work_order_id=identity,
        attempts=attempts,
        unknown_count=sum(a.status == "UNKNOWN" for a in attempts),
        retry_count=sum(a.attempt_number > 1 for a in attempts),
        planned=planned,
        known_list_price_subtotal_cny=format(sum(map(Decimal, prices), Decimal(0)), "f")
        if prices
        else None,
        unpriced_attempt_count=len(attempts) - len(prices),
        potential=potential,
        source_text_utf8_bytes=source_bytes if source_count else None,
        source_text_count=source_count,
        source_unknown_count=sum(i.production_status != "cancelled" for i in items) - source_count,
        measured_audio_ms=sum(durations.values()) if durations else None,
        audio_reference_count=audio_refs,
        audio_unknown_count=audio_unknown,
    )
