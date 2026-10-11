# V1.5 运营规则与 OCR 退役交付

分支 `feature/v1.5-20261011`。仅改分配的 admin-api 运营与旧媒体任务范围；未改环境文件、迁移、runtime 或内容生产文件，未调用云端或重启服务。

## 实现

- 活动详情容量 nullable；新活动默认不限，省略启动窗口取 duration_days。更新 capacity 省略保留原值，显式 null 清除上限，0/负数拒绝；数字不得低于累计唯一成功人数。SQL 与内存授予均支持不限，重试不重复占名额，撤销不释放累计数。
- 正式预览 `POST /api/v1/admin/formal-entitlements/preview-operation` 返回 `before`, `after`, `actual_operation`, `expected_version`, `previewed_at`。不存在权益版本0；已到期展示 EXPIRED；有效权益再次开通实际 RENEW，转永久为 CONVERT_PERMANENT。执行请求新增 expected_version（HTTP必须提供），锁定后核对版本；同键成功重试优先返回保存的 after_summary，不返回后续修改的新权益、不重复顺延。暂停/恢复/撤销原因必填。
- 限时新增 `POST /api/v1/admin/limited-entitlements/{id}/preview-operation`，body operation 为 PAUSE/RESUME/REVOKE/REMEDY，补救带 mode；同样返回前后、实际操作与版本。原 pause/resume/revoke 命令均要求 reason+expected_version；remedy 增加上述字段。执行在仓储 replay 后核对版本；补救 mode 与版本纳入幂等摘要，避免同键不同补救被误重放。
- 反馈回复/状态/SLA/outbox既有SQL事务已核对，不重写状态机；定向测试覆盖两轮补充、一次重开、SLA暂停恢复、公开回复投影。
- 匿名统计新增 SHORT_READING_STARTED/COMPLETED 事件，独立 SHORT_READING_STARTS/COMPLETIONS 指标及 short_reading:<id> 维度；不计入场景完成。
- 注销清理先锁定用户快照公开ID，删除本人 reference/projection，再删除快照（修订/旧映射cascade）和 short_reading_progress，与既有清理结果和回调同事务。内存实现同步覆盖。
- 删除旧 OCR Celery 队列/路由、task registration、dispatcher enqueue、worker run、生产供应商/额度装配与批次 OCR callback；保留只读历史候选存储结构与类型。旧 whole TTS task 返回 TTS_DISABLED，新受控词语TTS由 pronunciation 模块处理。系统配置隐藏 OCR 历史键并以410 OCR_RETIRED拒绝写入。

## 验证

- 定向 pytest：48 passed（运营、正式/限时权益、反馈、配置、统计及旧媒体剩余TTS行为）。新增 tests/unit/test_v15_operations.py 先验证旧行为失败，再实现。
- 定向 ruff 与 mypy 全部通过，未运行共享数据库测试。
- 新增 tests/integration/test_v15_operations.py，供主对话隔离MySQL runner执行：不限新建/默认窗口、遗漏与显式null更新、后续续期后旧成功结果重放、新键旧版本409。

## 集成边界

1. 根对话维护0017：limited_campaign_version.capacity nullable及CHECK必须允许null；新快照/保留/短文进度表需迁移后部署代码。
2. 管理前端需要消费新的预览对象并回传expected_version；限时resume现需JSON原因与版本，补救也需原因。
3. BFF删除快照前须同步清理reference/projection，或将公开favorite IDs交付清理流程；管理员不能从已删除快照反推出原本人关系。短文事件生产端须使用独立新事件名。
4. runtime/config、content OCR操作与OpenAPI、旧e2e OCR路径及环境文档由根对话删除/更新。旧队列部署时排空或终止未开始OCR任务，不自动采纳或发布旧候选；本轮未操作运行队列。
5. 能力开关、资源GC保护水位与新生产用量由集成/内容模块负责；本模块没有宣称云端、安全提供方、生产、设备或声音验收通过。
