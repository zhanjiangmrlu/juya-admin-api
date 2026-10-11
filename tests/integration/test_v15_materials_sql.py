import hashlib
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
import test_v15_import_flow as import_flow
from PIL import Image
from sqlalchemy import bindparam, text

from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_materials import LegacyMaterialService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

import_db = import_flow.import_db


@pytest.mark.asyncio
async def test_sql_material_zip_manual_stable_entry_binding_and_stale_cancel_guards(import_db):
    db = import_db
    production = db.service.production
    content = ContentService(SQLAlchemyContentRepository(db.sessions))
    source_ids = []
    entry_ids = []
    asset_id = new_ulid(datetime.now(UTC))
    image = BytesIO()
    Image.new("RGB", (256, 256), tuple(bytes.fromhex(uuid4().hex[:6]))).save(image, format="PNG")
    image_bytes = image.getvalue()
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr("images/Bank.png", image_bytes)
    calls = []
    invalidate = AsyncMock()
    materials = LegacyMaterialService(db.service, db.service.readings, invalidate)

    async def bind(data, kind, actor, role):
        assert data == image_bytes and kind == "images" and role == "WORD_IMAGE"
        calls.append(role)
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO media_asset(public_id,object_key,"
                    "asset_type,content_type,size_bytes,sha256,status,"
                    "security_status,created_by,width,height) VALUES(:id,"
                    ":key,'images','image/png',:size,:sha,'CONFIRMED',"
                    "'PASSED',:actor,256,256) ON DUPLICATE KEY UPDATE id=id"
                ),
                {
                    "id": asset_id,
                    "key": "sealed/media/" + asset_id + ".png",
                    "size": len(data),
                    "sha": hashlib.sha256(data).hexdigest(),
                    "actor": actor,
                },
            )
        return {"asset_id": asset_id}

    db.service.bind_media = bind
    try:
        for index in range(2):
            scene = await production.create_scene(
                db.series, "vocabulary", actor=db.actor, key=f"materials-scene-{index}"
            )
            async with db.sessions() as session:
                revision_id = await session.scalar(
                    text(
                        "SELECT r.public_id FROM scene s JOIN scene_revision r "
                        "ON r.id=s.draft_revision_id WHERE s.public_id=:id"
                    ),
                    {"id": scene},
                )
            assert revision_id
            revision = await content.get_revision(str(revision_id))
            revision = await content.save_revision(
                revision.id,
                {
                    "schema_version": 2,
                    "template_type": "word_card",
                    "title_en": "Bank",
                    "title_zh": "银行和河岸",
                    "vocabulary": [
                        {"english": "bank", "chinese": "河岸", "source_sentence": "by the river"},
                        {"english": "bank", "chinese": "银行", "source_sentence": "deposit money"},
                    ],
                },
                expected_version=revision.version,
                actor_id=db.actor,
            )
            source_ids.append(revision.id)
            entry_ids.extend(row["entry_id"] for row in revision.content["vocabulary"])
        first = await content.get_revision(source_ids[0])
        target_entry = first.content["vocabulary"][1]
        other_entry = first.content["vocabulary"][0]
        batch = await materials.create(
            db.series, "word_card", package.getvalue(), db.actor, "materials-create"
        )
        assert batch == await materials.create(
            db.series, "word_card", package.getvalue(), db.actor, "materials-create"
        )
        await materials.run(batch["stage_id"], "validate", datetime.now(UTC))
        preview = await materials.preview(batch["batch_id"])
        resource = preview["manifest"]["resources"][0]
        assert resource["ambiguous_title"] and len(resource["matching_candidates"]) == 2
        assert not calls
        with pytest.raises(AppError):
            await materials.queue_bind(
                batch["batch_id"],
                first.id,
                first.version,
                False,
                [
                    {
                        "path": "images/Bank.png",
                        "role": "WORD_IMAGE",
                        "entry_id": target_entry["entry_id"],
                    }
                ],
                db.actor,
                "unchecked",
            )
        with pytest.raises(AppError):
            await materials.queue_bind(
                batch["batch_id"],
                first.id,
                first.version,
                True,
                [{"path": "images/Bank.png", "role": "WORD_IMAGE", "entry_id": "missing"}],
                db.actor,
                "missing",
            )
        assert not calls
        selected = [
            {"path": "images/Bank.png", "role": "WORD_IMAGE", "entry_id": target_entry["entry_id"]}
        ]
        queued = await materials.queue_bind(
            batch["batch_id"], first.id, first.version, True, selected, db.actor, "bind-one"
        )
        assert queued == await materials.queue_bind(
            batch["batch_id"], first.id, first.version, True, selected, db.actor, "bind-one"
        )
        result = await materials.run(queued["stage_id"], "binder", datetime.now(UTC))
        assert result["status"] == "BOUND" and calls == ["WORD_IMAGE"]
        updated = await content.get_revision(first.id)
        selected_after = next(
            row
            for row in updated.content["vocabulary"]
            if row["entry_id"] == target_entry["entry_id"]
        )
        assert selected_after["icon_asset_id"] == asset_id
        assert (
            selected_after["sense_id"],
            selected_after["source_id"],
            selected_after["chinese"],
        ) == (target_entry["sense_id"], target_entry["source_id"], target_entry["chinese"])
        assert (
            next(
                row
                for row in updated.content["vocabulary"]
                if row["entry_id"] == other_entry["entry_id"]
            )["icon_asset_id"]
            is None
        )
        assert (await content.get_revision(source_ids[1])).content["vocabulary"][1][
            "icon_asset_id"
        ] is None
        invalidate.assert_awaited_once()
        with pytest.raises(AppError) as error:
            await materials.queue_bind(
                batch["batch_id"],
                first.id,
                first.version,
                True,
                selected,
                db.actor,
                "stale-version",
            )
        assert error.value.code == "REVISION_VERSION_CONFLICT"
        late = await materials.queue_bind(
            batch["batch_id"], first.id, updated.version, True, selected, db.actor, "late"
        )
        await materials.cancel(batch["batch_id"], db.actor)
        assert await materials.run(late["stage_id"], "cancelled", datetime.now(UTC)) is None
        assert calls == ["WORD_IMAGE"]
    finally:
        async with db.sessions() as session, session.begin():
            if entry_ids:
                for table in ["content_entry_revision", "content_source", "content_sense"]:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE entry_id IN :ids").bindparams(
                            bindparam("ids", expanding=True)
                        ),
                        {"ids": entry_ids},
                    )
                await session.execute(
                    text("DELETE FROM content_entry WHERE public_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": entry_ids},
                )
            await session.execute(
                text(
                    "DELETE FROM scene_media_reference WHERE "
                    "asset_id=(SELECT id FROM media_asset WHERE public_id=:id)"
                ),
                {"id": asset_id},
            )
            await session.execute(
                text("DELETE FROM media_asset WHERE public_id=:id"), {"id": asset_id}
            )
