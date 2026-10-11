from sqlalchemy import JSON, BigInteger, Column, DateTime, MetaData, String, Table, UniqueConstraint

metadata = MetaData()
budgets = Table(
    "content_cloud_budget",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("period", String(16), primary_key=True),
    Column("hard_limit", BigInteger, nullable=False),
    Column("reserved_units", BigInteger, nullable=False, default=0),
)
attempts = Table(
    "content_cloud_attempt",
    metadata,
    Column("public_id", String(64), primary_key=True),
    Column("stage_id", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("period", String(16), nullable=False),
    Column("reserved_units", BigInteger, nullable=False),
    Column("actual_units", BigInteger),
    Column("usage", JSON),
    Column("provider_request_id", String(128)),
    Column("status", String(32), nullable=False),
    Column("price_version", String(64), nullable=False),
    Column("region", String(64), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("completed_at", DateTime),
    UniqueConstraint("stage_id", name="uq_content_cloud_attempt_stage"),
)
