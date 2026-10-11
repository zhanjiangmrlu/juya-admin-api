# V1.5 内容校对集合

`ProofreadingStore(sessions, facts=None)` 使用共享 `proofreading_set` 与 `proofreading_confirmation` 表。`snapshot_hash` 为 sort_keys、紧凑 JSON、UTF-8 中文原样编码的 SHA256，与导入 canonical_hash 一致；`collection_hash` 绑定版本 ID 与该版本完整内容 hash。

`create(candidates, actor, set_id=None)` 冻结每项 content_type、content_id、revision_id、candidate_hash 和后端 group_id（默认同模板一组）。重放相同 set_id 不重新随机或替换候选；重放仍验证当前草稿指针及 hash。管理员普通样本不能自选，服务器使用 SystemRandom；N=0 时0项，否则 min(N,20,max(1,ceil(0.05N)))。`append_samples` 仅可追加冻结集合已有版本，不能替换原样本。

`confirm(set_id, revision_id, candidate_hash, actor, checks)` 逐项校对；必须勾选 text_checked，有实际图片／音频引用时分别要求 images_checked／audio_listened。人工勾选不能替代 SQL 素材事实。默认验证媒体 CONFIRMED／安全 PASSED／固定 object_key、音频版本 CONFIRMED、场景义项确认及已确认单句切片；短文使用纯阅读发布字段与音频绑定校验。生产装配建议注入 `facts(session,content_type,revision_id,snapshot)->{ready,has_audio,has_images,...}` 合并完整 production publish checks，避免在校对模块依赖 ProductionStore。

`fail` 保存失败错误及报告版本，并撤销同组全部版本的确认（包括这些版本其他集合内旧确认），要求逐项重新检查。`complete` 不将抽样结果自动推广至整组：每个冻结候选必须有独立人工确认，当前 hash 不变且没有未处理失败。失败／检查事实保存在 candidates JSON 的 metadata，版本身份及 collection_hash 不变。所有写入与 audit_event 同一事务。

发布事务调用 `is_confirmed_within(session,revision_id,candidate_hash)`；草稿变化事务调用 `invalidate_within(session,revision_id)`。hash 变化也会自然拒绝旧确认，更新草稿不能沿用旧校对。`create_proofreading_router(store, *, current_admin, current_admin_write)` 提供创建、查询、追加样本、逐项确认／失败、整组完成，写权限使用既有 CSRF 管理依赖。路由不允许客户端指定普通样本或 group_id。

本地单元验证抽样边界和 hash 顺序稳定；`tests/integration/test_v15_proofreading_sql.py` 在隔离迁移 MySQL 验证重放不漂移、追加不替换、检查缺失拒绝、整组不自动通过、同类失败撤销确认和修改 hash 拒绝。人工试听、图片内容正确性及文义检查必须由管理员实际操作，测试不能代替这些验收。
