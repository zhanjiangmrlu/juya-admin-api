# V1.5 AI／ASR 调用计量与硬预算

`CandidateService` 新增 `content_ai_daily_token_limit=0`、`content_ai_max_output_tokens=4096`、`asr_daily_seconds_limit=0`、`max_asr_polls=60` 和可注入 `usage=CloudUsageStore()`。零日预算默认关闭新收费请求；既有已提交 ASR 的结果查询可继续。预算日期为北京时间。Root 配置与 runtime 负责装配，迁移生成器须合并 `juya_admin_api.modules.cloud_usage.models.metadata` 的两张新表。

`content_cloud_budget` 以 kind＋period 为主键，原子条件 UPDATE 防止并发超额；`content_cloud_attempt` 的 stage_id 唯一，关联持久收费尝试、预占量、真实返回用量、供应商标识、价格版本／地域／币种与结果。AI 预占完整 UTF-8 输入字节（保守 token 上界）、角色模板余量及明确 max_tokens 输出上限；ASR 仅允许实际已解码、已确认、安全通过的固定音频，按真实毫秒时长向上取整至秒预占。预算与 SUBMITTING、attempt 在同一事务提交，提交事务成功后才收费。

预占保守保留，失败与 UNKNOWN 不释放以免低估收费。AI `suggest` 返回独立 `ContentAIResult(candidates,usage,request_id)`，移除共享可变 last_usage；每个响应校验 prompt／completion／total_tokens 与输出上限，再同事务保存候选、用量与阶段成功。用量不是根据候选文字长度猜测。

供应商用量异常高于预占时，账本保守补记差额并标记 reservation_overrun，后续调用仍受已消耗硬预算约束。已记录 SUCCEEDED 的收费事实不会被后续旧 UNKNOWN 或重复回执覆写；provider／model／config_version 保存在既有 usage JSON，没有追加 SQL 列。

ASR 已提交任务持久保存 provider_task_id、poll_stage_id 和 usage_attempt_id。官方异步 output.result 与旧 results 单项形式均支持；计量读取响应顶层 usage.seconds 和 request_id，实际音频测量另存 measured_audio_duration_ms。未返回供应商计量时 actual_units 为 NULL，不能把测量值冒充账单。价格版本默认为 UNVERIFIED，本次不计算或声称实际费用。

所有 worker 写候选与收费事实均核验 claim 状态、lease owner、fencing_version、有效期、input_hash 与 config_version。错误分支也必须携带 claim；过期 worker 不能覆写后继状态。过期 SUBMITTING／UNKNOWN 及唯一 paid attempt 拒绝再次收费，不因重投或点击自动重试。ASR poll_count 每次查询前持久化，默认至多60次；查询临时失败仅重查既有供应商任务，不再次 submit。已明确失败的任务不自动重查；耗尽查询上限转 UNKNOWN 并人工核对。

MySQL 并发测试覆盖最后一份预算原子预占、相同 paid stage 无法重复调用、UNKNOWN 保留占额和旧 lease 无法落库。候选集成 fixture 显式打开有限预算并提供每次响应 usage，不使用真实云凭据或调用。真实供应商 token／秒数、账单、账户额度及审核仍待生产验收。

官方核验日期2026-10-11：[Qwen Completions](https://help.aliyun.com/en/model-studio/completions)、[Qwen ASR异步接口](https://help.aliyun.com/en/model-studio/qwen-asr-api-reference)、[模型费用](https://help.aliyun.com/en/model-studio/model-pricing)。ASR result URL 必须遵循严格 HTTPS 供应商允许主机配置；不为兼容示例 HTTP 链接放开任意取址。
