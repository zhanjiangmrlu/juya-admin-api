from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.access_policy.domain import AccessGrant, AccessLevel
from juya_admin_api.modules.access_policy.router import serialize_preview_scene
from juya_admin_api.modules.access_policy.service import AccessPolicyService

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class FakeContentAccess:
    def __init__(self, *, opened: bool = False, preview: bool = False) -> None:
        # 功能:初始化 FakeContentAccess 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeContentAccess 测试替身实例,保存本用例的预设状态或调用记录。
        #     opened: 预设场景是否开放访问。
        #     preview: 预设场景是否允许预览。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.opened = opened
        self.preview = preview

    async def is_open(self, scene_id: str, now: datetime) -> bool:
        # 功能:返回当前场景预设的开放访问状态。
        # 参数:
        #     self: 当前 FakeContentAccess 测试替身实例,保存本用例的预设状态或调用记录。
        #     scene_id: 目标学习场景标识。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:是否开放访问的预设布尔值。
        return self.opened

    async def is_preview(self, scene_id: str, now: datetime) -> bool:
        # 功能:返回当前场景预设的预览访问状态。
        # 参数:
        #     self: 当前 FakeContentAccess 测试替身实例,保存本用例的预设状态或调用记录。
        #     scene_id: 目标学习场景标识。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:是否允许预览的预设布尔值。
        return self.preview


class FakeGrantPort:
    def __init__(self, grants: tuple[AccessGrant, ...] = ()) -> None:
        # 功能:初始化 FakeGrantPort 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeGrantPort 测试替身实例,保存本用例的预设状态或调用记录。
        #     grants: 测试用户的完整访问授权来源集合。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.grants = grants

    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]:
        # 功能:返回测试用户的预设有效授权来源。
        # 参数:
        #     self: 当前 FakeGrantPort 测试替身实例,保存本用例的预设状态或调用记录。
        #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
        #     scene_id: 目标学习场景标识。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:tuple[AccessGrant, ...],由本用例预设的数据或所组装的测试资源构成。
        return self.grants


@pytest.mark.asyncio
async def test_authorization_is_union_of_all_full_access_sources() -> None:
    # 功能:验证授权合并所有完整访问来源。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    expiry = NOW + timedelta(days=10)
    service = AccessPolicyService(
        FakeContentAccess(opened=True, preview=True),
        FakeGrantPort((AccessGrant("formal-1", expiry),)),
        FakeGrantPort((AccessGrant("limited-1", NOW + timedelta(days=3)),)),
    )

    decision = await service.authorize("user-1", "scene-1", NOW)

    assert decision.level is AccessLevel.OPEN
    assert decision.sources == ("OPEN", "formal-1", "limited-1")
    assert decision.earliest_expires_at == NOW + timedelta(days=3)


@pytest.mark.asyncio
async def test_expiry_is_exclusive_at_exact_instant() -> None:
    # 功能:验证权益到期时刻使用排他的上界。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service = AccessPolicyService(
        FakeContentAccess(preview=True),
        FakeGrantPort((AccessGrant("formal-expired", NOW),)),
        FakeGrantPort((AccessGrant("limited-expired", NOW - timedelta(seconds=1)),)),
    )

    decision = await service.authorize("user-1", "scene-1", NOW)

    assert decision.level is AccessLevel.PREVIEW
    assert decision.sources == ("PREVIEW",)
    assert decision.earliest_expires_at is None


@pytest.mark.asyncio
async def test_hidden_when_no_full_or_preview_access() -> None:
    # 功能:验证没有完整或预览权限时隐藏入口。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service = AccessPolicyService(
        FakeContentAccess(),
        FakeGrantPort(),
        FakeGrantPort(),
    )

    decision = await service.authorize("user-1", "scene-1", NOW)

    assert decision.level is AccessLevel.HIDDEN
    assert decision.sources == ()


def test_preview_serializer_has_an_independent_allowlist() -> None:
    # 功能:验证预览序列化使用独立字段白名单。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    result = serialize_preview_scene(
        {
            "public_id": "scene-1",
            "title": "At the station",
            "series": {"title": "Travel"},
            "cover_url": "https://signed.example/cover",
            "introduction": "Preview",
            "preview_status": "AVAILABLE",
            "entries": [{"secret": "full content"}],
            "internal_notes": "must not leak",
            "oss_object_key": "private/key.jpg",
        }
    )

    assert set(result) == {
        "public_id",
        "title",
        "series",
        "cover_url",
        "introduction",
        "preview_status",
    }
