"""Real 0017→0018 upgrade preserves immutable data and the older read contract."""

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tests.integration.test_admin_contact_schema import legacy_schema  # noqa: F401


def test_0018_preserves_old_rows_hashes_and_paid_attempts(legacy_schema):  # noqa: F811
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0017")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO short_reading(public_id,series_id,status,draft_revision_id,"
                    "published_revision_id,updated_at) VALUES('old-reading','old-series',"
                    "'PUBLISHED','old-revision','old-revision',UTC_TIMESTAMP(6))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO short_reading_revision(public_id,short_reading_id,version_no,"
                    "edit_version,status,content_snapshot,content_hash,created_by) VALUES("
                    "'old-revision','old-reading',1,1,'PUBLISHED',:snapshot,:hash,'migration-test')"
                ),
                {"snapshot": '{"title_en":"Old published","paragraphs":[]}', "hash": "a" * 64},
            )
            conn.execute(
                text(
                    "INSERT INTO content_cloud_attempt(public_id,stage_id,kind,period,"
                    "reserved_units,actual_units,status,price_version,region,currency,created_at) "
                    "VALUES('old-paid','old-stage','AI','2026-10-11',100,70,'FAILED','v1','cn','CNY',"
                    "UTC_TIMESTAMP(6))"
                )
            )
            before = conn.execute(
                text(
                    "SELECT public_id,short_reading_id,version_no,edit_version,status,"
                    "content_snapshot,content_hash FROM short_reading_revision"
                )
            ).all()
            ledger = conn.execute(
                text(
                    "SELECT public_id,stage_id,reserved_units,actual_units,status "
                    "FROM content_cloud_attempt"
                )
            ).all()
        command.upgrade(config, "0018")
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 18
            assert (
                conn.execute(
                    text(
                        "SELECT public_id,short_reading_id,version_no,edit_version,status,"
                        "content_snapshot,content_hash FROM short_reading_revision"
                    )
                ).all()
                == before
            )
            assert (
                conn.execute(
                    text(
                        "SELECT public_id,stage_id,reserved_units,actual_units,status "
                        "FROM content_cloud_attempt"
                    )
                ).all()
                == ledger
            )
            assert conn.scalar(text("SELECT sort_order FROM short_reading")) == 0
            assert conn.scalar(text("SELECT attempt_number FROM content_cloud_attempt")) == 1
            # Older readers still see exactly the same published revision and snapshot.
            assert (
                conn.scalar(
                    text(
                        "SELECT r.content_hash FROM short_reading s JOIN short_reading_revision r "
                        "ON r.public_id=s.published_revision_id WHERE s.status='PUBLISHED'"
                    )
                )
                == "a" * 64
            )
            assert conn.scalar(text("SELECT COUNT(*) FROM publication_range")) == 0
        path = root / "migrations/versions/0018_v15_contract_closeout.py"
        spec = importlib.util.spec_from_file_location("closeout_migration", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.downgrade()
        except RuntimeError as error:
            assert "Retain" in str(error)
        else:
            raise AssertionError("Downgrade must preserve immutable records")
    finally:
        engine.dispose()
