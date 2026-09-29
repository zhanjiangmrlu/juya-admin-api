from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 28, 23, 0, tzinfo=UTC)


def make_service() -> tuple[FeedbackService, InMemoryFeedbackRepository]:
    repository = InMemoryFeedbackRepository()
    return FeedbackService(repository), repository


@pytest.mark.asyncio
async def test_create_boundaries_and_default_sla() -> None:
    service, _repository = make_service()
    ticket = await service.create(
        "user-1",
        "CONTENT",
        "x" * 300,
        {"page": "scene"},
        ["feedback/user-1/image.png"],
        "create-1",
        NOW,
    )

    assert ticket.status == "PENDING"
    assert ticket.deadline_at == NOW + timedelta(hours=48)
    with pytest.raises(AppError) as too_long:
        await service.create("user-1", "CONTENT", "x" * 301, {}, [], "create-2", NOW)
    assert too_long.value.code == "FEEDBACK_DESCRIPTION_TOO_LONG"
    with pytest.raises(AppError) as too_many_images:
        await service.create(
            "user-1", "CONTENT", "problem", {}, ["one.png", "two.png"], "create-3", NOW
        )
    assert too_many_images.value.code == "FEEDBACK_SCREENSHOT_LIMIT"


@pytest.mark.asyncio
async def test_need_more_pauses_sla_supply_resets_full_cycle_and_only_two_rounds() -> None:
    service, repository = make_service()
    ticket = await service.create("user-1", "FUNCTION", "problem", {}, [], "create-1", NOW)
    await service.start_processing(ticket.id, "admin-1", "start-1", NOW)

    paused = await service.request_supplement(
        ticket.id, "请补充复现步骤", "admin-1", "need-1", NOW + timedelta(hours=10)
    )
    assert paused.status == "NEED_MORE"
    assert paused.deadline_at is None
    assert paused.sla_remaining_seconds == 38 * 60 * 60
    paused_detail = await service.get_admin(ticket.id)
    assert paused_detail.rounds[0].request_text == "请补充复现步骤"
    assert paused_detail.rounds[0].paused_at == NOW + timedelta(hours=10)
    supplied = await service.supply(
        ticket.id, "补充内容", "user-1", "supply-1", NOW + timedelta(hours=20)
    )
    assert supplied.status == "USER_SUPPLIED"
    assert supplied.deadline_at == NOW + timedelta(hours=68)
    supplied_detail = await service.get_admin(ticket.id)
    assert supplied_detail.rounds[0].supplement_text == "补充内容"
    assert supplied_detail.rounds[0].supplied_at == NOW + timedelta(hours=20)

    await service.start_processing(ticket.id, "admin-1", "start-2", NOW + timedelta(hours=20))
    await service.request_supplement(
        ticket.id, "第二轮", "admin-1", "need-2", NOW + timedelta(hours=21)
    )
    await service.supply(ticket.id, "第二次补充", "user-1", "supply-2", NOW + timedelta(hours=22))
    await service.start_processing(ticket.id, "admin-1", "start-3", NOW + timedelta(hours=22))
    with pytest.raises(AppError) as third_round:
        await service.request_supplement(
            ticket.id, "第三轮", "admin-1", "need-3", NOW + timedelta(hours=23)
        )
    assert third_round.value.code == "FEEDBACK_SUPPLEMENT_LIMIT"
    assert len(repository.rounds[ticket.id]) == 2


@pytest.mark.asyncio
async def test_resolve_reopen_once_within_seven_days_and_idempotent_outbox() -> None:
    service, repository = make_service()
    ticket = await service.create(
        "user-1", "DISPLAY", "problem", {}, ["image.png"], "create-1", NOW
    )
    await service.start_processing(ticket.id, "admin-1", "start-1", NOW)
    resolved = await service.resolve(
        ticket.id,
        "RESOLVED",
        "处理完成",
        "admin-1",
        "resolve-1",
        NOW,
    )
    replay = await service.resolve(
        ticket.id,
        "RESOLVED",
        "处理完成",
        "admin-1",
        "resolve-1",
        NOW,
    )
    assert replay == resolved
    assert len(repository.outbox) == 1
    assert len([item for item in repository.timeline if item.event_type == "RESOLVED"]) == 1

    reopened = await service.reopen(
        ticket.id, "问题仍存在", "user-1", "reopen-1", NOW + timedelta(days=6)
    )
    assert reopened.status == "PROCESSING"
    assert reopened.reopen_count == 1
    await service.resolve(
        ticket.id,
        "RESOLVED",
        None,
        "admin-1",
        "resolve-2",
        NOW + timedelta(days=6),
    )
    with pytest.raises(AppError) as second_reopen:
        await service.reopen(ticket.id, "again", "user-1", "reopen-2", NOW + timedelta(days=6))
    assert second_reopen.value.code == "FEEDBACK_REOPEN_LIMIT"


@pytest.mark.asyncio
async def test_close_schedules_screenshot_deletion_after_thirty_days() -> None:
    service, repository = make_service()
    ticket = await service.create(
        "user-1", "PRONUNCIATION", "problem", {}, ["image.png"], "create-1", NOW
    )
    await service.start_processing(ticket.id, "admin-1", "start-1", NOW)
    closed = await service.close_insufficient(ticket.id, "信息不足", "admin-1", "close-1", NOW)

    assert closed.status == "CLOSED_INSUFFICIENT"
    assert repository.screenshots[ticket.id].delete_after == NOW + timedelta(days=30)


@pytest.mark.asyncio
async def test_internal_notes_are_admin_only_ordered_and_idempotent() -> None:
    service, repository = make_service()
    ticket = await service.create("user-1", "CONTENT", "problem", {}, [], "create-1", NOW)

    first = await service.add_internal_note(
        ticket.id, "admin-1", "仅管理员可见", "note-1", NOW + timedelta(minutes=2)
    )
    replay = await service.add_internal_note(
        ticket.id, "admin-1", "不会重复写入", "note-1", NOW + timedelta(minutes=3)
    )
    detail = await service.get_admin(ticket.id)

    assert replay == first
    assert [note.content for note in detail.internal_notes] == ["仅管理员可见"]
    assert detail.timeline[-1].event_type == "INTERNAL_NOTE_ADDED"
    assert detail.timeline[-1].visibility == "ADMIN"
    assert not hasattr(await service.get(ticket.id), "internal_notes")
    assert repository.outbox == {}


@pytest.mark.asyncio
async def test_internal_note_rejects_blank_and_more_than_two_hundred_characters() -> None:
    service, _repository = make_service()
    ticket = await service.create("user-1", "CONTENT", "problem", {}, [], "create-1", NOW)

    for value in ("   ", "x" * 201):
        with pytest.raises(AppError) as invalid:
            await service.add_internal_note(ticket.id, "admin-1", value, "note-key", NOW)
        assert invalid.value.code == "FEEDBACK_INTERNAL_NOTE_INVALID"
