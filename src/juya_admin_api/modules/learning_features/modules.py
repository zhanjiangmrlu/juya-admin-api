"""Learning module metadata with actual deployment capabilities and coordinated versions."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.production_store import ProductionStore, encode
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

MODULES = frozenset({"scene_learning", "short_reading", "long_sentence", "grammar"})


class LearningModulesStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], capabilities: dict[str, bool]):
        self.sessions = sessions
        self.capabilities = {key: capabilities.get(key) is True for key in MODULES}
        self.commands = ProductionStore(sessions)

    def project(self, row: Any) -> dict[str, Any]:
        ready = self.capabilities.get(row["module_type"], False)
        return dict(row) | {
            "enabled": bool(row["enabled"]),
            "ready": ready,
            "reason": None if ready else "MODULE_NOT_IMPLEMENTED",
        }

    async def listing(self) -> dict[str, Any]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT module_type,display_name,entry_path,sort_order,"
                            "enabled,config_version "
                            "FROM learning_module_config ORDER BY sort_order ASC,module_type ASC"
                        )
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [self.project(row) for row in rows if row["module_type"] in MODULES]}

    async def command(
        self, module: str, actor: str, key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if module not in MODULES:
            raise AppError("LEARNING_MODULE_INVALID", "学习模块无效", 422)
        if not key or len(key) > 100:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "需要有效幂等键", 422)
        name = payload.get("display_name")
        order = payload.get("sort_order")
        version = payload.get("expected_version")
        enabled = payload.get("enabled")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 80
            or type(order) is not int
            or not 0 <= order <= 65535
            or type(version) is not int
            or version < 1
            or type(enabled) is not bool
        ):
            raise AppError("LEARNING_MODULE_CONFIG_INVALID", "模块名称、排序或版本无效", 422)
        scope = "learning-module:" + module
        digest = snapshot_hash(payload)
        async with self.sessions() as session, session.begin():
            replay = await self.commands.creation_replay(session, scope, actor, key, digest)
            if replay is not None:
                return replay
            if enabled and not self.capabilities[module]:
                raise AppError("LEARNING_MODULE_NOT_READY", "当前模块尚未实现,无法开启", 409)
            # Legacy discovery mutations lock this row first; global CAS prevents stale overwrite.
            global_version = await session.scalar(
                text("SELECT version FROM discovery_config_state WHERE id=1 FOR UPDATE")
            )
            if global_version is None:
                raise AppError("DISCOVERY_CONFIG_NOT_READY", "学习模块配置尚未初始化", 409)
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT module_type,display_name,entry_path,sort_order,"
                            "enabled,config_version "
                            "FROM learning_module_config WHERE module_type=:module FOR UPDATE"
                        ),
                        {"module": module},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("LEARNING_MODULE_NOT_FOUND", "学习模块配置不存在", 404)
            if row["config_version"] != version:
                raise AppError("CONFIG_VERSION_CONFLICT", "学习模块配置已变化", 409)
            await session.execute(
                text(
                    "UPDATE learning_module_config SET display_name=:name,sort_order=:position,"
                    "enabled=:enabled,config_version=config_version+1 "
                    "WHERE module_type=:module AND config_version=:version"
                ),
                {
                    "name": name.strip(),
                    "position": order,
                    "enabled": enabled,
                    "module": module,
                    "version": version,
                },
            )
            await session.execute(
                text("UPDATE discovery_config_state SET version=version+1 WHERE id=1")
            )
            result = self.project(
                dict(row)
                | {
                    "display_name": name.strip(),
                    "sort_order": order,
                    "enabled": enabled,
                    "config_version": version + 1,
                }
            )
            result["discovery_version"] = int(global_version) + 1
            now = datetime.now(UTC)
            await session.execute(
                text(
                    "INSERT INTO audit_event(public_id,actor_public_id,action,object_type,"
                    "object_public_id,before_summary,after_summary,request_id,created_at) "
                    "VALUES(:id,:actor,'learning_module.update','learning_module',:module,"
                    ":before,:after,:key,:now)"
                ),
                {
                    "id": new_ulid(now),
                    "actor": actor,
                    "module": module,
                    "before": encode(self.project(row)),
                    "after": encode(result),
                    "key": key,
                    "now": now,
                },
            )
            await self.commands.remember_creation(session, scope, actor, key, digest, result, now)
            return result
