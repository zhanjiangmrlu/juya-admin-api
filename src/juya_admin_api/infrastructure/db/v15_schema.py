"""V1.5 shared SQL tables. Admin API is the sole migration owner.

BFF writes favorite_snapshot*, favorite_resource_reference and short_reading_progress.
All external identities use public strings. Existing user_account numeric FK is retained.
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.schema import SchemaItem

metadata = sa.MetaData()
ID = sa.String(64, collation="utf8mb4_bin")
BIG = mysql.BIGINT(unsigned=True)
DT = mysql.DATETIME(fsp=6)


def col(
    name: str, type_: Any, *, null: bool = False, default: str | None = None
) -> sa.Column[object]:
    return sa.Column(
        name, type_, nullable=null, server_default=sa.text(default) if default else None
    )


def table(name: str, *columns: SchemaItem, unique: tuple[tuple[str, ...], ...] = ()) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column("id", BIG, primary_key=True, autoincrement=True),
        *columns,
        *(
            sa.UniqueConstraint(*keys, name="uq_" + name + "_" + str(i))
            for i, keys in enumerate(unique)
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )


def public() -> sa.Column[object]:
    return col("public_id", ID)


def created() -> sa.Column[object]:
    return col("created_at", DT, default="CURRENT_TIMESTAMP(6)")


def typed() -> list[sa.Column[object]]:
    return [col("content_type", sa.String(32)), col("content_id", ID)]


def snapshot_fields() -> list[sa.Column[object]]:
    return [
        col("english", sa.Text()),
        col("phonetic", sa.String(200), default="''"),
        col("meaning_zh", sa.Text()),
        col("explanation", sa.Text()),
        col("source_sentence", sa.Text()),
        col("source_locator", mysql.JSON, null=True),
        col("safe_title_en", sa.String(200), default="''"),
        col("safe_title_zh", sa.String(200), default="''"),
        col("image_asset_id", ID, null=True),
        col("pronunciation_target_id", ID, null=True),
        col("resource_version", ID, null=True),
        col("legacy_incomplete", sa.Boolean(), default="0"),
        col("missing_fields", mysql.JSON, null=True),
    ]


table(
    "content_work_order",
    public(),
    col("import_batch_id", ID, null=True),
    col("source_mode", sa.String(32)),
    col("series_id", ID),
    col("content_type", sa.String(32)),
    col("version", sa.Integer(), default="1"),
    col("status", sa.String(32), default="'ACTIVE'"),
    col("proofreading_set_id", ID, null=True),
    col("publication_token", ID, null=True),
    col("active_command_id", ID, null=True),
    col("created_by", ID),
    created(),
    col("updated_at", DT, default="CURRENT_TIMESTAMP(6)"),
    unique=(("public_id",), ("import_batch_id",)),
)
table(
    "content_work_item",
    public(),
    col("work_order_id", ID),
    col("import_item_id", ID, null=True),
    col("position", sa.Integer()),
    col("content_id", ID, null=True),
    col("original_revision_id", ID, null=True),
    col("source_snapshot", mysql.JSON, null=True),
    col("source_hash", sa.CHAR(64), null=True),
    col("historical_issues", mysql.JSON, null=True),
    col("cancelled", sa.Boolean(), default="0"),
    created(),
    sa.Index("ix_work_item_content", "content_id", "work_order_id"),
    unique=(("public_id",), ("work_order_id", "import_item_id")),
)
table(
    "content_work_item_revision",
    col("work_item_id", ID),
    col("revision_id", ID),
    created(),
    unique=(("work_item_id", "revision_id"),),
)
table(
    "content_work_item_stage",
    col("work_item_id", ID),
    col("stage_id", ID),
    col("revision_id", ID, null=True),
    col("sentence_id", ID, null=True),
    created(),
    unique=(("work_item_id", "stage_id"),),
)
table(
    "content_work_issue_resolution",
    col("work_item_id", ID),
    col("issue_id", ID),
    col("source_hash", sa.CHAR(64)),
    col("candidate_hash", sa.CHAR(64)),
    col("resolved_by", ID),
    created(),
    unique=(("work_item_id", "issue_id", "candidate_hash"),),
)
table(
    "content_work_command",
    public(),
    col("work_order_id", ID),
    col("actor_id", ID),
    col("idempotency_key", sa.String(100)),
    col("action", sa.String(32)),
    col("request_hash", sa.CHAR(64)),
    col("request", mysql.JSON),
    col("status", sa.String(32), default="'PENDING'"),
    col("result", mysql.JSON, null=True),
    col("lease_until", DT),
    col("fencing_version", sa.Integer(), default="1"),
    created(),
    unique=(("public_id",), ("work_order_id", "actor_id", "idempotency_key")),
)


table(
    "content_identity",
    *typed(),
    col("legacy_scene_id", ID, null=True),
    created(),
    unique=(("content_type", "content_id"),),
)
table(
    "short_reading",
    public(),
    col("series_id", ID),
    col("status", sa.String(32), default="'DRAFT'"),
    col("draft_revision_id", ID, null=True),
    col("published_revision_id", ID, null=True),
    col("version", sa.Integer(), default="1"),
    col("sort_order", sa.Integer(), default="0"),
    created(),
    col("updated_at", DT),
    unique=(("public_id",),),
)
table(
    "short_reading_revision",
    public(),
    col("short_reading_id", ID),
    col("source_revision_id", ID, null=True),
    col("version_no", sa.Integer()),
    col("edit_version", sa.Integer(), default="1"),
    col("status", sa.String(32), default="'DRAFT'"),
    col("content_snapshot", mysql.JSON),
    col("content_hash", sa.CHAR(64)),
    col("created_by", ID),
    created(),
    unique=(("public_id",), ("short_reading_id", "version_no")),
)
table(
    "short_reading_trial",
    col("short_reading_id", ID),
    col("enabled", sa.Boolean(), default="0"),
    col("version", sa.Integer(), default="1"),
    col("updated_by", ID),
    col("updated_at", DT),
    unique=(("short_reading_id",),),
)
table(
    "content_package_item",
    col("package_id", BIG),
    *typed(),
    col("position", sa.Integer()),
    created(),
    unique=(("package_id", "content_type", "content_id"),),
)
table(
    "import_content_mapping",
    col("namespace", sa.String(64)),
    col("content_type", sa.String(32)),
    col("import_content_id", ID),
    col("content_id", ID),
    col("input_hash", sa.CHAR(64)),
    col("version", sa.Integer(), default="1"),
    created(),
    unique=(("namespace", "content_type", "import_content_id"),),
)
table(
    "import_entry_mapping",
    col("namespace", sa.String(64)),
    *typed(),
    col("import_entry_id", ID),
    col("entry_id", ID),
    col("sense_id", ID),
    col("source_id", ID),
    col("entry_type", sa.String(16)),
    col("source_fingerprint", sa.CHAR(64)),
    col("input_hash", sa.CHAR(64)),
    col("version", sa.Integer(), default="1"),
    created(),
    unique=(("namespace", "content_type", "content_id", "import_entry_id"),),
)
table(
    "content_import_batch",
    public(),
    col("namespace", sa.String(64)),
    col("series_id", ID),
    col("content_type", sa.String(32)),
    col("import_mode", sa.String(32)),
    col("asset_id", ID),
    col("input_hash", sa.CHAR(64)),
    col("manifest", mysql.JSON, null=True),
    col("preview_version", sa.Integer(), default="0"),
    col("status", sa.String(32), default="'PENDING'"),
    col("created_by", ID),
    created(),
    col("cancelled_at", DT, null=True),
    unique=(("public_id",),),
)
table(
    "content_import_item",
    public(),
    col("batch_id", ID),
    col("row_no", sa.Integer()),
    col("import_content_id", ID),
    col("content_id", ID, null=True),
    col("revision_id", ID, null=True),
    col("input_hash", sa.CHAR(64)),
    col("expected_version", sa.Integer(), null=True),
    col("operation", sa.String(32)),
    col("status", sa.String(32)),
    col("candidate", mysql.JSON),
    col("issues", mysql.JSON),
    col("result", mysql.JSON, null=True),
    created(),
    unique=(("public_id",), ("batch_id", "row_no")),
)
table(
    "content_import_stage",
    public(),
    col("item_id", ID),
    col("stage", sa.String(32)),
    col("input_hash", sa.CHAR(64)),
    col("config_version", ID),
    col("status", sa.String(32), default="'PENDING'"),
    col("attempt", sa.Integer(), default="0"),
    col("fencing_version", sa.Integer(), default="0"),
    col("lease_owner", ID, null=True),
    col("lease_until", DT, null=True),
    col("output", mysql.JSON, null=True),
    col("error_code", sa.String(64), null=True),
    col("next_retry_at", DT, null=True),
    created(),
    unique=(("public_id",), ("item_id", "stage", "input_hash", "config_version")),
)
table(
    "content_entry",
    public(),
    *typed(),
    col("entry_type", sa.String(16)),
    col("lexicon_entry_id", ID, null=True),
    col("sense_id", ID),
    col("source_id", ID),
    created(),
    unique=(("public_id",),),
)
table(
    "content_sense",
    public(),
    col("entry_id", ID),
    col("meaning_zh", sa.Text()),
    col("confirmed_by", ID),
    col("confirmed_at", DT, null=True),
    created(),
    unique=(("public_id",),),
)
table(
    "content_source",
    public(),
    *typed(),
    col("entry_id", ID),
    col("source_fingerprint", sa.CHAR(64)),
    col("locator", mysql.JSON),
    created(),
    unique=(("public_id",),),
)
table(
    "content_entry_revision",
    public(),
    col("entry_id", ID),
    col("revision_id", ID),
    col("entry_version", sa.Integer()),
    col("sense_id", ID),
    col("source_id", ID),
    col("details", mysql.JSON),
    col("details_hash", sa.CHAR(64)),
    col("confirmed_by", ID),
    col("confirmed_at", DT, null=True),
    created(),
    unique=(("public_id",), ("entry_id", "revision_id")),
)
table(
    "lexicon_candidate",
    public(),
    *typed(),
    col("revision_id", ID),
    col("input_hash", sa.CHAR(64)),
    col("candidate", mysql.JSON),
    col("status", sa.String(32), default="'PENDING'"),
    col("decided_by", ID, null=True),
    col("decided_at", DT, null=True),
    created(),
    unique=(("public_id",),),
)
table(
    "sentence_audio_slice",
    public(),
    *typed(),
    col("revision_id", ID),
    col("sentence_id", ID),
    col("source_audio_version", ID),
    col("source_sha256", sa.CHAR(64)),
    col("start_ms", sa.Integer()),
    col("end_ms", sa.Integer()),
    col("target_id", ID),
    col("resource_version", ID),
    col("asset_id", ID),
    col("dependency_hash", sa.CHAR(64)),
    col("status", sa.String(32)),
    col("actual_duration_ms", sa.Integer()),
    col("slice_sha256", sa.CHAR(64)),
    col("byte_size", BIG),
    col("confirmed_by", ID, null=True),
    col("confirmed_at", DT, null=True),
    col("encoding_config", mysql.JSON),
    created(),
    unique=(("public_id",), ("revision_id", "sentence_id", "dependency_hash")),
)
table(
    "proofreading_set",
    public(),
    col("collection_hash", sa.CHAR(64)),
    col("candidates", mysql.JSON),
    col("normal_count", sa.Integer()),
    col("sample_ids", mysql.JSON),
    col("status", sa.String(32), default="'PENDING'"),
    col("created_by", ID),
    created(),
    unique=(("public_id",),),
)
table(
    "proofreading_confirmation",
    col("set_id", ID),
    *typed(),
    col("revision_id", ID),
    col("candidate_hash", sa.CHAR(64)),
    col("method", sa.String(32)),
    col("confirmed_by", ID),
    col("confirmed_at", DT),
    unique=(("set_id", "revision_id"),),
)
table(
    "favorite_snapshot",
    public(),
    col("user_id", BIG),
    *typed(),
    col("entry_type", sa.String(16)),
    col("entry_id", ID),
    col("sense_id", ID),
    col("source_id", ID),
    col("active", sa.Boolean(), default="1"),
    col("current_snapshot_revision_id", ID, null=True),
    col("snapshot_version", sa.Integer(), default="1"),
    col("favorited_at", DT),
    col("cancelled_at", DT, null=True),
    col("last_reviewed_at", DT, null=True),
    sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
    unique=(
        ("public_id",),
        (
            "user_id",
            "content_type",
            "content_id",
            "entry_type",
            "entry_id",
            "sense_id",
            "source_id",
        ),
    ),
)
table(
    "favorite_snapshot_revision",
    public(),
    col("favorite_id", BIG),
    col("snapshot_version", sa.Integer()),
    *typed(),
    col("saved_content_version", sa.Integer(), null=True),
    col("revision_id", ID, null=True),
    *snapshot_fields(),
    created(),
    sa.ForeignKeyConstraint(["favorite_id"], ["favorite_snapshot.id"], ondelete="CASCADE"),
    unique=(("public_id",), ("favorite_id", "snapshot_version")),
)
table(
    "favorite_legacy_map",
    col("legacy_favorite_id", BIG),
    col("legacy_source_id", BIG),
    col("favorite_id", BIG),
    col("snapshot_revision_id", ID),
    col("migration_status", sa.String(32)),
    created(),
    sa.ForeignKeyConstraint(["favorite_id"], ["favorite_snapshot.id"], ondelete="CASCADE"),
    unique=(("legacy_source_id",),),
)
table(
    "favorite_resource_reference",
    col("favorite_id", ID),
    col("snapshot_revision_id", ID),
    *typed(),
    col("revision_id", ID),
    col("image_asset_id", ID, null=True),
    col("pronunciation_target_id", ID, null=True),
    col("resource_version", ID, null=True),
    col("active", sa.Boolean()),
    col("event_id", sa.String(128)),
    col("sequence", BIG),
    col("updated_at", DT),
    unique=(("favorite_id",), ("event_id",)),
)
table(
    "favorite_resource_projection",
    col("favorite_id", ID),
    col("snapshot_revision_id", ID),
    *typed(),
    col("revision_id", ID),
    col("image_asset_id", ID, null=True),
    col("pronunciation_target_id", ID, null=True),
    col("resource_version", ID, null=True),
    col("active", sa.Boolean()),
    col("event_id", sa.String(128)),
    col("sequence", BIG),
    col("updated_at", DT),
    unique=(("favorite_id",), ("event_id",)),
)
table(
    "short_reading_progress",
    col("user_id", BIG),
    col("short_reading_id", ID),
    col("saved_content_version", sa.Integer()),
    col("paragraph_id", ID, null=True),
    col("offset", sa.Integer(), default="0"),
    col("position", mysql.JSON),
    col("last_client_sequence", BIG, default="0"),
    col("started_at", DT),
    col("last_read_at", DT),
    col("completed_at", DT, null=True),
    sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
    unique=(("user_id", "short_reading_id"),),
)

# Existing table is represented only to resolve the FK in schema compilation, never created here.
sa.Table("user_account", metadata, sa.Column("id", BIG, primary_key=True), info={"existing": True})
