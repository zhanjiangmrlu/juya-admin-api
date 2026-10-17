# 独立资料补全入口开关

需求 6.3 的 `profile_completion_enabled` 与录音审核开关共同持久化在
`system_config.learning_features_v15`，但各自独立读写；不复用 `contact_prompt_eligible`，
不改变内容访问权限，不将此功能开关塞进 SceneDetail。

`POST /internal/v1/learning-features` 仍只接受空对象和已验证的 BFF 服务身份，返回：

```json
{"recording_enabled": false, "profile_completion_enabled": false, "version": 0}
```

两项字段均为严格布尔。缺失配置、错误 JSON 形状和错误字段类型默认 false；
按字段判断，保留另一个合法布尔值。接口响应 no-store，客户端不能从读取端设置开关。

管理员 GET/PUT `/api/v1/admin/learning-features` 返回两项完整值与版本。
PUT 保留旧契约：`recording_enabled` 和 `expected_version` 必填；
`profile_completion_enabled` 可省略，省略表示在配置行锁内保留当前资料开关。
显式 false 会关闭；显式 null、数值和字符串均拒绝。
请求序列化 exclude_unset，旧 recording-only 请求的幂等摘要保持不变。

更新使用 CAS、持久幂等回执与同事务审计。首先无锁查询已有回执；配置行锁后
再次锁定回执，再验证 expected_version。配置首次创建使用保持现有值的排他 upsert，
重复分支不改变已有 flag 或 version。旧 INSERT IGNORE 在真实三并发首次创建测试中触发
SELECT FOR UPDATE 的 1213 死锁；改用上述排他 upsert 后，同一严格断言通过，仅一次版本与审计。
100 字符幂等键完整存入原回执；超过 audit.request_id 的 64 字符上限时，审计存 SHA-256，
不截断、不改变客户端幂等身份。重放旧回执不修改当前配置；旧回执没有资料字段时
新响应 DTO 以 false 表示当时该功能未知/未启用。需要当前值时使用 GET。

本地与私有 SQL 回归覆盖旧请求兼容、三并发同键仅一次版本/审计、长键边界、CAS、
切换两项布尔、旧回执重放、缺失与混合坏字段。无新表、迁移或云调用。

验证：5 个模块单测、Ruff、5 source Mypy 通过；UUID 私有 MySQL/Redis 的 scoped general
18 项全部通过，含 profile 完整持久化回归及原录音/排序 SQL。
单独 ops 套件被该 scope 筛选为空，因此运行器整体退出码为 1，不将其表述为全量套件通过。
