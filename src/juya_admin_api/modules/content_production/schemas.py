"""Explicit transport contracts; originals are evidence, current content is discriminated."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from juya_admin_api.modules.content.schemas import SceneContent
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


class WorkIssue(Model):
    id: str
    code: str
    message: str
    location: list[str] = Field(default_factory=list)
    correction_target: Literal["content", "source", "audio", "entries", "publication"] = "content"
    historical: bool = False
    resolved: bool = False


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


class WorkItemDetail(WorkItem):
    current: CurrentContent | None = None
    source_available: bool
    source_snapshot: dict[str, JsonValue] | None = None
    historical_issues: list[WorkIssue] = Field(default_factory=list)


class ItemPage(Model):
    items: list[WorkItem]
    total: int
    page: int
    page_size: int
    counts: Counts


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


class ImportPreview(Model):
    work_order_id: str
    batch_id: str
    status: str
    preview_hash: str
    preview_version: int
    items: list[ImportPreviewItem]
    batch_issues: list[WorkIssue]


class SamplingCandidate(Model):
    content_type: ContentType
    content_id: str
    revision_id: str
    candidate_hash: str
    normal: bool = False


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
