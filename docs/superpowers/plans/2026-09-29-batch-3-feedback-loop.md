# 第三批反馈闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 A14–A16 的反馈列表、筛选、完整时间线、截图临时地址、内部备注和处理闭环。

**Architecture:** 扩展现有 FeedbackService/Repository，不改变已有状态机；截图只保存 `object_key`，由管理端会话按需签发短期 URL。内部备注使用独立字段和审计事件，永不进入用户端响应。

**Tech Stack:** FastAPI、SQLAlchemy async、Alembic、MySQL、OSS provider、Vue 3、Vitest、Playwright。

**Spec:** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`

## Global Constraints

- 截图签名 URL 默认短期有效，禁止进入日志、审计摘要和持久化响应缓存。
- 内部备注最多 200 字，只对管理员可见。
- 反馈命令维持现有幂等、SLA 和状态机规则。
- 列表支持状态、分类、用户/反馈编号和 SLA 筛选。

## Review Focus

- HTML 反馈正文必须按纯文本显示。
- 已关闭工单的重复命令返回 409 且不追加重复事件。
- 补充阶段 SLA 暂停与恢复时间准确。
- 签名 URL 过期后重新签发，不复用旧 URL。
- 内部备注不得出现在 miniapp 内部详情或用户消息中。

---

### Task 1: 反馈查询、时间线与内部备注仓储

**Files:**
- Create: `migrations/versions/0011_feedback_admin_loop.py`
- Modify: `src/juya_admin_api/modules/feedback/domain.py`
- Modify: `src/juya_admin_api/modules/feedback/repository.py`
- Modify: `src/juya_admin_api/modules/feedback/service.py`
- Test: `tests/integration/test_feedback_admin_queries.py`
- Test: `tests/unit/feedback/test_state_machine.py`

**Interfaces:**
- Produces: `FeedbackRepository.list_admin/get_admin/add_internal_note` 和聚合详情 DTO。
- Consumes: 现有 ticket、supplement、resolution、attachment、timeline 表。

- [ ] **Step 1: 写列表筛选、时间线排序、备注隔离和 SLA 失败测试。**
- [ ] **Step 2: 运行测试确认查询/备注能力缺失。**
- [ ] **Step 3: 追加迁移、组合索引和 repository/service 实现。**
- [ ] **Step 4: 运行聚焦与反馈全量测试。**
- [ ] **Step 5: 提交 `feat: 增加反馈管理聚合查询`。**

### Task 2: 反馈管理 API 与截图签名

**Files:**
- Modify: `src/juya_admin_api/modules/feedback/router.py`
- Modify: `src/juya_admin_api/modules/media/service.py`
- Modify: `src/juya_admin_api/infrastructure/runtime.py`
- Test: `tests/e2e/test_admin_feedback_flow.py`
- Test: `tests/contract/test_admin_route_inventory.py`

**Interfaces:**
- Produces: `GET /api/v1/admin/feedback`、聚合详情、`POST .../screenshot-url`、`POST .../internal-notes`。
- Consumes: Task 1 聚合查询和现有 OssProvider。

- [ ] **Step 1: 写鉴权、no-store、短期签名、内部备注和路由清单失败测试。**
- [ ] **Step 2: 运行测试确认端点缺失。**
- [ ] **Step 3: 实现管理路由并记录不含正文/URL的脱敏审计。**
- [ ] **Step 4: 运行 admin-api 全量门禁。**
- [ ] **Step 5: 提交 `feat: 完成反馈管理闭环接口`。**

### Task 3: A14–A16 前端真实接入

**Files:**
- Modify: `juya-admin/src/features/feedback/*`
- Modify: `juya-admin/src/pages/feedback/*.vue`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.ts`
- Modify: `juya-admin/tests/e2e/fixtures/admin-api.ts`
- Create: `juya-admin/tests/e2e/feedback-loop.spec.ts`

**Interfaces:**
- Produces: 可分页反馈列表、完整详情/时间线、按需截图和内部备注 controller。
- Consumes: Task 2 OpenAPI。

- [ ] **Step 1: 写列表、纯文本正文、URL 不持久化、409 和备注失败测试。**
- [ ] **Step 2: 运行测试确认占位状态。**
- [ ] **Step 3: 实现 adapter/composable 和 A14–A16 页面。**
- [ ] **Step 4: 更新 capability、运行全量前端门禁与双视口检查。**
- [ ] **Step 5: 提交 `feat: 完成反馈闭环后台页面`。**

