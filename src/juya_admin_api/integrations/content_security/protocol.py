from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SecurityResult:
    provider_request_id: str
    status: str
    labels: tuple[str, ...] = ()


class ContentSecurityProvider(Protocol):
    async def scan_text(self, text: str) -> SecurityResult:
        # 功能: 审核文本并返回内容安全状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     text: 待审核或合成语音的文本内容.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        ...

    async def scan_image(self, object_key: str) -> SecurityResult:
        # 功能: 审核 OSS 图片对象并返回内容安全状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        ...

    async def scan_audio(self, object_key: str) -> SecurityResult:
        # 功能: 提交 OSS 音频审核并返回任务或审核状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        ...

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        # 功能: 查询音频审核任务进度和最终状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     provider_request_id: 云内容安全提供方返回的音频审核任务标识.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        ...
