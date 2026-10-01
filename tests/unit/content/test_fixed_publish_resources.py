import pytest

from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.schemas import SceneContent


@pytest.mark.parametrize("key", ["uploads/images/owner/a.png", "", "sealed/other/a.png"])
def test_mutable_original_cannot_pass_publication(key: str) -> None:
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
    from juya_admin_api.modules.content.production_store import ProductionStore

    active = False
    prepared = []

    class Session:
        async def __aenter__(self):
            nonlocal active
            active = True
            return self

        async def __aexit__(self, *args):
            nonlocal active
            active = False

        async def scalar(self, *args):
            return {"original_image_asset_id": "original"}

    class Store(ProductionStore):
        async def facts(self, *args, **kwargs):
            return {name: {} for name in ["original", "cover", "whole", "icon", "entry-audio"]}, {}

    async def prepare(asset_id):
        assert not active, "network work must not run inside a publication/receipt session"
        prepared.append(asset_id)

    await Store(Session, prepare_asset=prepare).prepare_resources("revision")
    assert set(prepared) == {"original", "cover", "whole", "icon", "entry-audio"}
