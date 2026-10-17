"""Execute the actual 0024 -> 0025 migration against owned legacy MySQL fixtures."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tests.integration.test_admin_contact_schema import legacy_schema as isolated_schema

db = isolated_schema
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("case", ["matching", "sense_mismatch", "source_mismatch", "ambiguous"])
def test_0025_backfills_only_unique_matching_entry_sense_source(db, case):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", db.replace("%", "%%"))
    command.upgrade(config, "0024")
    engine = create_engine(db)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO content_entry(public_id,content_type,content_id,entry_type,"
                    "sense_id,"
                    "source_id,lexicon_entry_id,lexicon_entry_version) "
                    "VALUES('entry','dialogue','content','word','current-sense','current-source',"
                    "'central',3)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO content_entry_revision(public_id,entry_id,revision_id,"
                    "entry_version,"
                    "sense_id,source_id,details,details_hash,confirmed_by) "
                    "VALUES('entry-revision','entry','revision',2,'current-sense','current-source',"
                    "'{\"english\":\"bank\"}',:hash,'admin')"
                ),
                dict(hash="a" * 64),
            )
            mappings = [
                dict(
                    key="old-key",
                    sense="old-sense" if case == "sense_mismatch" else "current-sense",
                    source="old-source" if case == "source_mismatch" else "current-source",
                )
            ]
            if case == "ambiguous":
                mappings.append(
                    dict(key="another-key", sense="current-sense", source="current-source")
                )
            for mapping in mappings:
                connection.execute(
                    text(
                        "INSERT INTO import_entry_mapping(namespace,content_type,content_id,"
                        "import_entry_id,"
                        "entry_id,sense_id,source_id,entry_type,source_fingerprint,input_hash) "
                        "VALUES('juya-content-library','dialogue','content',:key,'entry',:sense,"
                        ":source,"
                        "'word',:hash,:hash)"
                    ),
                    mapping | dict(hash="b" * 64),
                )
            before = connection.execute(
                text("SELECT * FROM import_entry_mapping ORDER BY id")
            ).all()
            details_before = connection.execute(
                text(
                    "SELECT details,details_hash FROM content_entry_revision WHERE entry_id='entry'"
                )
            ).one()
        # Real Alembic execution includes the production backfill, not a copied query or SQL mock.
        command.upgrade(config, "0025")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT import_namespace,import_entry_id,identity_provenance FROM "
                    "content_entry "
                    "WHERE public_id='entry'"
                )
            ).one()
            expected = (
                ("juya-content-library", "old-key", "legacy_import_mapping")
                if case == "matching"
                else (None, None, "legacy_unmapped")
            )
            assert tuple(row) == expected
            assert (
                connection.execute(text("SELECT * FROM import_entry_mapping ORDER BY id")).all()
                == before
            )
            assert (
                connection.execute(
                    text(
                        "SELECT details,details_hash FROM content_entry_revision WHERE "
                        "entry_id='entry'"
                    )
                ).one()
                == details_before
            )
            assert tuple(
                connection.execute(
                    text(
                        "SELECT lexicon_entry_id,lexicon_entry_version FROM content_entry_revision "
                        "WHERE entry_id='entry'"
                    )
                ).one()
            ) == (None, None)
            assert connection.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 25
    finally:
        engine.dispose()
