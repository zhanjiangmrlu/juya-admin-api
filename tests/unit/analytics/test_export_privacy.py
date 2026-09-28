from datetime import date

import pytest

from juya_admin_api.modules.analytics.service import AnalyticsRow, export_aggregate_rows
from juya_admin_api.shared.errors import AppError


def test_export_contains_only_allowlisted_aggregate_dimensions_and_metrics() -> None:
    rows = (
        AnalyticsRow(date(2026, 9, 28), "ACTIVE_USERS", "ALL", 42),
        AnalyticsRow(date(2026, 9, 28), "SCENE_COMPLETIONS", "scene:public-1", 12),
    )

    exported = export_aggregate_rows(rows)

    assert exported[0] == {
        "day": "2026-09-28",
        "metric": "ACTIVE_USERS",
        "dimension": "ALL",
        "value": 42,
    }
    forbidden = {"nickname", "wechat_id", "screenshot", "juya_id", "user_id", "trajectory"}
    assert forbidden.isdisjoint(exported[0])


def test_export_rejects_non_aggregate_metric_or_personal_dimension() -> None:
    with pytest.raises(AppError) as error:
        export_aggregate_rows((AnalyticsRow(date.today(), "USER_TRACE", "user:1", 1),))
    assert error.value.code == "ANALYTICS_EXPORT_FORBIDDEN"
