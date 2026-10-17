# V1.5 导入阶段持久化执行

新增 content_import/stages.py 的 StageStore。唯一输入身份为 item/stage/input_hash/config_version；ensure_within 保留同身份历史结果并取消不同身份的运行中旧阶段。调用方应在同事务先锁导入条目、校验条目未取消，再ensure，避免新输入与条目取消并发越界。

claim 原子更新抢占阶段，递增 fencing_version 与 attempt、清空旧输出并登记owner和租约。finish/fail必须同时满足运行状态、owner、fence、hash/config以及未过期租约，否则409 IMPORT_STAGE_LEASE_LOST。失败可进入30秒后可重试状态；终态与取消阶段不盲重开。cancel_item_within 与导入条目更新共享调用方事务。

enqueue 仅接受到期PENDING/RETRY或租约过期RUNNING，原子切换QUEUED与写入admin_outbox CONTENT_IMPORT_STAGE事件（payload stage_id）。并发调度只产生一个事件；claim可领取QUEUED阶段。outbox消费方须按此事件单独派发，不能发往小程序业务投影。QUEUED由持久化outbox负责重投，due扫描PENDING/到期RETRY/过期RUNNING。取消后的旧事件claim返回None。没有付费供应商调用。

迁移建议索引：content_import_stage(status,next_retry_at,lease_until) 及 (item_id,stage,status)。本任务未改迁移。

SQL并发测试 tests/integration/test_v15_import_stage_concurrency.py 覆盖6路唯一ensure、enqueue、claim、单一outbox、过期回写与旧fence拒绝、延迟重试、hash/config变化隔离、取消不可重开。需隔离MySQL迁移到head后执行。本环境无JUYA_TEST_DATABASE_URL：测试1 skipped，不宣称SQL并发验收通过。源文件mypy和定向ruff通过。
