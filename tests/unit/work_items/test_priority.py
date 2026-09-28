from datetime import UTC, datetime, timedelta

from juya_admin_api.modules.work_items.service import WorkItemFact, project_work_items

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


def test_work_items_use_fixed_priority_and_completed_items_disappear() -> None:
    items = project_work_items(
        (
            WorkItemFact("feedback:new", "NEW_FEEDBACK", NOW, False),
            WorkItemFact("feedback:late", "FEEDBACK_OVERDUE", NOW, False),
            WorkItemFact("campaign:done", "CAMPAIGN_STARTING", NOW, True),
            WorkItemFact("feedback:supplied", "USER_SUPPLIED", NOW, False),
            WorkItemFact("campaign:soon", "CAMPAIGN_STARTING", NOW + timedelta(hours=20), False),
        ),
        NOW,
    )

    assert [item.priority_rank for item in items] == [10, 20, 40, 60]
    assert all(item.key != "campaign:done" for item in items)
