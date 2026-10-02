from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def limited_achievements(
    session: AsyncSession,
    user_id: int,
    version_id: int,
    activated_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> dict[str, int]:
    """Count activity records; scene and favorite counts use its fixed scene set."""
    empty = {
        "completed_scenes": 0,
        "learning_days": 0,
        "favorite_vocabulary": 0,
        "favorite_phrases": 0,
    }
    if activated_at is None:
        return empty
    start = activated_at.replace(tzinfo=UTC) if activated_at.tzinfo is None else activated_at
    expiry = (
        expires_at.replace(tzinfo=UTC) if expires_at and expires_at.tzinfo is None else expires_at
    )
    end = min(now, expiry) if expiry else now
    parameters = {
        "user": user_id,
        "version": version_id,
        "start": start.astimezone(UTC).replace(tzinfo=None),
        "end": end.astimezone(UTC).replace(tzinfo=None),
    }
    completed = await session.scalar(
        text(
            "SELECT COUNT(DISTINCT p.scene_id) FROM learning_progress p "
            "JOIN scene s ON s.public_id=p.scene_id "
            "JOIN limited_campaign_scene cs ON cs.scene_id=s.id "
            "WHERE p.user_id=:user AND cs.campaign_version_id=:version "
            "AND p.completed_at>=:start AND p.completed_at<:end"
        ),
        parameters,
    )
    days = await session.scalar(
        text(
            "SELECT COUNT(DISTINCT DATE(CONVERT_TZ(occurred_at,'+00:00','+08:00'))) "
            "FROM analytics_event WHERE user_id=:user AND event_type='USER_ACTIVE' "
            "AND occurred_at>=:start AND occurred_at<:end"
        ),
        parameters,
    )
    favorites = (
        (
            await session.execute(
                text(
                    "SELECT f.entry_type,COUNT(DISTINCT f.id) AS total FROM favorite_entry f "
                    "JOIN favorite_source fs ON fs.favorite_id=f.id "
                    "JOIN scene s ON s.public_id=fs.scene_id "
                    "JOIN limited_campaign_scene cs ON cs.scene_id=s.id "
                    "WHERE f.user_id=:user AND cs.campaign_version_id=:version "
                    "AND fs.created_at>=:start AND fs.created_at<:end GROUP BY f.entry_type"
                ),
                parameters,
            )
        )
        .mappings()
        .all()
    )
    counts = {row["entry_type"]: int(row["total"]) for row in favorites}
    return {
        "completed_scenes": int(completed or 0),
        "learning_days": int(days or 0),
        "favorite_vocabulary": counts.get("VOCABULARY", 0),
        "favorite_phrases": counts.get("PHRASE", 0),
    }
