from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from juya_admin_api.modules.audit.service import AuditService
from juya_admin_api.modules.content.domain import Scene, SceneRevision
from juya_admin_api.modules.content.production_router import create_production_content_router
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.domain import OcrCandidate
from juya_admin_api.modules.media.service import MediaAdminService, MediaService
from juya_admin_api.shared.errors import AppError, install_error_handlers


@pytest.mark.parametrize("mismatch", [None, "revision_id", "scene_id", "asset_id"])
def test_suggestions_are_authenticated_bound_to_draft_and_use_scene_template(mismatch):
    # 功能:验证 OCR 建议要求认证、绑定草稿且采用场景模板。
    # 参数:
    #     mismatch: 是否制造请求或资源归属不匹配。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    content = AsyncMock(spec=ContentService)
    content.get_revision.return_value = SceneRevision(
        "rev", "scene", None, content={"original_image_asset_id": "image"}
    )
    content.get_scene.return_value = Scene("scene", "series", template_type="vocabulary")
    admin_media = AsyncMock(spec=MediaAdminService)
    payload = {"revision_id": "rev", "scene_id": "scene"}
    if mismatch in payload:
        payload[mismatch] = "unrelated"
    admin_media.get_job.return_value = SimpleNamespace(input_payload=payload)
    admin_media.get_ocr_candidate.return_value = OcrCandidate(
        "candidate",
        "job",
        "unrelated" if mismatch == "asset_id" else "image",
        "business",
        None,
        "SUCCEEDED",
        "dialogue",
        {"blocks": [{"text": "apple"}]},
        0.9,
        None,
        datetime.now(UTC),
    )

    async def current_admin(request: Request):
        # 功能:提供当前测试的管理员认证依赖。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:本用例预设的调用结果或所构造的测试资源。
        if request.headers.get("Authorization") != "test-session":
            raise AppError("SESSION_REQUIRED", "请先登录", 401)
        return Mock()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_production_content_router(
            Mock(spec=ProductionStore),
            content,
            Mock(spec=MediaService),
            admin_media,
            audit_service=Mock(spec=AuditService),
            current_admin=current_admin,
            current_admin_write=current_admin,
        )
    )
    with TestClient(app) as client:
        path = "/api/v1/admin/content/revisions/rev/ocr-suggestions/job"
        assert client.get(path).status_code == 401
        content.get_revision.assert_not_awaited()
        response = client.get(path, headers={"Authorization": "test-session"})
    if mismatch:
        assert response.status_code == 409
        assert response.json()["code"] == "OCR_REVISION_INVALID"
        content.get_scene.assert_not_awaited()
    else:
        assert response.status_code == 200
        assert response.json()["template_type"] == "vocabulary"
        assert response.json()["unassigned_line_ids"] == [0]
    # Only existing read methods are used; no dispatcher/provider or mutation is needed.
    assert {call[0] for call in admin_media.mock_calls} == {"get_job", "get_ocr_candidate"}
