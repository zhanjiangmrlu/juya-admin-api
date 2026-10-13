# 内容生产工作单契约

增量迁移 `0022` 保存工作单范围、不可变输入、版本历史、真实阶段关联、问题确认和命令回执；`0023` 增加原文问题确认的依赖证据；`0024` 增加中央词库活动义项引用和本地条目的中央定义版本引用。运行时最低数据库版本为 24。工作单复用已有内容、导入、校对与发布模型；稳定内容 ID 和小程序契约保持一致。

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
| POST | `/{work_order_id}/organize` | OrganizeRequest → CommandResult，202 |
| GET | `/{work_order_id}/candidates` | → CandidateGroups |
| POST | `/{work_order_id}/candidate-decisions` | CandidateDecisionRequest → CommandResult |
| POST | `/{work_order_id}/entry-confirmations` | EntryConfirmationRequest → CommandResult |
| GET | `/{work_order_id}/usage` | → WorkOrderUsage |
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

原始诊断不删除。修正可传 resolved_issue_ids，但须 `original_compared=true`、原文确实存在、问题确属该条目。确认保存原始输入 hash、新正文 hash、选定字段证据/依赖 hash、管理员和审计。双语配对确认依赖完整对应正文、说话者、顺序和来源；相关字段变化会撤销确认，合法试听元数据变化不会撤销。旧记录缺少依赖证据时不会沿用。当前素材/模板检查不能被此确认覆盖。工作单、旧校对分类和发布使用的通过查询共享有效问题规则；旧诊断保留为 historical_issues，已明确处理的记录带 resolved=true。未处理 AI 结果也会阻止使用旧通过记录直接发布，全部明确拒绝后正文未变化的原通过记录仍可使用。

## 恢复、重试与成本

创建、修正及范围命令使用 Idempotency-Key。创建/import delegate 保持既有持久回执；范围命令额外保存 action、请求摘要、冻结条目、确定性 delegate ID、租约和结果。预留事务先提交，再调用会打开独立会话的已有服务，避免跨服务持有工作单锁。未知结果保留 PENDING 回执；WorkOrder.pending_commands 显示 command_id/action/retry_at，原管理员可在租约到期后调用 resume，无须浏览器保留原键。确定失败持久化错误，相同键重放错误；成功重放原回执。

抽样复用确定性 proofreading_set_id；发布预览复用确定性 token，不能在不确定结果时创建另一套范围。发布 token 保持已有管理员归属检查；其他管理员须重新预览自己的合格范围。202 仅表示已排队，逐项结果须 GET publication。

导入恢复使用持久化原冻结 preview 的精确失败行，不重新生成成功行的映射，也不再次上传其素材。恢复首先复验原委托键的已提交排队回执，再检查尚未提交命令的当前失败资格；工作已完成仍能补齐原工作单回执。发布重试在预留事务中持久化属于原范围的精确 FAILED/UNKNOWN revision 子集，后续只用该子集重放既有发布回执；SUCCEEDED 永不重发。旧待恢复发布命令没有此字段时，从已提交委托回执补建精确子集。

取消仅允许尚未开始且没有内容的导入行，成功和已发布内容保留。部分取消在既有批次/导入行锁内同时写入 `content_work_item.cancelled` 和权威 `content_import_item.status=CANCELLED`。既有 ImportService 在准备素材前及最终保存前都核对该状态，旧导入入口也不能创建已取消行；其他行照常继续。取消本地事务已提交但外层结果丢失时，恢复识别两侧已提交标志并补齐相同回执。全部取消另调用既有批次取消，阻止旧入口继续确认整个批次。

未导入篇目的 validation/CONFIRM 阶段按原始 outbox 的 batch_id/stage_id/confirmation.selected 精确关联。只将该工作单未取消、尚未成功的适用行计为处理；排队/运行的新尝试覆盖旧失败分类，成功后转回真实候选事实。任务历史仍保留，未选中的行不能显示为同一确认任务正在处理。

