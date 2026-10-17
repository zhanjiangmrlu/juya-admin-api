"""Persistent independent switches, concurrent replay and old-body compatibility."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.production_store import ProductionStore, decode, encode
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.learning_features.router import FeaturesResponse
from juya_admin_api.modules.learning_features.store import CONFIG_KEY, LearningFeaturesStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_sql_profile_flags_cas_preserved_old_body_receipts_and_same_key_creation():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = LearningFeaturesStore(sessions)
    actor = new_ulid(datetime.now(UTC))
    key = "key-" + actor + "x" * 70
    assert len(key) == 100
    async with sessions() as session, session.begin():
        old = (
            (
                await session.execute(
                    text("SELECT * FROM system_config WHERE config_key=:key FOR UPDATE"),
                    {"key": CONFIG_KEY},
                )
            )
            .mappings()
            .first()
        )
        previous = dict(old) if old else None
        await session.execute(
            text("DELETE FROM system_config WHERE config_key=:key"), {"key": CONFIG_KEY}
        )
    try:
        assert await store.read() == {
            "recording_enabled": False,
            "profile_completion_enabled": False,
            "version": 0,
        }
        payload = {
            "recording_enabled": True,
            "profile_completion_enabled": True,
            "expected_version": 0,
        }
        replies = await asyncio.gather(
            *[store.command(actor, key, payload) for _ in range(3)], return_exceptions=True
        )
        enabled = {"recording_enabled": True, "profile_completion_enabled": True, "version": 1}
        assert replies == [enabled] * 3
        async with sessions() as session:
            audit = (
                (
                    await session.execute(
                        text(
                            "SELECT request_id,after_summary FROM audit_event "
                            "WHERE actor_public_id=:actor AND action='learning_features.update'"
                        ),
                        {"actor": actor},
                    )
                )
                .mappings()
                .all()
            )
            assert (
                len(audit) == 1
                and audit[0]["request_id"] == hashlib.sha256(key.encode()).hexdigest()
            )
        preserved = await store.command(
            actor, "old-body", {"recording_enabled": False, "expected_version": 1}
        )
        assert preserved == {
            "recording_enabled": False,
            "profile_completion_enabled": True,
            "version": 2,
        }
        with pytest.raises(AppError) as conflict:
            await store.command(
                actor,
                "stale",
                {
                    "recording_enabled": False,
                    "profile_completion_enabled": False,
                    "expected_version": 1,
                },
            )
        assert conflict.value.code == "CONFIG_VERSION_CONFLICT"
        disabled = await store.command(
            actor,
            "profile-off",
            {
                "recording_enabled": False,
                "profile_completion_enabled": False,
                "expected_version": 2,
            },
        )
        assert disabled["version"] == 3
        assert await store.command(actor, key, payload) == enabled
        assert await store.read() == disabled
        with pytest.raises(AppError) as reused:
            await store.command(actor, key, {**payload, "profile_completion_enabled": False})
        assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"
        # A pre-feature receipt remains replayable without modifying current flags.
        legacy_payload = {"recording_enabled": True, "expected_version": 0}
        legacy_receipt = {"recording_enabled": True, "version": 1}
        async with sessions() as session, session.begin():
            await ProductionStore(sessions).remember_creation(
                session,
                CONFIG_KEY,
                actor,
                "legacy-receipt",
                snapshot_hash(legacy_payload),
                legacy_receipt,
                datetime.now(UTC),
            )
        replay = await store.command(actor, "legacy-receipt", legacy_payload)
        assert (
            replay == legacy_receipt
            and FeaturesResponse(**replay).profile_completion_enabled is False
        )
        assert await store.read() == disabled
        for value, recording, profile in (
            ({"recording_enabled": True}, True, False),
            ({"recording_enabled": True, "profile_completion_enabled": "true"}, True, False),
            ({"recording_enabled": 1, "profile_completion_enabled": True}, False, True),
            ("malformed shape", False, False),
        ):
            async with sessions() as session, session.begin():
                await session.execute(
                    text("UPDATE system_config SET value=:value WHERE config_key=:key"),
                    {"value": encode(value), "key": CONFIG_KEY},
                )
            assert await store.read() == {
                "recording_enabled": recording,
                "profile_completion_enabled": profile,
                "version": 3,
            }
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM idempotency_record WHERE scope=:scope AND actor_id=:actor"),
                {"scope": CONFIG_KEY, "actor": actor},
            )
            await session.execute(
                text(
                    "DELETE FROM audit_event WHERE actor_public_id=:actor "
                    "AND action='learning_features.update'"
                ),
                {"actor": actor},
            )
            await session.execute(
                text("DELETE FROM system_config WHERE config_key=:key"), {"key": CONFIG_KEY}
            )
            if previous:
                await session.execute(
                    text(
                        "INSERT INTO system_config(config_key,value,version,updated_by,updated_at) "
                        "VALUES(:key,:value,:version,:actor,:at)"
                    ),
                    {
                        "key": CONFIG_KEY,
                        "value": encode(decode(previous["value"])),
                        "version": previous["version"],
                        "actor": previous["updated_by"],
                        "at": previous["updated_at"],
                    },
                )
        await engine.dispose()
