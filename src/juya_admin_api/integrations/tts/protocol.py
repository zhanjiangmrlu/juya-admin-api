from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TtsResult:
    provider_request_id: str
    object_key: str
    duration_ms: int


class TtsProvider(Protocol):
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        # 功能: 定义文本到指定发音人语音的合成能力.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     audio_target: 待生成语音的业务目标类型.
        #     voice: 语音合成使用的发音人标识.
        #     text: 待审核或合成语音的文本内容.
        # 返回: 合成音频对应的对象键及任务结果.
        ...
