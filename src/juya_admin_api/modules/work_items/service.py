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
    async def facts(self, now: datetime) -> tuple[WorkItemFact, ...]: ...


class WorkItemService:
    def __init__(self, source: WorkItemSource) -> None:
        self._source = source

    async def active(self, now: datetime) -> tuple[WorkItem, ...]:
        return project_work_items(await self._source.facts(now), now)


def project_work_items(facts: tuple[WorkItemFact, ...], now: datetime) -> tuple[WorkItem, ...]:
    projected: list[WorkItem] = []
    for fact in facts:
        if fact.completed or fact.kind not in PRIORITY_RANKS:
            continue
        if fact.kind == "CAMPAIGN_STARTING" and fact.due_at > now + timedelta(hours=24):
            continue
        projected.append(WorkItem(fact.key, fact.kind, PRIORITY_RANKS[fact.kind], fact.due_at))
    return tuple(sorted(projected, key=lambda item: (item.priority_rank, item.due_at, item.key)))
