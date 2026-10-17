# V1.5 独立短文后端

独立 short_reading / short_reading_revision 保存纯阅读正文。草稿允许缺字段；发布要求双语标题、分类、主题、级别、安全简介、非空英文自然段、全文中文译文、版权来源与版权确认、固定候选 hash 的校对及单独人工发布确认。自然段有稳定 ID，非法重复或长度不匹配拒绝。没有词汇、语块、句子、候选或 TTS 字段。

可选整篇上传音频必须引用真实 CONFIRMED 媒体与 ACTIVE/CONFIRMED 固定 audio_version；素材类型、审核、时长、目标及 asset_id 一致，试听确认必须对应同一音频版本和素材。无音频不影响发布。封面如提供也核对素材类型及审核。

## 装配

- ShortReadingStore(sessions, check_confirmation=callback, require_review=True)。callback(session, revision_id, content_hash) 返回可信服务端固定校对集合确认结果；未装配即禁止发布。
- create_within(session, series_id, content, actor_id, identity=None) 与外部导入映射使用同一事务。返回 content_id / revision_id / version。
- create_short_reading_router(store, current_admin=..., current_admin_write=...) 注册 /api/v1/admin/content/short-readings。写依赖必须包括管理员与 CSRF；写命令使用 Idempotency-Key，发布/下线/试读/新修订使用 expected_version，编辑另带 expected_edit_version。
- create_short_reading_internal_router(store, current_context=..., authorizer=..., resource_signer=...) 注册 /internal/v1/short-readings。上下文必须由签名认证产生。authorizer(session, identity, context) 核对当前模块、当前发布、当前账号、正式包或独立试读。PREVIEW 用途仅可授权安全白名单摘要。resource_signer(session, target_id, resource_version, context) 再核验媒体和权限并设置短 TTL；不得信任客户端任意资源。
- published_detail_within(session, identity) 返回 ShortReadingDetail；它是内容事实查询，调用者先做实时权限核对。
- associate_published_within(session, package_public_id, content_type, content_id, position) 仅关联对应类型已发布内容；不修改旧 content_package_scene。调用方在统一包命令事务内执行其幂等、CAS 和审计。

发布、下线、试读先锁 short_reading 模块再锁短文对象；审计和幂等回执均同事务提交。发布修订不可原地编辑，后续修改复制为新草稿。重放先返回已提交原回执，不重新检查已变化状态。

## 验证与边界

先写纯阅读边界及缺字段测试，确认未实现时导入失败，再实现。单元覆盖不完整草稿、纯阅读额外字段拒绝、版权门禁、校对 fail-closed 与固定 hash、幂等重放不读当前状态、预览用途不能越权打开正文或签名全文音频。隔离 MySQL 测试覆盖发布、同版详情、下线、重复发布回执、CAS 冲突和发布不可编辑。

本模块本地运行：ruff check、mypy 均通过；pytest 单元 6 通过。SQL 集成测试因当前未提供 JUYA_TEST_DATABASE_URL 跳过，由集成负责人在隔离迁移环境执行。真实版权、试听、OSS 和真机不由自动化假定完成。内部授权与资源签名必须由运行时装配后验收。

