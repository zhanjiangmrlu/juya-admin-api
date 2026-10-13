import pytest

from juya_admin_api.modules.content_production.projection import counts, select_items
from juya_admin_api.modules.content_production.schemas import WorkItemDetail
from juya_admin_api.shared.errors import AppError


def item(identity, status, **fields):
    return WorkItemDetail(
        item_id=identity,
        work_order_id="order",
        production_status=status,
        source_available=False,
        **fields,
    )


def test_global_selection_is_independent_of_page_and_requires_order_membership():
    items = [item(str(n), "normal" if n % 2 else "review_required") for n in range(1000)]
    selected = select_items(items, {"scope": "normal"})
    assert len(selected) == 500 and selected[-1].item_id == "999"
    assert counts(items).normal == 500 and counts(items).total == 1000
    with pytest.raises(AppError) as error:
        select_items(items, {"scope": "selected", "selected_item_ids": ["another-order"]})
    assert error.value.code == "WORK_SELECTION_INVALID"


def test_published_lifecycle_and_pending_draft_status_count_separately():
    summary = counts(
        [
            item("live", "review_required", published=True),
            item("ready", "normal", confirmed=True, publishable=True),
            item("cancel", "cancelled"),
        ]
    )
    assert summary.published == 1 and summary.review_required == 1
    assert summary.confirmed == 1 and summary.publishable == 1 and summary.cancelled == 1
