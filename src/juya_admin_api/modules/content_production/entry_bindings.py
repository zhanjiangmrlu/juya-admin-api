"""Read fixed references for one version under the work-item content scope."""

from typing import TYPE_CHECKING

from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content_production.schemas import EntryBinding, EntryBindings
from juya_admin_api.shared.errors import AppError

if TYPE_CHECKING:
    from juya_admin_api.modules.content_production.service import WorkOrderService


async def read(
    service: "WorkOrderService", identity: str, item_id: str, revision_id: str | None = None
) -> EntryBindings:
    item = await service.item(identity, item_id)
    revision_id = revision_id or item.revision_id
    if not revision_id or not item.content_id:
        raise AppError("WORK_REVISION_NOT_FOUND", "没有可读取的内容版本", 404)
    async with service.sessions() as session:
        snapshot = await session.scalar(
            text(
                "SELECT r.content_snapshot FROM scene_revision r JOIN scene s ON s.id=r.scene_id "
                "WHERE r.public_id=:revision AND s.public_id=:content"
            ),
            dict(revision=revision_id, content=item.content_id),
        )
        if snapshot is None:
            raise AppError("WORK_REVISION_NOT_FOUND", "版本不属于此工作单篇目", 404)
        body = decode(snapshot)
        ids = {e.get("entry_id") for e in [*body.get("vocabulary", []), *body.get("chunks", [])]}
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT r.revision_id,r.entry_id,r.entry_version,r.sense_id,r.source_id,"
                        "r.lexicon_entry_id,r.lexicon_entry_version AS definition_version,"
                        "e.import_namespace AS namespace,e.content_type,e.content_id,"
                        "e.import_entry_id,"
                        "e.identity_provenance FROM content_entry_revision r JOIN content_entry e "
                        "ON e.public_id=r.entry_id WHERE r.revision_id=:revision AND "
                        "e.content_id=:content"
                    ),
                    dict(revision=revision_id, content=item.content_id),
                )
            )
            .mappings()
            .all()
        )
        return EntryBindings(
            work_order_id=identity,
            item_id=item_id,
            revision_id=revision_id,
            bindings=[
                EntryBinding.model_validate(dict(row)) for row in rows if row["entry_id"] in ids
            ],
        )
