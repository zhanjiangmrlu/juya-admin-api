from typing import Protocol

from juya_admin_api.modules.feedback.domain import FeedbackOutboxMessage


class MiniappMessageSender(Protocol):
    async def create_message(
        self,
        *,
        user_id: str,
        event_id: str,
        message_type: str,
        title: str,
        summary: str,
        related_type: str,
        related_id: str,
    ) -> None: ...


class FeedbackMessageDispatcher:
    def __init__(self, sender: MiniappMessageSender) -> None:
        self._sender = sender

    async def dispatch(self, message: FeedbackOutboxMessage) -> None:
        await self._sender.create_message(
            user_id=message.user_id,
            event_id=message.event_id,
            message_type=message.message_type,
            title=message.title,
            summary=message.summary,
            related_type=message.related_type,
            related_id=message.related_id,
        )
