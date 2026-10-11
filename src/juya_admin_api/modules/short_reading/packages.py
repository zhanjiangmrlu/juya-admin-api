"""Typed package association without modifying legacy scene membership."""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.shared.errors import AppError


async def associate_published_within(
    session: AsyncSession, package_id: str, content_type: str, content_id: str, position: int
) -> None:
    if content_type not in {"dialogue", "word_card", "short_reading"} or position < 1:
        raise AppError("PACKAGE_CONTENT_INVALID", "内容类型或排序无效", 422)
    package = await session.scalar(
        text("SELECT id FROM content_package WHERE public_id=:id FOR UPDATE"), {"id": package_id}
    )
    if package is None:
        raise AppError("PACKAGE_NOT_FOUND", "内容包不存在", 404)
    if content_type == "short_reading":
        valid = await session.scalar(
            text(
                "SELECT public_id FROM short_reading "
                "WHERE public_id=:id AND status='PUBLISHED' AND published_revision_id IS NOT NULL "
                "FOR UPDATE"
            ),
            {"id": content_id},
        )
    else:
        template = "dialogue" if content_type == "dialogue" else "vocabulary"
        valid = await session.scalar(
            text(
                "SELECT s.public_id FROM scene s "
                "JOIN scene_revision r ON r.id=s.published_revision_id "
                "JOIN content_template t ON t.id=s.template_id "
                "WHERE s.public_id=:id AND s.status='PUBLISHED' AND t.template_type=:template "
                "FOR UPDATE"
            ),
            {"id": content_id, "template": template},
        )
    if valid is None:
        raise AppError("CONTENT_NOT_PUBLISHED", "仅允许关联对应类型的已发布内容", 409)
    await session.execute(
        text(
            "INSERT INTO content_package_item(package_id,content_type,"
            "content_id,position,created_at) VALUES(:package,:type,:content,:position,:now) "
            "ON DUPLICATE KEY UPDATE position=:position"
        ),
        {
            "package": package,
            "type": content_type,
            "content": content_id,
            "position": position,
            "now": datetime.now(UTC),
        },
    )
