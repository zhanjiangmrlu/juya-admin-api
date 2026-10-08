from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

PRIORITY_RANKS = {
    "FEEDBACK_OVERDUE": 10,
    "USER_SUPPLIED": 20,
    "FEEDBACK_DUE_SOON": 30,
    "CAMPAIGN_STARTING": 40,
    "CAMPAIGN_START_EXPIRED": 50,
    "NEW_FEEDBACK": 60,
    "ACTIVE_ENTITLEMENT_EXPIRING": 100,
}


@dataclass(frozen=True, slots=True)
class WorkItemFact:
    key: str
    kind: str
    due_at: datetime
    completed: bool


@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    kind: str
    priority_rank: int
    due_at: datetime


class WorkItemSource(Protocol):
    async def facts(self, now: datetime) -> tuple[WorkItemFact, ...]:
        # 功能: 查询反馈,活动及权益相关的运营待办事实.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 查询得到的运营待办事实.
        ...


class WorkItemService:
    def __init__(self, source: WorkItemSource) -> None:
        # 功能: 初始化运营待办对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     source: 提供反馈,活动和权益待办事实的查询端口.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._source = source

    async def active(self, now: datetime) -> tuple[WorkItem, ...]:
        # 功能: 筛选并按优先级生成当前待办列表.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 按优先级和截止时间排序的有效待办.
        return project_work_items(await self._source.facts(now), now)


def project_work_items(facts: tuple[WorkItemFact, ...], now: datetime) -> tuple[WorkItem, ...]:
    # 功能: 过滤已完成或未知类型待办,并按优先级和截止时间排序.
    # 参数:
    #     facts: 待办事实集合,包含类型,截止时间和完成状态.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 按优先级和截止时间排序的有效待办.
    projected: list[WorkItem] = []
    for fact in facts:
        if fact.completed or fact.kind not in PRIORITY_RANKS:
            continue
        if fact.kind == "CAMPAIGN_STARTING" and fact.due_at > now + timedelta(hours=24):
            continue
        projected.append(WorkItem(fact.key, fact.kind, PRIORITY_RANKS[fact.kind], fact.due_at))
    # 匿名函数: 为运营待办提供优先级和截止时间排序键.
    # 参数:
    #     item: 已投影的运营待办, 包含优先级,截止时间和去重键.
    # 返回: 优先级数值,截止时间和待办键组成的排序元组.
    return tuple(sorted(projected, key=lambda item: (item.priority_rank, item.due_at, item.key)))
