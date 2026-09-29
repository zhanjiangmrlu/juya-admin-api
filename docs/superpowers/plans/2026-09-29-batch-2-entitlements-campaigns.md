# 第二批权益与活动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 A05–A12 的统一权益查询、权益详情、内容包查询以及活动草稿、版本、容量和生命周期管理。

**Architecture:** 保留现有正式权益、限时权益命令状态机，在 repository 增加分页查询与详情视图；活动模块新增 SQL repository 和管理路由。前端沿用 `composable → adapter → ApiClient`，以服务端返回的 `available_operations` 驱动按钮。

**Tech Stack:** FastAPI、SQLAlchemy async、Alembic、MySQL、Vue 3、TypeScript、Element Plus、Vitest、Playwright。

**Spec:** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`

## Global Constraints

- 列表统一返回 `items/page/page_size/total`，筛选值使用白名单。
- 写命令要求 CSRF；可重试命令要求 `X-Idempotency-Key`。
- 活动首次成功开通后锁定时长、场景顺序和启动窗口。
- 容量不得低于累计开通人数；版本更新使用 `expected_version`，冲突返回 409。
- 三个仓库均在当前 `main` 开发并使用中文 Conventional Commit。

## Review Focus

- 同一用户同时存在正式与限时权益时，统一列表排序和类型字段必须稳定。
- 过期、暂停、撤销权益只能返回服务端当前允许的操作。
- 已锁定活动不得通过编辑或复制绕过不可变字段。
- 并发调整容量和开通时不得超卖。
- 409 后前端保留管理员输入并展示服务端最新版本。

---

### Task 1: 权益与活动查询迁移和 SQL 视图

**Files:**
- Create: `migrations/versions/0010_entitlement_campaign_admin.py`
- Modify: `src/juya_admin_api/modules/formal_entitlements/repository.py`
- Modify: `src/juya_admin_api/modules/limited_entitlements/repository.py`
- Create: `src/juya_admin_api/modules/campaigns/repository.py`
- Test: `tests/integration/test_entitlement_campaign_queries.py`

**Interfaces:**
- Produces: `list_entitlements(filters, page, page_size)`, `get_formal(id)`, `get_limited(id)`, `list_packages()`, `CampaignRepository.list/get/save/command`。
- Consumes: 现有 entitlement/campaign 表和领域状态机。

- [ ] **Step 1: 写分页、筛选、详情、乐观锁和并发容量失败测试。**
- [ ] **Step 2: 运行 `uv run pytest tests/integration/test_entitlement_campaign_queries.py -q`，确认因查询接口缺失失败。**
- [ ] **Step 3: 增加必要索引和活动版本审计字段，实现 SQL 查询与行锁。**
- [ ] **Step 4: 重跑聚焦测试并执行迁移升降级测试。**
- [ ] **Step 5: 提交 `feat: 增加权益活动管理查询`。**

### Task 2: 管理端权益与活动 API

**Files:**
- Modify: `src/juya_admin_api/modules/formal_entitlements/router.py`
- Modify: `src/juya_admin_api/modules/limited_entitlements/router.py`
- Create: `src/juya_admin_api/modules/campaigns/router.py`
- Modify: `src/juya_admin_api/modules/campaigns/service.py`
- Modify: `src/juya_admin_api/infrastructure/runtime.py`
- Test: `tests/contract/test_admin_route_inventory.py`
- Test: `tests/e2e/test_admin_entitlement_campaign_flow.py`

**Interfaces:**
- Produces: `/api/v1/admin/entitlements`、正式/限时权益详情、`/content-packages`、`/campaigns`、`/campaigns/{id}`、版本复制及活动生命周期/容量命令。
- Consumes: Task 1 repositories，现有 CSRF、幂等和审计服务。

- [ ] **Step 1: 写路由清单、鉴权、状态冲突和审计失败测试。**
- [ ] **Step 2: 运行聚焦测试并确认缺少路由。**
- [ ] **Step 3: 实现响应模型、查询路由、命令路由和审计摘要。**
- [ ] **Step 4: 更新 OpenAPI 快照源测试并运行 admin-api 全量门禁。**
- [ ] **Step 5: 提交 `feat: 完成权益活动管理接口`。**

### Task 3: A05–A12 前端真实接入

**Files:**
- Modify: `juya-admin/src/features/entitlements/*`
- Modify: `juya-admin/src/features/campaigns/*`
- Modify: `juya-admin/src/pages/entitlements/*.vue`
- Modify: `juya-admin/src/pages/campaigns/*.vue`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.ts`
- Modify: `juya-admin/tests/e2e/fixtures/admin-api.ts`
- Create: `juya-admin/tests/e2e/entitlements-campaigns.spec.ts`

**Interfaces:**
- Produces: 权益列表/详情 controller、活动列表/编辑/版本 controller，第二批 capability 全部 `available`。
- Consumes: Task 2 OpenAPI 契约。

- [ ] **Step 1: 先写 adapter 路径、分页、409 草稿保留和页面状态失败测试。**
- [ ] **Step 2: 运行相关 Vitest，确认仍显示待接入。**
- [ ] **Step 3: 更新快照与生成类型，实现 adapters/composables 并替换 A05–A12 占位。**
- [ ] **Step 4: 运行 Vitest、Playwright、双视口截图和 Impeccable detector。**
- [ ] **Step 5: 提交 `feat: 完成权益活动后台页面`。**

