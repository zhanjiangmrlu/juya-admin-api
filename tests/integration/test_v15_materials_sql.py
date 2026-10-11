import hashlib
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZipFile

import pytest
import test_v15_import_flow as import_flow
from openpyxl import Workbook
from PIL import Image
from sqlalchemy import bindparam, text

from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_materials import LegacyMaterialService
from juya_admin_api.modules.content_import.preview import ImportPreviewStore
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
                    "security_status,created_by,created_at,width,height) VALUES(:id,"
                    ":key,'images','image/png',:size,:sha,'CONFIRMED',"
                    "'PASSED',:actor,:now,256,256) ON DUPLICATE KEY UPDATE id=id"
                ),
                {
                    "id": asset_id,
                    "key": "sealed/media/" + asset_id + ".png",
                    "size": len(data),
                    "sha": hashlib.sha256(data).hexdigest(),
                    "actor": actor,
                    "now": datetime.now(UTC).replace(tzinfo=None),
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


@pytest.mark.asyncio
async def test_sql_formal_word_table_atomic_create_skip_update_and_source_identity(import_db):
    db = import_db
    content = ContentService(SQLAlchemyContentRepository(db.sessions))
    materials = LegacyMaterialService(db.service, db.service.readings, AsyncMock())
    created_entries = []
    source_contents = []
    image = BytesIO()
    Image.new("RGB", (256, 256), tuple(bytes.fromhex(uuid4().hex[:6]))).save(image, format="PNG")
    image_bytes = image.getvalue()

    def package(meaning="银行", explanation="", image=False):
        book = Workbook()
        book.active.append(
            [
                "import_entry_id",
                "entry_type",
                "english",
                "meaning_zh",
                "source_sentence",
                "explanation",
                "meaning_change",
                "image_path",
            ]
        )
        book.active.append(
            [
                "bank-1",
                "word",
                "bank",
                meaning,
                "deposit money",
                explanation,
                "wording" if explanation else "",
                "images/bank.png" if image else "",
            ]
        )
        stream = BytesIO()
        book.save(stream)
        result = BytesIO()
        with ZipFile(result, "w") as archive:
            archive.writestr("词汇表.xlsx", stream.getvalue())
            if image:
                archive.writestr("images/bank.png", image_bytes)
        return result.getvalue()

    calls = []
    asset_id = new_ulid(datetime.now(UTC))

    async def bind(data, kind, actor, role):
        calls.append(role)
        assert data == image_bytes and kind == "images" and role == "WORD_IMAGE"
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO media_asset(public_id,object_key,asset_type,content_type,"
                    "size_bytes,sha256,status,security_status,created_by,created_at,width,height) "
                    "VALUES(:id,:key,'images','image/png',:size,:sha,'CONFIRMED','PASSED',"
                    ":actor,UTC_TIMESTAMP(6),256,256) ON DUPLICATE KEY UPDATE id=id"
                ),
                {
                    "id": asset_id,
                    "key": "sealed/materials/" + asset_id,
                    "size": len(data),
                    "sha": hashlib.sha256(data).hexdigest(),
                    "actor": actor,
                },
            )
        return {"asset_id": asset_id}

    db.service.bind_media = bind

    async def stage(revision, data, key):
        batch = await materials.create(db.series, "word_card", data, db.actor, key)
        await materials.run(batch["stage_id"], key, datetime.now(UTC))
        preview = await materials.vocabulary_preview(
            batch["batch_id"], revision.id, revision.version
        )
        return batch, preview

    async def apply(batch, preview, revision, key, **selection):
        queued = await materials.queue_bind(
            batch["batch_id"],
            revision.id,
            revision.version,
            True,
            [],
            db.actor,
            key,
            [{"import_entry_id": "bank-1", **selection}],
            preview["preview_hash"],
        )
        return await materials.run(queued["stage_id"], key, datetime.now(UTC))

    try:
        revisions = []
        for index in range(2):
            scene = await db.service.production.create_scene(
                db.series, "vocabulary", actor=db.actor, key=f"word-table-scene-{index}"
            )
            source_contents.append(scene)
            async with db.sessions() as session:
                identifier = await session.scalar(
                    text(
                        "SELECT r.public_id FROM scene s JOIN scene_revision r "
                        "ON r.id=s.draft_revision_id WHERE s.public_id=:id"
                    ),
                    {"id": scene},
                )
            revision = await content.get_revision(identifier)
            revisions.append(
                await content.save_revision(
                    revision.id,
                    {"schema_version": 2, "template_type": "word_card"},
                    expected_version=revision.version,
                    actor_id=db.actor,
                )
            )
        first = revisions[0]
        batch, preview = await stage(first, package(), "table-create")
        assert preview["vocabulary_rows"][0]["operation"] == "CREATE"
        with pytest.raises(AppError) as error:
            await apply(batch, preview, first, "no-image-unchecked")
        assert error.value.code == "MATERIAL_NO_IMAGE_CONFIRMATION_REQUIRED"
        wrong = {**preview, "preview_hash": "0" * 64}
        with pytest.raises(AppError) as error:
            await apply(batch, wrong, first, "stale-preview", no_image_confirmed=True)
        assert error.value.code == "MATERIAL_VOCABULARY_PREVIEW_STALE"
        result = await apply(batch, preview, first, "table-apply", no_image_confirmed=True)
        assert result["vocabulary_results"][0]["status"] == "CREATED"
        first = await content.get_revision(first.id)
        original = first.content["vocabulary"][0]
        created_entries.append(original["entry_id"])
        assert not original["confirmed"] and original["icon_asset_id"] is None
        skipped_preview = await materials.vocabulary_preview(
            batch["batch_id"], first.id, first.version
        )
        assert skipped_preview["vocabulary_rows"][0]["operation"] == "SKIP"
        result = await apply(batch, skipped_preview, first, "table-skip", no_image_confirmed=True)
        assert result["edit_version"] == first.version
        assert result["vocabulary_results"][0]["status"] == "SKIPPED"
        assert materials.invalidate.await_count == 1
        conflict, conflict_preview = await stage(first, package("河岸"), "table-new-sense")
        assert any(
            issue["code"] == "IMPORT_ENTRY_IDENTITY_CHANGE_REQUIRED"
            for issue in conflict_preview["vocabulary_rows"][0]["issues"]
        )
        with pytest.raises(AppError) as error:
            await apply(
                conflict, conflict_preview, first, "forbid-new-sense", no_image_confirmed=True
            )
        assert error.value.code == "MATERIAL_VOCABULARY_ROW_INVALID"
        update, update_preview = await stage(
            first, package(explanation="存放资金的机构"), "table-update"
        )
        assert update_preview["vocabulary_rows"][0]["operation"] == "UPDATE"
        with pytest.raises(AppError) as error:
            await apply(update, update_preview, first, "unchecked-diff", no_image_confirmed=True)
        assert error.value.code == "MATERIAL_DIFFERENCE_CONFIRMATION_REQUIRED"
        await apply(
            update,
            update_preview,
            first,
            "checked-diff",
            no_image_confirmed=True,
            difference_confirmed=True,
        )
        first = await content.get_revision(first.id)
        changed = first.content["vocabulary"][0]
        assert tuple(changed[key] for key in ("entry_id", "sense_id", "source_id")) == tuple(
            original[key] for key in ("entry_id", "sense_id", "source_id")
        )
        assert changed["entry_version"] == original["entry_version"] + 1
        other, other_preview = await stage(revisions[1], package(), "table-other-source")
        assert other_preview["vocabulary_rows"][0]["operation"] == "CREATE"
        await apply(
            other, other_preview, revisions[1], "table-other-apply", no_image_confirmed=True
        )
        second = await content.get_revision(revisions[1].id)
        created_entries.append(second.content["vocabulary"][0]["entry_id"])
        assert len(set(created_entries)) == 2
        graphic, graphic_preview = await stage(
            first, package(explanation="存放资金的机构", image=True), "table-graphic"
        )
        await apply(
            graphic, graphic_preview, first, "table-graphic-apply", difference_confirmed=True
        )
        first = await content.get_revision(first.id)
        assert first.content["vocabulary"][0]["icon_asset_id"] == asset_id
        assert first.content["vocabulary"][0]["entry_id"] == original["entry_id"]
        repeat = await materials.vocabulary_preview(graphic["batch_id"], first.id, first.version)
        assert repeat["vocabulary_rows"][0]["operation"] == "SKIP"
        assert calls == ["WORD_IMAGE"]
        previews = ImportPreviewStore(db.service, b"isolated-local-test-secret-32-bytes", "ffprobe")
        signed = await previews.issue(graphic["batch_id"], "images/bank.png", db.actor)
        assert signed["content_type"] == "image/png"
        token = parse_qs(urlsplit(signed["url"]).query)["token"][0]
        data, mime = await previews.content(graphic["batch_id"], token, db.actor)
        assert data == image_bytes and mime == "image/png"
        with pytest.raises(AppError):
            await previews.content(graphic["batch_id"], token, "another-admin")
    finally:
        async with db.sessions() as session, session.begin():
            if source_contents:
                await session.execute(
                    text("DELETE FROM import_entry_mapping WHERE content_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": source_contents},
                )
            if created_entries:
                for table in ["content_entry_revision", "content_source", "content_sense"]:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE entry_id IN :ids").bindparams(
                            bindparam("ids", expanding=True)
                        ),
                        {"ids": created_entries},
                    )
                await session.execute(
                    text("DELETE FROM content_entry WHERE public_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": created_entries},
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
