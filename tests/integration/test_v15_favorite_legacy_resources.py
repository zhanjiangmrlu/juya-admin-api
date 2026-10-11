"""Real SQL old-version ownership and actual inspected media; OSS/review are local fixtures."""

import io
import os
import shutil
import wave
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.integrations.oss.provider import ObjectMetadata
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.pronunciation.domain import Target
from juya_admin_api.modules.pronunciation.models import targets
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.resources import SQLResourceSigner
from juya_admin_api.modules.v15_access.sql_adapters import SQLFavoriteAuthority
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_owned_legacy_image_and_old_audio_version_are_fixed_reviewable_and_never_whole():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    ids = {
        name: new_ulid(now)
        for name in ("user", "favorite", "snapshot", "image", "audio", "upload", "v1", "v2", "pron")
    }
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 6400)
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    ffprobe = shutil.which("ffprobe")
    assert ffprobe
    facts = {
        "audio": await inspect_media(audio.getvalue(), "audio", ffprobe),
        "image": await inspect_media(image.getvalue(), "images", ffprobe),
    }
    objects = {}
    oss = AsyncMock()

    async def head(key):
        return objects[key]

    oss.head_object.side_effect = head
    oss.sign_get_url.return_value = "https://fixture.invalid/media?Expires=" + str(
        int((now + timedelta(seconds=60)).timestamp())
    )
    signer = SQLResourceSigner(oss, clock=lambda: now)
    async with sessions() as session:
        transaction = await session.begin()
        try:
            await session.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status) "
                    "VALUES(:user,:number,'ACTIVE')"
                ),
                {"user": ids["user"], "number": uuid4().hex[:10]},
            )
            for name, actual in facts.items():
                key = "sealed/media/" + ("audio/" if name == "audio" else "images/") + ids[name]
                objects[key] = ObjectMetadata(
                    key, actual.size, actual.content_type, actual.sha256, {}
                )
                await session.execute(
                    text(
                        "INSERT INTO media_asset(public_id,object_key,asset_type,content_type,"
                        "size_bytes,sha256,status,security_status,created_by,created_at,"
                        "duration_ms,width,height) VALUES(:id,:key,:type,:mime,:size,:sha,"
                        "'CONFIRMED','PASSED','fixture',:now,:duration,:width,:height)"
                    ),
                    {
                        "id": ids[name],
                        "key": key,
                        "type": "audio" if name == "audio" else "images",
                        "mime": actual.content_type,
                        "size": actual.size,
                        "sha": actual.sha256,
                        "now": now,
                        "duration": actual.duration_ms,
                        "width": actual.width,
                        "height": actual.height,
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable_key,target_type) "
                    "VALUES(:id,:id,'WORD')"
                ),
                {"id": ids["upload"]},
            )
            for number, name, state in ((1, "v1", "SUPERSEDED"), (2, "v2", "ACTIVE")):
                await session.execute(
                    text(
                        "INSERT INTO audio_version(public_id,target_id,asset_id,version_no,"
                        "source,status,created_at) SELECT :id,t.id,m.id,:number,'MANUAL',:state,"
                        ":now FROM audio_target t,media_asset m "
                        "WHERE t.public_id=:target AND m.public_id=:asset"
                    ),
                    {
                        "id": ids[name],
                        "number": number,
                        "state": state,
                        "now": now,
                        "target": ids["upload"],
                        "asset": ids["audio"],
                    },
                )
            await session.execute(
                insert(targets).values(
                    **asdict(
                        Target(
                            ids["pron"],
                            "hello",
                            uploaded_target_id=ids["upload"],
                            uploaded_resource_version=ids["v2"],
                        )
                    ),
                    confirmed_by="admin",
                    confirmed_at=1,
                    created_at=1,
                )
            )
            await session.execute(
                text(
                    "INSERT INTO favorite_snapshot(public_id,user_id,entry_type,entry_id,"
                    "sense_id,source_id,active,current_snapshot_revision_id,favorited_at) "
                    "SELECT :favorite,id,'VOCABULARY','entry','sense','source',1,:snapshot,:now "
                    "FROM user_account WHERE public_id=:user"
                ),
                {**ids, "now": now},
            )
            await session.execute(
                text(
                    "INSERT INTO favorite_snapshot_revision(public_id,favorite_id,"
                    "snapshot_version,content_type,content_id,saved_content_version,revision_id,"
                    "english,meaning_zh,explanation,source_sentence,image_asset_id,"
                    "pronunciation_target_id,resource_version) SELECT :snapshot,id,1,'SCENE',"
                    "'archived-scene',1,'archived-revision','hello','你好','','hello',"
                    ":image,:pron,:v1 FROM favorite_snapshot WHERE public_id=:favorite"
                ),
                ids,
            )
            authority = await SQLFavoriteAuthority()(session, ids["user"], ids["favorite"])
            assert authority.entry_type == "word" and authority.content_type == "dialogue"
            request = ResourceRequest(
                ids["user"],
                "dialogue",
                "archived-scene",
                "archived-revision",
                ids["pron"],
                ids["v1"],
                "WORD",
                favorite_id=ids["favorite"],
            )
            signed = await signer(session, request, now + timedelta(seconds=60))
            assert signed["asset_id"] == ids["audio"] and signed["resource_version"] == ids["v1"]
            image_request = replace(
                request, target_id=ids["image"], resource_version=None, role="WORD_IMAGE"
            )
            assert (await signer(session, image_request, now + timedelta(seconds=60)))[
                "asset_id"
            ] == ids["image"]
            for forbidden in (
                replace(request, resource_version=ids["v2"]),
                replace(request, user_id="other"),
                replace(request, role="WHOLE"),
            ):
                with pytest.raises(AppError):
                    await signer(session, forbidden, now + timedelta(seconds=60))
            await session.execute(
                text(
                    "UPDATE favorite_snapshot_revision SET pronunciation_target_id=:upload "
                    "WHERE public_id=:snapshot"
                ),
                ids,
            )
            legacy_request = replace(request, target_id=ids["upload"])
            assert (await signer(session, legacy_request, now + timedelta(seconds=60)))[
                "asset_id"
            ] == ids["audio"]
            await session.execute(
                text("UPDATE media_asset SET security_status='BLOCKED' WHERE public_id=:audio"), ids
            )
            with pytest.raises(AppError):
                await signer(session, legacy_request, now + timedelta(seconds=60))
            # Rejection did not issue a new signature or rewrite any historical binding.
            assert (
                await session.scalar(
                    text(
                        "SELECT resource_version FROM favorite_snapshot_revision "
                        "WHERE public_id=:snapshot"
                    ),
                    ids,
                )
                == ids["v1"]
            )
        finally:
            await transaction.rollback()
    await engine.dispose()
