from dataclasses import dataclass, field
from typing import Protocol

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    upload_url: str
    object_key_prefix: str
    max_bytes: int
    expires_in: int
    fields: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    object_key: str
    size: int
    content_type: str
    sha256: str
    metadata: dict[str, str]


class OssProvider(Protocol):
    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        # 功能: 将素材写入不可覆盖的固定私有对象并保留版本定位符.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     data: 待校验,读取或固定存储的素材原始字节.
        #     asset_type: 固定素材类别,只接受 images 或 audio.
        #     content_type: 素材的 MIME 类型,例如 image/png 或 audio/mpeg.
        # 返回: 固定媒体对象键,启用版本控制时附带编码后的版本号.
        ...

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        # 功能: 生成限制对象目录,MIME 类型,大小和时效的 OSS 直传策略.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key_prefix: 浏览器直传允许使用的 OSS 对象键前缀.
        #     max_bytes: 素材允许的最大字节数;OSS 读取上限不超过 50 MiB.
        #     expires_in: 上传策略或下载签名的期望有效时长,单位为秒,最大 600 秒.
        # 返回: 上传地址,对象前缀,大小限制,有效期和签名表单字段.
        ...

    async def head_object(self, object_key: str) -> ObjectMetadata:
        # 功能: 读取 OSS 对象大小,类型和摘要元数据.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 对象的大小,MIME 类型,SHA-256 摘要及元数据.
        ...

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        # 功能: 在大小限制内读取 OSS 对象的原始字节.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     max_bytes: 素材允许的最大字节数;OSS 读取上限不超过 50 MiB.
        # 返回: 加密内容或读取到的原始字节.
        ...

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能: 签发受凭据剩余寿命限制的私有 OSS 下载地址.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     expires_in: 上传策略或下载签名的期望有效时长,单位为秒,最大 600 秒.
        # 返回: 带访问签名和有效期的私有 OSS 下载地址.
        ...

    async def delete_object(self, object_key: str) -> None:
        # 功能: 删除允许清理的 OSS 对象并保护固定教学素材.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...


def validate_object_key(key: str) -> None:
    # 功能: 检查 OSS 对象键和固定素材版本定位符的合法性.
    # 参数:
    #     key: 待校验的 OSS 桶内对象键或固定版本定位符.
    # 返回: 无返回值;正常完成表示本次操作成功.
    prefixes = (
        "uploads/images/",
        "uploads/audio/",
        "feedback/",
        "generated/audio/",
        "oss-live-tests/",
        "sealed/media/",
    )
    if (
        not 1 <= len(key) <= 512
        or not key.startswith(prefixes)
        or any(part in {".", ".."} for part in key.split("/"))
        or any(ord(character) < 32 for character in key)
        or any(value in key for value in ("\\", "?", "#", "%", ":", "$"))
    ):
        raise AppError("OSS_OBJECT_KEY_INVALID", "OSS对象键不在授权范围", 422)
