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
    ) -> None:
        # 功能: 向小程序内部服务提交幂等站内通知.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     event_id: 跨服务事件的唯一标识,防止重复投递或重复清理.
        #     message_type: 小程序站内通知的业务类型.
        #     title: 站内通知展示标题.
        #     summary: 站内通知展示的简短摘要.
        #     related_type: 站内消息关联的业务对象类型.
        #     related_id: 站内消息关联的业务对象标识.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...


class FeedbackMessageDispatcher:
    def __init__(self, sender: MiniappMessageSender) -> None:
        # 功能: 初始化后台服务对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     sender: 向小程序服务发送站内通知的端口.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._sender = sender

    async def dispatch(self, message: FeedbackOutboxMessage) -> None:
        # 功能: 向小程序发送反馈 outbox 通知.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     message: 待派发的反馈 outbox 消息记录.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._sender.create_message(
            user_id=message.user_id,
            event_id=message.event_id,
            message_type=message.message_type,
            title=message.title,
            summary=message.summary,
            related_type=message.related_type,
            related_id=message.related_id,
        )