读取工作单只刷新本地关联和事实，不创建供应商任务。保存场景/词汇卡草稿会自动建立已有词表的提供值与中央可选定义对照；场景继续复用已有 AUTO_AUDIO→ASR→真实句子切片。短文只检查正文及可选全文音频/封面准备，不创建词库、ASR或切片。organize 没有实际异步阶段时返回 SUCCEEDED；存在阶段时返回 QUEUED，实际逐项阶段在 items.tasks。`OrganizeRequest.include_recommendations` 默认 false；显式 true 且有完整中英文来源正文、无词汇/语块的场景或词汇卡才排 AI 推荐，仍受已有预算/去重约束。合法翻译或条目修正不重跑未改变英文/源音频的自动音频依赖。

本地整理和可选 AI 排队各有委托回执；恢复先匹配 actor、命令派生键和冻结请求，再检查尚未提交部分的可变条件。已提交的整理/排队不因之后正文编辑被错误拒绝或再产生付费尝试。逐条/批量词条确认、AI/中央决定、阅读试听同样先恢复精确委托回执。导入聚合结果包含本次尝试和先前已持久化成功项，部分失败的 CONFIRM 阶段为 FAILED，历史成功不会因失败重试丢失。取消行的本次失败结果仍保留，权威 CANCELLED 约束不变。

## 已有词表、中央定义与 AI 决定

`CandidateGroups.groups[]` 返回 candidate_id、所属 item_id/revision_id、当前 expected_edit_version、kind（PROVIDED_LEXICON/AI_LEXICON）、status、stale 和 results。界面从响应携带这些标识，无须管理员输入。stale=true 的旧结果用于追溯，不可提交；刷新当前候选后继续。每个 CandidateResult 有稳定 result_id、PENDING/ADOPTED/REJECTED；提供词表还带本地 entry_id/provided，以及 central_options（lexicon_entry_id、definition_version、sense_key、exact/variant、完整定义、meaning_differs）。AI 行带 english/chinese/source_id/start/end/requires_manual_correction，正常来源位置由服务端推导，需修正的结果须在原文中选择精确位置。

`CandidateDecisionRequest` 继承范围字段，但一次命令仅选择一个候选组所属篇目；decisions 为该组 1–1000 个结果的原子批量决定。多篇候选组按各自当前 edit_version 提交，各组返回成功后刷新 work-order version。AI 支持 ADOPTED/REJECTED，可覆盖 chinese、VOCABULARY/PHRASE 的 entry_type 及经过人工定位的 source_id/start/end。连续采纳同组不同结果只接受服务自身记录的采用后依赖；外部正文、音频、配置或版本改变会拒绝。全部拒绝明确结束可选推荐，不制造正文条目。

PROVIDED_LEXICON 支持 KEEP_PROVIDED（保留上传值）、USE_CENTRAL（明确选定 central ID/version）、REJECTED（仅移除对应本地条目，模板主体要求仍检查）。`central_action` 默认 none，不写中央词库；完全相同且唯一的中央定义可绑定但不改上传内容。new_sense 是显式新增并保留其他活动义项；wording 必须指定要替代的活动中央 ID/version，不能推断哪一条应被替换。过期选项返回 CENTRAL_DEFINITION_STALE。改变上传定义必须由 USE_CENTRAL 明确选择，自动整理不会静默替换。

中央仍使用现有 lexicon_entry 和不可变 lexicon_entry_version；lexicon_active_sense 只标记活动定义。迁移仅将可信 current_version 设为活动，不把任意历史措辞版本提升为其他义项。中央更新在原 entry 行锁下维护 current_version 与活动引用，旧版本内容不变。本地 entry_id/sense_id/source_id/entry_version 保持独立，content_entry 的 lexicon_entry_id + lexicon_entry_version 指向精确中央定义；以后本地词形、释义、说明、音标或变体改变会清除此关联。决定审计记录原候选输入、对应来源和逐结果选择。

