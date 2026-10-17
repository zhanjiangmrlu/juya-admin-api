"""Group only checks already performed by preparation, without granting publication."""

SCENE_GROUPS = {
    "parsing": {"TITLE_REQUIRED", "DIALOGUE_REQUIRED", "COPYRIGHT_SOURCE_REQUIRED"},
    "matching": {
        "ORIGINAL_IMAGE_REQUIRED",
        "VOCABULARY_REQUIRED",
        "CHUNKS_REQUIRED",
        "SOURCE_REFERENCE_INVALID",
        "COVER_INVALID",
        "ICONS_INVALID",
        "CHUNK_IMAGE_INVALID",
        "ENTRY_AUDIO_INVALID",
        "ENTRY_REFERENCE_INVALID",
    },
    "audio": {
        "AUDIO_MISSING",
        "SENTENCE_TIMING_INVALID",
        "SENTENCE_SLICE_INVALID",
        "SENTENCE_SLICE_FACTS_INVALID",
    },
}


def scene_checks(errors: list[str]) -> dict[str, bool | None]:
    """Partition fully evaluated schema2 preparation errors; unknown errors fail closed."""
    known = set().union(*SCENE_GROUPS.values())
    if set(errors) - known:
        return dict.fromkeys(SCENE_GROUPS)
    return {key: not bool(set(errors) & values) for key, values in SCENE_GROUPS.items()}


def reading_checks(errors: list[str]) -> dict[str, bool | None]:
    """Reading evaluates complete text, metadata and optional resources, without scene alignment."""
    parsing = {
        "TITLE_EN_REQUIRED",
        "TITLE_ZH_REQUIRED",
        "CATEGORY_REQUIRED",
        "TOPIC_REQUIRED",
        "LEVEL_REQUIRED",
        "SAFE_INTRO_REQUIRED",
        "TRANSLATION_ZH_REQUIRED",
        "PARAGRAPHS_REQUIRED",
        "COPYRIGHT_REQUIRED",
    }
    matching = {"COVER_RESOURCE_INVALID", "AUDIO_RESOURCE_INVALID"}
    if set(errors) - parsing - matching:
        return {"parsing": None, "matching": None, "audio": None}
    return {
        "parsing": not bool(set(errors) & parsing),
        "matching": not bool(set(errors) & matching),
        "audio": None,
    }
