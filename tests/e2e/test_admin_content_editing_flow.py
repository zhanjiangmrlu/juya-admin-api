from datetime import UTC, datetime

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.domain import Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.router import create_content_router
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.shared.errors import AppError, install_error_handlers

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


class AuditRepository:
    def __init__(self) -> None:
        # 功能:初始化 AuditRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        # 功能:向测试仓库追加审计事件,供后续断言操作次数和内容。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     event: 待记录的审计或业务事件。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        # 功能:从测试仓库返回最近的指定数量审计事件。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
        # 返回:list[AuditEvent],由本用例预设的数据或所组装的测试资源构成。
        return self.events[-limit:]


def _client() -> tuple[TestClient, InMemoryContentRepository, AuditRepository]:

    # 功能:创建使用 HTTP 替身传输的小程序内部接口客户端。
    # 参数:无。
    # 返回:tuple[TestClient, InMemoryContentRepository, AuditRepository],由本用例预设的数据或所
    #       组装的测试资源构成。
    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        # 功能:检查测试认证头并返回预设管理员会话。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        # 返回:测试管理员会话。
        if x_test_admin is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return SessionRecord("session", 7, "token", "csrf", "test device", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        # 功能:在测试认证通过后检查写请求 CSRF 令牌。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        #     x_csrf_token: 写操作请求的 CSRF 令牌,与当前测试会话的预设值比较。
        # 返回:测试管理员会话。
        admin = await read_admin(x_test_admin)
        if x_csrf_token != "csrf":
            raise AppError("ADMIN_CSRF_INVALID", "CSRF token 无效", 403)
        return admin

    repository = InMemoryContentRepository()
    for index in range(1, 7):
        scene_id = f"scene-{index}"
        repository.scenes[scene_id] = Scene(
            scene_id,
            "series-1",
            title=f"Scene {index}",
            series_title="Daily English",
            summary=f"Summary {index}",
            status="PUBLISHED",
            draft_revision_id="draft-1" if index == 1 else None,
            published_revision_id=f"published-{index}",
            updated_at=NOW,
        )
        repository.revisions[f"published-{index}"] = SceneRevision(
            f"published-{index}",
            scene_id,
            None,
            version=1,
            status="PUBLISHED",
            content={"title_en": f"Published {index}"},
            created_by="7",
            created_at=NOW,
        )
    repository.revisions["draft-1"] = SceneRevision(
        "draft-1",
        "scene-1",
        "published-1",
        version=3,
        status="DRAFT",
        stable_sentence_ids=("sentence-1",),
        stable_entry_ids=("entry-1",),
        content={"title_en": "Local draft", "dialogue": []},
        created_by="7",
        created_at=NOW,
    )
    audit_repository = AuditRepository()
    app = FastAPI()
    install_error_handlers(app)
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    app.include_router(
        create_content_router(
            ContentService(repository),
            current_admin=read_admin,
            current_admin_write=write_admin,
            audit_service=AuditService(audit_repository),
            clock=lambda: NOW,
        )
    )
    return TestClient(app), repository, audit_repository


def test_content_catalog_and_details_require_session_and_return_real_data() -> None:
    # 功能:验证内容目录和详情要求登录并返回实际数据。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _repository, _audit = _client()
    assert client.get("/api/v1/admin/content/scenes").status_code == 401
    headers = {"X-Test-Admin": "1"}

    page = client.get(
        "/api/v1/admin/content/scenes?query=Scene&series_id=series-1"
        "&status=PUBLISHED&page=1&page_size=2",
        headers=headers,
    )
    scene = client.get("/api/v1/admin/content/scenes/scene-1", headers=headers)
    revision = client.get("/api/v1/admin/content/revisions/draft-1", headers=headers)

    assert page.status_code == 200
    assert page.json()["total"] == 6
    assert len(page.json()["items"]) == 2
    assert scene.json()["draft_revision_id"] == "draft-1"
    assert revision.json()["version"] == 3
    assert revision.json()["stable_sentence_ids"] == ["sentence-1"]


def test_complete_history_requires_session_and_rejects_invalid_sources_without_publication():
    # 功能:验证完整历史要求登录且未发布内容的无效来源被拒绝。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository, _audit = _client()
    path = "/api/v1/admin/content/scenes/scene-1/revisions"
    assert client.get(path).status_code == 401
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
    first = client.get(path + "?page=1&page_size=1", headers=headers)
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert first.json()["items"][0]["id"] == "draft-1"
    second = client.get(path + "?page=2&page_size=1", headers=headers)
    assert second.json()["items"][0]["is_current"] is True
    assert client.get(path + "?page_size=101", headers=headers).status_code == 422
    assert (
        client.post(path, headers=headers, json={"source_revision_id": "published-2"}).status_code
        == 422
    )
    candidate = client.post(path, headers=headers, json={"source_revision_id": "published-1"})
    assert candidate.status_code == 201
    assert candidate.json()["source_revision_id"] == "published-1"
    assert repository.scenes["scene-1"].published_revision_id == "published-1"
    assert client.get("/api/v1/admin/content/scenes?status=BAD", headers=headers).status_code == 422


def test_revision_save_requires_csrf_and_returns_current_version_on_conflict() -> None:
    # 功能:验证保存修订要求 CSRF 且版本冲突返回当前版本。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository, audit = _client()
    path = "/api/v1/admin/content/revisions/draft-1"
    assert (
        client.put(
            path,
            json={"expected_version": 3, "content": {"title_en": "Saved"}},
            headers={"X-Test-Admin": "1"},
        ).status_code
        == 403
    )
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
    invalid = client.put(
        path,
        json={"expected_version": 0, "content": {"title_en": "Invalid"}},
        headers=headers,
    )
    assert invalid.status_code == 422

    conflict = client.put(
        path,
        json={"expected_version": 2, "content": {"title_en": "Stale"}},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["details"] == {
        "current_revision_id": "draft-1",
        "current_version": 3,
    }
    assert repository.revisions["draft-1"].content["title_en"] == "Local draft"

    saved = client.put(
        path,
        json={"expected_version": 3, "content": {"title_en": "Saved", "dialogue": []}},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 4
    assert [event.action for event in audit.events] == ["content.revision.save"]
    assert "Saved" not in repr(audit.events)


def test_discovery_config_read_write_uses_one_version_and_validates_modules() -> None:
    # 功能:验证发现页配置共享同一版本并检查模块约束。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _repository, audit = _client()
    read_headers = {"X-Test-Admin": "1"}
    write_headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
    path = "/api/v1/admin/content/discovery-config"

    initial = client.get(path, headers=read_headers)
    assert initial.status_code == 200
    assert initial.json()["version"] == 0

    invalid = client.put(
        path,
        json={
            "expected_version": 0,
            "open_scene_ids": ["scene-1", "scene-2", "scene-3"],
            "preview_by_series": {"series-1": ["scene-4", "scene-5", "scene-6"]},
            "learning_modules": {"scene_learning": True, "grammar": True},
        },
        headers=write_headers,
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "LEARNING_MODULE_NOT_ALLOWED"

    saved = client.put(
        path,
        json={
            "expected_version": 0,
            "open_scene_ids": ["scene-1", "scene-2", "scene-3"],
            "preview_by_series": {"series-1": ["scene-4", "scene-5", "scene-6"]},
            "learning_modules": {"scene_learning": True, "grammar": False},
        },
        headers=write_headers,
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert client.get(path, headers=read_headers).json() == saved.json()
    assert [event.action for event in audit.events] == ["content.discovery-config.save"]


def test_admin_preview_is_no_store_includes_draft_and_has_no_side_effect() -> None:
    # 功能:验证管理员预览包含草稿、禁用缓存且不产生业务副作用。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository, audit = _client()
    before = (
        repository.scenes["scene-1"].status,
        repository.scenes["scene-1"].published_revision_id,
        repository.revisions["draft-1"].status,
    )

    response = client.get(
        "/api/v1/admin/content/revisions/draft-1/preview",
        headers={"X-Test-Admin": "1"},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["revision_status"] == "DRAFT"
    assert response.json()["content"]["title_en"] == "Local draft"
    assert (
        repository.scenes["scene-1"].status,
        repository.scenes["scene-1"].published_revision_id,
        repository.revisions["draft-1"].status,
    ) == before
    assert audit.events == []
