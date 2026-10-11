# V1.5 冻结发布范围

## 接入

新 `content/publication_ranges.py` 提供 PublicationRangeService(sessions, stages, inspect_candidate, publish_candidate)；`publication_ranges_router.py` 提供 create_publication_range_router。根对话装配真实候选检查与最终发布事务，不改既有内容store、runtime、迁移。

- POST `/api/v1/admin/content/publication-ranges` body candidates（1至1000）。每项 content_type/content_id/revision_id/expected_version/candidate_hash/warning_codes，短文还需 expected_edit_version。冻结排序集合、全部版本和警告代码，服务端逐项检查，返回 token/public_id、collection_hash、version、15分钟expires_at和持久results。
- GET `/{token}` 仅创建管理员可恢复，返回真实状态/版本/结果，no-store。
- POST `/{token}/confirm` body manual_confirmed=true、expected_version、collection_hash、selected_revision_ids（冻结子集且不重复），Header Idempotency-Key，返回202以及token/stage_id/selected_revision_ids。幂等结果先于TTL/CAS检查返回，异载荷409。
- stage名称 `PUBLICATION_RANGE_<请求摘要前12位>`，config_version `publication-range-v1`，item_id为token；worker见prefix调用 service.run(stage_id,owner)。确认、QUEUED阶段和 CONTENT_IMPORT_STAGE outbox同事务，沿用既有dispatcher。

## 回调契约

inspect_candidate(candidate)异步返回 ready必须为True，所有冻结候选字段的当前权威值（warning_codes默认为[]）；不能直接信任客户端ready/hash/version。预览和新确认分别检查，集合外候选拒绝。

publish_candidate(candidate,actor,key)异步返回持久结果。key为 `range-item:<token及完整候选摘要>`，同一token/候选跨stage重试和崩溃始终固定。回调必须先重放此key的成功结果，再在真实发布事务锁定正文/hash/版本/current draft及人工审批/能力/版权，失配拒绝。不能用此模块的前置检查代替最终事务检查。

## 执行保证

每项独立提交结果，成功项目不因其他失败回滚。worker在外部publisher的独立事务期间持有stage FOR UPDATE及range锁，cancel/reclaim等待；写进度前重新检查owner/fence/lease/input/config，旧lease不可写入。外部已成功但进度未提交时，下一worker仍调用同一item key以复用原结果。明确AppError记录FAILED；未知异常记录UNKNOWN，重试直接进入真实publisher的重放/最终事务核验，避免外部已提交造成旧预览检查挡住恢复。任何新收费调用均不由此模块发起。

预览到期阻止新的确认；已确认任务的离页/崩溃恢复可以继续。GET不伪造完成状态；stage无claim时返回claimed=false。部分失败状态PARTIAL可再确认剩余项，成功项跳过。

## 验证

- 3个unit：候选身份/双版本/集合摘要、真实ready与hash/warnings变更、1000上限。
- SQL文件 tests/integration/test_publication_ranges_sql.py：真实范围/阶段/outbox +明确fake publisher状态机；子集、人工确认、部分失败/成功复用/同键重放/TTL/CAS/本人隔离、取消阶段、过期lease不写进度并同key恢复。
- 同SQL文件单列真实 ShortReadingStore 1000篇无音频合法正文与版权fixture，ProofreadingStore实际冻结正常集合1000抽20；N0抽0、重放不换样本/集合摘要。只验证SQL及服务端集合，不代表云端或人工实际校对。
- 本地无SQL环境：3 passed/4 skipped；ruff/mypy通过。真实0018 MySQL、实际publish callback、端到端worker由根对话隔离runner接入验证，skip不计通过。
