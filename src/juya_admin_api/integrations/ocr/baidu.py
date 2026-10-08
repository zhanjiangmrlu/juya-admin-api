import base64
from typing import Any
from urllib.parse import urlencode

import httpx

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.ocr.protocol import OcrProvider, OcrResult
from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.shared.errors import AppError


class BaiduOcrProvider:
    def __init__(
        self,
        oss: OssProvider,
        api_key: str,
        secret_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 功能: 初始化图片文字识别对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     oss: 读取素材或签发对象地址的 OSS 提供方.
        #     api_key: 百度 OCR 应用的 API Key.
        #     secret_key: 百度 OCR 应用的 Secret Key.
        #     transport: 可注入的 HTTP 传输实现,用于调用或测试 OCR 服务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self.oss = oss
        self.api_key = api_key
        self.secret_key = secret_key
        self.transport = transport

    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能: 读取并校验 OSS 图片,调用百度通用文字识别并提取文字行及位置.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     template_type: OCR 业务模板类型;当前百度实现忽略此值,统一使用通用文字识别.
        # 返回: OCR 提取的文字及提供方识别结果.
        del template_type
        data = await self.oss.read_bytes(object_key, 8 * 1024 * 1024)
        await validate_ocr_image(data)
        params = {
            "image": base64.b64encode(data).decode(),
            "language_type": "CHN_ENG",
            "paragraph": "true",
            "probability": "true",
        }
        async with httpx.AsyncClient(
            transport=self.transport, timeout=20, follow_redirects=False
        ) as client:
            request_id = None
            try:
                token_response = await client.post(
                    "https://aip.baidubce.com/oauth/2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.api_key,
                        "client_secret": self.secret_key,
                    },
                )
                token_response.raise_for_status()
                token = token_response.json().get("access_token")
                if not isinstance(token, str) or not token:
                    raise ValueError("missing token")
                response = await client.post(
                    "https://aip.baidubce.com/rest/2.0/ocr/v1/general",
                    params={"access_token": token},
                    data=params,
                )
                body: dict[str, Any] = response.json()
                log_id = body.get("log_id")
                if isinstance(log_id, int) and not isinstance(log_id, bool) and log_id >= 0:
                    request_id = str(log_id)
                elif (
                    isinstance(log_id, str)
                    and log_id.isascii()
                    and log_id.isdigit()
                    and len(log_id) <= 128
                ):
                    request_id = log_id
                response.raise_for_status()
                if "error_code" in body or not isinstance(body.get("words_result"), list):
                    raise ValueError("provider failure")
                if request_id is None:
                    raise ValueError("provider log id is missing or invalid")
                paragraphs: dict[int, dict[str, object]] = {}
                for paragraph_index, paragraph in enumerate(body.get("paragraphs_result", [])):
                    indices = paragraph.get("words_result_idx", [])
                    for line_index in indices:
                        if isinstance(line_index, int):
                            paragraphs[line_index] = {
                                "index": paragraph_index,
                                "line_indices": indices,
                            }
                blocks = []
                for index, line in enumerate(body["words_result"]):
                    blocks.append(
                        {
                            "line": index,
                            "text": str(line.get("words", "")),
                            "location": line.get("location", {}),
                            "confidence": line.get("probability", {}).get("average"),
                            "paragraph": paragraphs.get(index, {}),
                        }
                    )
                return OcrResult(request_id, "\n".join(block["text"] for block in blocks), blocks)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise AppError(
                    "OCR_PROVIDER_FAILED",
                    "百度 OCR 调用失败, 额度已计入",
                    503,
                    {"provider_request_id": request_id} if request_id else {},
                ) from None


async def validate_ocr_image(data: bytes) -> None:
    # 功能: 校验 OCR 图片字节是否满足格式,大小及尺寸约束.
    # 参数:
    #     data: 待校验,读取或固定存储的素材原始字节.
    # 返回: 无返回值;正常完成表示本次操作成功.
    inspected = await inspect_media(data, "images")
    if (
        inspected.content_type not in {"image/jpeg", "image/png", "image/bmp"}
        or inspected.width is None
        or inspected.height is None
        or min(inspected.width, inspected.height) < 15
        or max(inspected.width, inspected.height) > 4096
        or len(urlencode({"image": base64.b64encode(data).decode()}).encode()) > 8 * 1024 * 1024
    ):
        raise AppError(
            "OCR_IMAGE_INVALID", "OCR图片须为jpg/png/bmp, 15至4096像素且编码后不超过8MB", 422
        )


class DisabledOcrProvider:
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能: 在未配置 OCR 服务时返回明确的服务不可用错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     template_type: OCR 业务模板类型;当前百度实现忽略此值,统一使用通用文字识别.
        # 返回: 不正常返回;抛出 OCR 服务不可用的业务异常.
        raise AppError("OCR_DISABLED", "未配置百度 OCR 提供方", 503)


def create_ocr_provider(settings: Settings, oss: OssProvider) -> OcrProvider:
    # 功能: 根据配置选择百度 OCR 或禁用提供方.
    # 参数:
    #     settings: 已加载并校验的服务运行配置.
    #     oss: 读取素材或签发对象地址的 OSS 提供方.
    # 返回: 配置指定的 OCR 提供方.
    if (
        settings.ocr_provider == "baidu"
        and settings.baidu_ocr_api_key
        and settings.baidu_ocr_secret_key
    ):
        return BaiduOcrProvider(
            oss,
            settings.baidu_ocr_api_key.get_secret_value(),
            settings.baidu_ocr_secret_key.get_secret_value(),
        )
    return DisabledOcrProvider()
