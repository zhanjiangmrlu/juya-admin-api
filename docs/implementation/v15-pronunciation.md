# V1.5 受控发音接入

生产模块为 `modules/pronunciation`；表结构以 `models.metadata` 为准，由集成负责人的共享扩展迁移创建。目标仅能从管理员确认的 WORD／CHUNK 或合法收藏固定引用建立；不得为短文创建词语或全文目标。时间字段为 UTC epoch 秒，固定资源版本为字符串。

## 集成接口

- `SqlPronunciationRepository(async_sessionmaker)`：持久化目标、唯一 cache_key 任务、硬预算、尝试和缓存。
- `PronunciationService(repository, Config, authorizer, resources)`：`authorizer(target_id, context) -> bool` 必须由服务端重验账号、模块、内容版本及权益，或可信 BFF 签发的本人有效收藏上下文。`resources(target_id_or_cache_key, resource_version, context) -> dict` 必须再次核验目标停用／资源安全状态并签发短 TTL URL。返回 READY 资源或含 task_id 的 PREPARING／RETRY／FAILED／UNKNOWN。
- `create_router(service, trusted_bff_dependency)`：注册 `/internal/v1/pronunciations/{target_id}/resolve` 和 POST `/tasks/{task_id}`；统一采用带正文的 POST，避免 GET 请求漏签收藏上下文。依赖必须核验 HMAC、nonce、防重放及 BFF 调用方角色。客户端不能提交合成文本。
- `PronunciationWorker(repository, provider, oss, security, ffprobe_path).run(task_id)`：由独立 pronunciation queue 注册任务；`dispatch_pending(repository, async_dispatch)` 扫描数据库持久 outbox 和到期租约。队列派发后仍保留数据库记录，重复投递由事务 claim 和租约 fencing 合并。任务中断但无持久音频时转 UNKNOWN，禁止再次收费。
- `TencentTtsProvider(credentials_callable, optional_httpx_client)`：真实 TC3 签名 HTTP 请求；凭据由进程受控供应，不能回显或记录。默认关闭合成，价格版本未配置也拒绝收费。

`pronunciation_task.target_snapshot` 固定已确认文本、读音变体、语言和配置版本；任务不能读取后来改写的目标文本。缓存包含确认读音与所有声音参数，价格与启停开关不改变声音缓存键。已上传音频优先；已缓存音频在关闭新合成或预算不足时仍可解析。

## 预算与恢复

预算周期按北京时间日期 `YYYY-MM-DD` 建立 `budget_reservation` 行，金额使用人民币微元整数。管理员必须明确设置 character_limit、cost_limit 和 enabled；未配置无法收费。调用前原子条件 UPDATE 预占预算，尝试单独写入 cloud_usage_attempt。预占保守保留，包括失败及 UNKNOWN；实际字符由响应结果记录，供应商账单金额未核实前 actual_cost 保持 NULL，不把估算伪装成实账。

每个唯一任务至多三次收费尝试；仅供应商明确临时失败最多自动重试两次，指数退避。超时／不明确 HTTP／无响应转 UNKNOWN。SessionId 仅追踪，不假定供应商幂等。UNKNOWN 需人工确认供应商结果，不允许用户重复点击触发再次收费。

合成成功字节先持久化到数据库，再 FFprobe 检查、固定 OSS 对象、音频安全审核和事务缓存。上传或缓存落库失败只恢复本地阶段；已有 object_key 也会复用。BLOCKED 音频不会 READY。PENDING 审核冷却后恢复。源内容上下线不自动删除合法收藏词语资源；独立目标／资源停用必须由授权和取址层拒绝。

## 验证边界

本地测试覆盖声音配置／读音隔离、收费关闭复用缓存、签名请求格式、超时不重试、预算拒绝无云调用、最多两次自动重试、上传失败保留付费音频；MySQL 集成覆盖唯一任务并发、单租约、原子预算和过期任务 UNKNOWN。

真实账户第一次合成、第二次缓存、供应商账单字符／费用、OSS 私有分发、音频审核及人耳听读音、微信真机防叠音仍需外部验收。本次未调用云服务。

官方协议核验日期 2026-10-11：[TextToVoice](https://cloud.tencent.com/document/api/1073/37995)、[公共参数](https://cloud.tencent.cn/document/api/1073/37989)。英文请求最多500字母（含半角标点），适配器保守按 UTF-8 字节限制500，完整长文本不截断，应使用上传资源。
