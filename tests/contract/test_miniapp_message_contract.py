import pytest

from juya_admin_api.infrastructure.tasks.outbox import FeedbackMessageDispatcher
from juya_admin_api.modules.feedback.domain import FeedbackOutboxMessage


class CapturingSender:
    def __init__(self) -> None:
        # 功能:初始化 CapturingSender 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 CapturingSender 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.messages: list[dict[str, str]] = []

    async def create_message(self, **payload: str) -> None:
        # 功能:记录小程序消息创建请求,供发件箱投递契约检查。
        # 参数:
        #     self: 当前 CapturingSender 测试替身实例,保存本用例的预设状态或调用记录。
        #     payload: 待提交的业务请求载荷;进程脚本中为标准输入文本。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_feedback_outbox_maps_to_idempotent_miniapp_message_contract() -> None:
    # 功能:验证反馈发件箱事件映射为幂等的小程序消息。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    sender = CapturingSender()
    dispatcher = FeedbackMessageDispatcher(sender)
    event = FeedbackOutboxMessage(
        event_id="01M00000000000000000000001",
        user_id="01M00000000000000000000002",
        message_type="FEEDBACK_NEED_MORE",
        title="反馈需要补充",
        summary="请补充更多信息",
        related_type="FEEDBACK",
        related_id="01M00000000000000000000003",
    )

    await dispatcher.dispatch(event)

    assert sender.messages == [
        {
            "user_id": event.user_id,
            "event_id": event.event_id,
            "message_type": "FEEDBACK_NEED_MORE",
            "title": "反馈需要补充",
            "summary": "请补充更多信息",
            "related_type": "FEEDBACK",
            "related_id": event.related_id,
        }
    ]
