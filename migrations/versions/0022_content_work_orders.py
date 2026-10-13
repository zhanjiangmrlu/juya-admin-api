"""Persist work-order scope, immutable inputs and recoverable delegated commands.

Frozen additive DDL keeps historical upgrades independent of future model changes.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

TABLE_DDL = [
    """CREATE TABLE content_work_order (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	public_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	import_batch_id VARCHAR(64) COLLATE utf8mb4_bin,
	source_mode VARCHAR(32) NOT NULL,
	series_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	content_type VARCHAR(32) NOT NULL,
	version INTEGER NOT NULL DEFAULT 1,
	status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
	proofreading_set_id VARCHAR(64) COLLATE utf8mb4_bin,
	publication_token VARCHAR(64) COLLATE utf8mb4_bin,
	active_command_id VARCHAR(64) COLLATE utf8mb4_bin,
	created_by VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	CONSTRAINT uq_content_work_order_0 UNIQUE (public_id),
	CONSTRAINT uq_content_work_order_1 UNIQUE (import_batch_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin""",
    """CREATE TABLE content_work_item (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	public_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	work_order_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	import_item_id VARCHAR(64) COLLATE utf8mb4_bin,
	position INTEGER NOT NULL,
	content_id VARCHAR(64) COLLATE utf8mb4_bin,
	original_revision_id VARCHAR(64) COLLATE utf8mb4_bin,
	source_snapshot JSON,
	source_hash CHAR(64),
	historical_issues JSON,
	cancelled BOOL NOT NULL DEFAULT 0,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	CONSTRAINT uq_content_work_item_0 UNIQUE (public_id),
	CONSTRAINT uq_content_work_item_1 UNIQUE (work_order_id, import_item_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin""",
    """CREATE INDEX ix_work_item_content ON content_work_item (content_id, work_order_id)""",
    """CREATE TABLE content_work_item_revision (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	work_item_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	revision_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	CONSTRAINT uq_content_work_item_revision_0 UNIQUE (work_item_id, revision_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin""",
    """CREATE TABLE content_work_item_stage (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	work_item_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	stage_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	revision_id VARCHAR(64) COLLATE utf8mb4_bin,
	sentence_id VARCHAR(64) COLLATE utf8mb4_bin,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	CONSTRAINT uq_content_work_item_stage_0 UNIQUE (work_item_id, stage_id)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin""",
    """CREATE TABLE content_work_issue_resolution (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	work_item_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	issue_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	source_hash CHAR(64) NOT NULL,
	candidate_hash CHAR(64) NOT NULL,
	resolved_by VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	CONSTRAINT uq_content_work_issue_resolution_0 UNIQUE (work_item_id, issue_id, candidate_hash)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin""",
    """CREATE TABLE content_work_command (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	public_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	work_order_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	actor_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
	idempotency_key VARCHAR(100) NOT NULL,
	action VARCHAR(32) NOT NULL,
	request_hash CHAR(64) NOT NULL,
	request JSON NOT NULL,
	status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
	result JSON,
	lease_until DATETIME(6) NOT NULL,
	fencing_version INTEGER NOT NULL DEFAULT 1,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	CONSTRAINT uq_content_work_command_0 UNIQUE (public_id),
	CONSTRAINT uq_content_work_command_1 UNIQUE (work_order_id, actor_id, idempotency_key)
)ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_bin""",
]


def upgrade() -> None:
    for statement in TABLE_DDL:
        op.execute(statement)
    op.execute("UPDATE schema_version SET version=22 WHERE id=1")


def downgrade() -> None:
    raise RuntimeError("Retain original inputs and command receipts; restore the application only")
