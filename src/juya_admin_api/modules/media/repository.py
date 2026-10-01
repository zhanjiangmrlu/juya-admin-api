import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.access_policy.service import AccessPolicyService
from juya_admin_api.modules.media.domain import (
    AudioTarget,
    AudioVersion,
    BatchJob,
    BatchJobItem,
    OcrCandidate,
    ProcessingJob,
    TrashEntry,
)
from juya_admin_api.modules.media.service import MediaAsset
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class SQLAlchemyMediaRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_object_key(self, object_key: str) -> MediaAsset | None:
        async with self._session_factory() as session:
            identifier = await session.scalar(
                text("SELECT public_id FROM media_asset WHERE object_key=:key"), {"key": object_key}
            )
        return await self.get(str(identifier)) if identifier else None

    async def bind_fixed_object(self, asset: MediaAsset, object_key: str) -> MediaAsset:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE media_asset SET object_key=:fixed WHERE public_id=:id AND "
                    "object_key=:old AND sha256=:sha"
                ),
                {"fixed": object_key, "id": asset.id, "old": asset.object_key, "sha": asset.sha256},
            )
        saved = await self.get(asset.id)
        if saved is None or not saved.object_key.startswith("sealed/media/"):
            raise AppError("MEDIA_ASSET_CHANGED", "素材固定引用冲突", 409)
        return saved

    async def get(self, asset_id: str) -> MediaAsset | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT public_id, object_key, asset_type, content_type, "
                        "size_bytes, sha256, "
                        "status, security_status, created_by, created_at, "
                        "width, height, duration_ms, security_request_id "
                        "FROM media_asset WHERE public_id=:asset_id"
                    ),
                    {"asset_id": asset_id},
                )
            ).first()
        return None if row is None else _from_row(row)

    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT public_id, object_key, asset_type, content_type, "
                        "size_bytes, sha256, status, security_status, created_by, created_at, "
                        "width, height, duration_ms, security_request_id "
                        "FROM media_asset WHERE asset_type = :asset_type AND sha256 = :sha256"
                    ),
                    {"asset_type": asset_type, "sha256": sha256},
                )
            ).first()
        return None if row is None else _from_row(row)

    async def update_security(self, asset: MediaAsset) -> MediaAsset:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE media_asset SET status=:status, security_status=:security_status, "
                    "security_request_id=:security_request_id, content_type=:content_type, "
                    "size_bytes=:size, width=:width, height=:height, duration_ms=:duration_ms "
                    "WHERE public_id=:public_id"
                ),
                {
                    "status": asset.status,
                    "security_status": asset.security_status,
                    "security_request_id": asset.security_request_id,
                    "public_id": asset.id,
                    "content_type": asset.content_type,
                    "size": asset.size,
                    "width": asset.width,
                    "height": asset.height,
                    "duration_ms": asset.duration_ms,
                },
            )
        return asset

    async def save(self, asset: MediaAsset) -> MediaAsset:
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO media_asset "
                        "(public_id, object_key, asset_type, content_type, size_bytes, sha256, "
                        "status, security_status, created_by, created_at, width, height, "
                        "duration_ms, security_request_id) VALUES "
                        "(:public_id, :object_key, :asset_type, :content_type, :size_bytes, "
                        ":sha256, :status, :security_status, :created_by, :created_at, "
                        ":width, :height, :duration_ms, :security_request_id)"
                    ),
                    {
                        "width": asset.width,
                        "height": asset.height,
                        "duration_ms": asset.duration_ms,
                        "security_request_id": asset.security_request_id,
                        "public_id": asset.id,
                        "object_key": asset.object_key,
                        "asset_type": asset.asset_type,
                        "content_type": asset.content_type,
                        "size_bytes": asset.size,
                        "sha256": asset.sha256,
                        "status": asset.status,
                        "security_status": asset.security_status,
                        "created_by": asset.created_by,
                        "created_at": asset.created_at,
                    },
                )
        except IntegrityError:
            existing = await self.get_by_hash(asset.asset_type, asset.sha256)
            if existing is None:
                raise
            return existing
        return asset


class SQLAlchemyMediaAdminRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_batch_with_items(self, batch: BatchJob, items: list[BatchJobItem]) -> BatchJob:
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO batch_job(public_id,job_type,business_key,status,total_count,"
                        "success_count,failure_count,created_by,created_at,updated_at,"
                        "input_payload,"
                        "result_payload) VALUES(:id,:kind,:key,'PENDING',:total,0,0,:actor,"
                        ":now,:now,"
                        ":input,JSON_OBJECT())"
                    ),
                    {
                        "id": batch.id,
                        "kind": batch.job_type,
                        "key": batch.business_key,
                        "total": batch.total_count,
                        "actor": batch.created_by,
                        "now": batch.created_at,
                        "input": json.dumps(batch.input_payload, ensure_ascii=False),
                    },
                )
                internal_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                for item in items:
                    await session.execute(
                        text(
                            "INSERT INTO batch_job_item(public_id,batch_job_id,item_key,"
                            "target_id,status,"
                            "attempt_count,updated_at) VALUES(:id,:batch,:key,:target,'PENDING',"
                            "0,:now)"
                        ),
                        {
                            "id": item.id,
                            "batch": internal_id,
                            "key": item.item_key,
                            "target": item.target_id,
                            "now": item.updated_at,
                        },
                    )
        except IntegrityError:
            existing = await self.get_batch_by_business_key(batch.business_key)
            if existing is None:
                raise
            return existing
        return batch

    async def claim_batch(
        self, batch_id: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE batch_job SET "
                    "status='RUNNING',updated_at=:now,lease_token=:token,lease_expires_at=:expiry "
                    "WHERE public_id=:id AND (status='PENDING' OR (status='RUNNING' AND "
                    "COALESCE(lease_expires_at,DATE_ADD(updated_at,INTERVAL 5 MINUTE))<=:now))"
                ),
                {
                    "id": batch_id,
                    "now": now,
                    "token": lease_token or new_ulid(now),
                    "expiry": now + timedelta(minutes=5),
                },
            )
            if getattr(result, "rowcount", 0) != 1:
                return False
            row = (
                await session.execute(
                    text(
                        "SELECT id,cancel_requested_at FROM batch_job WHERE public_id=:id FOR "
                        "UPDATE"
                    ),
                    {"id": batch_id},
                )
            ).first()
            assert row is not None
            if row.cancel_requested_at:
                items = (
                    await session.execute(
                        text(
                            "SELECT item_key,status FROM batch_job_item WHERE batch_job_id=:id "
                            "FOR UPDATE"
                        ),
                        {"id": row.id},
                    )
                ).all()
                current = await session.scalar(
                    text("SELECT result_payload FROM batch_job WHERE id=:id"), {"id": row.id}
                )
                results = _json_payload(current)
                for item in items:
                    if item.status not in {"RUNNING", "PENDING"}:
                        continue
                    receipt_payload = None
                    if item.status == "RUNNING":
                        receipt_payload = await session.scalar(
                            text(
                                "SELECT result_payload FROM batch_operation_receipt WHERE "
                                "operation_key=:key FOR UPDATE"
                            ),
                            {"key": f"{batch_id}:{item.item_key}"},
                        )
                    succeeded = receipt_payload is not None
                    status = (
                        "SUCCEEDED"
                        if succeeded
                        else "FAILED"
                        if item.status == "RUNNING"
                        else "CANCELLED"
                    )
                    code = "BATCH_INTERRUPTED_CANCELLED" if status == "FAILED" else None
                    payload = _json_payload(receipt_payload) if succeeded else {}
                    version = payload.get("version")
                    await session.execute(
                        text(
                            "UPDATE batch_job_item SET "
                            "status=:status,error_code=:code,result_version=:version,"
                            "updated_at=:now WHERE batch_job_id=:id AND item_key=:key"
                        ),
                        {
                            "id": row.id,
                            "key": item.item_key,
                            "status": status,
                            "code": code,
                            "version": version if isinstance(version, int) else None,
                            "now": now,
                        },
                    )
                    results[item.item_key] = {
                        "status": status,
                        "error_code": code,
                        "result": payload,
                    }
                counts = (
                    await session.execute(
                        text(
                            "SELECT SUM(status='SUCCEEDED') AS success,SUM(status='FAILED') AS "
                            "failure FROM batch_job_item WHERE batch_job_id=:id"
                        ),
                        {"id": row.id},
                    )
                ).one()
                await session.execute(
                    text(
                        "UPDATE batch_job SET "
                        "status='CANCELLED',completed_at=:now,success_count=:success,"
                        "failure_count=:failure,result_payload=:result WHERE id=:id"
                    ),
                    {
                        "id": row.id,
                        "now": now,
                        "success": counts.success,
                        "failure": counts.failure,
                        "result": json.dumps(results),
                    },
                )
            else:
                await session.execute(
                    text(
                        "UPDATE batch_job_item SET status='PENDING',updated_at=:now WHERE "
                        "batch_job_id=:id AND status='RUNNING'"
                    ),
                    {"id": row.id, "now": now},
                )
            return True

    async def heartbeat_batch(self, batch_id: str, lease_token: str, now: datetime) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE batch_job SET lease_expires_at=:expiry WHERE public_id=:id AND "
                    "lease_token=:token AND status='RUNNING'"
                ),
                {"id": batch_id, "token": lease_token, "expiry": now + timedelta(minutes=5)},
            )
            return getattr(result, "rowcount", 0) == 1

    async def list_recoverable_batches(self, now: datetime, limit: int = 100) -> list[str]:
        async with self._session_factory() as session:
            rows: Any = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id FROM batch_job WHERE status='PENDING' OR "
                            "(status='RUNNING' AND "
                            "COALESCE(lease_expires_at,DATE_ADD(updated_at,INTERVAL 5 "
                            "MINUTE))<=:now) ORDER BY updated_at LIMIT :limit"
                        ),
                        {"now": now, "limit": limit},
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    async def finish_claimed_batch_item(
        self,
        batch_id: str,
        item_key: str,
        lease_token: str,
        result: dict[str, object],
        error_code: str | None,
        now: datetime,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            batch = (
                await session.execute(
                    text(
                        "SELECT id,lease_token,cancel_requested_at,result_payload,total_count "
                        "FROM batch_job WHERE public_id=:id FOR UPDATE"
                    ),
                    {"id": batch_id},
                )
            ).first()
            if batch is None or batch.lease_token != lease_token:
                return False
            version = result.get("version")
            changed = await session.execute(
                text(
                    "UPDATE batch_job_item SET "
                    "status=:status,error_code=:error,result_version=:version,updated_at=:now "
                    "WHERE batch_job_id=:id AND item_key=:key AND status='RUNNING'"
                ),
                {
                    "id": batch.id,
                    "key": item_key,
                    "status": "FAILED" if error_code else "SUCCEEDED",
                    "error": error_code,
                    "version": version if isinstance(version, int) else None,
                    "now": now,
                },
            )
            if getattr(changed, "rowcount", 0) != 1:
                return False
            results = _json_payload(batch.result_payload)
            results[item_key] = {
                "status": "FAILED" if error_code else "SUCCEEDED",
                "error_code": error_code,
                "result": result,
            }
            counts = (
                await session.execute(
                    text(
                        "SELECT SUM(status='SUCCEEDED') AS success,SUM(status='FAILED') AS "
                        "failure,SUM(status IN ('SUCCEEDED','FAILED','CANCELLED')) AS terminal "
                        "FROM batch_job_item WHERE batch_job_id=:id"
                    ),
                    {"id": batch.id},
                )
            ).one()
            completed = counts.terminal == batch.total_count
            status = (
                "RUNNING"
                if not completed
                else "CANCELLED"
                if batch.cancel_requested_at
                else "COMPLETED_WITH_ERRORS"
                if counts.failure
                else "COMPLETED"
            )
            await session.execute(
                text(
                    "UPDATE batch_job SET "
                    "status=:status,success_count=:success,failure_count=:failure,"
                    "result_payload=:results,updated_at=:now,completed_at=:completed WHERE id=:id"
                ),
                {
                    "id": batch.id,
                    "status": status,
                    "success": counts.success,
                    "failure": counts.failure,
                    "results": json.dumps(results, ensure_ascii=False),
                    "now": now,
                    "completed": now if completed else None,
                },
            )
            return True

    async def claim_batch_item(
        self, batch_id: str, item_key: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            batch = (
                await session.execute(
                    text(
                        "SELECT id,cancel_requested_at,lease_token FROM batch_job WHERE "
                        "public_id=:id "
                        "FOR UPDATE"
                    ),
                    {"id": batch_id},
                )
            ).first()
            if (
                batch is None
                or batch.cancel_requested_at
                or (lease_token is not None and batch.lease_token != lease_token)
            ):
                return False
            result = await session.execute(
                text(
                    "UPDATE batch_job_item SET status='RUNNING', attempt_count=attempt_count+1, "
                    "updated_at=:now WHERE batch_job_id=:id AND item_key=:key AND status='PENDING'"
                ),
                {"id": batch.id, "key": item_key, "now": now},
            )
            return bool(getattr(result, "rowcount", 0) == 1)

    async def cancel_pending_batch_items(self, batch_id: str, now: datetime) -> None:
        async with self._session_factory() as session, session.begin():
            batch = await session.scalar(
                text("SELECT id FROM batch_job WHERE public_id=:id FOR UPDATE"), {"id": batch_id}
            )
            await session.execute(
                text("UPDATE batch_job SET cancel_requested_at=:now WHERE id=:id"),
                {"id": batch, "now": now},
            )
            await session.execute(
                text(
                    "UPDATE batch_job_item SET status='CANCELLED', updated_at=:now "
                    "WHERE batch_job_id=:id AND status='PENDING'"
                ),
                {"id": batch, "now": now},
            )

    async def claim_job(self, job_id: str, now: datetime) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE processing_job SET status='RUNNING', updated_at=:now "
                    "WHERE public_id=:job_id AND status='PENDING'"
                ),
                {"job_id": job_id, "now": now},
            )
            return bool(getattr(result, "rowcount", 0) == 1)

    async def get_job_by_business_key(self, business_key: str) -> ProcessingJob | None:
        return await self._get_job("j.business_key = :value", business_key)

    async def get_job(self, job_id: str) -> ProcessingJob | None:
        return await self._get_job("j.public_id = :value", job_id)

    async def _get_job(self, condition: str, value: str) -> ProcessingJob | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT j.public_id, j.business_key, j.job_type, j.target_id, "
                        "b.public_id AS batch_public_id, j.status, j.provider_request_id, "
                        "j.error_code, j.created_by, j.created_at, j.updated_at, "
                        "j.cancel_requested_at, j.input_payload FROM processing_job j "
                        "LEFT JOIN batch_job b ON b.id = j.batch_job_id WHERE " + condition
                    ),
                    {"value": value},
                )
            ).first()
        return None if row is None else _processing_job_from_row(row)

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        try:
            async with self._session_factory() as session, session.begin():
                existing_id = await session.scalar(
                    text("SELECT id FROM processing_job WHERE public_id = :public_id"),
                    {"public_id": job.id},
                )
                batch_id = None
                if job.batch_id is not None:
                    batch_id = await session.scalar(
                        text("SELECT id FROM batch_job WHERE public_id = :public_id"),
                        {"public_id": job.batch_id},
                    )
                values = {
                    "public_id": job.id,
                    "business_key": job.business_key,
                    "job_type": job.job_type,
                    "target_id": job.target_id,
                    "batch_job_id": batch_id,
                    "status": job.status,
                    "provider_request_id": job.provider_request_id,
                    "error_code": job.error_code,
                    "created_by": job.created_by,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "cancel_requested_at": job.cancel_requested_at,
                    "input_payload": json.dumps(
                        job.input_payload, ensure_ascii=False, separators=(",", ":")
                    ),
                }
                if existing_id is None:
                    await session.execute(
                        text(
                            "INSERT INTO processing_job "
                            "(public_id, business_key, job_type, target_id, batch_job_id, status, "
                            "provider_request_id, error_code, created_by, created_at, updated_at, "
                            "cancel_requested_at, input_payload) VALUES "
                            "(:public_id, :business_key, :job_type, "
                            ":target_id, :batch_job_id, :status, :provider_request_id, "
                            ":error_code, :created_by, :created_at, :updated_at, "
                            ":cancel_requested_at, :input_payload)"
                        ),
                        values,
                    )
                else:
                    await session.execute(
                        text(
                            "UPDATE processing_job SET status = :status, "
                            "provider_request_id = :provider_request_id, error_code = :error_code, "
                            "updated_at = :updated_at, cancel_requested_at = :cancel_requested_at "
                            "WHERE id = :id"
                        ),
                        values | {"id": existing_id},
                    )
        except IntegrityError:
            existing = await self.get_job_by_business_key(job.business_key)
            if existing is None:
                raise
            return existing
        return job

    async def get_ocr_candidate_by_job(self, job_id: str) -> OcrCandidate | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT c.public_id, j.public_id AS job_public_id, "
                        "a.public_id AS asset_public_id, c.business_key, "
                        "c.provider_request_id, c.status, c.template_type, "
                        "c.structured_candidate, c.confidence, c.error_code, c.created_at, "
                        "r.public_id AS confirmed_revision_public_id, c.confirmed_by, "
                        "c.confirmed_at FROM ocr_candidate c "
                        "JOIN processing_job j ON j.id = c.processing_job_id "
                        "JOIN media_asset a ON a.id = c.asset_id "
                        "LEFT JOIN scene_revision r ON r.id = c.confirmed_revision_id "
                        "WHERE j.public_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            ).first()
        return None if row is None else _ocr_candidate_from_row(row)

    async def save_ocr_candidate(self, candidate: OcrCandidate) -> OcrCandidate:
        try:
            async with self._session_factory() as session, session.begin():
                job_id = await session.scalar(
                    text("SELECT id FROM processing_job WHERE public_id = :public_id"),
                    {"public_id": candidate.job_id},
                )
                asset_id = await session.scalar(
                    text("SELECT id FROM media_asset WHERE public_id = :public_id"),
                    {"public_id": candidate.asset_id},
                )
                if job_id is None:
                    raise AppError("MEDIA_JOB_NOT_FOUND", "媒体任务不存在", 404)
                if asset_id is None:
                    raise AppError("MEDIA_ASSET_NOT_FOUND", "媒体素材不存在", 404)
                revision_id = None
                if candidate.confirmed_revision_id is not None:
                    revision_id = await session.scalar(
                        text("SELECT id FROM scene_revision WHERE public_id = :public_id"),
                        {"public_id": candidate.confirmed_revision_id},
                    )
                await session.execute(
                    text(
                        "INSERT INTO ocr_candidate "
                        "(public_id, asset_id, business_key, provider_request_id, status, "
                        "template_type, structured_candidate, confidence, error_code, created_at, "
                        "processing_job_id, confirmed_revision_id, confirmed_by, confirmed_at) "
                        "VALUES (:public_id, :asset_id, :business_key, :provider_request_id, "
                        ":status, :template_type, :structured_candidate, :confidence, :error_code, "
                        ":created_at, :processing_job_id, :confirmed_revision_id, :confirmed_by, "
                        ":confirmed_at) ON DUPLICATE KEY UPDATE status = VALUES(status), "
                        "confirmed_revision_id = VALUES(confirmed_revision_id), "
                        "confirmed_by = VALUES(confirmed_by), confirmed_at = VALUES(confirmed_at)"
                    ),
                    {
                        "public_id": candidate.id,
                        "asset_id": asset_id,
                        "business_key": candidate.business_key,
                        "provider_request_id": candidate.provider_request_id,
                        "status": candidate.status,
                        "template_type": candidate.template_type,
                        "structured_candidate": json.dumps(
                            candidate.structured_candidate,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "confidence": candidate.confidence,
                        "error_code": candidate.error_code,
                        "created_at": candidate.created_at,
                        "processing_job_id": job_id,
                        "confirmed_revision_id": revision_id,
                        "confirmed_by": candidate.confirmed_by,
                        "confirmed_at": candidate.confirmed_at,
                    },
                )
        except IntegrityError:
            existing = await self.get_ocr_candidate_by_job(candidate.job_id)
            if existing is None:
                raise
            return existing
        return candidate

    async def get_batch_by_business_key(self, business_key: str) -> BatchJob | None:
        return await self._get_batch("business_key = :value", business_key)

    async def get_batch(self, batch_id: str) -> BatchJob | None:
        return await self._get_batch("public_id = :value", batch_id)

    async def list_batches(self) -> list[BatchJob]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT public_id, business_key, job_type, status, total_count, "
                        "success_count, failure_count, created_by, created_at, updated_at, "
                        "completed_at, cancel_requested_at, input_payload, result_payload, "
                        "lease_token, lease_expires_at "
                        "FROM batch_job "
                        "ORDER BY created_at DESC"
                    )
                )
            ).all()
        return [_batch_job_from_row(row) for row in rows]

    async def _get_batch(self, condition: str, value: str) -> BatchJob | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT public_id, business_key, job_type, status, total_count, "
                        "success_count, failure_count, created_by, created_at, updated_at, "
                        "completed_at, cancel_requested_at, input_payload, result_payload, "
                        "lease_token, lease_expires_at "
                        "FROM batch_job WHERE " + condition
                    ),
                    {"value": value},
                )
            ).first()
        return None if row is None else _batch_job_from_row(row)

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        try:
            async with self._session_factory() as session, session.begin():
                existing_id = await session.scalar(
                    text("SELECT id FROM batch_job WHERE public_id = :public_id"),
                    {"public_id": batch.id},
                )
                values = {
                    "public_id": batch.id,
                    "business_key": batch.business_key,
                    "job_type": batch.job_type,
                    "status": batch.status,
                    "total_count": batch.total_count,
                    "success_count": batch.success_count,
                    "failure_count": batch.failure_count,
                    "created_by": batch.created_by,
                    "created_at": batch.created_at,
                    "updated_at": batch.updated_at,
                    "completed_at": batch.completed_at,
                    "cancel_requested_at": batch.cancel_requested_at,
                    "input_payload": json.dumps(batch.input_payload, ensure_ascii=False),
                    "result_payload": json.dumps(batch.result_payload, ensure_ascii=False),
                }
                if existing_id is None:
                    await session.execute(
                        text(
                            "INSERT INTO batch_job (public_id, job_type, business_key, status, "
                            "total_count, success_count, failure_count, created_by, created_at, "
                            "updated_at, completed_at, cancel_requested_at, input_payload, "
                            "result_payload) VALUES "
                            "(:public_id, :job_type, :business_key, :status, :total_count, "
                            ":success_count, :failure_count, :created_by, :created_at, "
                            ":updated_at, :completed_at, :cancel_requested_at, :input_payload, "
                            ":result_payload)"
                        ),
                        values,
                    )
                else:
                    await session.execute(
                        text(
                            "UPDATE batch_job SET status = CASE WHEN "
                            "cancel_requested_at IS NOT NULL "
                            "AND :status IN ('COMPLETED','COMPLETED_WITH_ERRORS') THEN 'CANCELLED' "
                            "ELSE :status END, "
                            "success_count = :success_count, "
                            "failure_count = :failure_count, updated_at = :updated_at, "
                            "completed_at = :completed_at, "
                            "cancel_requested_at = COALESCE("
                            "cancel_requested_at,:cancel_requested_at), "
                            "input_payload=:input_payload, "
                            "result_payload=:result_payload WHERE id = :id"
                        ),
                        values | {"id": existing_id},
                    )
        except IntegrityError:
            existing = await self.get_batch_by_business_key(batch.business_key)
            if existing is None:
                raise
            return existing
        return batch

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT i.public_id, b.public_id AS batch_public_id, i.item_key, "
                        "i.target_id, i.status, i.attempt_count, i.error_code, i.result_version, "
                        "i.updated_at, j.public_id AS processing_job_public_id "
                        "FROM batch_job_item i JOIN batch_job b ON b.id = i.batch_job_id "
                        "LEFT JOIN processing_job j ON j.id = i.processing_job_id "
                        "WHERE b.public_id = :batch_id ORDER BY i.id"
                    ),
                    {"batch_id": batch_id},
                )
            ).all()
        return [_batch_item_from_row(row) for row in rows]

    async def save_batch_item(self, item: BatchJobItem) -> BatchJobItem:
        async with self._session_factory() as session, session.begin():
            batch_id = await session.scalar(
                text("SELECT id FROM batch_job WHERE public_id = :public_id"),
                {"public_id": item.batch_id},
            )
            if batch_id is None:
                raise AppError("BATCH_JOB_NOT_FOUND", "批量任务不存在", 404)
            processing_job_id = None
            if item.processing_job_id is not None:
                processing_job_id = await session.scalar(
                    text("SELECT id FROM processing_job WHERE public_id = :public_id"),
                    {"public_id": item.processing_job_id},
                )
            await session.execute(
                text(
                    "INSERT INTO batch_job_item (public_id, batch_job_id, item_key, target_id, "
                    "status, attempt_count, error_code, result_version, updated_at, "
                    "processing_job_id) VALUES (:public_id, :batch_job_id, :item_key, "
                    ":target_id, :status, :attempt_count, :error_code, :result_version, "
                    ":updated_at, :processing_job_id) ON DUPLICATE KEY UPDATE "
                    "status = VALUES(status), attempt_count = VALUES(attempt_count), "
                    "error_code = VALUES(error_code), result_version = VALUES(result_version), "
                    "updated_at = VALUES(updated_at), "
                    "processing_job_id = VALUES(processing_job_id)"
                ),
                {
                    "public_id": item.id,
                    "batch_job_id": batch_id,
                    "item_key": item.item_key,
                    "target_id": item.target_id,
                    "status": item.status,
                    "attempt_count": item.attempt_count,
                    "error_code": item.error_code,
                    "result_version": item.result_version,
                    "updated_at": item.updated_at,
                    "processing_job_id": processing_job_id,
                },
            )
        return item

    async def get_audio_target(self, target_id: str) -> AudioTarget | None:
        return await self._get_audio_target("t.public_id = :value", target_id)

    async def get_audio_target_by_stable_key(self, stable_key: str) -> AudioTarget | None:
        return await self._get_audio_target("t.stable_key = :value", stable_key)

    async def _get_audio_target(self, condition: str, value: str) -> AudioTarget | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT t.public_id, t.stable_key, t.target_type, "
                        "v.public_id AS active_version_public_id FROM audio_target t "
                        "LEFT JOIN audio_version v ON v.id = t.active_version_id WHERE " + condition
                    ),
                    {"value": value},
                )
            ).first()
        return None if row is None else _audio_target_from_row(row)

    async def save_audio_target(self, target: AudioTarget) -> AudioTarget:
        try:
            async with self._session_factory() as session, session.begin():
                existing_id = await session.scalar(
                    text("SELECT id FROM audio_target WHERE public_id = :public_id"),
                    {"public_id": target.id},
                )
                if existing_id is None:
                    await session.execute(
                        text(
                            "INSERT INTO audio_target "
                            "(public_id, stable_key, target_type, active_version_id) "
                            "VALUES (:public_id, :stable_key, :target_type, NULL)"
                        ),
                        {
                            "public_id": target.id,
                            "stable_key": target.stable_key,
                            "target_type": target.target_type,
                        },
                    )
                else:
                    active_version_id = None
                    if target.active_version_id is not None:
                        active_version_id = await session.scalar(
                            text("SELECT id FROM audio_version WHERE public_id = :public_id"),
                            {"public_id": target.active_version_id},
                        )
                    await session.execute(
                        text(
                            "UPDATE audio_target SET active_version_id = :active_version_id "
                            "WHERE id = :id"
                        ),
                        {"active_version_id": active_version_id, "id": existing_id},
                    )
        except IntegrityError:
            existing = await self.get_audio_target_by_stable_key(target.stable_key)
            if existing is None:
                raise
            return existing
        return target

    async def list_audio_targets(self) -> list[AudioTarget]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT t.public_id, t.stable_key, t.target_type, "
                        "v.public_id AS active_version_public_id FROM audio_target t "
                        "LEFT JOIN audio_version v ON v.id = t.active_version_id "
                        "ORDER BY t.stable_key"
                    )
                )
            ).all()
        return [_audio_target_from_row(row) for row in rows]

    async def get_audio_version(self, version_id: str) -> AudioVersion | None:
        versions = await self._get_audio_versions("v.public_id = :value", version_id)
        return versions[0] if versions else None

    async def find_audio_version_by_provider_request(
        self, provider_request_id: str
    ) -> AudioVersion | None:
        versions = await self._get_audio_versions(
            "v.provider_request_id = :value", provider_request_id
        )
        return versions[0] if versions else None

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]:
        return await self._get_audio_versions("t.public_id = :value", target_id)

    async def _get_audio_versions(self, condition: str, value: str) -> list[AudioVersion]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT v.public_id, t.public_id AS target_public_id, "
                        "a.public_id AS asset_public_id, v.version_no, v.source, v.status, "
                        "v.provider_request_id, j.public_id AS processing_job_public_id, "
                        "v.created_by, v.created_at FROM audio_version v "
                        "JOIN audio_target t ON t.id = v.target_id "
                        "JOIN media_asset a ON a.id = v.asset_id "
                        "LEFT JOIN processing_job j ON j.id = v.processing_job_id WHERE "
                        + condition
                        + " ORDER BY v.version_no"
                    ),
                    {"value": value},
                )
            ).all()
        return [_audio_version_from_row(row) for row in rows]

    async def save_audio_version(self, version: AudioVersion) -> AudioVersion:
        try:
            async with self._session_factory() as session, session.begin():
                existing_id = await session.scalar(
                    text("SELECT id FROM audio_version WHERE public_id = :public_id"),
                    {"public_id": version.id},
                )
                if existing_id is not None:
                    await session.execute(
                        text("UPDATE audio_version SET status = :status WHERE id = :id"),
                        {"status": version.status, "id": existing_id},
                    )
                    return version
                target_id = await session.scalar(
                    text("SELECT id FROM audio_target WHERE public_id = :public_id"),
                    {"public_id": version.target_id},
                )
                asset_id = await session.scalar(
                    text("SELECT id FROM media_asset WHERE public_id = :public_id"),
                    {"public_id": version.asset_id},
                )
                if target_id is None:
                    raise AppError("AUDIO_TARGET_NOT_FOUND", "音频目标不存在", 404)
                if asset_id is None:
                    raise AppError("MEDIA_ASSET_NOT_FOUND", "媒体素材不存在", 404)
                processing_job_id = None
                if version.processing_job_id is not None:
                    processing_job_id = await session.scalar(
                        text("SELECT id FROM processing_job WHERE public_id = :public_id"),
                        {"public_id": version.processing_job_id},
                    )
                await session.execute(
                    text(
                        "INSERT INTO audio_version "
                        "(public_id, target_id, asset_id, version_no, source, status, "
                        "provider_request_id, processing_job_id, created_by, created_at) VALUES "
                        "(:public_id, :target_id, :asset_id, :version_no, :source, :status, "
                        ":provider_request_id, :processing_job_id, :created_by, :created_at)"
                    ),
                    {
                        "public_id": version.id,
                        "target_id": target_id,
                        "asset_id": asset_id,
                        "version_no": version.version_no,
                        "source": version.source,
                        "status": version.status,
                        "provider_request_id": version.provider_request_id,
                        "processing_job_id": processing_job_id,
                        "created_by": version.created_by,
                        "created_at": version.created_at,
                    },
                )
        except IntegrityError:
            if version.provider_request_id:
                existing = await self.find_audio_version_by_provider_request(
                    version.provider_request_id
                )
                if existing is not None:
                    return existing
            raise
        return version

    async def get_trash_entry(self, entry_id: str) -> TrashEntry | None:
        return await self._get_trash("public_id = :value", entry_id)

    async def get_trash_by_revision(self, revision_id: str) -> TrashEntry | None:
        return await self._get_trash("revision_public_id = :value", revision_id)

    async def _get_trash(self, condition: str, value: str) -> TrashEntry | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT public_id, scene_public_id, revision_public_id, status, "
                        "trashed_by, trashed_at, retention_until, restored_at, cleaned_at "
                        "FROM draft_trash WHERE " + condition
                    ),
                    {"value": value},
                )
            ).first()
        return None if row is None else _trash_entry_from_row(row)

    async def save_trash_entry(self, entry: TrashEntry) -> TrashEntry:
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO draft_trash "
                        "(public_id, scene_public_id, revision_public_id, status, trashed_by, "
                        "trashed_at, retention_until, restored_at, cleaned_at) VALUES "
                        "(:public_id, :scene_public_id, :revision_public_id, :status, "
                        ":trashed_by, :trashed_at, :retention_until, :restored_at, :cleaned_at) "
                        "ON DUPLICATE KEY UPDATE status = VALUES(status), "
                        "restored_at = VALUES(restored_at), cleaned_at = VALUES(cleaned_at)"
                    ),
                    {
                        "public_id": entry.id,
                        "scene_public_id": entry.scene_id,
                        "revision_public_id": entry.revision_id,
                        "status": entry.status,
                        "trashed_by": entry.trashed_by,
                        "trashed_at": entry.trashed_at,
                        "retention_until": entry.retention_until,
                        "restored_at": entry.restored_at,
                        "cleaned_at": entry.cleaned_at,
                    },
                )
        except IntegrityError:
            existing = await self.get_trash_by_revision(entry.revision_id)
            if existing is None:
                raise
            return existing
        return entry

    async def list_trash_entries(self) -> list[TrashEntry]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT public_id, scene_public_id, revision_public_id, status, "
                        "trashed_by, trashed_at, retention_until, restored_at, cleaned_at "
                        "FROM draft_trash ORDER BY trashed_at DESC"
                    )
                )
            ).all()
        return [_trash_entry_from_row(row) for row in rows]

    async def is_draft_revision(self, scene_id: str, revision_id: str) -> bool:
        async with self._session_factory() as session:
            count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM scene_revision r JOIN scene s ON s.id = r.scene_id "
                    "WHERE s.public_id = :scene_id AND r.public_id = :revision_id "
                    "AND r.status = 'DRAFT' AND s.published_revision_id IS NULL"
                ),
                {"scene_id": scene_id, "revision_id": revision_id},
            )
        return bool(count)

    async def has_draft_references(self, revision_id: str) -> bool:
        async with self._session_factory() as session:
            count = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM scene_revision r JOIN scene s ON s.id = r.scene_id "
                    "WHERE r.public_id = :revision_id AND ("
                    "s.published_revision_id = r.id OR "
                    "EXISTS (SELECT 1 FROM content_package_scene cps WHERE cps.scene_id = s.id) "
                    "OR EXISTS (SELECT 1 FROM limited_campaign_scene lcs "
                    "WHERE lcs.scene_id = s.id OR lcs.scene_revision_id = r.id) "
                    "OR EXISTS (SELECT 1 FROM open_scene_item osi WHERE osi.scene_id = s.id) "
                    "OR EXISTS (SELECT 1 FROM preview_config pc WHERE pc.scene_id = s.id) "
                    "OR EXISTS (SELECT 1 FROM scene_revision child "
                    "WHERE child.source_revision_id = r.id))"
                ),
                {"revision_id": revision_id},
            )
        return bool(count)

    async def purge_draft(self, scene_id: str, revision_id: str) -> None:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT s.id AS scene_id, r.id AS revision_id FROM scene s "
                        "JOIN scene_revision r ON r.scene_id = s.id "
                        "WHERE s.public_id = :scene_id AND r.public_id = :revision_id "
                        "AND r.status = 'DRAFT' FOR UPDATE"
                    ),
                    {"scene_id": scene_id, "revision_id": revision_id},
                )
            ).first()
            if row is None:
                raise AppError("DRAFT_NOT_TRASHABLE", "只有未发布草稿可清理", 409)
            await session.execute(
                text("UPDATE scene SET draft_revision_id = NULL WHERE id = :scene_id"),
                {"scene_id": row.scene_id},
            )
            await session.execute(
                text("DELETE FROM scene_revision WHERE id = :revision_id"),
                {"revision_id": row.revision_id},
            )

    async def transition_trash(
        self, entry_id: str, action: str, actor_id: str, now: datetime
    ) -> TrashEntry:
        if action not in {"RESTORE", "CLEANUP"}:
            raise ValueError("Unknown trash transition")
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT public_id,scene_public_id,revision_public_id,status,trashed_by,"
                        "trashed_at,retention_until,restored_at,cleaned_at FROM draft_trash "
                        "WHERE public_id=:id FOR UPDATE"
                    ),
                    {"id": entry_id},
                )
            ).first()
            if row is None:
                raise AppError("TRASH_ENTRY_NOT_FOUND", "回收站记录不存在", 404)
            entry = _trash_entry_from_row(row)
            if action == "RESTORE" and entry.status == "RESTORED":
                return entry
            if action == "CLEANUP" and entry.status == "CLEANED":
                return entry
            if entry.status != "TRASHED":
                raise AppError("TRASH_ENTRY_NOT_ACTIVE", "回收站记录当前不可操作", 409)
            revision = (
                await session.execute(
                    text(
                        "SELECT r.id AS revision_id,s.id AS "
                        "scene_id,r.status,s.published_revision_id "
                        "FROM scene_revision r JOIN scene s ON s.id=r.scene_id "
                        "WHERE r.public_id=:revision AND s.public_id=:scene FOR UPDATE"
                    ),
                    {"revision": entry.revision_id, "scene": entry.scene_id},
                )
            ).first()
            if (
                revision is None
                or revision.status != "DRAFT"
                or revision.published_revision_id is not None
            ):
                raise AppError("DRAFT_NOT_TRASHABLE", "草稿已不存在或已发布", 409)
            if action == "RESTORE":
                result = replace(entry, status="RESTORED", restored_at=now)
            else:
                if now < entry.retention_until:
                    raise AppError("TRASH_RETENTION_ACTIVE", "草稿仍在 30 天保留期内", 409)
                for table, predicate in (
                    ("content_package_scene", "scene_id=:scene"),
                    ("limited_campaign_scene", "scene_id=:scene OR scene_revision_id=:revision"),
                    ("open_scene_item", "scene_id=:scene"),
                    ("preview_config", "scene_id=:scene"),
                    ("scene_revision", "source_revision_id=:revision"),
                ):
                    reference = (
                        await session.execute(
                            text(f"SELECT 1 FROM {table} WHERE {predicate} FOR UPDATE"),
                            {"scene": revision.scene_id, "revision": revision.revision_id},
                        )
                    ).first()
                    if reference is not None:
                        raise AppError("DRAFT_REFERENCED", "草稿仍被其他对象引用", 409)
                await session.execute(
                    text(
                        "UPDATE scene SET draft_revision_id=NULL WHERE id=:scene "
                        "AND draft_revision_id=:revision"
                    ),
                    {"scene": revision.scene_id, "revision": revision.revision_id},
                )
                await session.execute(
                    text("DELETE FROM scene_revision WHERE id=:id"), {"id": revision.revision_id}
                )
                result = replace(entry, status="CLEANED", cleaned_at=now)
            await session.execute(
                text(
                    "UPDATE draft_trash SET "
                    "status=:status,restored_at=:restored,cleaned_at=:cleaned "
                    "WHERE public_id=:id"
                ),
                {
                    "id": entry_id,
                    "status": result.status,
                    "restored": result.restored_at,
                    "cleaned": result.cleaned_at,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO audit_event(public_id,actor_public_id,action,object_type,"
                    "object_public_id,after_summary,request_id) VALUES "
                    "(:id,:actor,:action,'draft_trash',:trash,"
                    "JSON_OBJECT('outcome',:outcome,'revision_id',:revision),:id)"
                ),
                {
                    "id": new_ulid(now),
                    "actor": actor_id,
                    "action": f"draft.{action.lower()}",
                    "trash": entry_id,
                    "outcome": result.status,
                    "revision": entry.revision_id,
                },
            )
            return result


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _required_utc_datetime(value: datetime | None) -> datetime:
    normalized = _utc_datetime(value)
    if normalized is None:
        raise ValueError("required database datetime is null")
    return normalized


def _processing_job_from_row(row: Any) -> ProcessingJob:
    return ProcessingJob(
        row.public_id,
        row.business_key,
        row.job_type,
        row.target_id,
        row.batch_public_id,
        row.status,
        row.provider_request_id,
        row.error_code,
        row.created_by,
        _required_utc_datetime(row.created_at),
        _required_utc_datetime(row.updated_at),
        _utc_datetime(row.cancel_requested_at),
        dict(
            json.loads(row.input_payload)
            if isinstance(row.input_payload, str)
            else row.input_payload or {}
        ),
    )


def _batch_job_from_row(row: Any) -> BatchJob:
    return BatchJob(
        row.public_id,
        row.business_key,
        row.job_type,
        row.status,
        row.total_count,
        row.success_count,
        row.failure_count,
        row.created_by,
        _required_utc_datetime(row.created_at),
        _required_utc_datetime(row.updated_at),
        _utc_datetime(row.completed_at),
        _utc_datetime(row.cancel_requested_at),
        _json_payload(row.input_payload),
        _json_payload(row.result_payload),
        row.lease_token,
        _utc_datetime(row.lease_expires_at),
    )


def _ocr_candidate_from_row(row: Any) -> OcrCandidate:
    structured = (
        json.loads(row.structured_candidate)
        if isinstance(row.structured_candidate, str)
        else row.structured_candidate
    )
    return OcrCandidate(
        row.public_id,
        row.job_public_id,
        row.asset_public_id,
        row.business_key,
        row.provider_request_id,
        row.status,
        row.template_type,
        dict(structured or {}),
        float(row.confidence) if row.confidence is not None else None,
        row.error_code,
        _required_utc_datetime(row.created_at),
        row.confirmed_revision_public_id,
        row.confirmed_by,
        _utc_datetime(row.confirmed_at),
    )


def _batch_item_from_row(row: Any) -> BatchJobItem:
    return BatchJobItem(
        row.public_id,
        row.batch_public_id,
        row.item_key,
        row.target_id,
        row.status,
        row.attempt_count,
        row.error_code,
        row.result_version,
        _required_utc_datetime(row.updated_at),
        row.processing_job_public_id,
    )


def _audio_target_from_row(row: Any) -> AudioTarget:
    return AudioTarget(
        row.public_id,
        row.stable_key,
        row.target_type,
        row.active_version_public_id,
    )


def _audio_version_from_row(row: Any) -> AudioVersion:
    return AudioVersion(
        row.public_id,
        row.target_public_id,
        row.asset_public_id,
        row.version_no,
        row.source,
        row.status,
        row.provider_request_id,
        row.processing_job_public_id,
        row.created_by,
        _required_utc_datetime(row.created_at),
    )


def _trash_entry_from_row(row: Any) -> TrashEntry:
    return TrashEntry(
        row.public_id,
        row.scene_public_id,
        row.revision_public_id,
        row.status,
        row.trashed_by,
        _required_utc_datetime(row.trashed_at),
        _required_utc_datetime(row.retention_until),
        _utc_datetime(row.restored_at),
        _utc_datetime(row.cleaned_at),
    )


def _from_row(row: Any) -> MediaAsset:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return MediaAsset(
        id=row.public_id,
        object_key=row.object_key,
        asset_type=row.asset_type,
        content_type=row.content_type,
        size=row.size_bytes,
        sha256=row.sha256,
        status=row.status,
        security_status=row.security_status,
        created_by=row.created_by,
        created_at=created_at,
        width=row.width,
        height=row.height,
        duration_ms=row.duration_ms,
        security_request_id=row.security_request_id,
    )


class SQLAlchemySignedTargetResolver:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        access_policy: AccessPolicyService,
    ) -> None:
        self._session_factory = session_factory
        self._access_policy = access_policy

    async def __call__(
        self, target_id: str, user_id: str, now: datetime
    ) -> tuple[str, datetime | None]:
        raise AppError("SCENE_RESOURCE_BINDING_REQUIRED", "媒体签名需要指定场景和内容版本", 409)


def _json_payload(value: Any) -> dict[str, object]:
    return dict(json.loads(value) if isinstance(value, str) else value or {})
