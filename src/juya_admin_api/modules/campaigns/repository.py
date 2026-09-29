"""Transactional campaign administration backed by the existing campaign tables."""

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.campaigns.domain import CampaignDuration, CampaignVersion
from juya_admin_api.modules.campaigns.service import CampaignService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class SQLAlchemyCampaignRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]:
        if set(filters) - {"status"} or (
            "status" in filters
            and filters["status"] not in {"DRAFT", "OPEN", "PAUSED", "ENDED", "ARCHIVED", "CLOSED"}
        ):
            raise AppError("CAMPAIGN_FILTER_INVALID", "活动筛选条件不正确", 422)
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        where = " WHERE c.status = :status" if "status" in filters else ""
        params: dict[str, Any] = {**filters, "limit": page_size, "offset": (page - 1) * page_size}
        async with self._session_factory() as session:
            total = await session.scalar(
                text("SELECT COUNT(*) FROM limited_campaign c" + where), params
            )
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT c.public_id AS id, c.name, c.status, c.version, "
                            "cv.public_id AS current_version_id, cv.capacity, "
                            "cv.granted_user_count, "
                            "c.created_at, c.updated_at FROM limited_campaign c "
                            "LEFT JOIN limited_campaign_version cv ON cv.id = c.current_version_id"
                            + where
                            + " ORDER BY c.created_at DESC, c.id DESC LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            item = dict(row)
            item["created_at"] = _utc(item["created_at"])
            item["updated_at"] = _utc(item["updated_at"])
            items.append(item)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total or 0,
        }

    async def get(self, campaign_id: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            return await self._get(session, campaign_id)

    async def _get(self, session: AsyncSession, campaign_id: str) -> dict[str, Any] | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, public_id, name, status, version, current_version_id, "
                        "created_at, updated_at FROM limited_campaign WHERE public_id = :id"
                    ),
                    {"id": campaign_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = {
            key: row[key] for key in ("name", "status", "version", "created_at", "updated_at")
        }
        result["created_at"] = _utc(result["created_at"])
        result["updated_at"] = _utc(result["updated_at"])
        result["id"] = row["public_id"]
        version = (
            (
                await session.execute(
                    text(
                        "SELECT public_id AS id, version_no, status, duration_days, "
                        "activation_window_days, capacity, granted_user_count, grant_starts_at, "
                        "grant_ends_at, locked_at, version "
                        "FROM limited_campaign_version WHERE id = :id"
                    ),
                    {"id": row["current_version_id"]},
                )
            )
            .mappings()
            .first()
            if row["current_version_id"]
            else None
        )
        if version is None:
            result["current_version"] = None
        else:
            scenes: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT s.public_id FROM limited_campaign_scene cs "
                            "JOIN scene s ON s.id = cs.scene_id "
                            "WHERE cs.campaign_version_id = :id ORDER BY cs.position"
                        ),
                        {"id": row["current_version_id"]},
                    )
                )
                .scalars()
                .all()
            )
            current_version = {**dict(version), "scene_ids": list(scenes)}
            for field in ("grant_starts_at", "grant_ends_at", "locked_at"):
                current_version[field] = _utc(current_version[field])
            result["current_version"] = current_version
        return result

    async def save(
        self,
        campaign_id: str | None,
        *,
        expected_version: int | None = None,
        name: str,
        now: datetime,
        duration_days: int | None = None,
        activation_window_days: int | None = None,
        capacity: int | None = None,
        scene_ids: tuple[str, ...] | None = None,
        actor_id: str = "system",
    ) -> dict[str, Any]:
        if not name.strip() or len(name) > 200:
            raise AppError("CAMPAIGN_NAME_INVALID", "活动名称不正确", 422)
        async with self._session_factory() as session, session.begin():
            if campaign_id is None:
                if duration_days is None or activation_window_days is None or capacity is None:
                    raise AppError("CAMPAIGN_FIELDS_REQUIRED", "活动版本字段不能为空", 422)
                campaign_id = new_ulid(now)
                version_id = new_ulid(now)
                version = CampaignVersion(
                    version_id,
                    campaign_id,
                    "DRAFT",
                    cast(CampaignDuration, duration_days),
                    activation_window_days,
                    capacity,
                    0,
                    scene_ids or (),
                )
                version.validate()
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign "
                        "(public_id, name, status, version, updated_at) "
                        "VALUES (:id, :name, 'DRAFT', 1, :now)"
                    ),
                    {"id": campaign_id, "name": name.strip(), "now": now},
                )
                internal_id = await session.scalar(
                    text("SELECT id FROM limited_campaign WHERE public_id = :id"),
                    {"id": campaign_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign_version (public_id, campaign_id, version_no, "
                        "status, duration_days, activation_window_days, "
                        "capacity, version, updated_at) "
                        "VALUES (:id, :campaign, 1, 'DRAFT', :duration, "
                        ":window, :capacity, 1, :now)"
                    ),
                    {
                        "id": version_id,
                        "campaign": internal_id,
                        "duration": duration_days,
                        "window": activation_window_days,
                        "capacity": capacity,
                        "now": now,
                    },
                )
                version_internal_id = await session.scalar(
                    text("SELECT id FROM limited_campaign_version WHERE public_id = :id"),
                    {"id": version_id},
                )
                await self._replace_scenes(session, version_internal_id, scene_ids or ())
                await session.execute(
                    text(
                        "UPDATE limited_campaign SET current_version_id = :version WHERE id = :id"
                    ),
                    {"version": version_internal_id, "id": internal_id},
                )
                before = None
            else:
                row = await self._lock_campaign(session, campaign_id, expected_version)
                if row.status != "DRAFT":
                    raise AppError("CAMPAIGN_EDIT_STATE_CONFLICT", "仅草稿活动可以编辑", 409)
                before = await self._get(session, campaign_id)
                version_row = (
                    await session.execute(
                        text("SELECT * FROM limited_campaign_version WHERE id = :id FOR UPDATE"),
                        {"id": row.current_version_id},
                    )
                ).first()
                assert version_row is not None
                current = await self._get(session, campaign_id)
                assert current is not None and current["current_version"] is not None
                existing = current["current_version"]
                version = CampaignVersion(
                    existing["id"],
                    campaign_id,
                    existing["status"],
                    existing["duration_days"],
                    existing["activation_window_days"],
                    existing["capacity"],
                    existing["granted_user_count"],
                    tuple(existing["scene_ids"]),
                    existing["version"],
                    _utc(existing["locked_at"]),
                )
                CampaignService.revise_version(
                    version,
                    duration_days=cast(CampaignDuration, duration_days)
                    if duration_days is not None
                    else version.duration_days,
                    activation_window_days=(
                        activation_window_days
                        if activation_window_days is not None
                        else version.activation_window_days
                    ),
                    capacity=capacity if capacity is not None else version.capacity,
                    scene_ids=scene_ids if scene_ids is not None else version.scene_ids,
                )
                await session.execute(
                    text(
                        "UPDATE limited_campaign SET name = :name, version = version + 1, "
                        "updated_at = :now WHERE id = :id"
                    ),
                    {"name": name.strip(), "now": now, "id": row.id},
                )
                await session.execute(
                    text(
                        "UPDATE limited_campaign_version SET duration_days = :duration, "
                        "activation_window_days = :window, capacity = :capacity, "
                        "version = version + 1, updated_at = :now WHERE id = :id"
                    ),
                    {
                        "duration": version.duration_days,
                        "window": version.activation_window_days,
                        "capacity": version.capacity,
                        "now": now,
                        "id": version_row.id,
                    },
                )
                if scene_ids is not None:
                    await self._replace_scenes(session, version_row.id, scene_ids)
                internal_id = row.id
            result = await self._get(session, campaign_id)
            assert result is not None
            await self._audit(session, internal_id, "SAVE", before, result, actor_id, now)
            return result

    async def command(
        self,
        campaign_id: str,
        operation: str,
        *,
        expected_version: int,
        now: datetime,
        capacity: int | None = None,
        actor_id: str = "system",
    ) -> dict[str, Any]:
        target = {
            "open": "OPEN",
            "pause": "PAUSED",
            "resume": "OPEN",
            "end": "ENDED",
            "archive": "ARCHIVED",
        }
        allowed = {
            "open": {"DRAFT"},
            "pause": {"OPEN"},
            "resume": {"PAUSED"},
            "end": {"OPEN", "PAUSED"},
            "archive": {"ENDED", "CLOSED"},
        }
        if operation not in {*target, "capacity", "copy"}:
            raise AppError("CAMPAIGN_OPERATION_INVALID", "活动操作不支持", 422)
        async with self._session_factory() as session, session.begin():
            row = await self._lock_campaign(session, campaign_id, expected_version)
            before = await self._get(session, campaign_id)
            version = (
                await session.execute(
                    text(
                        "SELECT id, capacity, granted_user_count FROM limited_campaign_version "
                        "WHERE id = :id FOR UPDATE"
                    ),
                    {"id": row.current_version_id},
                )
            ).first()
            if version is None:
                raise AppError("CAMPAIGN_VERSION_NOT_FOUND", "活动版本不存在", 404)
            if operation == "copy":
                if row.status not in {"DRAFT", "ENDED", "CLOSED"}:
                    raise AppError("CAMPAIGN_STATE_CONFLICT", "当前活动状态不可复制版本", 409)
                new_id = new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign_version "
                        "(public_id, campaign_id, version_no, status, duration_days, "
                        "activation_window_days, capacity, granted_user_count, grant_starts_at, "
                        "grant_ends_at, locked_at, version, updated_at) "
                        "SELECT :new_id, campaign_id, "
                        "(SELECT MAX(version_no) + 1 FROM (SELECT version_no FROM "
                        "limited_campaign_version WHERE campaign_id = :campaign_id) x), "
                        "'DRAFT', duration_days, activation_window_days, capacity, 0, "
                        "grant_starts_at, grant_ends_at, locked_at, 1, :now "
                        "FROM limited_campaign_version WHERE id = :old_id"
                    ),
                    {"new_id": new_id, "campaign_id": row.id, "old_id": version.id, "now": now},
                )
                new_internal_id = await session.scalar(
                    text("SELECT id FROM limited_campaign_version WHERE public_id = :id"),
                    {"id": new_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO limited_campaign_scene "
                        "(campaign_version_id, scene_id, position, scene_revision_id) "
                        "SELECT :new_id, scene_id, position, scene_revision_id "
                        "FROM limited_campaign_scene WHERE campaign_version_id = :old_id"
                    ),
                    {"new_id": new_internal_id, "old_id": version.id},
                )
                await session.execute(
                    text(
                        "UPDATE limited_campaign SET current_version_id = :version, "
                        "status = 'DRAFT' WHERE id = :id"
                    ),
                    {"version": new_internal_id, "id": row.id},
                )
            elif operation == "capacity":
                if capacity is None or capacity < version.granted_user_count:
                    raise AppError(
                        "CAMPAIGN_CAPACITY_BELOW_GRANTED", "容量不能低于累计开通人数", 409
                    )
                await session.execute(
                    text(
                        "UPDATE limited_campaign_version SET capacity = :capacity, "
                        "version = version + 1, updated_at = :now WHERE id = :id"
                    ),
                    {"capacity": capacity, "now": now, "id": version.id},
                )
            else:
                if row.status not in allowed[operation]:
                    raise AppError("CAMPAIGN_STATE_CONFLICT", "当前活动状态不可执行此操作", 409)
                status = target[operation]
                await session.execute(
                    text("UPDATE limited_campaign SET status = :status WHERE id = :id"),
                    {"status": status, "id": row.id},
                )
                if operation != "archive":
                    await session.execute(
                        text(
                            "UPDATE limited_campaign_version SET status = :status, "
                            "version = version + 1, updated_at = :now WHERE id = :id"
                        ),
                        {"status": status, "now": now, "id": version.id},
                    )
            await session.execute(
                text(
                    "UPDATE limited_campaign SET version = version + 1, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"now": now, "id": row.id},
            )
            result = await self._get(session, campaign_id)
            assert result is not None
            await self._audit(session, row.id, operation.upper(), before, result, actor_id, now)
            return result

    async def _lock_campaign(
        self, session: AsyncSession, campaign_id: str, expected_version: int | None
    ) -> Any:
        row = (
            await session.execute(
                text(
                    "SELECT id, status, version, current_version_id FROM limited_campaign "
                    "WHERE public_id = :id FOR UPDATE"
                ),
                {"id": campaign_id},
            )
        ).first()
        if row is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "活动不存在", 404)
        if expected_version != row.version:
            raise AppError(
                "CAMPAIGN_VERSION_CONFLICT", "活动版本已变化", 409, {"current_version": row.version}
            )
        return row

    async def _replace_scenes(
        self, session: AsyncSession, version_id: int, scene_ids: tuple[str, ...]
    ) -> None:
        if len(set(scene_ids)) != len(scene_ids):
            raise AppError("CAMPAIGN_SCENES_INVALID", "活动场景不可重复", 422)
        await session.execute(
            text("DELETE FROM limited_campaign_scene WHERE campaign_version_id = :id"),
            {"id": version_id},
        )
        for position, scene_id in enumerate(scene_ids, 1):
            scene = (
                await session.execute(
                    text(
                        "SELECT id, published_revision_id FROM scene WHERE public_id = :id "
                        "AND status = 'PUBLISHED'"
                    ),
                    {"id": scene_id},
                )
            ).first()
            if scene is None or scene.published_revision_id is None:
                raise AppError("CAMPAIGN_SCENE_INVALID", "活动场景未发布", 422)
            await session.execute(
                text(
                    "INSERT INTO limited_campaign_scene (campaign_version_id, scene_id, "
                    "position, scene_revision_id) VALUES (:version, :scene, :position, :revision)"
                ),
                {
                    "version": version_id,
                    "scene": scene.id,
                    "position": position,
                    "revision": scene.published_revision_id,
                },
            )

    async def _audit(
        self,
        session: AsyncSession,
        internal_id: int,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        actor_id: str,
        now: datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO limited_campaign_operation (public_id, campaign_id, "
                "operation_type, before_summary, after_summary, operator_id, created_at) "
                "VALUES (:id, :campaign, :operation, :before, :after, :actor, :now)"
            ),
            {
                "id": new_ulid(now),
                "campaign": internal_id,
                "operation": operation,
                "before": None if before is None else json.dumps(_audit_summary(before)),
                "after": json.dumps(_audit_summary(after)),
                "actor": actor_id,
                "now": now,
            },
        )


def _utc(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)


def _audit_summary(campaign: dict[str, Any]) -> dict[str, Any]:
    version = campaign.get("current_version") or {}
    return {
        "id": campaign["id"],
        "status": campaign["status"],
        "version": campaign["version"],
        "current_version_id": version.get("id"),
        "capacity": version.get("capacity"),
        "granted_user_count": version.get("granted_user_count"),
    }
