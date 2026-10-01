"""Correct untouched operations defaults while preserving administrator choices."""

from alembic import op

revision = "20261001_ops_defaults"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only the untouched seed values are corrected. Explicit saves increment version.
    op.execute(
        "UPDATE system_config SET value=JSON_OBJECT('value',48) "
        "WHERE config_key='feedback_sla_hours' AND version=1 AND updated_by IS NULL "
        "AND JSON_EXTRACT(value,'$.value')=24"
    )
    op.execute(
        "UPDATE system_config SET value=JSON_OBJECT('value',30) "
        "WHERE config_key='entitlement_expiry_warning_days' AND version=1 "
        "AND updated_by IS NULL AND JSON_EXTRACT(value,'$.value')=7"
    )


def downgrade() -> None:
    # Configuration is user data; reverting code must not overwrite current choices.
    pass
