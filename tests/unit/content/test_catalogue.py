"""Execute the unified catalogue SQL on representative relational content."""

import json
import sqlite3
from typing import Any

import pytest

from juya_admin_api.modules.content.catalogue_router import read_catalogue


class Result:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.cursor = cursor

    def mappings(self) -> "Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.cursor.fetchall()]


class Session:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.create_function("JSON_UNQUOTE", 1, lambda value: value)
        self.db.executescript("""
          CREATE TABLE content_series(id INTEGER,public_id TEXT,title TEXT);
          CREATE TABLE content_template(id INTEGER,template_type TEXT);
          CREATE TABLE scene(id INTEGER,public_id TEXT,series_id INTEGER,template_id INTEGER,
            title TEXT,status TEXT,draft_revision_id INTEGER,published_revision_id INTEGER,
            updated_at TEXT);
          CREATE TABLE scene_revision(id INTEGER,public_id TEXT,content_snapshot TEXT);
          CREATE TABLE short_reading(public_id TEXT,series_id TEXT,status TEXT,
            draft_revision_id TEXT,published_revision_id TEXT,updated_at TEXT);
          CREATE TABLE short_reading_revision(public_id TEXT,content_snapshot TEXT);
          INSERT INTO content_series VALUES(1,'series','日常英语');
          INSERT INTO content_template VALUES(1,'dialogue'),(2,'vocabulary');
          INSERT INTO scene VALUES(1,'s1',1,1,'Cafe','PUBLISHED',1,2,NULL);
          INSERT INTO scene VALUES(2,'s2',1,2,'Words','DRAFT',3,NULL,NULL);
          INSERT INTO short_reading VALUES('r1','series','DRAFT','rr1',NULL,NULL);
        """)
        for identity, public_id, en, zh in [
            (1, "rev1", "Cafe", "在咖啡店"),
            (2, "rev2", "Cafe", "旧标题"),
            (3, "rev3", "Words", "词卡"),
        ]:
            self.db.execute(
                "INSERT INTO scene_revision VALUES(?,?,?)",
                (identity, public_id, json.dumps({"title_en": en, "title_zh": zh})),
            )
        self.db.execute(
            "INSERT INTO short_reading_revision VALUES(?,?)",
            ("rr1", json.dumps({"title_en": "Garden", "title_zh": "城市花园"})),
        )

    async def scalar(self, statement: Any, params: dict[str, Any]) -> int:
        return int(self.db.execute(str(statement), params).fetchone()[0])

    async def execute(self, statement: Any, params: dict[str, Any]) -> Result:
        return Result(self.db.execute(str(statement), params))


@pytest.mark.asyncio
async def test_catalogue_pages_all_types_after_filtering_and_searches_chinese() -> None:
    session = Session()
    first = await read_catalogue(session, page=1, page_size=2)
    second = await read_catalogue(session, page=2, page_size=2)
    assert first["total"] == second["total"] == 3
    assert {row["content_type"] for row in first["items"] + second["items"]} == {
        "dialogue",
        "word_card",
        "short_reading",
    }
    found = await read_catalogue(session, query="城市", page_size=1)
    assert found["total"] == 1 and found["items"][0]["id"] == "r1"
    cards = await read_catalogue(session, content_type="word_card")
    assert cards["total"] == 1 and cards["items"][0]["id"] == "s2"
    published = await read_catalogue(session, status="PUBLISHED")
    assert published["total"] == 1
    assert published["items"][0]["title_zh"] == "在咖啡店"
    assert published["items"][0]["published_revision_id"] == "rev2"


@pytest.mark.asyncio
async def test_catalogue_filters_do_not_turn_wildcards_or_sql_into_matches() -> None:
    session = Session()
    assert (await read_catalogue(session, query="%"))["total"] == 0
    assert (await read_catalogue(session, query="' OR 1=1 --"))["total"] == 0
    assert (await read_catalogue(session, series_id="other"))["total"] == 0
    assert (await read_catalogue(session, content_type="dialogue", status="DRAFT"))["total"] == 0
