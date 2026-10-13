# 内容生产工作单契约

增量迁移 `0022` 保存工作单范围、不可变输入、版本历史、真实阶段关联、问题确认和命令回执。运行时最低数据库版本为 22。工作单复用已有内容、导入、校对与发布模型；稳定内容 ID 和小程序契约保持一致。

## 路径与类型

基址 `/api/v1/admin/content-production/work-orders`。除原文件下载外，所有新响应都有具名 Pydantic/OpenAPI 类型。读接口使用现有管理员读权限；创建、修正、抽样、发布、重试、取消与恢复使用写权限。

| 方法 | 相对路径 | 请求 → 响应 |
| --- | --- | --- |
| POST | `/`，multipart | series_id/content_type/mode/file → WorkOrder，202 |
| POST | `/from-batch` | FromBatchRequest → WorkOrder |
| POST | `/manual` | ManualRequest → WorkOrder |
| GET | `/` | page/page_size/series_id → OrderPage |
| GET | `/{work_order_id}` | → WorkOrder |
| GET | `/{work_order_id}/items` | page/page_size/status → ItemPage |
| GET | `/{work_order_id}/items/{item_id}` | → WorkItemDetail |
| GET | `/{work_order_id}/items/{item_id}/original` | → WorkItemDetail |
| GET | `/{work_order_id}/original-file` | 已认证下载上传原文件；不暴露存储路径 |
| GET | `/{work_order_id}/import-preview` | → ImportPreview |
| POST | `/{work_order_id}/import-confirm`、`/import-retry` | ImportConfirmRequest → CommandResult，202 |
| PUT | `/{work_order_id}/items/{item_id}/correction` | CorrectionRequest → CommandResult |
| POST | `/{work_order_id}/organize` | ScopedRequest → CommandResult，202 |
| POST / GET | `/{work_order_id}/sampling` | ScopedRequest → CommandResult / SamplingState |
| POST | `/{work_order_id}/sample-actions` | SampleActionRequest → CommandResult |
| POST | `/{work_order_id}/publication-preview` | ScopedRequest → CommandResult |
| POST | `/{work_order_id}/publication-confirm`、`/publication-retry` | PublicationConfirmRequest → CommandResult，202 |
| GET | `/{work_order_id}/publication` | → PublicationState |
| POST | `/{work_order_id}/stage-retry` | StageRetryRequest → CommandResult，202 |
| POST | `/{work_order_id}/cancel` | ScopedRequest → CommandResult |
| POST | `/{work_order_id}/commands/{command_id}/resume` | 服务端回执恢复 → CommandResult |

列表上下文接口为 `POST /api/v1/admin/content-production/contexts`：`ContextLookupRequest`（content_type 和最多100个当前列表 content_ids）→ `ContentContexts`。同一内容在多个工作单中会返回多个关联，不能据此把全库候选加入工作单。现有目录分页、过滤不变。

## 原文与当前内容

`WorkItemDetail.current` 按 `template` 区分：`short_reading` 对应严格 `ReadingContent`，`dialogue/word_card` 对应严格 `SceneContent`。修正请求使用相同包装，不能把导入解析元数据写入正文。

原文只复制 `content_import_item.candidate` 内顶层的 `source_snapshot`。此列实际上保存完整导入 item，不能将其误认为纯正文。新工作单仅首次插入原文；刷新、修正、自动任务、阅读更换草稿都不更新原文。缺失原文返回 `source_available=false/source_snapshot=null`。未导入且有阻断错误的行只能修正/补充源文件，修正命令返回 `WORK_SOURCE_CORRECTION_REQUIRED`。

`original_revision_id` 和 `content_work_item_revision` 保留原始关联与后续版本历史。当前 revision/edit_version/hash 始终来自现有内容的当前草稿。已上线但另有草稿的条目显示独立的 `lifecycle_status=PUBLISHED` 与待处理 `production_status`。

独立 `MATERIAL_ZIP` 保留原素材入口：先在现有素材接口选定目标并绑定，再调用 `/from-batch`。工作单从持久化 `manifest.binding_requests` 的真实 revision 查询同类型、同分类的内容归属，并关联其实际阶段；没有绑定目标时明确返回 `WORK_MATERIAL_BIND_REQUIRED`。素材包原文件可下载，正文原文仍返回缺失，不从现有草稿伪造。其预览/确认继续使用既有素材绑定 API；工作单 `/import-preview` 返回 `WORK_MATERIAL_USE_BINDINGS`。标准导入确认不接收素材包。

## 范围、计数与版本

