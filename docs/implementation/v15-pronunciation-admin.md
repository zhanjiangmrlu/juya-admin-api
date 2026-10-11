# V1.5 发音管理与历史资源回填

挂载 `create_pronunciation_admin_router(store, *, current_admin, current_admin_write)`。
`PronunciationAdminStore(sessions, config, *, sign_asset, verify_asset)` 使用现有表，无新增配置。
`verify_asset(asset_id)` 必须读取真实固定对象 HEAD，并核对记录中的 SHA、长度和实际音频事实，失败须抛错；
`sign_asset(asset_id, ttl)` 只为该固定对象签名，严格遵守传入 60 秒有效期。

- GET `/api/v1/admin/pronunciations/targets/{target_id}`：当前配置缓存、任务状态和按次计量。
  白名单投影不包含对象路径、音频字节、提供商 request ID、内部 config 或凭据。
- POST `/targets/{target_id}/preview`，body `{resource_version}`：固定上传优先；
  校验 WORD/CHUNK 类型、真实 audio_version 和已审核媒体，真实 HEAD 通过后才签名。
  该请求不合成、不接受文本、不自动认定人工试听；响应 `no-store`、TTL 60 秒。
- POST `/tasks/{task_id}/retry`：仅恢复 RECOVER 中已留存的音频本地上传/缓存阶段，
  拒绝 UNKNOWN、FAILED、有效租约和无留存音频，不增加收费尝试。

历史收藏回填保留每来源身份和历史访问策略。资源字段只能取收藏时保存的快照，
或精确归属的归档版本中唯一稳定 entry_id 对应条目；不按英文匹配当前词库。
图片、发音目标、资源版本实际写入 favorite_snapshot_revision；无法确定的值保持 NULL，
明确记入 missing_fields。发音目标与版本从同一历史绑定读取，禁止跨快照拼配。
不伪造 pronunciation_target，不把旧 audio_target 当作受控发音目标。

候选 GET 新增顶层 revision_id、kind、实际 stage_id/stage_kind/stage_status/config_hash，
异步 ASR 状态使用实际轮询阶段而非原提交阶段。

本地验证使用 mock signer/provider。真实云调用、供应商计费、内容审核与人工试听验收仍需实际环境验证。
