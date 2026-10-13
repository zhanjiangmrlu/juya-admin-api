"""Preparation facts for hash-bound scene audio review; never a publication exemption."""

import hashlib
import json
from copy import deepcopy
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.audio_pipeline.slicing import FFmpegSlicer
from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.schemas import SceneContent


async def scene_preparation_facts(
    sessions: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    content_type: str,
    revision_id: str,
    snapshot: dict[str, Any],
    *,
    require_review: bool,
) -> dict[str, Any]:
    # 功能:锁定实际资源并检查可供人工抽样的场景准备事实,不产生试听或发布确认
    # 参数:sessions 为会话工厂,session 为当前事务,content_type/revision_id 固定草稿归属
    # snapshot 为冻结内容,require_review 为当前安全审核策略
    facts: dict[str, Any] = {
        "ready": False,
        "normal": False,
        "has_audio": False,
        "has_images": False,
    }
    try:
        content = SceneContent.model_validate(snapshot)
    except ValidationError:
        return facts | {"error_codes": ["SCENE_CONTENT_INVALID"]}
    production = ProductionStore(sessions, require_review=require_review)
    assets, versions = await production.facts(session, content, lock=True)
    content_id = await session.scalar(
        text(
            "SELECT c.public_id FROM scene_revision r JOIN scene c ON c.id=r.scene_id "
            "WHERE r.public_id=:revision FOR UPDATE"
        ),
        {"revision": revision_id},
    )
    errors: list[str] = []
    if not content_id or content_type != (
        "dialogue" if content.template_type == "dialogue" else "word_card"
    ):
        errors.append("SCENE_IDENTITY_INVALID")
    # ProductionStore 提供发布所需完整素材事实,额外锁定实际文件大小,不能只信 READY 标签
    for asset_id, asset in assets.items():
        size = (
            (
                await session.execute(
                    text("SELECT size_bytes FROM media_asset WHERE public_id=:id FOR UPDATE"),
                    {"id": asset_id},
                )
            )
            .mappings()
            .first()
        )
        asset["size_bytes"] = size.get("size_bytes") if size else None
        actual_size = asset.get("size_bytes")
        if (
            not asset.get("sha256")
            or len(str(asset["sha256"])) != 64
            or not isinstance(actual_size, int)
            or actual_size <= 0
        ):
            errors.append("ASSET_FILE_FACTS_INVALID")
    slices: list[dict[str, Any]] = []
    # 词卡中的来源句不是待切片对话;可选整音频仍交给完整内容/素材事实检查
    for sentence in content.dialogue if content.template_type == "dialogue" else []:
        if not content.audio or not sentence.slice_version:
            errors.append("SENTENCE_SLICE_INVALID")
            continue
        saved = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM sentence_audio_slice WHERE resource_version=:id FOR UPDATE"
                    ),
                    {"id": sentence.slice_version},
                )
            )
            .mappings()
            .all()
        )
        if len(saved) != 1:
            errors.append("SENTENCE_SLICE_INVALID")
            continue
        sliced = dict(saved[0])
        version = versions.get(sentence.slice_version, {})
        asset = assets.get(str(version.get("asset_id", "")), {})
        source = assets.get(content.audio.asset_id, {})
        encoding = sliced.get("encoding_config")
        if isinstance(encoding, str):
            encoding = json.loads(encoding)
        expected = {
            "content_type": "dialogue",
            "content_id": content_id,
            "revision_id": revision_id,
            "sentence_id": sentence.id,
            "source_audio_version": content.audio.version_id,
            "source_sha256": source.get("sha256"),
            "start_ms": sentence.start_ms,
            "end_ms": sentence.end_ms,
            "target_id": sentence.slice_target_id,
            "resource_version": sentence.slice_version,
            "asset_id": version.get("asset_id"),
            "dependency_hash": sentence.slice_dependency_hash,
            "actual_duration_ms": asset.get("duration_ms"),
            "slice_sha256": asset.get("sha256"),
            "byte_size": asset.get("size_bytes"),
        }
        duration = sliced.get("actual_duration_ms")
        if (
            any(sliced.get(name) != value for name, value in expected.items())
            or sliced.get("status") not in {"READY", "CONFIRMED"}
            or version.get("target_type") not in {"sentence", "SENTENCE"}
            or not isinstance(duration, int)
            or duration <= 0
            or sentence.start_ms is None
            or sentence.end_ms is None
            or abs(duration - (sentence.end_ms - sentence.start_ms)) > 30
            or encoding != FFmpegSlicer.encoding_config
            or (
                sliced.get("status") == "CONFIRMED"
                and not (sliced.get("confirmed_by") and sliced.get("confirmed_at"))
            )
        ):
            errors.append("SENTENCE_SLICE_FACTS_INVALID")
        slices.append(sliced)
    projected = deepcopy(versions)
    for identity, version in projected.items():
        if version.get("status") == "READY":
            version["status"] = "CONFIRMED"
        if any(sliced["resource_version"] == identity for sliced in slices):
            version["slice_confirmed"] = True
    # 仅投影准备态以复用全部发布映射/时间轴规则;实际版本、切片、正文均保持原状态
    errors.extend(
        item.code
        for item in check_content(content, assets, projected, require_review=require_review)
        if not item.passed
    )
    entry_check = await production.entry_references(session, content, revision_id)
    if not entry_check.passed:
        errors.append(entry_check.code)
    evidence = {
        "require_review": require_review,
        "content_id": content_id,
        "revision_id": revision_id,
        "assets": assets,
        "versions": {
            key: {
                name: row.get(name)
                for name in (
                    "public_id",
                    "target_id",
                    "target_type",
                    "asset_id",
                    "dependency_hash",
                    "source_audio_version",
                    "sentence_id",
                    "start_ms",
                    "end_ms",
                    "source_sha256",
                )
            }
            for key, row in versions.items()
        },
        "slices": [
            {
                name: value
                for name, value in row.items()
                if name not in {"id", "status", "confirmed_by", "confirmed_at", "created_at"}
            }
            for row in slices
        ],
    }
    facts.update(
        ready=not errors,
        normal=not errors,
        has_audio=bool(versions),
        has_images=any(asset.get("asset_type") == "images" for asset in assets.values()),
        asset_count=len(assets),
        audio_version_count=len(versions),
        audio_version_ids=sorted(versions),
        slice_ids=sorted(row["public_id"] for row in slices),
        preparation_hash=hashlib.sha256(
            json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        error_codes=sorted(set(errors)),
    )
    return facts
