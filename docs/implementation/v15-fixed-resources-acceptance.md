# V1.5 固定资源真实 SQL 验收

精确用例：`tests/integration/test_v15_fixed_resources_flow.py::test_sql_fixed_current_and_favorite_resources_respect_live_and_saved_authority`。

隔离 MySQL 已完成全部迁移后设置 `JUYA_TEST_DATABASE_URL`，执行：

```powershell
uv run pytest tests/integration/test_v15_fixed_resources_flow.py::test_sql_fixed_current_and_favorite_resources_respect_live_and_saved_authority -q
```

测试使用真实 SQLResourceSigner.__call__、SQLAuthorizer、SQLFavoriteAuthority，并创建 schema2 已发布词卡、短文、ACTIVE 用户、typed package 正式权益、短文试看及固定上传 pronunciation target。发布数据由 SQL fixture 固定，用例验证资源授予，不替代生产人工发布流程验收。收藏 snapshot SQL fixture 表示 BFF 已保存的权威事实，不是管理员伪造 exists 的线上实现。

外部 OSS 是唯一替身：HEAD 返回固定 bytes 对应 SHA、size、MIME，签名 URL 带真实数值 expiry；不会访问云或产生付费请求。覆盖：

- 当前正式授权词图、上传 WORD pronunciation 与短文试看 whole audio；无正式权益用户可通过试看访问短文。
- 正式权益剩余 23 秒限制签名；HEAD 耗时 11 秒后 60 秒上限收缩至 49 秒；实际 SHA 改变拒绝签名。
- 上传 pronunciation 不接受 whole audio 的版本；关闭试看后不再签 whole。
- 撤销当前权益、内容下线及模块关闭后，本人有效收藏仍可签固定词图及 pronunciation；WHOLE、SENTENCE、LEARNING_IMAGE 禁止。
- 他人收藏、SUSPENDED 账号、取消收藏及未知历史 revision/version 均拒绝；不会从当前内容猜配旧资源。

全部 SQL fixture 与模块开关处于一个连接事务，最终 rollback，包括异常分支；不会按条件删除其他数据。每个 public ID 使用 new_ulid(now)。本地 ruff 通过，pytest 可收集但因缺少隔离数据库跳过，真实 MySQL 结果由 root 私有 runner 记录。内部 short-reading 列表的无正文摘要已由 root 独立接入，本用例保持同事务资源验收边界，没有额外跨连接列表请求。
