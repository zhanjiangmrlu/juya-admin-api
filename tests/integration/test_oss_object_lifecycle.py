import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import bindparam, create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["none", "feedback", "media", "cover", "active"])
async def test_cleanup_protects_references_and_audits_without_object_urls(
    reference: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证清理保护引用且审计不包含对象 URL。
    # 参数:
    #     reference: 测试设置的媒体引用事实,用于判断清理是否应受保护。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_engine(url)
    now = datetime.now(UTC)
    ticket_id, other_id, asset_id, series_id = [new_ulid(now) for _ in range(4)]
    key = f"feedback/{ticket_id}/image.png"
    deleted: list[str] = []

    class FakeOss:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            # 功能:初始化 FakeOss 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
            #     _args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。 当前替身
            #       保留该形参以兼容调用接口。
            #     _kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。 当前替身保留
            #       该形参以兼容调用接口。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            pass

        async def delete_object(self, object_key: str) -> None:
            # 功能:模拟删除指定对象并记录清理操作。
            # 参数:
            #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
            #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            deleted.append(object_key)

    monkeypatch.setattr(maintenance, "AliyunOssProvider", FakeOss)
    settings = Settings(
        environment="test",
        database_url=url,
        oss_region="cn-shenzhen",
        oss_bucket="juya-test",
        oss_expected_bucket="juya-test",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
    )
    try:
        with engine.begin() as connection:
            for public_id in [ticket_id] + ([other_id] if reference == "feedback" else []):
                connection.execute(
                    text(
                        "INSERT INTO feedback_ticket (public_id,user_id,category,description,"
                        "source,"
                        "status,sla_hours,create_idempotency_key,created_at,updated_at) VALUES "
                        "(:id,NULL,'FUNCTION','test',JSON_OBJECT(),:status,48,:id,:now,:now)"
                    ),
                    {
                        "id": public_id,
                        "status": "PROCESSING" if reference == "active" else "RESOLVED",
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO feedback_screenshot "
                        "(ticket_id,object_key,security_status,delete_after)"
                        " SELECT id,:key,'PASSED',:due FROM feedback_ticket WHERE public_id=:id"
                    ),
                    {"id": public_id, "key": key, "due": None if public_id == other_id else now},
                )
            if reference == "media":
                connection.execute(
                    text(
                        "INSERT INTO media_asset (public_id,object_key,asset_type,content_type,"
                        "size_bytes,"
                        "sha256,status,security_status,created_by,created_at) VALUES "
                        "(:id,:key,'images','image/png',10,:hash,'CONFIRMED','PASSED','system',:now)"
                    ),
                    {"id": asset_id, "key": key, "hash": ticket_id.ljust(64, "0"), "now": now},
                )
            if reference == "cover":
                connection.execute(
                    text(
                        "INSERT INTO content_series (public_id,slug,title,cover_object_key,status) "
                        "VALUES (:id,:id,'test',:key,'PUBLISHED')"
                    ),
                    {"id": series_id, "key": key},
                )
        result = await maintenance._cleanup_feedback_screenshots(settings)
        assert result == {"deleted": int(reference == "none"), "failed": 0}
        assert deleted == ([key] if reference == "none" else [])
        with engine.connect() as connection:
            audit = connection.execute(
                text(
                    "SELECT after_summary FROM audit_event WHERE object_type='feedback_screenshot' "
                    "AND object_public_id=:id ORDER BY id DESC LIMIT 1"
                ),
                {"id": ticket_id},
            ).scalar()
        assert audit is not None
        summary = json.loads(audit) if isinstance(audit, str) else audit
        assert summary["outcome"] == ("DELETED" if reference == "none" else "PROTECTED")
        assert key not in str(summary)
        assert "secret" not in str(summary)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM feedback_ticket WHERE public_id IN (:id,:other)"),
                {"id": ticket_id, "other": other_id},
            )
            connection.execute(
                text("DELETE FROM media_asset WHERE public_id=:id"), {"id": asset_id}
            )
            connection.execute(
                text("DELETE FROM content_series WHERE public_id=:id"), {"id": series_id}
            )
            connection.execute(
                text("DELETE FROM audit_event WHERE object_public_id=:id"), {"id": ticket_id}
            )
        engine.dispose()


@pytest.mark.asyncio
async def test_protected_full_batch_does_not_starve_later_deletable_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证整批受保护记录不会阻塞后续可删除截图。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_engine(url)
    now = datetime.now(UTC)
    group = new_ulid(now)
    ids = [new_ulid(now) for _ in range(101)]
    shared_key = f"feedback/{group}/shared.png"
    final_key = f"feedback/{group}/last.png"

    class FakeOss:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            # 功能:初始化 FakeOss 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
            #     _args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。 当前替身
            #       保留该形参以兼容调用接口。
            #     _kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。 当前替身保留
            #       该形参以兼容调用接口。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            pass

        async def delete_object(self, object_key: str) -> None:
            # 功能:模拟删除指定对象并记录清理操作。
            # 参数:
            #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
            #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            assert object_key == final_key  # The shared object must never be deleted.

    monkeypatch.setattr(maintenance, "AliyunOssProvider", FakeOss)
    settings = Settings(
        environment="test",
        database_url=url,
        oss_region="cn-shenzhen",
        oss_bucket="juya-test",
        oss_expected_bucket="juya-test",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
    )
    try:
        with engine.begin() as connection:
            for index, public_id in enumerate(ids):
                connection.execute(
                    text(
                        "INSERT INTO feedback_ticket (public_id,user_id,category,description,"
                        "source,"
                        "status,sla_hours,create_idempotency_key,created_at,updated_at) VALUES "
                        "(:id,NULL,'FUNCTION','test',JSON_OBJECT(),'RESOLVED',48,:id,:now,:now)"
                    ),
                    {"id": public_id, "now": now},
                )
                connection.execute(
                    text(
                        "INSERT INTO feedback_screenshot "
                        "(ticket_id,object_key,security_status,delete_after) "
                        "SELECT id,:key,'PASSED',:now FROM feedback_ticket WHERE public_id=:id"
                    ),
                    {"id": public_id, "key": final_key if index == 100 else shared_key, "now": now},
                )
        first = await maintenance._cleanup_feedback_screenshots(settings)
        second = await maintenance._cleanup_feedback_screenshots(settings)
        assert first == {"deleted": 0, "failed": 0}
        assert second == {"deleted": 1, "failed": 0}
        with engine.connect() as connection:
            final_deleted = connection.execute(
                text("SELECT deleted_at FROM feedback_screenshot WHERE object_key=:key"),
                {"key": final_key},
            ).scalar()
            shared_deleted = connection.execute(
                text(
                    "SELECT COUNT(*) FROM feedback_screenshot WHERE object_key=:key "
                    "AND deleted_at IS NOT NULL"
                ),
                {"key": shared_key},
            ).scalar()
        assert final_deleted is not None
        assert shared_deleted == 0
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM feedback_ticket WHERE public_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": ids},
            )
            connection.execute(
                text("DELETE FROM audit_event WHERE object_public_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": ids},
            )
        engine.dispose()
