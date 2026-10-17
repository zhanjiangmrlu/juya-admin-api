# 导入管理操作真实 SQL 回归

`tests/integration/test_v15_import_admin_operations.py` 使用迁移后的私有 MySQL、实际生产服务工厂、真实 ZIP/DOCX/XLSX、PNG 和一秒 PCM WAV。归档读取、SHA-256、图片解码和 FFprobe 均执行真实代码；管理员权限依赖为测试夹具，云接口禁止调用。

- 正式 ZIP 校验后签发素材预览：凭据最多 60 秒，管理员与批次隔离，实际响应字节和 SHA 一致；HTTP `no-store`，实际 Range 206 与越界 416。
- 错误路径、Office 文档、过期令牌和归档原文件篡改被拒绝。预览仅返回 `ADMIN_PREVIEW_ONLY`，不产生素材审核或人工听审结论。
- VALIDATE 实际取得租约后失败，手动重试检查当前输入、配置和 fence，拒绝取消批次及原文件变化。恢复使用相同 stage ID、新 fence、QUEUED 租约和一条待派发 outbox；同幂等键重放不重复派发。
- 旧 claim 无法完成，实际重新解析 DOCX 成功，成功阶段拒绝新的重试；再次执行不会增加 attempt。

执行：`uv run pytest tests/integration/test_v15_import_admin_operations.py`，需要显式测试数据库与测试进程可用的 FFprobe。没有测试数据库时跳过，不能视为 SQL 验收通过。真实 OSS、云审核、生产权限部署与人工听审仍属于外部验收。
