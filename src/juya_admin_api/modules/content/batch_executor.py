import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.production_store import ProductionStore, decode
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.domain import BatchJob
from juya_admin_api.modules.media.service import MediaAdminRepository, MediaAdminService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

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
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 BatchExecutor 实例,持有本方法访问的依赖和业务状态。
        #     service: 媒体管理服务,读取和更新批任务及其任务项。
        #     repository: 媒体管理仓储,管理作业、批任务、音频版本和草稿回收。
        #     operation: 内容批操作异步回调,按类型、目标、输入和幂等键执行单项业务。
        #     audit: 批任务项执行后的审计回调,接收操作类型、目标、结果及时间;可为空。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.service = service
        self.repository = repository
        self.operation = operation
        self.audit = audit

    async def run(self, batch_id: str, now: datetime) -> BatchJob:
        # 功能:领取批任务租约,逐项执行内容操作并持续续租和记录结果。
        # 参数:
        #     self: 当前 BatchExecutor 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:批任务及执行统计和当前状态。
        token = new_ulid(now)
        if not await self.repository.claim_batch(batch_id, now, token):
            return await self.service.get_batch(batch_id)
        batch = await self.service.get_batch(batch_id)
        if batch.status == "CANCELLED":
            return batch
        heartbeat = asyncio.create_task(self._heartbeat(batch_id, token))
        try:
            for item in await self.service.list_batch_items(batch_id):
                if not await self.repository.heartbeat_batch(batch_id, token, datetime.now(UTC)):
                    break
                if not await self.repository.claim_batch_item(
                    batch_id, item.item_key, datetime.now(UTC), token
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
                finished = await self.repository.finish_claimed_batch_item(
                    batch_id, item.item_key, token, result, code, datetime.now(UTC)
                )
                if not finished:
                    break
                if self.audit is not None:
                    await self.audit(
                        batch.job_type,
                        item.target_id,
                        {"batch_id": batch.id, "error_code": code, "result": result},
                        datetime.now(UTC),
                    )
            return await self.service.get_batch(batch_id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, batch_id: str, token: str) -> None:
        # 功能:定期续租批任务,直到执行结束或租约失效。
        # 参数:
        #     self: 当前 BatchExecutor 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     token: 当前批任务执行器持有的租约令牌,续租时核对所有权。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        while True:
            await asyncio.sleep(30)
            if not await self.repository.heartbeat_batch(batch_id, token, datetime.now(UTC)):
                return


class ContentBatchOperations:
    def __init__(
        self, content: ContentService, store: ProductionStore, *, ocr: Operation | None = None
    ) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 ContentBatchOperations 实例,持有本方法访问的依赖和业务状态。
        #     content: 内容服务,读取和保存场景草稿并执行发布前校验。
        #     store: 内容生产事务存储,管理系列、词典版本和内容引用。
        #     ocr: OCR 内容批操作异步回调;为空时拒绝批量识别。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:在同一事务内执行内容批量操作并保存幂等回执。
        # 参数:
        #     self: 当前 ContentBatchOperations 实例,持有本方法访问的依赖和业务状态。
        #     kind: 内容批操作类型代码,决定校验、发布、OCR 或编辑等业务分支。
        #     target: 当前批操作的场景公开标识,定位目标场景及其草稿。
        #     payload: 批操作输入字段,包含标签、版权、内容包或目标预期版本等当前命令信息。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:当前内容操作的结果字典,重复请求返回已保存回执。
        if not key or len(key) > 191:
            raise AppError("IDEMPOTENCY_KEY_INVALID", "批量操作键无效", 422)
        if kind in {"VALIDATE", "PUBLISH", "RESTORE"}:
            scene = await self.content.get_scene(target)
            revision_id = (
                scene.published_revision_id
                if kind == "RESTORE"
                else scene.draft_revision_id or scene.published_revision_id
            )
            if revision_id is not None:
                await self.store.prepare_resources(revision_id)
        request_hash = hashlib.sha256(
            json.dumps(
                [kind, target, payload, actor], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        async with self.store.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT IGNORE INTO "
                    "batch_operation_receipt(operation_key,request_hash,created_at) "
                    "VALUES(:key,:hash,:now)"
                ),
                {"key": key, "hash": request_hash, "now": now},
            )
            row = (
                await session.execute(
                    text(
                        "SELECT request_hash,result_payload FROM batch_operation_receipt WHERE "
                        "operation_key=:key FOR UPDATE"
                    ),
                    {"key": key},
                )
            ).one()
            if row.request_hash != request_hash:
                raise AppError("IDEMPOTENCY_KEY_CONFLICT", "批量操作键已用于不同请求", 409)
            if row.result_payload is not None:
                return decode(row.result_payload)

            # All content writes and their receipt commit together, so a process crash
            # cannot leave a committed edit without its replay result.
            def borrow() -> Any:
                # 功能:构造共享当前外层事务的会话代理。
                # 参数:无。
                # 返回:共享外层事务的会话代理。
                return _BorrowedSession(session)

            sessions = cast(async_sessionmaker[AsyncSession], borrow)
            scoped = ContentBatchOperations(
                ContentService(
                    SQLAlchemyContentRepository(sessions, require_review=self.store.require_review)
                ),
                ProductionStore(sessions, require_review=self.store.require_review),
                ocr=self.ocr,
            )
            result = await scoped._execute(kind, target, payload, actor, key, now)
            await session.execute(
                text(
                    "UPDATE batch_operation_receipt SET result_payload=:result WHERE "
                    "operation_key=:key"
                ),
                {"key": key, "result": json.dumps(result, ensure_ascii=False)},
            )
            return result

    async def _execute(
        self,
        kind: str,
        target: str,
        payload: dict[str, object],
        actor: str,
        key: str,
        now: datetime,
    ) -> dict[str, object]:
        # 功能:按操作类型执行场景校验、发布、导出或草稿编辑。
        # 参数:
        #     self: 当前 ContentBatchOperations 实例,持有本方法访问的依赖和业务状态。
        #     kind: 内容批操作类型代码,决定校验、发布、OCR 或编辑等业务分支。
        #     target: 当前批操作的场景公开标识,定位目标场景及其草稿。
        #     payload: 批操作输入字段,包含标签、版权、内容包或目标预期版本等当前命令信息。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:场景标识、操作状态或内容版本等当前命令的结果字段。
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
                    *check_content(
                        snapshot, assets, audios, require_review=self.store.require_review
                    ),
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
        # 功能:将指定场景加入内容包,重复绑定时复用现有关系。
        # 参数:
        #     self: 当前 ContentBatchOperations 实例,持有本方法访问的依赖和业务状态。
        #     target: 当前批操作的场景公开标识,定位目标场景及其草稿。
        #     payload: 批操作输入字段,包含标签、版权、内容包或目标预期版本等当前命令信息。
        # 返回:场景和其所属内容包的公开标识。
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


class _BorrowedSession:
    """Lend one outer transaction to the existing repositories without closing it."""

    def __init__(self, session: AsyncSession) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 _BorrowedSession 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.session = session

    async def __aenter__(self) -> Any:
        # 功能:借用外层事务的数据库会话,进入异步上下文。
        # 参数:
        #     self: 当前 _BorrowedSession 实例,持有本方法访问的依赖和业务状态。
        # 返回:借用会话代理本身,不关闭外层会话。
        return self

    async def __aexit__(self, *args: object) -> None:
        # 功能:退出借用会话的上下文,保留外层事务和连接。
        # 参数:
        #     self: 当前 _BorrowedSession 实例,持有本方法访问的依赖和业务状态。
        #     args: 异步上下文退出时传入的异常类型、异常实例和回溯信息;代理不处理这些信息。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        return None

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        # 功能:借用已经开启的外层事务,提供兼容的异步事务上下文。
        # 参数:
        #     self: 当前 _BorrowedSession 实例,持有本方法访问的依赖和业务状态。
        # 返回:异步上下文迭代器,进入时产出 None,事务由外层负责。
        yield None

    def __getattr__(self, name: str) -> Any:
        # 功能:将借用会话的属性访问转发给外层数据库会话。
        # 参数:
        #     self: 当前 _BorrowedSession 实例,持有本方法访问的依赖和业务状态。
        #     name: 需要从外层数据库会话读取的属性名。
        # 返回:外层数据库会话上对应属性的原始值。
        return getattr(self.session, name)
