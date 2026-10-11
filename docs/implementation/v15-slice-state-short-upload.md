# 切片刷新恢复与短文直接上传

## 当前句切片状态

新增 `GET /api/v1/admin/content/revisions/{revision_id}/sentences/{sentence_id}/slice-state?expected_version=N`，使用 authoring router 已有管理员读依赖、当前未发布草稿与版本校验，返回 `Cache-Control: no-store`。

公开字段包含 revision_id/sentence_id/edit_version、status、dependency_hash、stage_id/stage_status/error_code、asset_id/target_id/resource_version、asset_status/security_status/duration_ms、listening_confirmed。状态为 UNALIGNED、SOURCE_NOT_READY、PREPARING、FAILED、CANCELLED、REVIEW_PENDING、READY_TO_LISTEN、CONFIRMED。阶段和资源均从真实 SQL 查询当前句与依赖，不依赖浏览器组件临时 queue。

读取先验证当前原音频版本/hash与句边界，再查询阶段、切片与实际素材事实，最后二次读取当前正文/原音频依赖和素材审核状态。版本或依赖变化拒绝旧响应；阶段已成功但真实资源缺失返回失败。GET 不调用审核供应商、不签名、不更新草稿，也不自动确认听音。READY_TO_LISTEN 的真实文件继续通过既有 `slice-preview` 取受控60秒试听地址；只有显式 `slice-confirm(listened=true)` 才写入人工确认。

## 短文专用 FULL 上传目标

根集成挂载 `create_short_reading_upload_router(store, media, current_admin_write=..., current_admin=...)`。不需要新增表；复用实际 audio_target/audio_version/media_asset 和短文当前草稿。

新增 `POST /api/v1/admin/content/short-readings/{identity}/revisions/{revision_id}/audio-upload-target`：必填 `Idempotency-Key`，body 为 expected_version（短文版本）与 expected_edit_version（草稿编辑版本）。目标角色固定 FULL，稳定键由服务器绑定内容与当前草稿，返回与旧 AudioTarget DTO 一致的 id/stable_key/target_type/active_version_id，并附 content_id/revision_id/version/edit_version。创建不改正文与版本，不制造人工听音。并发相同请求复用同一目标/回执。

上传仍复用 `/api/v1/admin/media/upload-policies` → OSS 表单上传 → `/api/v1/admin/media/uploads/confirm`。这些机制验证实际字节并固定私有素材；临时上传凭据不进入专用目标的幂等回执。

新增同路径 `/bind` POST：body 为 expected_version、expected_edit_version、target_id、asset_id，同样要求 Idempotency-Key。校验目标确属当前短文草稿 FULL 角色、读取并解码真实媒体字节、复核 SHA/时长/审核事实，再在事务内复用已有 MANUAL 资源版本或创建 READY 版本并保存草稿；内容/编辑双版本按既有短文 save 命令推进。

绑定返回固定 ReadingAudio 引用，listening_confirmed=false、confirmed_asset_id/confirmed_version_id=null。不能仅因上传成功发布；管理员仍须实际试听并通过既有媒体确认与短文音频人工确认机制。成功回执优先于旧 CAS 和重复文件 I/O，换请求内容重用同一键拒绝。别篇/旧草稿目标不能借用，已发布修订不可原地绑定。

## 验证范围

新增接口单元先观察404/缺失模块失败再实现，通过公开路由验证版本/幂等头必填和no-store。真实 SQL 集成覆盖原 MediaService upload-policy、WAV上传/完整探测/冻结/SHA、FULL角色落库、CAS/同键并发、跨草稿拒绝、历史回执不重新读文件、真实版本复用及不制造听音。既有真实 FFmpeg authoring 流程增加跨刷新 PREPARING/REVIEW_PENDING/READY_TO_LISTEN/CONFIRMED 状态与审核拒绝验证。

OSS和审核仅为明确本地夹具；不代表云审核、真音频内容质量、版权或生产验收。私有 MySQL 最终运行与整体门禁由根任务执行。
