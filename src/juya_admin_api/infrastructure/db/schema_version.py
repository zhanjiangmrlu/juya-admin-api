from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def check_minimum_schema_version(
    factory: async_sessionmaker[AsyncSession], minimum_version: int
) -> Mapping[str, bool]:
    async with factory() as session:
        current_version = await session.scalar(
            text("SELECT version FROM schema_version WHERE id = 1")
        )
    return {
        "mysql": current_version is not None,
        "schema": bool(current_version >= minimum_version),
    }
