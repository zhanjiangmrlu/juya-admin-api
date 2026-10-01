from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import text

from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.production_store import ProductionStore, decode
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.domain import BatchJob
from juya_admin_api.modules.media.service import MediaAdminRepository, MediaAdminService
from juya_admin_api.shared.errors import AppError

Operation = Callable[
    [str, str, dict[str, object], str, str, datetime], Awaitable[dict[str, object]]
]
AuditOperation = Callable[[str, str, dict[str, object], datetime], Awaitable[None]]
BATCH_OPERATIONS = {
    "TAGS",
    "COPYRIGHT",
    "PACKAGE",
    "VALIDATE",
    "PUBLISH",
    "OFFLINE",
    "RESTORE",
    "EXPORT",
    "OCR",
}


class BatchExecutor:
    def __init__(
        self,
        service: MediaAdminService,
        repository: MediaAdminRepository,
        operation: Operation,
        *,
        audit: AuditOperation | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.operation = operation
        self.audit = audit

    async def run(self, batch_id: str, now: datetime) -> BatchJob:
        if not await self.repository.claim_batch(batch_id, now):
            return await self.service.get_batch(batch_id)
        batch = await self.service.get_batch(batch_id)
        for item in await self.service.list_batch_items(batch_id):
            if not await self.repository.claim_batch_item(
                batch_id, item.item_key, datetime.now(UTC)
            ):
                continue
            result: dict[str, object] = {}
            code = None
            try:
                if batch.job_type not in BATCH_OPERATIONS:
                    raise AppError("BATCH_OPERATION_INVALID", "批量操作不支持", 422)
                result = await self.operation(
                    batch.job_type,
                    item.target_id,
                    batch.input_payload,
                    batch.created_by,
                    f"{batch.id}:{item.item_key}",
                    datetime.now(UTC),
                )
            except AppError as error:
                code = error.code
            except Exception:
                code = "BATCH_ITEM_FAILED"
            current = await self.service.get_batch(batch_id)
            results = dict(current.result_payload)
            results[item.item_key] = {
                "status": "FAILED" if code else "SUCCEEDED",
                "error_code": code,
                "result": result,
            }
            await self.repository.save_batch(replace(current, result_payload=results))
            version = result.get("version")
            await self.service.finish_batch_item(
                batch_id,
                item.item_key,
                succeeded=code is None,
                error_code=code,
                result_version=version if isinstance(version, int) else None,
                now=datetime.now(UTC),
            )
            if self.audit is not None:
                await self.audit(
                    batch.job_type,
                    item.target_id,
                    {"batch_id": batch.id, "error_code": code, "result": result},
                    datetime.now(UTC),
                )
        return await self.service.get_batch(batch_id)


class ContentBatchOperations:
    def __init__(
        self, content: ContentService, store: ProductionStore, *, ocr: Operation | None = None
    ) -> None:
        self.content = content
        self.store = store
        self.ocr = ocr

    async def __call__(
        self,
        kind: str,
        target: str,
        payload: dict[str, object],
        actor: str,
        key: str,
        now: datetime,
    ) -> dict[str, object]:
        scene = await self.content.get_scene(target)
        if kind == "OCR":
            if self.ocr is None:
                raise AppError("OCR_DISABLED", "OCR未配置", 409)
            return await self.ocr(kind, target, payload, actor, key, now)
        if kind == "OFFLINE":
            await self.content.offline_scene(target, actor, now)
            return {"scene_id": target, "status": "OFFLINE"}
        if kind == "PACKAGE":
            return await self._package(target, payload)
        if kind == "RESTORE":
            if not scene.published_revision_id:
                raise AppError("SCENE_NOT_RESTORABLE", "仅已发布下线内容可恢复", 409)
            await self.content.validate_publish(scene.published_revision_id, frozenset())
            async with self.store.sessions() as session, session.begin():
                row = (
                    await session.execute(
                        text(
                            "SELECT s.status,r.public_id AS revision_id,r.content_snapshot "
                            "FROM scene s "
                            "JOIN scene_revision r ON r.id=s.published_revision_id "
                            "WHERE s.public_id=:id FOR UPDATE"
                        ),
                        {"id": target},
                    )
                ).first()
                if (
                    row is None
                    or row.status != "OFFLINE"
                    or row.revision_id != scene.published_revision_id
                ):
                    raise AppError("SCENE_NOT_RESTORABLE", "场景不在下线状态", 409)
                snapshot = SceneContent.model_validate(decode(row.content_snapshot))
                assets, audios = await self.store.facts(session, snapshot, lock=True)
                checks = [
                    *check_content(snapshot, assets, audios),
                    await self.store.entry_references(session, snapshot),
                ]
                errors = [check.code for check in checks if not check.passed]
                if errors:
                    raise AppError(
                        "PUBLISH_CHECK_FAILED",
                        "内容未通过发布检查",
                        409,
                        {"error_codes": errors},
                    )
                await session.execute(
                    text("UPDATE scene SET status='PUBLISHED' WHERE public_id=:id"), {"id": target}
                )
            return {"scene_id": target, "status": "PUBLISHED"}
        revision_id = scene.draft_revision_id or scene.published_revision_id
        if revision_id is None:
            raise AppError("REVISION_NOT_FOUND", "场景没有内容版本", 404)
        revision = await self.content.get_revision(revision_id)
        if kind == "EXPORT":
            return {
                "scene_id": target,
                "revision_id": revision.id,
                "version": revision.version,
                "content": revision.content,
            }
        if kind == "VALIDATE":
            checks = await self.store.checks(revision.id)
            errors = [
                check.code for check in checks if check.severity == "ERROR" and not check.passed
            ]
            if errors:
                raise AppError("PUBLISH_CHECK_FAILED", "场景校验未通过", 409)
            return {
                "scene_id": target,
                "revision_id": revision.id,
                "version": revision.version,
                "checks": [{"code": check.code, "passed": check.passed} for check in checks],
            }
        expected_map = payload.get("expected_versions", {})
        expected = expected_map.get(target) if isinstance(expected_map, dict) else None
        if expected is not None and expected != revision.version:
            raise AppError("REVISION_VERSION_CONFLICT", "批量选择后的草稿版本已变化", 409)
        if kind == "PUBLISH":
            if not isinstance(expected, int):
                raise AppError("PUBLISH_EXPECTED_VERSION_REQUIRED", "批量发布需要每项草稿版本", 422)
            published = await self.content.publish_revision(
                revision.id, actor, key, now, expected_version=expected
            )
            return {
                "scene_id": target,
                "revision_id": published.revision_id,
                "version": expected,
                "status": "PUBLISHED",
            }
        if kind not in {"TAGS", "COPYRIGHT"}:
            raise AppError("BATCH_OPERATION_INVALID", "批量操作不支持", 422)
        if revision.status in {"PUBLISHED", "SUPERSEDED"}:
            revision = await self.content.create_revision(target, revision.id, actor, now)
        proposed = dict(revision.content)
        field = "tags" if kind == "TAGS" else "copyright"
        proposed[field] = payload.get(field)
        saved = await self.content.save_revision(
            revision.id, proposed, expected_version=revision.version, actor_id=actor
        )
        return {"scene_id": target, "revision_id": saved.id, "version": saved.version}

    async def _package(self, target: str, payload: dict[str, object]) -> dict[str, object]:
        package_id = payload.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            raise AppError("PACKAGE_REQUIRED", "内容包归属需要内容包ID", 422)
        async with self.store.sessions() as session, session.begin():
            package = await session.scalar(
                text("SELECT id FROM content_package WHERE public_id=:id FOR UPDATE"),
                {"id": package_id},
            )
            if package is None:
                raise AppError("CONTENT_PACKAGE_NOT_FOUND", "内容包不存在", 404)
            scene = await session.scalar(
                text("SELECT id FROM scene WHERE public_id=:id FOR UPDATE"), {"id": target}
            )
            existing = await session.scalar(
                text("SELECT id FROM content_package_scene WHERE package_id=:p AND scene_id=:s"),
                {"p": package, "s": scene},
            )
            if existing is None:
                order = await session.scalar(
                    text(
                        "SELECT COALESCE(MAX(sort_order),0)+1 FROM content_package_scene "
                        "WHERE package_id=:p"
                    ),
                    {"p": package},
                )
                await session.execute(
                    text(
                        "INSERT INTO content_package_scene(package_id,scene_id,sort_order) "
                        "VALUES(:p,:s,:o)"
                    ),
                    {"p": package, "s": scene, "o": order},
                )
        return {"scene_id": target, "package_id": package_id}
