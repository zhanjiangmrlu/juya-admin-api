"""A real 0020 -> 0021 migration retains paid audio and the Tencent ledger."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from juya_admin_api.modules.pronunciation.domain import Config as TTSConfig
from juya_admin_api.modules.pronunciation.domain import Target
from tests.integration.test_admin_contact_schema import legacy_schema  # noqa: F401


def test_0021_adds_independent_review_checkpoint_without_changing_paid_audio(legacy_schema):  # noqa: F811
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0020")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO pronunciation_task(public_id,cache_key,target_id,config,"
                    "target_snapshot,status,attempt,lease_until,next_retry_at,audio,object_key,"
                    "request_id,error_code,outbox_pending,created_at) VALUES('paid-task',:key,"
                    "'target',:config,:target,'RECOVER',1,0,0,:audio,"
                    "'sealed/media/audio/paid.mp3','tencent-existing-request',"
                    "'AUDIO_SECURITY_PENDING',1,0)"
                ),
                {
                    "key": "a" * 64,
                    "config": json.dumps(asdict(TTSConfig(enabled=True))),
                    "target": json.dumps(asdict(Target("target", "hello"))),
                    "audio": b"retained-paid-audio",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO cloud_usage_attempt(public_id,task_id,attempt,period,"
                    "estimated_characters,estimated_cost,actual_characters,price_version,currency,"
                    "region,provider_request_id,status,created_at) VALUES("
                    "'paid-attempt','paid-task',"
                    "1,'migration-period',5,150,5,'20261013','CNY','ap-guangzhou',"
                    "'tencent-existing-request','SUCCEEDED',0)"
                )
            )
            before = connection.execute(text("SELECT * FROM pronunciation_task")).mappings().one()
            before = dict(before)
            ledger = connection.execute(text("SELECT * FROM cloud_usage_attempt")).mappings().all()
            assert connection.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 20
        command.upgrade(config, "0021")
        with engine.begin() as connection:
            after = dict(
                connection.execute(text("SELECT * FROM pronunciation_task")).mappings().one()
            )
            assert after.pop("security_request_id") is None
            assert after.pop("security_status") is None
            assert after == before
            assert (
                connection.execute(text("SELECT * FROM cloud_usage_attempt")).mappings().all()
                == ledger
            )
            assert connection.scalar(text("SELECT COUNT(*) FROM pronunciation_cache")) == 0
            assert connection.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 21
            # An existing reader still sees the original independent Tencent request identity.
            assert (
                connection.scalar(text("SELECT request_id FROM pronunciation_task"))
                == "tencent-existing-request"
            )
        with pytest.raises(RuntimeError, match="Retain paid audio moderation task identities"):
            command.downgrade(config, "0020")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 21
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021"
            assert connection.execute(
                text("SELECT security_request_id,security_status FROM pronunciation_task")
            ).one() == (None, None)
    finally:
        engine.dispose()
