"""Startup reminders use individual grants, independently of campaign grant windows."""

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from juya_admin_api.modules.work_items.repository import SQLAlchemyWorkItemSource


@pytest.mark.asyncio
async def test_startup_deadline_reminders_ignore_campaign_window_and_started_grants() -> None:
    # 功能: 使用真实查询验证限时权益截止边界与排除状态
    # 参数: 无
    # 返回: 无,断言失败时报告不符合启动时限的事项
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE feedback_ticket (public_id TEXT,status TEXT,deadline_at TEXT,created_at TEXT);
        CREATE TABLE limited_campaign_version (public_id TEXT,status TEXT,grant_ends_at TEXT);
        CREATE TABLE limited_entitlement (
            public_id TEXT,status TEXT,start_deadline TEXT,activated_at TEXT,expires_at TEXT
        );
        INSERT INTO limited_campaign_version VALUES ('campaign-only','OPEN','2026-10-11 00:00:00');
        INSERT INTO limited_entitlement VALUES
          ('due','PENDING','2026-10-11 20:00:00',NULL,NULL),
          ('edge','PENDING','2026-10-11 12:00:00',NULL,NULL),
          ('expired','START_EXPIRED','2026-10-10 12:00:00',NULL,NULL),
          ('future','PENDING','2026-10-13 12:00:00',NULL,NULL),
          ('paused','PAUSED','2026-10-11 12:00:00',NULL,NULL),
          ('revoked','REVOKED','2026-10-11 12:00:00',NULL,NULL),
          ('started','ACTIVE','2026-10-11 12:00:00','2026-10-10 12:00:00','2026-10-12 11:00:00');
    """)

    class Session:
        async def execute(self, statement, parameters):
            # 功能: 在隔离 SQLite 数据上执行仓储查询并兼容 MySQL 日期和拼接函数
            # 参数: statement 为真实仓储语句,parameters 为其时间参数
            # 返回: 保留仓储行访问语义的结果
            sql = str(statement).replace(
                "DATE_ADD(:now, INTERVAL 12 HOUR)", "datetime(:now, '+12 hours')"
            )
            sql = sql.replace("DATE_ADD(:now, INTERVAL 24 HOUR)", "datetime(:now, '+24 hours')")
            db.create_function("CONCAT", -1, lambda *parts: "".join(parts))
            rows = db.execute(
                sql, {"now": parameters["now"].strftime("%Y-%m-%d %H:%M:%S")}
            ).fetchall()
            result = [
                SimpleNamespace(**(dict(row) | {"due_at": datetime.fromisoformat(row["due_at"])}))
                for row in rows
            ]
            return SimpleNamespace(all=lambda: result)

    @asynccontextmanager
    async def sessions():
        # 功能: 提供隔离仓储查询会话
        # 参数: 无
        # 返回: 测试会话
        yield Session()

    try:
        facts = await SQLAlchemyWorkItemSource(sessions).facts(
            datetime(2026, 10, 11, 12, tzinfo=UTC)
        )
        by_key = {fact.key: fact.kind for fact in facts}
        assert by_key == {
            "limited:due": "CAMPAIGN_STARTING",
            "limited:edge": "CAMPAIGN_START_EXPIRED",
            "limited:expired": "CAMPAIGN_START_EXPIRED",
            "limited:started": "ACTIVE_ENTITLEMENT_EXPIRING",
        }
    finally:
        db.close()
