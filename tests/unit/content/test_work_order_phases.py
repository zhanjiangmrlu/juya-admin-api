"""Read-only phase outcomes preserve unknowns and count every actual member once."""

import importlib.util

from juya_admin_api.modules.content_production.schemas import WorkItemDetail


def item(identity, *, checks=None, tasks=None, status="review_required", template="dialogue"):
    return WorkItemDetail.model_validate(
        dict(
            item_id=identity,
            work_order_id="order",
            production_status=status,
            source_available=False,
            source_snapshot=None,
            **({"phase_checks": checks} if checks is not None else {}),
            tasks=tasks or [],
        )
    )


def projection():
    module = "juya_admin_api.modules.content_production.phases"
    assert importlib.util.find_spec(module), "authoritative phase projection missing"
    from juya_admin_api.modules.content_production.phases import aggregate

    return aggregate


def test_no_tasks_or_checks_never_means_success_and_every_phase_conserves_members():
    phases = projection()([item("one"), item("two")], content_type="dialogue")
    assert len(phases) == 4
    for phase in phases:
        assert phase.counts.total == 2
        assert phase.counts.succeeded == 0
        assert sum(phase.counts.model_dump(exclude={"total"}).values()) == 2


def test_current_failed_audio_task_cannot_mask_successful_other_item_or_legacy_history():
    task = dict(
        stage_id="failed",
        stage="SENTENCE_SLICE",
        status="FAILED",
        input_hash="a" * 64,
        config_version="v1",
        fencing_version=2,
        attempt=1,
        current=True,
        retryable=True,
    )
    rows = [
        item("one", checks={"parsing": True, "matching": True, "audio": True}),
        item("two", checks={"parsing": True, "matching": True, "audio": False}, tasks=[task]),
        item("three", tasks=[task | {"stage_id": "old", "current": False, "retryable": False}]),
    ]
    audio = next(p for p in projection()(rows, content_type="dialogue") if p.key == "audio")
    assert audio.counts.succeeded == audio.counts.failed == audio.counts.unknown == 1
    assert [(r.item_id, r.stage_id) for r in audio.retryable] == [("two", "failed")]


def test_reading_and_word_card_have_no_scene_alignment_and_publication_is_explicit():
    for kind in ("short_reading", "word_card"):
        phases = projection()([item("one")], content_type=kind)
        audio = next(p for p in phases if p.key == "audio")
        assert audio.counts.not_applicable == 1
        publication = next(p for p in phases if p.key == "publication")
        assert publication.counts.review_required == 1 and publication.counts.succeeded == 0


def test_preparation_check_groups_keep_audio_failure_and_unclassified_error_explicit():
    module = "juya_admin_api.modules.content_production.preparation_phases"
    assert importlib.util.find_spec(module), "existing preparation check groups missing"
    from juya_admin_api.modules.content_production.preparation_phases import scene_checks

    assert scene_checks(["SENTENCE_SLICE_FACTS_INVALID"]) == {
        "parsing": True,
        "matching": True,
        "audio": False,
    }
    assert scene_checks(["ENTRY_REFERENCE_INVALID"])["matching"] is False
    assert scene_checks(["UNCLASSIFIED_NEW_ERROR"]) == {
        "parsing": None,
        "matching": None,
        "audio": None,
    }


def test_batch_validate_success_does_not_override_explicit_unknown_preparation():
    rows = [item("unknown", checks={"parsing": None, "matching": None, "audio": None})]
    parsing = next(
        p for p in projection()(rows, content_type="dialogue", parsed=True) if p.key == "parsing"
    )
    assert parsing.counts.unknown == 1
    assert parsing.counts.succeeded == 0


def test_current_entry_issue_is_not_hidden_by_prepared_resources():
    rows = [item("one")]
    rows[0].phase_checks = {"parsing": True, "matching": True, "audio": True}
    from juya_admin_api.modules.content_production.schemas import WorkIssue

    rows[0].issues = [
        WorkIssue(
            id="pending", code="LEXICON_PENDING", message="待选择", correction_target="entries"
        )
    ]
    matching = next(p for p in projection()(rows, content_type="dialogue") if p.key == "matching")
    assert matching.counts.review_required == 1


def test_publication_uses_real_succeeded_failed_unknown_and_held_statuses():
    rows = [item(str(n)) for n in range(4)]
    state = next(
        p
        for p in projection()(
            rows,
            content_type="short_reading",
            publication_results={
                "0": "SUCCEEDED",
                "1": "FAILED",
                "2": "UNKNOWN",
            },
        )
        if p.key == "publication"
    )
    assert state.counts.succeeded == state.counts.failed == state.counts.unknown == 1
    assert state.counts.review_required == 1


def test_publication_batch_task_cannot_overwrite_frozen_item_success():
    task = dict(
        stage_id="batch-publish",
        stage="PUBLICATION_RANGE_test",
        status="RUNNING",
        input_hash="a" * 64,
        config_version="v1",
        fencing_version=2,
        attempt=1,
        current=True,
        retryable=False,
    )
    rows = [item("done", tasks=[task]), item("running", tasks=[task])]
    state = next(
        p
        for p in projection()(
            rows, content_type="short_reading", publication_results={"done": "SUCCEEDED"}
        )
        if p.key == "publication"
    )
    assert state.counts.succeeded == 1 and state.counts.processing == 1
