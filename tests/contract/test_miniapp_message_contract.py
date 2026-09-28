import pytest

from juya_admin_api.infrastructure.tasks.outbox import FeedbackMessageDispatcher
from juya_admin_api.modules.feedback.domain import FeedbackOutboxMessage


class CapturingSender:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def create_message(self, **payload: str) -> None:
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_feedback_outbox_maps_to_idempotent_miniapp_message_contract() -> None:
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
