# 两个后台项目本地收尾计划

> **For agentic workers:** 按 superpowers:test-driven-development 与 verification-before-completion 逐任务执行；独立任务并行，共享服务、数据库写入、云配置与 Git 提交由主对话统一处理。

**Goal:** 完成 juya-admin、juya-admin-api 可在本机实施和自动验收的剩余项，并留下准确的素材/外部依赖记录。

**Architecture:** 复用既有页面、内容草稿、OSS 上传与隔离 SQL/Redis 测试。只补齐现有收尾工具与真实验收，不引入新供应商或新生产系统。

**Tech Stack:** Vue 3、Vitest、Playwright、FastAPI、pytest、MySQL、Redis、阿里云 OSS V2 SDK。

**Spec:** `docs/implementation/v13-unified/acceptance.md` 与 `../juya-admin/docs/implementation/word-audio-repair.md`。用户于 2026-10-02 明确限定“完成本地开发与可自动执行的验收”。

## Global Constraints

- 使用两个仓库当前 main；中文提交；不推送、不部署生产，不修改小程序前端。
- 不启用此前明确关闭的 TTS 和云内容审核，不重复真实 OCR 调用。
- 正式教学草稿只能做可逆、可追溯更新；未听音的候选不绑定，版权/来源与时间确认不编造，不发布正式教学内容。
- OSS 仅操作显式绑定的私有 juya-test；保留原 CORS 全部规则，不访问生产 Bucket。
- 所有 SQL 破坏性测试使用本次独占的隔离数据库；不能重置主站库或其他对话库/Redis。
- 不将签名 URL、账号密码、token 或 AccessKey 写入日志和 Git。

## Review Focus

- 旧 CORS 虽允许 Content-Type，但缺少 Range 或响应头时必须识别为不完整；补全不修改原规则。
- 同一数据库不可同时执行迁移回退和运营分页数据准备。
- 未提供源版权和实际听音结果时，不能将缺素材条目标记已验收。
- 关闭供应商审核是已确认配置，不能误当代码缺失并触发付费调用。
- 主站 readiness 和受控浏览器回归不能替代真实上传/下载与用户听音验收。

## Task 1: 本地 OSS CORS 收尾

**Files:** `scripts/oss-cors-audit.py`、`tests/live/test_oss_cors_plan.py`，结果保存在 `docs/implementation/local-closeout/evidence/`。

- [x] 对缺 Range、缺暴露头、localhost 来源、原规则保留、容量和幂等行为写测试，运行并观察 RED。
- [x] 最小修改规划器，补齐精确来源/方法/头，运行 GREEN。
- [x] 只读获取现状，记录写入 AccessDenied 和最小策略；用户明确要求跳过权限后不再申请或写入云配置。
- [x] 真实 OPTIONS 与只读浏览器 GET/HEAD 已执行，现有 5173 成功、localhost/18173 被阻；结果如实记录。
- [ ] **用户明确延期**：新规则云应用、Range/新来源浏览器 POST 正向验收及头像固定写入/读回，需外部补权。

## Task 2: 素材与音频交付

**Files:** `../juya-admin/docs/implementation/local-closeout-materials.md`；使用既有草稿保存接口。

- [x] 核实原图/音频/候选哈希与最新草稿版本，准备有依据的 6 句中文译文。
- [x] 可逆保存译文和 ASR 时间候选并回读；原固定引用保留，六句核对仍 false，设备预览保存后当前 DRAFT v10。
- [x] 提供 9 个候选试听入口与 2 项缺录音条目；用户确认前保持独立音频未绑定。
- [x] 记录版权来源、人工标时与实际 Chrome 听音仍需的信息。
- [ ] **人工输入待办**：授权/来源、逐句听音确认、候选听音及两项缺录音；不得代填确认或发布。

## Task 3: 隔离自动验收与统一交付

**Files:** `scripts/test-v13-isolated.py`、相关测试（仅当审计确认工具缺陷时修改）；`docs/implementation/local-closeout/acceptance.md`。

- [x] 验证独占数据库与 Redis 测试范围，迁移到 schema16；运营用例使用独立库。
- [x] 运行后台 check、294 单测、build、117 Chromium 业务/双视口回归。
- [x] 运行管理 API Ruff/format/mypy 与完整隔离 pytest，最终 388 passed / 4 skipped；真实主站运营 HTTP 通过，历史 Worker/Beat 实测保留为历史证据。
- [x] 核对主站 API/Worker/Beat 就绪，独立审查发现两项工具问题，均以 RED/GREEN 修复后全套复跑。
- [x] 写最新验收台账，明确每项已完成/外部依赖；按两个 main 仓库分别交付中文提交。

## 执行记录

- 2026-10-02：两个工作区起始干净；主站 5173 返回200，8000/8001 readiness 为 ready，管理API、两个Worker、Beat均healthy。
- 初次 CORS只读：5173 POST/GET/HEAD预检200；18173三项403；原规则只有Content-Type与ETag。
- 用户确认本轮仅本地收尾；生产交付从本轮范围移除。
- 实际 PutBucketCors 与头像 avatars/* PutObject 均 AccessDenied；用户要求先跳过权限。最小策略已保存，云 CORS 未改变。
- ASR 候选和六句译文保存后 v9；浏览器设备预览按现有流程保存为 v10，真实音频 duration 24.38、readyState 4、无 error，未替代人工听音。
- 独立审查：CORS 首条通配匹配遗漏、中断时 CREATE/归属登记和 DROP 清理窗口。首项 4 个 RED 后定向 26 passed；后项实际 SIGINT 三例与文案/进程组两例 RED 后定向 15 passed。
- 修复后管理 API 通用 387 passed / 4 skipped、运营独立 1 passed，总计 388 passed / 4 skipped；临时 UUID 库/Redis 清理，无共享资源重置。Ruff/format/mypy 通过。
- 额外 CORS 前端 18173 已停止，主站 5173/8000/8001 和 Worker/Beat 保持正常。
