"""Prepared automatic audio is not publication approval or per-sentence listening."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, dependency_hash
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore


def audio_fixture():
    # 返回同一草稿、正文、真实资源记录的一致模拟数据库事实,测试不生成试听确认
    source_sha, slice_sha = "a" * 64, "b" * 64
    digest = dependency_hash(
        sentence_id="sentence",
        english="Hello.",
        source_sha256=source_sha,
        source_audio_version="whole-version",
        boundary=Boundary(0, 900),
        encoding_config=FFmpegSlicer.encoding_config,
    )
    snapshot = dict(
        schema_version=2,
        template_type="dialogue",
        title_en="Hello",
        title_zh="你好",
        copyright="Owned",
        source="Fixture",
        audio=dict(
            target_id="whole", version_id="whole-version", asset_id="whole-asset", duration_ms=1000
        ),
        dialogue=[
            dict(
                id="sentence",
                speaker="A",
                english="Hello.",
                chinese="你好",
                start_ms=0,
                end_ms=900,
                audio_version_id="whole-version",
                timing_confirmed=True,
                boundary_verified_by="system:asr-alignment",
                slice_target_id="slice",
                slice_version="slice-version",
                slice_dependency_hash=digest,
            )
        ],
    )
    assets = {
        "whole-asset": dict(
            public_id="whole-asset",
            status="CONFIRMED",
            security_status="SKIPPED",
            asset_type="audio",
            duration_ms=1000,
            width=None,
            height=None,
            object_key="sealed/media/audio/whole",
            sha256=source_sha,
            size_bytes=32044,
        ),
        "slice-asset": dict(
            public_id="slice-asset",
            status="CONFIRMED",
            security_status="SKIPPED",
            asset_type="audio",
            duration_ms=900,
            width=None,
            height=None,
            object_key="sealed/media/audio/slice",
            sha256=slice_sha,
            size_bytes=43244,
        ),
    }
    versions = {
        "whole-version": dict(
            public_id="whole-version",
            target_id="whole",
            target_type="WHOLE",
            asset_id="whole-asset",
            status="READY",
        ),
        "slice-version": dict(
            public_id="slice-version",
            target_id="slice",
            target_type="sentence",
            asset_id="slice-asset",
            status="READY",
        ),
    }
    sliced = dict(
        public_id="slice-row",
        content_type="dialogue",
        content_id="content",
        revision_id="revision",
        sentence_id="sentence",
        source_audio_version="whole-version",
        source_sha256=source_sha,
        start_ms=0,
        end_ms=900,
        target_id="slice",
        resource_version="slice-version",
        asset_id="slice-asset",
        dependency_hash=digest,
        status="READY",
        actual_duration_ms=900,
        slice_sha256=slice_sha,
        byte_size=43244,
        encoding_config=FFmpegSlicer.encoding_config,
        confirmed_by=None,
        confirmed_at=None,
    )
    return snapshot, assets, versions, sliced


def fake_session(assets, versions, sliced):
    # assets/versions/sliced 为手工校验一致性的素材、版本和切片数据库行
    async def execute(statement, params=None):
        sql, params = str(statement), params or {}
        if "sentence_audio_slice" in sql:
            identity = params.get("id") or params.get("version")
            row = sliced if identity in (None, "slice-version") else None
        elif "audio_version" in sql:
            row = versions.get(params.get("id"))
        elif "media_asset" in sql:
            row = assets.get(params.get("id"))
        else:
            row = None
        value = SimpleNamespace(_mapping=row, **row) if row else None
        return SimpleNamespace(
            first=lambda: value,
            mappings=lambda: SimpleNamespace(first=lambda: row, all=lambda: [row] if row else []),
            all=lambda: [value] if value else [],
        )

    return SimpleNamespace(execute=execute, scalar=AsyncMock(return_value="content"))


@pytest.mark.asyncio
async def test_ready_source_and_real_ready_slices_are_prepared_without_listening():
    snapshot, assets, versions, sliced = audio_fixture()
    before = deepcopy(snapshot)
    facts = await ProofreadingStore(Mock(), require_review=False).default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert facts["ready"] is True and facts["normal"] is True
    assert facts["has_audio"] is True
    assert facts["preparation_hash"]
    assert snapshot == before
    assert versions["whole-version"]["status"] == versions["slice-version"]["status"] == "READY"
    assert sliced["status"] == "READY" and sliced["confirmed_by"] is None


@pytest.mark.asyncio
async def test_source_without_positive_actual_file_size_is_not_prepared():
    snapshot, assets, versions, sliced = audio_fixture()
    assets["whole-asset"]["size_bytes"] = -1
    facts = await ProofreadingStore(Mock(), require_review=False).default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert facts["ready"] is False and facts["normal"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("revision_id", "other"),
        ("content_id", "other"),
        ("target_id", "other"),
        ("asset_id", "other"),
        ("source_audio_version", "other"),
        ("source_sha256", "changed"),
        ("dependency_hash", "changed"),
        ("start_ms", 1),
        ("slice_sha256", "changed"),
        ("actual_duration_ms", 899),
        ("byte_size", 1),
        ("status", "FAILED"),
    ],
)
async def test_ready_status_never_exempts_slice_identity_or_real_file_facts(field, invalid):
    snapshot, assets, versions, sliced = audio_fixture()
    sliced[field] = invalid
    facts = await ProofreadingStore(Mock(), require_review=False).default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert facts["ready"] is False and facts["normal"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "security"), [(True, "SKIPPED"), (False, "BLOCKED"), (False, "PENDING")]
)
async def test_prepared_audio_retains_actual_environment_security_policy(policy, security):
    snapshot, assets, versions, sliced = audio_fixture()
    assets["slice-asset"]["security_status"] = security
    facts = await ProofreadingStore(Mock(), require_review=policy).default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert not facts["ready"] and not facts["normal"]


@pytest.mark.asyncio
async def test_preparation_hash_stays_stable_only_across_approval_status_change():
    snapshot, assets, versions, sliced = audio_fixture()
    store = ProofreadingStore(Mock(), require_review=False)
    first = await store.default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert first["ready"] is True
    for version in versions.values():
        version["status"] = "CONFIRMED"
    sliced.update(status="CONFIRMED", confirmed_by="reviewer", confirmed_at="now")
    approved = await store.default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert first["preparation_hash"] == approved["preparation_hash"]
    assets["slice-asset"]["object_key"] = "sealed/media/audio/replaced"
    changed = await store.default_facts(
        fake_session(assets, versions, sliced), "dialogue", "revision", snapshot
    )
    assert first["preparation_hash"] != changed["preparation_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_whole", [False, True])
async def test_word_card_source_sentence_does_not_require_dialogue_slices(monkeypatch, with_whole):
    # 功能:词卡来源句不强制对话切片;with_whole 参数覆盖无整音频及可选整音频。
    from juya_admin_api.modules.content.domain import PublishCheck
    from juya_admin_api.modules.content.production_store import ProductionStore

    snapshot, assets, versions, sliced = audio_fixture()
    snapshot["template_type"] = "word_card"
    snapshot["dialogue"] = [dict(id="sentence", english="Hello.", chinese="你好")]
    snapshot["vocabulary"] = [
        dict(entry_id="entry", english="Hello", chinese="你好", source_sentence_ids=["sentence"])
    ]
    if not with_whole:
        snapshot["audio"] = None
    monkeypatch.setattr(
        ProductionStore,
        "entry_references",
        AsyncMock(return_value=PublishCheck("ENTRY_REFERENCE_INVALID", "ERROR", True)),
    )
    facts = await ProofreadingStore(Mock(), require_review=False).default_facts(
        fake_session(assets, versions, sliced), "word_card", "revision", snapshot
    )
    assert facts["ready"] and facts["normal"], facts["error_codes"]
    assert facts["has_audio"] is with_whole
    assert facts["slice_ids"] == []
    assert facts["audio_version_ids"] == (["whole-version"] if with_whole else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "security", "duration"),
    [
        (True, "SKIPPED", 1000),
        (False, "BLOCKED", 1000),
        (False, "PENDING", 1000),
        (False, "SKIPPED", 900),
    ],
)
async def test_word_card_optional_whole_retains_security_and_actual_duration_checks(
    monkeypatch, policy, security, duration
):
    # 功能:词卡可选整音频仍检查实际状态和时长;policy/security/duration 控制异常事实。
    from juya_admin_api.modules.content.domain import PublishCheck
    from juya_admin_api.modules.content.production_store import ProductionStore

    snapshot, assets, versions, sliced = audio_fixture()
    snapshot["template_type"] = "word_card"
    snapshot["dialogue"] = [dict(id="sentence", english="Hello.", chinese="你好")]
    snapshot["vocabulary"] = [
        dict(entry_id="entry", english="Hello", chinese="你好", source_sentence_ids=["sentence"])
    ]
    assets["whole-asset"].update(security_status=security, duration_ms=duration)
    monkeypatch.setattr(
        ProductionStore,
        "entry_references",
        AsyncMock(return_value=PublishCheck("ENTRY_REFERENCE_INVALID", "ERROR", True)),
    )
    facts = await ProofreadingStore(Mock(), require_review=policy).default_facts(
        fake_session(assets, versions, sliced), "word_card", "revision", snapshot
    )
    assert not facts["ready"] and not facts["normal"]
    assert "AUDIO_MISSING" in facts["error_codes"]
