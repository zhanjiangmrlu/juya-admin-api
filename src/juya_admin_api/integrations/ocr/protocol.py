from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OcrResult:
    provider_request_id: str
    text: str
    blocks: list[dict[str, Any]]


class OcrProvider(Protocol):
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能: 读取 OSS 图片并按模板进行 OCR 识别.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     template_type: OCR 业务模板类型;当前百度实现忽略此值,统一使用通用文字识别.
        # 返回: OCR 提取的文字及提供方识别结果.
        ...
