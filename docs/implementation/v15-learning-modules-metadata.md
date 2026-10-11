# V1.5 学习模块名称、排序与真实 readiness

装配接口：LearningModulesStore(sessions, capabilities=module_capabilities)，create_module_admin_router(store, current_admin=..., current_admin_write=...)。root 注入已实现能力：scene_learning / short_reading 为 true，long_sentence / grammar 为 false。缺省能力为 false，客户端不能提交 ready、reason 或 entry_path。

GET /api/v1/admin/content/learning-modules 返回 items，其中每项包含 module_type、display_name、entry_path、sort_order、enabled、config_version、ready、reason。固定四类模块，排序 sort_order ASC / module_type ASC。ready 只依据当前部署实际 capabilities；reason 为未实现时 MODULE_NOT_IMPLEMENTED，否则 null。数据库 enabled 与 readiness 单独呈现，readiness 不伪造为已实现。

PUT /api/v1/admin/content/learning-modules/{module_type} 要求管理员写依赖（现有 CSRF）、Idempotency-Key，body 为 expected_version>=1、display_name 1..80、sort_order 0..65535、enabled 严格布尔。空白名称拒绝，持久名称去首尾空格。历史 learning_module_config.sort_order 为 SMALLINT UNSIGNED，所以不接受负数或超范围整数；短文独立 INT 排序仍另用 int32。

先读取幂等回执，同载荷已提交回执先返回；异载荷同 key 拒绝。真实 readiness=false 时无法新开启模块。修改按 discovery_config_state FOR UPDATE → learning_module_config FOR UPDATE 锁序，校验 module.config_version CAS；成功 module.config_version+1、全局 discovery.version+1、audit 与回执同事务。原有 discovery 的全局 CAS 可检测此修改，防止旧页面覆盖配置。entry_path 保留服务端固定值。既有可信内部 discovery 已从同表读名称与排序，本次不新增重复内部通道。

精确真实 SQL 回归：

```powershell
uv run pytest tests/integration/test_v15_learning_modules_sql.py::test_sql_module_metadata_readiness_cas_global_version_and_replay -q
```

需要 JUYA_TEST_DATABASE_URL 指向完成迁移的私有 MySQL。验证四类 readiness、模块CAS、开启未实现模块拒绝、全局版本+2/audit2、重放不覆盖后来关闭状态；所有配置及回执在外层事务/savepoint中最终rollback。本地unit/ruff/mypy通过；SQL无隔离数据库时跳过，由root私有runner记录真实结果。此代码不等于管理端页面联调、未实现长难句/语法模块或真实微信审核已完成。