中央选项过期后以新命令键再次 organize。只有仍待处理选项证据发生变化才建立新的候选组；原组保存为 SUPERSEDED，并拒绝继续决定。已 ADOPTED/REJECTED 的结果携带旧决定来源进入新组，状态不重置，不能重复采纳；本地条目标识不变。全部已决定时不会因中央后续改词而重新要求决定，已选固定定义引用仍指向原不可变版本。

`EntryConfirmationRequest.manual_confirmed=true` 显式核对当前范围的词条与发音目标，复用已有人工确认规则。CommandResult.entry_results 为逐 revision 的 SUCCESS/FAILED、成功 edit_version 或 error_code；整体 CONFIRMED/PARTIAL。词条确认与中央候选选择是独立操作，不能用上传或 AI 推荐冒充人工确认。

## 阅读准备、抽样与精确失效

机器准备接受真实 READY 的 FULL 音频版本，但校验精确 target/version/asset、目标归属、媒体确认/审核、sealed 对象、SHA256、正时长/大小和内容引用；这不是已经试听。上传复用的 FULL 目标必须匹配不可变素材 hash。无音频短文无需 audio_listened。

从旧版本复制的手工 FULL 仅在当前草稿的 source_revision_id 链逐跳属于同一稳定短文、每跳 target/version/asset/duration 完全一致、且链上真实所有者版本匹配目标 stable_key 时允许复用；不能仅凭同内容前缀或任意历史目标放行。来源链证据进入准备 hash，外部来源、改变引用或循环链不成立时仍拒绝。新草稿仍须建立自己的抽样/通过记录，不能继承旧版本通过；原已发布快照不修改。

创建 sampling 后 GET SamplingState。按服务端 sample_ids 对每篇提交 sample-actions(operation=confirm)，传当前 candidate_hash，明确 text_checked、存在封面时 images_checked、存在音频时 audio_listened。试听事务更新阅读 listening_confirmed/fixed asset+version、正文 hash、编辑号、冻结候选 hash、collection_hash 和通过记录，且保存精确委托回执。**每次成功后重新 GET sampling/items，再提交下一次或批量操作，不复用旧 hash。** preparation_hash 排除合法版本确认状态变化，但素材、时长、SHA、审核或目标改变会拒绝后续 bulk/complete。

样本全部通过后，operation=bulk-confirm 传新 collection_hash、accept_normal_results=true 和服务端范围；其后 operation=complete。异常草稿可用 allow_abnormal_drafts=true 留下，合格子集另行 publication-preview/confirm。抽样数仍是 N=0→0，其他 min(N,20,max(1,ceil(N*0.05)))。原始发布正文与资源校验保持严格，抽样不能跳过资源验证。

任务历史保留在 tasks；WorkTask.current 只标记当前 revision、最新同阶段/同句、且实际输入依赖仍有效的记录。ASR/AI 使用真实候选 lineage，切片使用源 SHA、固定版本、英文、边界及编码配置。当前成功覆盖历史失败，历史 FAILED 不永久阻断已修正条目。

## 本批关联用量

GET usage 返回 WorkOrderUsage，provenance 固定 associated_stage_records：通过当前工作单真实关联阶段读取 DISTINCT content_cloud_attempt，重复关联同一 attempt 只返回一次。它表示这些关联记录的供应商尝试，不承诺都是该工作单创建后新增消耗；历史补关联/缓存复用不得展示为再次收费。

UsageAttempt 保留 attempt_id/stage_id、attempt_number、status、created_at、model；kind 为 AI_TOKENS/ASR_MILLISECONDS。reserved_units 是该尝试的原预留量，actual_units 是实际结算值，input_tokens/output_tokens/measured_audio_ms 来自真实用量证据。缺测为 null，不显示零；音频测得时长与供应商实际结算可不同，不能互相冒充。unknown_count 单列 UNKNOWN，retry_count 统计 attempt_number>1 的真实尝试；cost 始终 null，因为没有已确认费率。GET 不调用供应商，UI 不从全局 quota summary 伪造本批消耗。

冻结 OpenAPI：`docs/contracts/v15-openapi.json`；实际挂载路由表：`docs/contracts/v15-routes.md`。类型以导出 schema 为准。
