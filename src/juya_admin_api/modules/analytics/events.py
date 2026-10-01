"""Immutable business events; never accept identifying strings in payloads."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.analytics.service import is_anonymous_dimension
from juya_admin_api.shared.ids import new_ulid

EVENT_METRICS = {
    "USER_CREATED": "NEW_USERS",
    "USER_ACTIVE": "ACTIVE_USERS",
    "USER_ACTIVE_WEEK": "WEEK_ACTIVE_USERS",
    "USER_ACTIVE_MONTH": "MONTH_ACTIVE_USERS",
    "SCENE_STARTED": "SCENE_STARTS",
    "SCENE_COMPLETED": "SCENE_COMPLETIONS",
    "OPEN_SCENE_COMPLETED": "OPEN_SCENE_COMPLETIONS",
    "OPEN_LEARNER_STARTED": "OPEN_LEARNERS",
    "OPEN_ALL_COMPLETED": "OPEN_ALL_COMPLETIONS",
    "CONTACT_PROMPT_EXPOSED": "CONTACT_EXPOSURES",
    "CONTACT_SUBMITTED": "CONTACT_SUBMISSIONS",
    "CONTACT_CHANGED": "CONTACT_CHANGES",
    "CONTACT_WITHDRAWN": "CONTACT_WITHDRAWALS",
    "CONTACT_STATUS_CHANGED": "CONTACT_STATE_CHANGES",
    "FORMAL_GRANTED": "FORMAL_ENTITLEMENTS",
    "FORMAL_EXPIRED": "FORMAL_EXPIRATIONS",
    "FORMAL_STATUS_CHANGED": "FORMAL_STATE_CHANGES",
    "LIMITED_GRANTED": "LIMITED_GRANTS",
    "LIMITED_STARTED": "LIMITED_STARTS",
    "LIMITED_EXPIRED": "LIMITED_EXPIRATIONS",
    "LIMITED_START_EXPIRED": "LIMITED_START_EXPIRATIONS",
    "LIMITED_COMPLETED": "LIMITED_COMPLETIONS",
    "LIMITED_STATUS_CHANGED": "LIMITED_STATE_CHANGES",
    "FAVORITE_CREATED": "FAVORITES",
    "REVIEW_COMPLETED": "REVIEWS",
    "SCENE_REVISITED": "REVISITS",
    "FEEDBACK_CREATED": "FEEDBACK_NEW",
    "FEEDBACK_RESPONDED": "FEEDBACK_RESPONSES",
    "FEEDBACK_SUPPLEMENTED": "FEEDBACK_SUPPLEMENTS",
    "FEEDBACK_RESOLVED": "FEEDBACK_RESOLUTIONS",
    "FEEDBACK_REOPENED": "FEEDBACK_REOPENS",
    "FEEDBACK_OVERDUE": "FEEDBACK_TIMEOUTS",
    "FEEDBACK_STATUS_CHANGED": "FEEDBACK_STATE_CHANGES",
    "DELETION_REQUESTED": "DELETION_REQUESTS",
    "DELETION_WITHDRAWN": "DELETION_WITHDRAWALS",
    "DELETION_EFFECTIVE": "DELETIONS",
}
PAYLOAD_FIELDS = frozenset(
    {
        "mode",
        "status",
        "category",
        "response_seconds",
        "supplement_rounds",
        "before_expiry",
        "prompted",
        "cohort_day",
        "started_day",
        "created_day",
        "contact_cohort",
    }
)


def validate_event(event_type: str, dimension: str, payload: Mapping[str, object]) -> None:
    if event_type not in EVENT_METRICS or not is_anonymous_dimension(dimension):
        raise ValueError("Unsupported analytics event or non-anonymous dimension")
    if set(payload) - PAYLOAD_FIELDS:
        raise ValueError("Analytics payload contains unsupported personal fields")
    for key, value in payload.items():
        if key == "contact_cohort":
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
                raise ValueError("Contact cohort must be a random anonymous token")
        elif key in {"cohort_day", "started_day", "created_day"}:
            if not isinstance(value, str):
                raise ValueError("Analytics cohort must be an ISO day")
            date.fromisoformat(value)
        elif key in {"status", "category"}:
            if not isinstance(value, str) or not is_anonymous_dimension(value):
                raise ValueError("Analytics payload enum is invalid")
        elif key == "mode":
            if value not in {3, 5}:
                raise ValueError("Analytics mode must be 3 or 5")
        elif key in {"before_expiry", "prompted"}:
            if not isinstance(value, bool):
                raise ValueError("before_expiry must be a boolean")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Analytics numeric payload is invalid")


async def append_event(
    session: AsyncSession,
    *,
    event_key: str,
    event_type: str,
    user_id: int | None,
    occurred_at: datetime,
    dimension: str = "ALL",
    payload: Mapping[str, object] | None = None,
) -> bool:
    body = dict(payload or {})
    validate_event(event_type, dimension, body)
    if not event_key or len(event_key) > 191:
        raise ValueError("Invalid event key")
    result = await session.execute(
        text(
            "INSERT IGNORE INTO analytics_event "
            "(id,event_key,user_id,event_type,occurred_at,dimension,payload) "
            "VALUES (:id,:key,:user,:type,:at,:dimension,:payload)"
        ),
        {
            "id": new_ulid(occurred_at),
            "key": event_key,
            "user": user_id,
            "type": event_type,
            "at": occurred_at.astimezone(UTC).replace(tzinfo=None),
            "dimension": dimension,
            "payload": json.dumps(body, separators=(",", ":")),
        },
    )
    return int(getattr(result, "rowcount", 0)) == 1


@dataclass(frozen=True, slots=True)
class AnalyticsEvent:
    id: str
    event_type: str
    occurred_at: datetime
    dimension: str
    payload: Mapping[str, object]
    contact_subject: int | None = None


def aggregate_events(events: Iterable[AnalyticsEvent], day: date) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    contact_seen: set[tuple[str, str]] = set()

    def add(metric: str, dimension: str, value: int = 1) -> None:
        counts[(metric, dimension)] = counts.get((metric, dimension), 0) + value
        if dimension in {"NUMERATOR", "DENOMINATOR"}:
            mode = event.payload.get("mode")
            if mode in {3, 5}:
                mode_dimension = f"MODE_{mode}_{dimension}"
                counts[(metric, mode_dimension)] = counts.get((metric, mode_dimension), 0) + value

    ordered = sorted(
        events,
        key=lambda item: (
            item.occurred_at.replace(tzinfo=UTC)
            if item.occurred_at.tzinfo is None
            else item.occurred_at,
            item.id,
        ),
    )
    for event in ordered:
        at = event.occurred_at
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if event.id in seen:
            continue
        seen.add(event.id)
        validate_event(event.event_type, event.dimension, event.payload)
        cohort = event.payload.get("contact_cohort")
        if event.event_type == "CONTACT_SUBMITTED" and event.contact_subject is not None:
            identity = ("CONTACT_SUBMITTED_USER", str(event.contact_subject))
            if identity in contact_seen:
                continue
            contact_seen.add(identity)
        if event.event_type in {
            "CONTACT_SUBMITTED",
            "CONTACT_PROMPT_EXPOSED",
            "CONTACT_WITHDRAWN",
        } and isinstance(cohort, str):
            identity = (event.event_type, cohort)
            if identity in contact_seen:
                continue
            contact_seen.add(identity)
        event_day = at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        grant_day = date.fromisoformat(str(event.payload.get("cohort_day", event_day)))
        start_day = date.fromisoformat(str(event.payload.get("started_day", event_day)))
        created_day = date.fromisoformat(str(event.payload.get("created_day", event_day)))
        kind = event.event_type
        if kind == "LIMITED_GRANTED" and event_day == day:
            add("LIMITED_STARTS", "DENOMINATOR")
            add("LIMITED_START_EXPIRATIONS", "DENOMINATOR")
        if kind == "LIMITED_STARTED":
            if grant_day == day:
                add("LIMITED_STARTS", "NUMERATOR")
            if event_day == day:
                add("LIMITED_COMPLETIONS", "DENOMINATOR")
        if kind == "LIMITED_COMPLETED" and start_day == day and event.payload.get("before_expiry"):
            add("LIMITED_COMPLETIONS", "NUMERATOR")
        if kind == "LIMITED_START_EXPIRED" and grant_day == day:
            add("LIMITED_START_EXPIRATIONS", "NUMERATOR")
        if kind == "FEEDBACK_CREATED" and event_day == day:
            add("FEEDBACK_SLA", "DENOMINATOR")
            add("FEEDBACK_SOLVE_RATE", "DENOMINATOR")
        if (
            kind == "FEEDBACK_RESPONDED"
            and created_day == day
            and event.payload.get("before_expiry")
        ):
            add("FEEDBACK_SLA", "NUMERATOR")
        if kind == "FEEDBACK_RESOLVED":
            if created_day == day:
                add("FEEDBACK_SOLVE_RATE", "NUMERATOR")
            if created_day == day:
                add("FEEDBACK_REOPEN_RATE", "DENOMINATOR")
        if kind == "FEEDBACK_REOPENED" and created_day == day:
            add("FEEDBACK_REOPEN_RATE", "NUMERATOR")
        if kind == "FEEDBACK_CREATED" and event_day == day:
            add("FEEDBACK_TIMEOUT_RATE", "DENOMINATOR")
        if kind == "FEEDBACK_OVERDUE" and created_day == day:
            add("FEEDBACK_TIMEOUT_RATE", "NUMERATOR")
        if kind == "OPEN_LEARNER_STARTED" and event_day == day:
            add("OPEN_ALL_RATE", "DENOMINATOR")
        if kind == "OPEN_ALL_COMPLETED" and grant_day == day:
            add("OPEN_ALL_RATE", "NUMERATOR")
        if kind == "CONTACT_SUBMITTED":
            if event_day == day:
                add("CONTACT_WITHDRAW_RATE", "DENOMINATOR")
            if grant_day == day and event.payload.get("prompted") is not False:
                add("CONTACT_FUNNEL", "NUMERATOR")
        if kind == "CONTACT_WITHDRAWN" and grant_day == day:
            add("CONTACT_WITHDRAW_RATE", "NUMERATOR")
        if event_day != day:
            continue
        metric = EVENT_METRICS[event.event_type]
        dimensions = {"ALL", event.dimension}
        mode = event.payload.get("mode")
        if mode in {3, 5}:
            dimensions.add(f"MODE_{mode}")
        for field in ("status", "category"):
            value = event.payload.get(field)
            if isinstance(value, str):
                dimensions.add(value)
        for dimension in dimensions:
            add(metric, dimension)
        if event.event_type == "CONTACT_PROMPT_EXPOSED":
            add("CONTACT_FUNNEL", "DENOMINATOR")
        if event.event_type == "FEEDBACK_RESPONDED":
            add(
                "FEEDBACK_RESPONSE_SECONDS",
                "NUMERATOR",
                int(str(event.payload.get("response_seconds", 0))),
            )
            add("FEEDBACK_RESPONSE_SECONDS", "DENOMINATOR")
            add(
                "FEEDBACK_RESPONSE_SECONDS",
                "ALL",
                int(str(event.payload.get("response_seconds", 0))),
            )
        if event.event_type == "FEEDBACK_SUPPLEMENTED":
            add(
                "FEEDBACK_SUPPLEMENT_ROUNDS",
                "ALL",
                1,
            )
    # Missing halves are explicit zero counts. Ratios with a zero denominator remain null.
    for metric in (
        "CONTACT_FUNNEL",
        "LIMITED_STARTS",
        "LIMITED_COMPLETIONS",
        "LIMITED_START_EXPIRATIONS",
        "FEEDBACK_SLA",
        "FEEDBACK_SOLVE_RATE",
        "FEEDBACK_REOPEN_RATE",
        "FEEDBACK_TIMEOUT_RATE",
        "OPEN_ALL_RATE",
        "CONTACT_WITHDRAW_RATE",
    ):
        for dimension in ("NUMERATOR", "DENOMINATOR"):
            counts.setdefault((metric, dimension), 0)
            if metric in {"LIMITED_STARTS", "LIMITED_COMPLETIONS", "LIMITED_START_EXPIRATIONS"}:
                for mode in (3, 5):
                    counts.setdefault((metric, f"MODE_{mode}_{dimension}"), 0)
    counts.setdefault(("FEEDBACK_RESPONSE_SECONDS", "NUMERATOR"), 0)
    counts.setdefault(("FEEDBACK_RESPONSE_SECONDS", "DENOMINATOR"), 0)
    # Emit only valid ratio pairs; raw event counts still explain missing denominators.
    for metric in (
        "CONTACT_FUNNEL",
        "LIMITED_STARTS",
        "LIMITED_COMPLETIONS",
        "LIMITED_START_EXPIRATIONS",
        "FEEDBACK_SLA",
        "FEEDBACK_SOLVE_RATE",
        "FEEDBACK_REOPEN_RATE",
        "FEEDBACK_TIMEOUT_RATE",
        "OPEN_ALL_RATE",
        "CONTACT_WITHDRAW_RATE",
    ):
        for prefix in ("", "MODE_3_", "MODE_5_"):
            numerator = (metric, prefix + "NUMERATOR")
            denominator = (metric, prefix + "DENOMINATOR")
            if counts.get(numerator, 0) > counts.get(denominator, 0):
                counts.pop(numerator, None)
                counts.pop(denominator, None)
    return counts
