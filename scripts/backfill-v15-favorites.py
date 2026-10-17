"""Default report only. Never uses the application's default/shared database URL."""

import argparse
import asyncio
import json
import os

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.v15_backfill import FavoriteBackfill


async def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    url = os.getenv("JUYA_BACKFILL_DATABASE_URL")
    if not url:
        parser.error(
            "JUYA_BACKFILL_DATABASE_URL must explicitly select an isolated/approved database"
        )
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    try:
        result = await FavoriteBackfill(async_sessionmaker(engine)).run(
            execute=args.execute, batch_size=args.batch_size
        )
        print(json.dumps(result, ensure_ascii=False))
    except SQLAlchemyError:
        raise SystemExit(
            "Backfill failed: check database grants/schema/data; current page rolled back"
        ) from None
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
