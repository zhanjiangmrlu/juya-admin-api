# 第六批统计与全量验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成日/周/月匿名统计查询，复核 A01、A13、A25、A26 联动，并对 A01–A26 做全量接口、E2E、视觉和文档验收。

**Architecture:** 在现有 aggregate repository 上增加白名单统计查询，前端按 period 转换图表模型。最终以 OpenAPI 清单、Playwright 主链路和双视口页面清单证明全部需求内 capability 已可用。

**Tech Stack:** FastAPI、SQLAlchemy async、Vue 3、ECharts、Vitest、Playwright、Impeccable。

**Spec:** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`

## Global Constraints

- 统计只返回去标识化聚合，不接受用户 ID、微信号或个人轨迹维度。
- 比率数据包含分子、分母和口径；分母为零时 rate 为 null。
- 所有需求内 capability 最终为 `available`，源码页面不再出现“接口待接入”。
- 视觉验收覆盖 1440×900 与 1280×800。

## Review Focus

- 未知指标和个人标识维度必须被服务端与前端双重拒绝。
- 周/月边界按 Asia/Shanghai 口径稳定。
- 工作台和待办数量与新状态一致。
- 系统配置 409 保留本地草稿并展示远端版本。
- 页面不得通过删除提示文字来伪装能力完成。

---

### Task 1: 匿名统计查询 API

**Files:**
- Modify: `src/juya_admin_api/modules/analytics/service.py`
- Create: `src/juya_admin_api/modules/analytics/router.py`
- Modify: `src/juya_admin_api/infrastructure/runtime.py`
- Test: `tests/integration/test_analytics_period_queries.py`
- Test: `tests/contract/test_admin_route_inventory.py`

**Interfaces:**
- Produces: `GET /api/v1/admin/analytics?period=day|week|month&start=&end=`。
- Consumes: 现有匿名日聚合表和 export privacy rules。

- [ ] **Step 1: 写 period、时区、未知指标和分母为零失败测试。**
- [ ] **Step 2: 运行测试确认查询路由缺失。**
- [ ] **Step 3: 实现白名单查询、响应模型和路由。**
- [ ] **Step 4: 运行 admin-api 全量门禁并更新 OpenAPI。**
- [ ] **Step 5: 提交 `feat: 增加匿名汇总统计查询`。**

### Task 2: A01/A13/A25/A26 联动与 pending 清零

**Files:**
- Modify: `juya-admin/src/features/analytics/*`
- Modify: `juya-admin/src/pages/analytics/analytics-page.vue`
- Modify: `juya-admin/src/pages/dashboard/dashboard-page.vue`
- Modify: `juya-admin/src/pages/work-items/work-item-page.vue`
- Modify: `juya-admin/src/pages/settings/settings-page.vue`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.ts`
- Test: `juya-admin/src/shared/capabilities/capability-registry.spec.ts`

**Interfaces:**
- Produces: day/week/month chart controller 和所有 capability `available`。
- Consumes: Task 1 API 及前五批状态。

- [ ] **Step 1: 写统计切换、隐私拒绝、工作台联动和 pending=0 失败测试。**
- [ ] **Step 2: 运行测试确认 analytics.query 仍为 pending。**
- [ ] **Step 3: 接入真实统计并清理最后的占位引用。**
- [ ] **Step 4: 运行前端单元测试和构建。**
- [ ] **Step 5: 提交 `feat: 完成统计与后台能力联动`。**

### Task 3: A01–A26 全量验收和接口文档

**Files:**
- Modify: `juya-admin/tests/e2e/fixtures/admin-api.ts`
- Modify: `juya-admin/tests/visual/page-manifest.ts`
- Create: `juya-admin/tests/e2e/full-admin-flow.spec.ts`
- Modify: `docs/api/*.md`
- Modify: `D:/个人/juya/doc/接口文档/juya-admin-api-接口文档.md`

**Interfaces:**
- Produces: 完整质量门禁、26 页双视口证据、最终接口覆盖报告。
- Consumes: 前六批全部接口和页面。

- [ ] **Step 1: 写 OpenAPI 能力矩阵和 26 页无待接入失败测试。**
- [ ] **Step 2: 运行测试并修复真实遗漏，不允许只改文案。**
- [ ] **Step 3: 运行三个仓库完整门禁、关键真实 HTTP、Playwright E2E。**
- [ ] **Step 4: 完成一次集中双视口检查、一次修正、一次确认，并运行 detector。**
- [ ] **Step 5: 更新文档并提交 `test: 完成管理后台全量验收`。**

