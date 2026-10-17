"""Explicit transport contracts; originals are evidence, current content is discriminated."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.short_reading.schemas import ReadingContent

ContentType = Literal["dialogue", "word_card", "short_reading"]
ProductionStatus = Literal[
    "processing", "normal", "review_required", "failed", "published", "cancelled"
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SceneCandidate(Model):
    template: Literal["dialogue", "word_card"]
    content: SceneContent


class ReadingCandidate(Model):
    template: Literal["short_reading"]
    content: ReadingContent


CurrentContent = Annotated[SceneCandidate | ReadingCandidate, Field(discriminator="template")]


class SourceDiagnostic(Model):
    """Read-only evidence of one rejected word/chunk source declaration"""

    entry_id: str | None = None
    entry_type: str | None = None
    entry_text: str | None = None
    source_locations: list[str] = Field(default_factory=list)
    declared_locator: JsonValue = None
    declared_text: str | None = None
    declared_speaker: str | None = None
    actual_sentence_id: str | None = None
    actual_sentence_number: int | None = None
    actual_text: str | None = None
    actual_speaker: str | None = None
    actual_source_locations: list[str] = Field(default_factory=list)
    unsupported_fields: list[str] = Field(default_factory=list)
    matched_sentence_numbers: list[int] = Field(default_factory=list)


class WorkIssue(Model):
    id: str
    code: str
    message: str
    location: list[str] = Field(default_factory=list)
    correction_target: Literal["content", "source", "audio", "entries", "publication"] = "content"
    historical: bool = False
    resolved: bool = False
    acknowledgement_eligible: bool = False
    origin_work_item_id: str | None = None
    source_hash: str | None = None
    severity: str | None = None
    source_diagnostic: SourceDiagnostic | None = None


class WorkTask(Model):
    stage_id: str
    revision_id: str | None = None
    sentence_id: str | None = None
    stage: str
    status: str
    input_hash: str
    config_version: str
    fencing_version: int
    attempt: int
    error_code: str | None = None
    retryable: bool = False
    current: bool = True


class Counts(Model):
    total: int = 0
    processing: int = 0
    normal: int = 0
    review_required: int = 0
    failed: int = 0
    confirmed: int = 0
    publishable: int = 0
    published: int = 0
    cancelled: int = 0


class WorkItem(Model):
    item_id: str
    work_order_id: str
    import_item_id: str | None = None
    content_id: str | None = None
    revision_id: str | None = None
    original_revision_id: str | None = None
    candidate_hash: str | None = None
    edit_version: int | None = None
    content_version: int | None = None
    title_en: str = ""
    title_zh: str = ""
    lifecycle_status: str = "DRAFT"
    production_status: ProductionStatus
    issues: list[WorkIssue] = Field(default_factory=list)
    tasks: list[WorkTask] = Field(default_factory=list)
    confirmed: bool = False
    publishable: bool = False
    published: bool = False
    current_draft: bool = False


class WorkItemDetail(WorkItem):
    current: CurrentContent | None = None
    source_available: bool
    source_snapshot: dict[str, JsonValue] | None = None
    historical_issues: list[WorkIssue] = Field(default_factory=list)
    phase_checks: dict[str, bool | None] = Field(default_factory=dict)


class ItemPage(Model):
    items: list[WorkItem]
    total: int
    page: int
    page_size: int
    counts: Counts
    version: int = Field(default=1, ge=1)
    collection_hash: str | None = None


class WorkOrder(Model):
    work_order_id: str
    import_batch_id: str | None
    source_mode: str
    series_id: str
    series_title: str
    content_type: ContentType
    version: int
    status: str
    created_at: datetime
    updated_at: datetime
    counts: Counts
    available_actions: list[str]
    proofreading_set_id: str | None = None
    publication_token: str | None = None
    pending_commands: list["PendingCommand"] = Field(default_factory=list)


class OrderPage(Model):
    items: list[WorkOrder]
    total: int
    page: int
    page_size: int


class PendingCommand(Model):
    command_id: str
    action: str
    created_at: datetime
    retry_at: datetime


class CommandResult(Model):
    command_id: str
    work_order_id: str
    action: str
    status: str
    version: int
    selected_item_ids: list[str] = Field(default_factory=list)
    stage_id: str | None = None
    proofreading_set_id: str | None = None
    publication_token: str | None = None
    revision_id: str | None = None
    candidate_hash: str | None = None
    edit_version: int | None = None
    entry_results: list["EntryConfirmationResult"] = Field(default_factory=list)


class EntryConfirmationResult(Model):
    revision_id: str
    status: Literal["SUCCESS", "FAILED"]
    edit_version: int | None = None
    error_code: str | None = None


class ScopedRequest(Model):
    expected_version: int = Field(ge=1)
    scope: Literal["all", "normal", "confirmed", "publishable", "failed", "selected"] = "all"
    selected_item_ids: list[str] = Field(default_factory=list, max_length=1000)


class FromBatchRequest(Model):
    batch_id: str = Field(min_length=1, max_length=64)


class ManualRequest(Model):
    series_id: str = Field(min_length=1, max_length=64)
    content_type: ContentType
    content_id: str | None = Field(default=None, min_length=1, max_length=64)
    current: CurrentContent | None = None


class CorrectionRequest(Model):
    expected_version: int = Field(ge=1)
    expected_revision_id: str
    expected_edit_version: int = Field(ge=1)
    expected_content_version: int = Field(ge=1)
    candidate_hash: str = Field(pattern="^[0-9a-f]{64}$")
    current: CurrentContent
    resolved_issue_ids: list[str] = Field(default_factory=list, max_length=2000)
    original_compared: bool = False


class ImportConfirmRequest(ScopedRequest):
    preview_hash: str = Field(pattern="^[0-9a-f]{64}$")
    preview_version: int = Field(ge=1)
    acknowledged_defaults: list[str] = Field(default_factory=list, max_length=2000)
    confirmed_conflicts: list[str] = Field(default_factory=list, max_length=3000)


class SampleActionRequest(ScopedRequest):
    operation: Literal["confirm", "fail", "append", "bulk-confirm", "complete"]
    observed_item_id: str | None = None
    candidate_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    collection_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    text_checked: bool = False
    images_checked: bool = False
    audio_listened: bool = False
    accept_normal_results: bool = False
    allow_abnormal_drafts: bool = False
    error_code: str | None = Field(default=None, min_length=1, max_length=128)


class PublicationConfirmRequest(ScopedRequest):
    publication_token: str
    range_version: int = Field(ge=1)
    collection_hash: str = Field(pattern="^[0-9a-f]{64}$")
    manual_confirmed: bool


class StageRetryRequest(ScopedRequest):
    stage_id: str
    expected_fence: int = Field(ge=0)
    input_hash: str = Field(pattern="^[0-9a-f]{64}$")
    config_version: str = Field(min_length=1, max_length=128)
    expected_edit_version: int = Field(ge=1)
    manual_confirmed: bool
    acknowledged_charged_retry: bool = False


class ImportItemResult(Model):
    item_id: str
    status: str
    operation: str | None = None
    content_id: str | None = None
    revision_id: str | None = None
    error_code: str | None = None


class ImportPreviewItem(Model):
    item_id: str
    import_item_id: str
    import_content_id: str
    row_no: int
    operation: str
    status: str
    candidate: dict[str, JsonValue]
    issues: list[WorkIssue]
    defaults: list[str]
    resources: dict[str, JsonValue]
    entries: list[dict[str, JsonValue]]
    source_snapshot: dict[str, JsonValue] | None
    result: ImportItemResult | None = None


class ImportPreview(Model):
    work_order_id: str
    batch_id: str
    status: str
    preview_hash: str
    preview_version: int
    items: list[ImportPreviewItem]
    batch_issues: list[WorkIssue]
    unreferenced_files: list[str] = Field(default_factory=list)


class SamplingCandidate(Model):
    content_type: ContentType
    content_id: str
    revision_id: str
    candidate_hash: str
    normal: bool = False
    failure: dict[str, str] | None = None


class SamplingState(Model):
    work_order_id: str
    proofreading_set_id: str
    collection_hash: str
    status: str
    normal_count: int
    sample_ids: list[str]
    confirmed_revision_ids: list[str]
    candidates: list[SamplingCandidate]


class PublicationCandidate(Model):
    content_type: ContentType
    content_id: str
    revision_id: str
    expected_version: int
    expected_edit_version: int | None = None
    candidate_hash: str
    warning_codes: list[str]


class PublicationItemResult(Model):
    revision_id: str
    status: str
    error_code: str | None = None


class PublicationState(Model):
    work_order_id: str
    token: str
    collection_hash: str
    version: int
    status: str
    stage_id: str | None
    expires_at: datetime
    candidates: list[PublicationCandidate]
    results: list[PublicationItemResult]


class ContextLookupRequest(Model):
    content_type: ContentType
    content_ids: list[str] = Field(min_length=1, max_length=100)


class ContentContext(Model):
    content_id: str
    work_order_id: str
    work_item_id: str
    production_status: ProductionStatus
    issue_count: int


class ContentContexts(Model):
    items: list[ContentContext]


class CentralOption(Model):
    lexicon_entry_id: str
    definition_version: int
    sense_key: str
    match: Literal["exact", "variant"]
    content: SceneEntry
    meaning_differs: bool


class CandidateResult(Model):
    result_id: str
    status: Literal["PENDING", "ADOPTED", "REJECTED"]
    entry_id: str | None = None
    provided: SceneEntry | None = None
    english: str = ""
    chinese: str = ""
    source_id: str | None = None
    start: int | None = None
    end: int | None = None
    requires_manual_correction: bool = False
    central_options: list[CentralOption] = Field(default_factory=list)


class CandidateGroup(Model):
    candidate_id: str
    item_id: str
    revision_id: str
    expected_edit_version: int
    kind: Literal["PROVIDED_LEXICON", "AI_LEXICON"]
    status: str
    stale: bool
    dismissed: bool = False
    dismissal_reason: str | None = None
    results: list[CandidateResult]


class CandidateGroups(Model):
    work_order_id: str
    groups: list[CandidateGroup]


class CandidateDecision(Model):
    result_id: str
    decision: Literal["ADOPTED", "REJECTED", "KEEP_PROVIDED", "USE_CENTRAL"]
    meaning_change: Literal["wording", "new_sense"] | None = None
    central_action: Literal["none", "new_sense", "wording"] = "none"
    lexicon_entry_id: str | None = None
    definition_version: int | None = None
    chinese: str | None = None
    entry_type: Literal["VOCABULARY", "PHRASE"] | None = None
    source_id: str | None = None
    start: int | None = None
    end: int | None = None


class CandidateDecisionRequest(ScopedRequest):
    candidate_id: str
    expected_edit_version: int = Field(ge=1)
    decisions: list[CandidateDecision] = Field(min_length=1, max_length=1000)


class CandidateDismissalRequest(ScopedRequest):
    candidate_id: str
    expected_edit_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class EntryConfirmationRequest(ScopedRequest):
    manual_confirmed: bool


class OrganizeRequest(ScopedRequest):
    include_recommendations: bool = False


class UsageAttempt(Model):
    attempt_id: str
    stage_id: str
    kind: Literal["AI_TOKENS", "ASR_MILLISECONDS"]
    attempt_number: int
    status: str
    reserved_units: int
    actual_units: int | None
    input_tokens: int | None
    output_tokens: int | None
    measured_audio_ms: int | None
    model: str | None
    created_at: datetime
    provider_region: str | None = None
    list_price_estimate: "ListPriceEstimate | None" = None


class ListPriceEstimate(Model):
    basis: Literal["official_list_price"] = "official_list_price"
    price_version: str = "aliyun-list-cn-beijing-2026-10-14"
    currency: Literal["CNY"] = "CNY"
    amount_cny: str | None = None
    unknown_reason: str | None = None
    sources: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class PlannedUsage(Model):
    candidate_id: str | None
    item_id: str
    kind: str
    model: str | None
    provider_region: str | None
    input_tokens_upper_bound: int | None = None
    output_tokens_upper_bound: int | None = None
    measured_audio_ms: int | None = None
    list_price_estimate: ListPriceEstimate
    planning_status: Literal["planned", "potential"] = "planned"


class WorkOrderUsage(Model):
    work_order_id: str
    provenance: Literal["associated_stage_records"] = "associated_stage_records"
    attempts: list[UsageAttempt]
    unknown_count: int
    retry_count: int
    cost: None = None
    planned: list[PlannedUsage] = Field(default_factory=list)
    known_list_price_subtotal_cny: str | None = None
    unpriced_attempt_count: int = 0
    estimation_scope: str = "associated_attempts_and_current_planned_candidates"
    potential: list[PlannedUsage] = Field(default_factory=list)
    source_text_utf8_bytes: int | None = None
    source_text_count: int = 0
    source_unknown_count: int = 0
    measured_audio_ms: int | None = None
    audio_reference_count: int = 0
    audio_unknown_count: int = 0


class PhaseCounts(Model):
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    processing: int = 0
    review_required: int = 0
    unknown: int = 0
    not_applicable: int = 0
    cancelled: int = 0


class PhaseRetry(Model):
    item_id: str
    stage_id: str


class WorkPhase(Model):
    key: Literal["parsing", "matching", "audio", "publication"]
    label: str
    counts: PhaseCounts
    retryable: list[PhaseRetry] = Field(default_factory=list)
    provenance: str = "current_hash_bound_preparation_and_persisted_stage_results"


class WorkOrderPhases(Model):
    work_order_id: str
    version: int
    counts: Counts
    phases: list[WorkPhase]


class EntryBinding(Model):
    revision_id: str
    entry_id: str
    entry_version: int
    sense_id: str
    source_id: str
    lexicon_entry_id: str | None
    definition_version: int | None
    namespace: str | None
    content_type: ContentType
    content_id: str
    import_entry_id: str | None
    identity_provenance: str


class EntryBindings(Model):
    work_order_id: str
    item_id: str
    revision_id: str
    bindings: list[EntryBinding]
