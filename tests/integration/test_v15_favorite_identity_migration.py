"""Tiny real MySQL fixtures for content-scoped favorite identity and safe rollback."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from tests.integration.test_admin_contact_schema import legacy_schema  # noqa: F401

OLD_KEY = "user_id,entry_type,entry_id,sense_id,source_id"
NEW_KEY = "user_id,content_type,content_id,entry_type,entry_id,sense_id,source_id"


def config() -> Config:
    root = Path(__file__).resolve().parents[2]
    result = Config(str(root / "alembic.ini"))
    result.set_main_option("script_location", str(root / "migrations"))
    return result


def seed_old(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO user_account(public_id,juya_number,status) "
            "VALUES('identity-user','9919000001','ACTIVE')"
        )
    )
    conn.execute(
        text(
            "INSERT INTO favorite_snapshot(public_id,user_id,entry_type,entry_id,sense_id,"
            "source_id,current_snapshot_revision_id,snapshot_version,favorited_at) "
            "SELECT 'old-favorite',id,'word','entry','sense','source','old-snapshot-2',2,"
            "UTC_TIMESTAMP(6) FROM user_account WHERE public_id='identity-user'"
        )
    )
    for version in (1, 2):
        conn.execute(
            text(
                "INSERT INTO favorite_snapshot_revision(public_id,favorite_id,snapshot_version,"
                "content_type,content_id,saved_content_version,revision_id,english,meaning_zh,"
                "explanation,source_sentence) SELECT :public,id,:version,'word_card','article-A',"
                ":version,:revision,'coffee','咖啡','unchanged','A coffee.' "
                "FROM favorite_snapshot WHERE public_id='old-favorite'"
            ),
            {"public": f"old-snapshot-{version}", "version": version, "revision": f"rev-{version}"},
        )
    conn.execute(
        text(
            "INSERT INTO favorite_resource_reference(favorite_id,snapshot_revision_id,"
            "content_type,content_id,revision_id,resource_version,active,event_id,sequence,"
            "updated_at) VALUES('old-favorite','old-snapshot-2','word_card','article-A','rev-2',"
            "'resource-2',1,'old-event',2,UTC_TIMESTAMP(6))"
        )
    )
    conn.execute(
        text(
            "INSERT INTO content_cloud_attempt(public_id,stage_id,kind,period,reserved_units,"
            "actual_units,status,price_version,region,currency,created_at) VALUES('old-paid',"
            "'old-stage','AI','2026-10-11',100,70,'FAILED','v1','cn','CNY',UTC_TIMESTAMP(6))"
        )
    )


def rows(conn: Connection, table: str) -> list[dict]:
    assert table in {
        "favorite_snapshot",
        "favorite_snapshot_revision",
        "favorite_resource_reference",
        "content_cloud_attempt",
    }
    ordering = "public_id" if table == "content_cloud_attempt" else "id"
    result = [
        dict(row)
        for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY {ordering}")).mappings()
    ]
    if table == "favorite_snapshot":
        for row in result:
            row.pop("content_type", None)
            row.pop("content_id", None)
    return result


def index_columns(conn: Connection) -> str:
    return conn.scalar(
        text(
            "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
            "FROM information_schema.statistics WHERE table_schema=DATABASE() "
            "AND table_name='favorite_snapshot' AND index_name='uq_favorite_snapshot_1' "
            "AND NON_UNIQUE=0"
        )
    )


def test_upgrade_preserves_history_and_separates_content_with_downgrade_guard(legacy_schema):  # noqa: F811
    migration = config()
    command.upgrade(migration, "0018")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as conn:
            seed_old(conn)
            tables = (
                "favorite_snapshot",
                "favorite_snapshot_revision",
                "favorite_resource_reference",
                "content_cloud_attempt",
            )
            before = {table: rows(conn, table) for table in tables}
        command.upgrade(migration, "head")
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 19
            assert index_columns(conn) == NEW_KEY
            assert {table: rows(conn, table) for table in tables} == before
            assert conn.execute(
                text("SELECT content_type,content_id FROM favorite_snapshot")
            ).one() == ("word_card", "article-A")
            # Same shared entry IDs in another article and another content type stay independent.
            for public, kind, article in (
                ("article-B-favorite", "word_card", "article-B"),
                ("dialogue-favorite", "dialogue", "article-A"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO favorite_snapshot(public_id,user_id,content_type,content_id,"
                        "entry_type,entry_id,sense_id,source_id,snapshot_version,favorited_at) "
                        "SELECT :public,user_id,:kind,:article,entry_type,entry_id,sense_id,"
                        "source_id,1,favorited_at FROM favorite_snapshot "
                        "WHERE public_id='old-favorite'"
                    ),
                    {"public": public, "kind": kind, "article": article},
                )
            # A new content/snapshot version does not create a new identity in the same article.
            with pytest.raises(IntegrityError) as duplicate:
                conn.execute(
                    text(
                        "INSERT INTO favorite_snapshot(public_id,user_id,content_type,content_id,"
                        "entry_type,entry_id,sense_id,source_id,snapshot_version,favorited_at) "
                        "SELECT 'duplicate',user_id,content_type,content_id,entry_type,entry_id,"
                        "sense_id,source_id,3,favorited_at FROM favorite_snapshot "
                        "WHERE public_id='old-favorite'"
                    )
                )
            assert duplicate.value.orig.args[0] == 1062
            retained = {table: rows(conn, table) for table in tables}
        with pytest.raises(RuntimeError, match="content namespace collides"):
            command.downgrade(migration, "0018")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 19
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0019"
            assert index_columns(conn) == NEW_KEY
            assert {table: rows(conn, table) for table in tables} == retained
            assert conn.scalar(text("SELECT COUNT(*) FROM favorite_snapshot")) == 3
    finally:
        engine.dispose()


def test_collision_free_downgrade_and_reupgrade_keep_public_ids_and_saved_revisions(legacy_schema):  # noqa: F811
    migration = config()
    command.upgrade(migration, "0018")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as conn:
            seed_old(conn)
            before = rows(conn, "favorite_snapshot")
            revisions = rows(conn, "favorite_snapshot_revision")
        command.upgrade(migration, "0019")
        command.downgrade(migration, "0018")
        with engine.connect() as conn:
            assert index_columns(conn) == OLD_KEY
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 18
            assert rows(conn, "favorite_snapshot") == before
            assert rows(conn, "favorite_snapshot_revision") == revisions
            assert (
                conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() "
                        "AND table_name='favorite_snapshot' "
                        "AND column_name IN ('content_type','content_id')"
                    )
                )
                == 0
            )
        command.upgrade(migration, "0019")
        with engine.connect() as conn:
            assert index_columns(conn) == NEW_KEY
            assert rows(conn, "favorite_snapshot") == before
            assert rows(conn, "favorite_snapshot_revision") == revisions
    finally:
        engine.dispose()


@pytest.mark.parametrize("invalid", ["missing_pointer", "wrong_owner", "empty_scope"])
def test_upgrade_refuses_unproven_scope_before_any_ddl(legacy_schema, invalid):  # noqa: F811
    migration = config()
    command.upgrade(migration, "0018")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as conn:
            seed_old(conn)
            if invalid == "missing_pointer":
                conn.execute(text("UPDATE favorite_snapshot SET current_snapshot_revision_id=NULL"))
            elif invalid == "empty_scope":
                conn.execute(text("UPDATE favorite_snapshot_revision SET content_id=''"))
            else:
                conn.execute(
                    text(
                        "INSERT INTO favorite_snapshot(public_id,user_id,entry_type,entry_id,"
                        "sense_id,source_id,current_snapshot_revision_id,snapshot_version,"
                        "favorited_at) SELECT 'other-favorite',user_id,entry_type,'other-entry',"
                        "sense_id,source_id,'old-snapshot-2',2,favorited_at "
                        "FROM favorite_snapshot WHERE public_id='old-favorite'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE favorite_snapshot_revision SET favorite_id="
                        "(SELECT id FROM favorite_snapshot WHERE public_id='other-favorite') "
                        "WHERE public_id='old-snapshot-2'"
                    )
                )
            before = rows(conn, "favorite_snapshot")
            revisions = rows(conn, "favorite_snapshot_revision")
        with pytest.raises(RuntimeError, match="namespace cannot be proven"):
            command.upgrade(migration, "0019")
        with engine.connect() as conn:
            assert index_columns(conn) == OLD_KEY
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 18
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0018"
            assert rows(conn, "favorite_snapshot") == before
            assert rows(conn, "favorite_snapshot_revision") == revisions
            assert (
                conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() "
                        "AND table_name='favorite_snapshot' "
                        "AND column_name IN ('content_type','content_id')"
                    )
                )
                == 0
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("phase", ["columns_added", "identity_installed"])
def test_interrupted_ddl_can_resume_without_rewriting_old_facts(legacy_schema, phase):  # noqa: F811
    migration = config()
    command.upgrade(migration, "0018")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as conn:
            seed_old(conn)
            before = rows(conn, "favorite_snapshot")
            revisions = rows(conn, "favorite_snapshot_revision")
        if phase == "columns_added":
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE favorite_snapshot "
                        "ADD COLUMN content_type VARCHAR(32) COLLATE utf8mb4_bin NULL, "
                        "ADD COLUMN content_id VARCHAR(64) COLLATE utf8mb4_bin NULL"
                    )
                )
        else:
            command.upgrade(migration, "0019")
            with engine.begin() as conn:
                # Simulate DDL commit followed by process loss before the version update committed.
                conn.execute(text("UPDATE alembic_version SET version_num='0018'"))
                conn.execute(text("UPDATE schema_version SET version=18 WHERE id=1"))
        command.upgrade(migration, "0019")
        with engine.connect() as conn:
            assert index_columns(conn) == NEW_KEY
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 19
            assert rows(conn, "favorite_snapshot") == before
            assert rows(conn, "favorite_snapshot_revision") == revisions
            assert conn.execute(
                text("SELECT content_type,content_id FROM favorite_snapshot")
            ).one() == ("word_card", "article-A")
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", ["content_type", "content_id"])
def test_interrupted_namespace_refuses_tail_space_before_ddl(legacy_schema, field):  # noqa: F811
    migration = config()
    command.upgrade(migration, "0018")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as conn:
            seed_old(conn)
            conn.execute(
                text(
                    "ALTER TABLE favorite_snapshot "
                    "ADD COLUMN content_type VARCHAR(32) COLLATE utf8mb4_bin NULL, "
                    "ADD COLUMN content_id VARCHAR(64) COLLATE utf8mb4_bin NULL"
                )
            )
            conn.execute(
                text("UPDATE favorite_snapshot SET content_type='word_card',content_id='article-A'")
            )
            assert field in {"content_type", "content_id"}
            conn.execute(text(f"UPDATE favorite_snapshot SET {field}=CONCAT({field},' ')"))
            namespace = conn.execute(
                text("SELECT content_type,content_id FROM favorite_snapshot")
            ).one()
            before = rows(conn, "favorite_snapshot")
            revisions = rows(conn, "favorite_snapshot_revision")
        with pytest.raises(RuntimeError, match="namespace contradicts current revision"):
            command.upgrade(migration, "0019")
        with engine.connect() as conn:
            assert index_columns(conn) == OLD_KEY
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 18
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0018"
            assert rows(conn, "favorite_snapshot") == before
            assert rows(conn, "favorite_snapshot_revision") == revisions
            assert (
                conn.execute(text("SELECT content_type,content_id FROM favorite_snapshot")).one()
                == namespace
            )
            assert conn.execute(
                text(
                    "SELECT IS_NULLABLE FROM information_schema.columns "
                    "WHERE table_schema=DATABASE() AND table_name='favorite_snapshot' "
                    "AND column_name IN ('content_type','content_id')"
                )
            ).scalars().all() == ["YES", "YES"]
    finally:
        engine.dispose()
