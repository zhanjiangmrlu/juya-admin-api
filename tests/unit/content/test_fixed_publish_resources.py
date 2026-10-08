import pytest

from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.schemas import SceneContent


@pytest.mark.parametrize("key", ["uploads/images/owner/a.png", "", "sealed/other/a.png"])
def test_mutable_original_cannot_pass_publication(key: str) -> None:
    # 功能:验证可修改的原始资源不能通过发布检查。
    # 参数:
    #     key: 参数化测试的原始图片对象键,用于区分可变和封存目录。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    content = SceneContent(original_image_asset_id="image")
    assets = {
        "image": {
            "status": "CONFIRMED",
            "security_status": "PASSED",
            "asset_type": "images",
            "width": 10,
            "height": 10,
            "object_key": key,
        }
    }
    checks = {check.code: check.passed for check in check_content(content, assets, {})}
    assert checks["ORIGINAL_IMAGE_REQUIRED"] is False


def test_sealed_original_can_pass_resource_check() -> None:
    # 功能:验证已封存原始资源可通过资源检查。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    content = SceneContent(original_image_asset_id="image")
    assets = {
        "image": {
            "status": "CONFIRMED",
            "security_status": "PASSED",
            "asset_type": "images",
            "width": 10,
            "height": 10,
            "object_key": "sealed/media/images/unique.png",
        }
    }
    checks = {check.code: check.passed for check in check_content(content, assets, {})}
    assert checks["ORIGINAL_IMAGE_REQUIRED"] is True


@pytest.mark.asyncio
async def test_legacy_prepare_releases_database_read_before_cloud_work() -> None:
    # 功能:验证旧资源准备在云端操作前释放数据库读会话。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.content.production_store import ProductionStore

    active = False
    prepared = []

    class Session:
        async def __aenter__(self):
            # 功能:进入模拟异步会话或事务并返回当前对象。
            # 参数:
            #     self: 当前 Session 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:本用例预设的调用结果或所构造的测试资源。
            nonlocal active
            active = True
            return self

        async def __aexit__(self, *args):
            # 功能:退出模拟异步上下文并更新测试记录。
            # 参数:
            #     self: 当前 Session 测试替身实例,保存本用例的预设状态或调用记录。
            #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            nonlocal active
            active = False

        async def scalar(self, *args):
            # 功能:返回模拟 SQL 结果中的单个标量值。
            # 参数:
            #     self: 当前 Session 测试替身实例,保存本用例的预设状态或调用记录。
            #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
            # 返回:本用例预设的调用结果或所构造的测试资源。
            return {"original_image_asset_id": "original"}

    class Store(ProductionStore):
        async def facts(self, *args, **kwargs):
            # 功能:返回当前测试预设的媒体文件事实。
            # 参数:
            #     self: 当前 Store 测试替身实例,保存本用例的预设状态或调用记录。
            #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
            #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
            # 返回:本用例预设的调用结果或所构造的测试资源。
            return {name: {} for name in ["original", "cover", "whole", "icon", "entry-audio"]}, {}

    async def prepare(asset_id):
        # 功能:检查资源准备发生在数据库读会话之外并记录资源标识。
        # 参数:
        #     asset_id: 需要固定、检查或引用的媒体资源标识。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        assert not active, "network work must not run inside a publication/receipt session"
        prepared.append(asset_id)

    await Store(Session, prepare_asset=prepare).prepare_resources("revision")
    assert set(prepared) == {"original", "cover", "whole", "icon", "entry-audio"}