所有范围在服务端遍历逻辑工作单计算，不依赖当前页。`scope` 为 all/normal/confirmed/publishable/failed/selected；可同时用 selected_item_ids 收窄，每个 ID 必须属于当前工作单且不能重复。过滤先于分页；ItemPage.counts 始终为整单计数。normal/processing/review_required/failed/cancelled 是候选处理分类；published 是生命周期事实，confirmed/publishable 为独立事实，可重叠。

工作单 version 保护范围命令和抽样/发布引用变化，每个成功范围命令递增；正文修正不递增整单 version。修正必须同时匹配工作单 version、expected_revision_id、expected_edit_version、expected_content_version、candidate_hash。同内容跨工作单也受现有内容版本保护。后台任务只改变任务/正文事实，不能凭进度变化使整单编辑版本冲突。

原始诊断不删除。修正可传 resolved_issue_ids，但须 `original_compared=true`、原文确实存在、问题确属该条目。确认保存原始输入 hash、新正文 hash、管理员和审计；之后正文变更将使旧确认失效。当前素材/模板检查不能被此确认覆盖。Task 3 将把该有效问题规则接入旧校对分类。

## 恢复、重试与成本

创建、修正及范围命令使用 Idempotency-Key。创建/import delegate 保持既有持久回执；范围命令额外保存 action、请求摘要、冻结条目、确定性 delegate ID、租约和结果。预留事务先提交，再调用会打开独立会话的已有服务，避免跨服务持有工作单锁。未知结果保留 PENDING 回执；WorkOrder.pending_commands 显示 command_id/action/retry_at，原管理员可在租约到期后调用 resume，无须浏览器保留原键。确定失败持久化错误，相同键重放错误；成功重放原回执。

抽样复用确定性 proofreading_set_id；发布预览复用确定性 token，不能在不确定结果时创建另一套范围。发布 token 保持已有管理员归属检查；其他管理员须重新预览自己的合格范围。202 仅表示已排队，逐项结果须 GET publication。

导入恢复使用持久化原冻结 preview 的精确失败行，不重新生成成功行的映射，也不再次上传其素材。恢复首先复验原委托键的已提交排队回执，再检查尚未提交命令的当前失败资格；工作已完成仍能补齐原工作单回执。发布重试在预留事务中持久化属于原范围的精确 FAILED/UNKNOWN revision 子集，后续只用该子集重放既有发布回执；SUCCEEDED 永不重发。旧待恢复发布命令没有此字段时，从已提交委托回执补建精确子集。

取消仅允许尚未开始且没有内容的导入行，成功和已发布内容保留。部分取消在既有批次/导入行锁内同时写入 `content_work_item.cancelled` 和权威 `content_import_item.status=CANCELLED`。既有 ImportService 在准备素材前及最终保存前都核对该状态，旧导入入口也不能创建已取消行；其他行照常继续。取消本地事务已提交但外层结果丢失时，恢复识别两侧已提交标志并补齐相同回执。全部取消另调用既有批次取消，阻止旧入口继续确认整个批次。

未导入篇目的 validation/CONFIRM 阶段按原始 outbox 的 batch_id/stage_id/confirmation.selected 精确关联。只将该工作单未取消、尚未成功的适用行计为处理；排队/运行的新尝试覆盖旧失败分类，成功后转回真实候选事实。任务历史仍保留，未选中的行不能显示为同一确认任务正在处理。

读取工作单只刷新本地关联和事实，不创建供应商任务。当前 organize 复用已有 dialogue AUTO_AUDIO；无需整段音频的模板不会伪造任务成功。自动词库整理、AI逐条决策与阅读音频确认闭环由 Task 3 完成。

## Task 3 扩展位置

- `WorkProjection` 调用真实 `ProofreadingStore.facts`，并计算历史诊断的 hash-bound resolution；最终统一有效问题分类应让工作单与 `ProofreadingStore.classify_within` 使用同一规则。
- `content_work_item_revision` 是追加版本历史；`content_work_item_stage` 关联 AUTO_AUDIO、ASR/AI candidate 与 poll stage、句子切片、素材绑定与发布阶段。句子阶段按 outbox revision/sentence 关联，不能用 batch_id 冒充关联。
- `commands.delegate(..., action='organize')` 是模板自动整理接入口；保留 receipt/delegate ID，扩展实际持久任务，不执行 AI 改写。
- `WorkTask.current` 排除旧 revision，并按 AUTO_AUDIO 的英文/音频依赖判定；Task 3 可完善 ASR/slice 输入与最新候选结果状态，翻译修正应复用未变的音频依赖。
- 阅读人工试听、AI逐结果决策、异常修正后全域有效问题刷新与最终 OpenAPI 客户端冻结由后续任务负责。
