"""Actual MySQL authorization and fixed resources; only the external OSS is fake."""

import hashlib
import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from juya_admin_api.integrations.oss.provider import ObjectMetadata, OssProvider
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.pronunciation.domain import Target
from juya_admin_api.modules.pronunciation.models import targets
from juya_admin_api.modules.short_reading.schemas import ReadingContent
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.resources import SQLResourceSigner
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class FixedOSS:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.objects: dict[str, ObjectMetadata] = {}
        self.elapsed = 0
        self.changed = False
        self.ttls: list[int] = []

    async def head_object(self, key: str) -> ObjectMetadata:
        self.now += timedelta(seconds=self.elapsed)
        metadata = self.objects[key]
        return replace(metadata, sha256="0" * 64) if self.changed else metadata

    async def sign_get_url(self, key: str, expires: int) -> str:
        self.ttls.append(expires)
        expiry = int((self.now + timedelta(seconds=expires)).timestamp())
        return f"https://fixture.invalid/{key}?Expires={expiry}&Signature=fixture"


@pytest.mark.asyncio
async def test_sql_fixed_current_and_favorite_resources_respect_live_and_saved_authority() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required; no shared database used")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    now = datetime.now(UTC).replace(microsecond=0)
    ids = {
        name: new_ulid(now)
        for name in (
            "user",
            "other",
            "series",
            "scene",
            "revision",
            "reading",
            "reading_revision",
            "package",
            "grant",
            "favorite",
            "snapshot",
            "entry",
            "sense",
            "source",
            "image",
            "original",
            "audio",
            "whole",
            "whole_version",
            "word",
            "word_version",
            "pron",
        )
    }
    oss = FixedOSS(now)
    signer = SQLResourceSigner(cast(OssProvider, oss), clock=lambda: oss.now)
    # Every fixture, including module toggles, is rolled back. No cleanup affects other IDs.
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:

                async def sql(statement: str, **params: Any) -> Any:
                    return await session.execute(text(statement), params)

                try:
                    for user in ("user", "other"):
                        await sql(
                            "INSERT INTO user_account(public_id,juya_number,status) "
                            "VALUES(:id,:number,'ACTIVE')",
                            id=ids[user],
                            number="T" + ids[user][-19:],
                        )
                    await sql(
                        "UPDATE learning_module_config SET enabled=1 WHERE module_type IN "
                        "('scene_learning','short_reading')"
                    )
                    await sql(
                        "INSERT INTO content_series(public_id,title,slug,status) "
                        "VALUES(:id,'Fixed SQL',:slug,'PUBLISHED')",
                        id=ids["series"],
                        slug=ids["series"],
                    )
                    for name, kind, mime in (
                        ("image", "images", "image/png"),
                        ("original", "images", "image/png"),
                        ("audio", "audio", "audio/mpeg"),
                    ):
                        key = f"sealed/media/{kind}/{ids[name]}"
                        data = (name + ids[name]).encode()
                        digest = hashlib.sha256(data).hexdigest()
                        oss.objects[key] = ObjectMetadata(key, len(data), mime, digest, {})
                        await sql(
                            "INSERT INTO "
                            "media_asset(public_id,object_key,asset_type,content_type,size_bytes,"
                            "sha256,status,security_status,created_by,created_at,duration_ms,widt"
                            "h,height) "
                            "VALUES(:id,:key,:kind,:mime,:size,:sha,'CONFIRMED','PASSED','fixture"
                            "',:now,:duration,20,20)",
                            id=ids[name],
                            key=key,
                            kind=kind,
                            mime=mime,
                            size=len(data),
                            sha=digest,
                            now=now,
                            duration=1000 if kind == "audio" else None,
                        )
                    for target, version, kind in (
                        ("whole", "whole_version", "FULL"),
                        ("word", "word_version", "WORD"),
                    ):
                        await sql(
                            "INSERT INTO audio_target(public_id,stable_key,target_type) "
                            "VALUES(:id,:key,:kind)",
                            id=ids[target],
                            key=ids[target],
                            kind=kind,
                        )
                        await sql(
                            "INSERT INTO "
                            "audio_version(public_id,target_id,asset_id,version_no,source,status,"
                            "created_at) SELECT :id,t.id,m.id,1,'MANUAL','ACTIVE',:now FROM "
                            "audio_target t,media_asset m WHERE t.public_id=:target AND "
                            "m.public_id=:asset",
                            id=ids[version],
                            now=now,
                            target=ids[target],
                            asset=ids["audio"],
                        )
                    await session.execute(
                        insert(targets).values(
                            **asdict(
                                Target(
                                    ids["pron"],
                                    "medium",
                                    uploaded_target_id=ids["word"],
                                    uploaded_resource_version=ids["word_version"],
                                )
                            ),
                            confirmed_by="fixture",
                            confirmed_at=1,
                            created_at=1,
                        )
                    )
                    await sql(
                        "INSERT IGNORE INTO content_template(template_type,version,"
                        "required_modules,validation_rules) VALUES('vocabulary',1,"
                        "JSON_ARRAY('vocabulary'),JSON_OBJECT())"
                    )
                    content = SceneContent.model_validate(
                        dict(
                            schema_version=2,
                            template_type="word_card",
                            title_en="Fixed",
                            title_zh="固定",
                            original_image_asset_id=ids["original"],
                            vocabulary=[
                                dict(
                                    entry_id=ids["entry"],
                                    sense_id=ids["sense"],
                                    source_id=ids["source"],
                                    english="medium",
                                    chinese="中杯",
                                    confirmed=True,
                                    icon_asset_id=ids["image"],
                                    pronunciation_target_id=ids["pron"],
                                )
                            ],
                        )
                    ).model_dump(mode="json")
                    await sql(
                        "INSERT INTO scene(public_id,series_id,template_id,title,status) "
                        "SELECT :id,s.id,t.id,'Fixed','PUBLISHED' FROM content_series "
                        "s,content_template t WHERE s.public_id=:series AND "
                        "t.template_type='vocabulary' LIMIT 1",
                        id=ids["scene"],
                        series=ids["series"],
                    )
                    await sql(
                        "INSERT INTO "
                        "scene_revision(public_id,scene_id,version_no,edit_version,status,con"
                        "tent_snapshot,created_by,created_at) SELECT "
                        ":id,id,1,1,'PUBLISHED',:content,'fixture',:now FROM scene WHERE "
                        "public_id=:scene",
                        id=ids["revision"],
                        content=json.dumps(content),
                        now=now,
                        scene=ids["scene"],
                    )
                    await sql(
                        "UPDATE scene s JOIN scene_revision r ON r.scene_id=s.id SET "
                        "s.published_revision_id=r.id WHERE s.public_id=:id",
                        id=ids["scene"],
                    )
                    reading = ReadingContent.model_validate(
                        dict(
                            title_en="Reading",
                            title_zh="阅读",
                            category="nature",
                            topic="water",
                            level="A1",
                            safe_intro="Safe intro",
                            paragraphs_en=["Secret paragraph"],
                            translation_zh="私有翻译",
                            copyright_status="CONFIRMED",
                            copyright_source="Fixture",
                            audio=dict(
                                asset_id=ids["audio"],
                                target_id=ids["whole"],
                                version_id=ids["whole_version"],
                                duration_ms=1000,
                            ),
                        )
                    ).model_dump(mode="json")
                    await sql(
                        "INSERT INTO "
                        "short_reading(public_id,series_id,status,published_revision_id,versi"
                        "on,created_at,updated_at) "
                        "VALUES(:id,:series,'PUBLISHED',:revision,1,:now,:now)",
                        id=ids["reading"],
                        series=ids["series"],
                        revision=ids["reading_revision"],
                        now=now,
                    )
                    await sql(
                        "INSERT INTO "
                        "short_reading_revision(public_id,short_reading_id,version_no,edit_ve"
                        "rsion,status,content_snapshot,content_hash,created_by,created_at) "
                        "VALUES(:id,:reading,1,1,'PUBLISHED',:content,:hash,'fixture',:now)",
                        id=ids["reading_revision"],
                        reading=ids["reading"],
                        content=json.dumps(reading),
                        hash=snapshot_hash(reading),
                        now=now,
                    )
                    await sql(
                        "INSERT INTO "
                        "short_reading_trial(short_reading_id,enabled,version,updated_by,upda"
                        "ted_at) VALUES(:id,1,1,'fixture',:now)",
                        id=ids["reading"],
                        now=now,
                    )
                    await sql(
                        "INSERT INTO content_package(public_id,name,status) "
                        "VALUES(:id,'Fixed grant','ACTIVE')",
                        id=ids["package"],
                    )
                    await sql(
                        "INSERT INTO "
                        "content_package_item(package_id,content_type,content_id,position) "
                        "SELECT id,'word_card',:scene,1 FROM content_package WHERE "
                        "public_id=:package",
                        scene=ids["scene"],
                        package=ids["package"],
                    )
                    await sql(
                        "INSERT INTO "
                        "formal_entitlement(public_id,user_id,package_id,term,status,granted_"
                        "at,expires_at,updated_at) SELECT "
                        ":id,u.id,p.id,'permanent','ACTIVE',:now,NULL,:now FROM user_account "
                        "u,content_package p WHERE u.public_id=:user AND p.public_id=:package",
                        id=ids["grant"],
                        user=ids["user"],
                        package=ids["package"],
                        now=now,
                    )
                    await sql(
                        "INSERT INTO "
                        "favorite_snapshot(public_id,user_id,entry_type,entry_id,sense_id,sou"
                        "rce_id,active,current_snapshot_revision_id,favorited_at) SELECT "
                        ":id,id,'word',:entry,:sense,:source,1,:snapshot,:now FROM "
                        "user_account WHERE public_id=:user",
                        id=ids["favorite"],
                        entry=ids["entry"],
                        sense=ids["sense"],
                        source=ids["source"],
                        snapshot=ids["snapshot"],
                        now=now,
                        user=ids["user"],
                    )
                    await sql(
                        "INSERT INTO "
                        "favorite_snapshot_revision(public_id,favorite_id,snapshot_version,co"
                        "ntent_type,content_id,saved_content_version,revision_id,english,mean"
                        "ing_zh,explanation,source_sentence,image_asset_id,pronunciation_targ"
                        "et_id,resource_version,created_at) SELECT "
                        ":id,id,1,'word_card',:scene,1,:revision,'medium','中杯','','A medium "
                        "coffee.',:image,:pron,:version,:now FROM favorite_snapshot WHERE "
                        "public_id=:favorite",
                        id=ids["snapshot"],
                        scene=ids["scene"],
                        revision=ids["revision"],
                        image=ids["image"],
                        pron=ids["pron"],
                        version=ids["word_version"],
                        now=now,
                        favorite=ids["favorite"],
                    )

                    image = ResourceRequest(
                        ids["user"],
                        "word_card",
                        ids["scene"],
                        ids["revision"],
                        ids["image"],
                        None,
                        "WORD_IMAGE",
                    )
                    word = replace(
                        image,
                        target_id=ids["pron"],
                        resource_version=ids["word_version"],
                        role="WORD",
                    )
                    whole = ResourceRequest(
                        ids["user"],
                        "short_reading",
                        ids["reading"],
                        ids["reading_revision"],
                        ids["whole"],
                        ids["whole_version"],
                        "WHOLE",
                    )

                    async def signed(request: ResourceRequest) -> dict[str, Any]:
                        return await signer(session, request, oss.now + timedelta(seconds=60))

                    assert (await signed(image))["asset_id"] == ids["image"]
                    assert (await signed(word))["asset_id"] == ids["audio"]
                    assert (await signed(whole))["asset_id"] == ids["audio"]
                    # Trial serves an active user without a formal grant.
                    assert (await signed(replace(whole, user_id=ids["other"])))["asset_id"] == ids[
                        "audio"
                    ]
                    await sql(
                        "UPDATE formal_entitlement SET expires_at=:expiry WHERE public_id=:id",
                        expiry=oss.now + timedelta(seconds=23),
                        id=ids["grant"],
                    )
                    await signed(image)
                    assert oss.ttls[-1] == 23
                    await sql(
                        "UPDATE formal_entitlement SET expires_at=NULL WHERE public_id=:id",
                        id=ids["grant"],
                    )
                    oss.elapsed = 11
                    await signed(image)
                    assert oss.ttls[-1] == 49
                    oss.elapsed = 0
                    oss.changed = True
                    with pytest.raises(AppError) as changed:
                        await signed(image)
                    assert changed.value.code == "MEDIA_ASSET_CHANGED"
                    oss.changed = False
                    with pytest.raises(AppError):
                        await signed(replace(word, resource_version=ids["whole_version"]))
                    await sql(
                        "UPDATE short_reading_trial SET enabled=0 WHERE short_reading_id=:id",
                        id=ids["reading"],
                    )
                    with pytest.raises(AppError):
                        await signed(whole)
                    await sql(
                        "UPDATE formal_entitlement SET status='REVOKED' WHERE public_id=:id",
                        id=ids["grant"],
                    )
                    await sql(
                        "UPDATE scene SET status='OFFLINE' WHERE public_id=:id", id=ids["scene"]
                    )
                    await sql(
                        "UPDATE learning_module_config SET enabled=0 WHERE "
                        "module_type='scene_learning'"
                    )
                    with pytest.raises(AppError):
                        await signed(image)
                    saved_image = replace(image, favorite_id=ids["favorite"])
                    saved_word = replace(word, favorite_id=ids["favorite"])
                    assert (await signed(saved_image))["asset_id"] == ids["image"]
                    assert (await signed(saved_word))["asset_id"] == ids["audio"]
                    for role in ("WHOLE", "SENTENCE", "LEARNING_IMAGE"):
                        with pytest.raises(AppError):
                            await signed(replace(saved_image, role=role, target_id=ids["original"]))
                    with pytest.raises(AppError):
                        await signed(replace(saved_image, user_id=ids["other"]))
                    await sql(
                        "UPDATE user_account SET status='SUSPENDED' WHERE public_id=:id",
                        id=ids["user"],
                    )
                    with pytest.raises(AppError):
                        await signed(saved_word)
                    await sql(
                        "UPDATE user_account SET status='ACTIVE' WHERE public_id=:id",
                        id=ids["user"],
                    )
                    await sql(
                        "UPDATE favorite_snapshot_revision SET "
                        "saved_content_version=NULL,revision_id=NULL WHERE public_id=:id",
                        id=ids["snapshot"],
                    )
                    with pytest.raises(AppError):
                        await signed(saved_image)
                    await sql(
                        "UPDATE favorite_snapshot_revision SET "
                        "saved_content_version=1,revision_id=:revision WHERE public_id=:id",
                        id=ids["snapshot"],
                        revision=ids["revision"],
                    )
                    await sql(
                        "UPDATE favorite_snapshot SET active=0 WHERE public_id=:id",
                        id=ids["favorite"],
                    )
                    with pytest.raises(AppError):
                        await signed(saved_word)
                finally:
                    await transaction.rollback()
    finally:
        await engine.dispose()
