# V1.5 录音审核开关与短文排序

对应完整需求独立审核开关（112、346）、用户录音仅会话内不上传（345）及短文编辑运营排序要求。本服务只提供权威开关，不保存用户录音。管理端现有集成说明未证明这两个接口已完成页面联调，新增代码不能替代真实 AppID 审核或设备验收。

root 装配入口：

```python
store = LearningFeaturesStore(sessions)
router = create_learning_features_router(
    store, current_admin=current_admin, current_admin_write=current_admin_write,
    current_service=current_service,
)
```

管理 GET /api/v1/admin/learning-features 返回 recording_enabled / version；PUT 同路径要求 CSRF 写依赖及 Idempotency-Key，请求 recording_enabled 严格布尔、expected_version。system_config 固定键 learning_features_v15 缺失、格式异常或非布尔 true 时默认关闭，首次版本0；首次写入版本1，后续 CAS 更新。同键同载荷先返回原回执，不覆盖后来配置；异载荷409。配置、审计、回执同事务。内部可信 HMAC POST /internal/v1/learning-features body={}，仅 juya-miniapp-api 可读，拒绝附带客户端开关，响应 no-store。

SceneDetail 新 recording_enabled 默认 false；SQLContentLoader 在提供的 SQL session 中读取当前权威配置并持有 shared lock，动态能力不进入已发布文本 snapshot。旧调用者未装配配置时保持关闭。root 必须装配内部 HMAC 依赖和路由，BFF/客户端须消费服务器字段；本模块不会使用客户端请求或运行时环境变量假定开关已开启。

短文新增 POST /api/v1/admin/content/short-readings/{id}/sort-order，body expected_version / sort_order，排序为有符号 INT（含负数），要求 CSRF、幂等键，复用短文命令 CAS、module→content 锁序与 audit。0018 由 root 添加 short_reading.sort_order INT NOT NULL DEFAULT 0。管理、内部列表统一 sort_order ASC / public_id ASC；排序不改正文、hash、publication revision 或内容版本号，运营 CAS version 加1。旧数据默认0仍稳定排序。

精确真实 SQL 测试：

```powershell
uv run pytest tests/integration/test_v15_learning_features_sorting.py::test_sql_recording_cas_replay_and_short_reading_metadata_ordering -q
```

需要 JUYA_TEST_DATABASE_URL 指向完成0018的隔离数据库。测试使用真实 store/loader/authorizer，验证缺省关闭、CAS/replay/config反转动态读取、排序改变列表顺序且不改变snapshot/hash、安全摘要无正文/译文/audio；外层事务及所有 savepoint 最终 rollback。当前本地 unit 通过，SQL 无数据库而跳过，root 私有 runner 提供实际结果。
