from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditRepository, AuditService
from juya_admin_api.modules.content.domain import PublishCheck, Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.router import create_content_router
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.shared.errors import install_error_handlers

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def make_client(repository: InMemoryContentRepository) -> TestClient:
    # 功能:组装当前用例所需服务、错误处理器及路由的测试客户端。
    # 参数:
    #     repository: 测试使用的内存仓库,供设置草稿并组装服务。
    # 返回:HTTP 测试客户端。
    repository.scenes["scene"] = Scene(id="scene", series_id="series", status="DRAFT")
    repository.revisions["revision"] = SceneRevision(
        id="revision", scene_id="scene", source_revision_id=None, version=2, content={}
    )

    async def current_admin() -> SessionRecord:
        # 功能:提供当前测试的管理员认证依赖。
        # 参数:无。
        # 返回:测试管理员会话。
        return SessionRecord("session", 1, "token", "csrf", "test", NOW, NOW)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_content_router(
            ContentService(repository),
            audit_service=AuditService(AsyncMock(spec=AuditRepository)),
            current_admin=current_admin,
            current_admin_write=current_admin,
        )
    )
    return TestClient(app)


def test_incomplete_draft_returns_check_results_but_cannot_publish() -> None:
    # 功能:验证不完整草稿返回检查项但禁止发布。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    with make_client(InMemoryContentRepository(require_review=False)) as client:
        checked = client.post("/api/v1/admin/content/revisions/revision/publish-checks", json={})
        assert checked.status_code == 200
        result = checked.json()
        assert result["ready"] is False and result["version"] == 2
        assert {"TITLE_REQUIRED", "DIALOGUE_REQUIRED", "AUDIO_MISSING"} <= set(
            result["error_codes"]
        )
        assert result["warning_codes"] == []
        published = client.post(
            "/api/v1/admin/content/revisions/revision/commands/publish",
            json={"expected_version": 2},
            headers={"X-Idempotency-Key": "incomplete-draft"},
        )
        assert published.status_code == 409
        assert published.json()["code"] == "PUBLISH_CHECK_FAILED"
        missing = client.post("/api/v1/admin/content/revisions/missing/publish-checks", json={})
        assert missing.status_code == 404


def test_scene_catalog_preserves_legacy_template_identifiers() -> None:
    # 功能:验证场景目录保留旧模板标识。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryContentRepository()
    with make_client(repository) as client:
        repository.scenes["scene"].template_type = "learning-card"
        page = client.get("/api/v1/admin/content/scenes")
        detail = client.get("/api/v1/admin/content/scenes/scene")
        assert page.status_code == detail.status_code == 200
        assert page.json()["items"][0]["template_type"] == "learning-card"
        assert detail.json()["template_type"] == "learning-card"


@pytest.mark.parametrize("acknowledged,ready", [([], False), (["OPTIONAL_NOTICE"], True)])
def test_warning_check_returns_results_until_acknowledged(
    acknowledged: list[str], ready: bool
) -> None:
    # 功能:验证未确认警告时检查接口仍返回检查结果。
    # 参数:
    #     acknowledged: 管理员是否已确认发布警告。
    #     ready: 预设发布资源是否满足就绪条件。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    class WarningRepository(InMemoryContentRepository):
        async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]:
            # 功能:返回当前测试预设的发布检查结果。
            # 参数:
            #     self: 当前 WarningRepository 测试替身实例,保存本用例的预设状态或调用记录。
            #     revision_id: 待检查或发布的内容修订标识。
            # 返回:list[PublishCheck],由本用例预设的数据或所组装的测试资源构成。
            return [PublishCheck("OPTIONAL_NOTICE", "WARNING", False)]

    with make_client(WarningRepository()) as client:
        checked = client.post(
            "/api/v1/admin/content/revisions/revision/publish-checks",
            json={"acknowledged_warning_codes": acknowledged},
        )
        assert checked.status_code == 200
        assert checked.json()["ready"] is ready
        assert checked.json()["error_codes"] == []
        assert checked.json()["warning_codes"] == ["OPTIONAL_NOTICE"]
