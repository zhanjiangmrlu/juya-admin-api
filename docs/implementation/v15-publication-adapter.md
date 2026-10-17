# 冻结范围发布生产适配器

`build_publication_service(sessions, stages, production, content, readings)` 位于 `modules/content/publication_adapter.py`，返回 `PublicationRangeService`。由运行时统一注入真实 `ContentService`、`ProductionStore`、`ShortReadingStore` 与同一数据库会话工厂，不自行创建云服务。

场景候选的 `expected_version` 是实际草稿编辑版本，`candidate_hash` 是实际内容快照的 `snapshot_hash`；类型同时核对快照与服务端模板，旧 `vocabulary` 模板按公开契约映射为 `word_card`。发布警告和就绪状态来自现有完整发布检查。短文候选的 `expected_version` 是内容父记录版本，`expected_edit_version` 是草稿编辑版本，哈希同时校验真实快照和持久 `content_hash`，发布就绪状态来自现有短文素材、人工听审和校对检查。

发布首先读取相同业务幂等键的持久回执，再执行当前草稿检查。场景保留 `ContentService.validate_publish`，最终调用 `ProductionStore.publish(..., expected_candidate_hash=...)`；同一发布事务锁定当前草稿、核对编辑版本与冻结快照哈希，再执行既有素材、词条引用和校对检查。短文使用现有 `command('publish',...)`，其原始回执优先于父记录 CAS，最终发布事务额外核对当前草稿、编辑版本及实际快照哈希。成功发布后响应丢失，范围结果为 UNKNOWN，重试仍可从原业务回执恢复。

`tests/integration/test_publication_adapter_sql.py` 使用真实私有 MySQL、生产服务工厂和真实持久校对确认，覆盖词卡与短文：正常发布提交后响应丢失的 UNKNOWN 恢复、预览后实际内容变化拒绝、无版本变化的最终快照哈希围栏及旧短文草稿拒绝。测试不调用供应商、不代表真实内容版权或人工审阅验收。

逐项业务幂等键为带 `publication-range-item` 用途标记的完整 SHA-256 摘要，固定 64 字符，适配真实审计 `request_id` 字段。2026-10-11 独立 MySQL/Redis 运行器实际执行上述参数测试，4 passed in 3.17s；范围状态机单元测试 3 passed，适配器和范围服务 mypy、Ruff 均通过。
