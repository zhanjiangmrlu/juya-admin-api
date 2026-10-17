import json
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from juya_admin_api.modules.short_reading.router import create_short_reading_router
from juya_admin_api.modules.short_reading.store import ShortReadingStore


class Result:
    def __init__(self, cursor):
        self.rows = [dict(row) for row in cursor.fetchall()]

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class Session:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE short_reading(public_id TEXT,series_id TEXT,status TEXT,"
            "draft_revision_id TEXT,published_revision_id TEXT,version INT,sort_order INT);"
            "CREATE TABLE short_reading_trial(short_reading_id TEXT,enabled INT);"
            "CREATE TABLE short_reading_revision(public_id TEXT,content_snapshot TEXT);"
        )
        for index, (status, series, trial) in enumerate(
            [("DRAFT", "A", 0), ("PUBLISHED", "A", 1), ("PUBLISHED", "A", 0), ("PUBLISHED", "B", 1)]
        ):
            identity, revision = f"reading-{index}", f"revision-{index}"
            self.db.execute(
                "INSERT INTO short_reading VALUES(?,?,?,?,?,?,?)",
                (identity, series, status, None, revision, 3, index),
            )
            self.db.execute("INSERT INTO short_reading_trial VALUES(?,?)", (identity, trial))
            self.db.execute(
                "INSERT INTO short_reading_revision VALUES(?,?)",
                (revision, json.dumps({"title_en": "Coffee", "title_zh": "咖啡"})),
            )

    async def execute(self, statement, parameters=None):
        return Result(self.db.execute(str(statement), parameters or {}))

    async def scalar(self, statement, parameters=None):
        return self.db.execute(str(statement), parameters or {}).fetchone()[0]


class Sessions:
    def __init__(self, session):
        self.session = session

    @asynccontextmanager
    async def __call__(self):
        yield self.session


@pytest.mark.asyncio
async def test_admin_trial_readback_and_filters_precede_pagination():
    session = Session()
    store = ShortReadingStore(Sessions(session))

    async def admin():
        return object()

    app = FastAPI()
    app.include_router(
        create_short_reading_router(store, current_admin=admin, current_admin_write=admin)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/admin/content/short-readings",
            params={
                "status": "PUBLISHED",
                "series_id": "A",
                "query": "咖啡",
                "page": 2,
                "page_size": 1,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 2
        assert [row["public_id"] for row in result["items"]] == ["reading-2"]
        assert result["items"][0]["trial_enabled"] is False
        assert (await client.get("/api/v1/admin/content/short-readings/reading-1")).json()[
            "trial_enabled"
        ] is True
        assert (await client.get("/api/v1/admin/content/short-readings/reading-2")).json()[
            "trial_enabled"
        ] is False
        assert (
            await client.get("/api/v1/admin/content/short-readings", params={"status": "unknown"})
        ).status_code == 422
    session.db.close()
