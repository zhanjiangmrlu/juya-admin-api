# V1.5 独立素材 ZIP 与人工绑定

入口为 `/api/v1/admin/content-materials`。上传需 series_id、content_type、Idempotency-Key；返回 202，原始文件持久化后通过 CONTENT_IMPORT_STAGE outbox 派发 MATERIAL_VALIDATE。HTTP 不解压包。worker 使用受限 read_container(spool=True)，最终关闭磁盘 spool。

GET 批次返回路径、文件名、字节 SHA-256、适用角色和规范化标题候选。同名标题标 ambiguous_title，候选最多展示 5 个并声明总数。所有资源始终 requires_manual_selection；文件名、顺序与英文均不能自动确定绑定。预览是元数据检查，媒体解码和实际音频角色检查由 bind_media 完成。

POST /{batch_id}/bindings 需 revision_id、expected_version、manual_checked=true，以及 path/role/entry_id。词图与词音仅接受本草稿 vocabulary 的稳定 entry_id；块音仅接受 chunks 的稳定 entry_id；不接受块图、句切片或客户端提供英文来猜配。角色为 COVER_IMAGE、LEARNING_IMAGE、WORD_IMAGE、WHOLE_AUDIO、FULL_AUDIO、WORD_AUDIO、CHUNK_AUDIO。短文仅接受封面与全文音频。已发布不可变修订必须先创建当前草稿。

绑定同样返回 202，由 MATERIAL_BIND_<revision hash> 执行。相同请求 key 重放不重复派发，新 key 可显式重试失败绑定。worker 前后核验当前草稿版本、取消状态与阶段 fence/owner/未过期租约，在单一事务保存草稿、撤销校对证明、审计并完成阶段。词义、sense/source 与稳定 ID 保留；替换完整音频撤销句边界和切片，短文撤销听审确认。取消使旧 worker 无法提交；已准备但未采用的资源可能留待通常素材清理处理。

装配：LegacyMaterialService(runtime.imports, runtime.readings, invalidate_callback)，create_materials_router(service, current_admin=..., current_admin_write=...)。invalidate_callback 为 async(session, revision_id)，与既有草稿证明撤销逻辑一致。通用 worker 将 stage 名以 MATERIAL_ 开头的任务交给 service.run(stage_id, owner, now)。模块不调用付费供应商。

本地验证：tests/unit/content/test_v15_materials.py。隔离真实 SQL 验证：tests/integration/test_v15_materials_sql.py，由私有 runner 提供数据库，覆盖同名建议、同英文不同词义按 ID 采用、CAS、幂等、取消以及校对失效；测试仅本地伪媒体绑定。OSS、真实解码与人工听审属于外部验收。
