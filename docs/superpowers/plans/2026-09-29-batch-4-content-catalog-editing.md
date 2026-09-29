# 第四批内容目录与编辑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 A17、A20、A22、A23 的内容列表、场景/revision 详情、结构化保存、配置读取和管理员预览。

**Architecture:** 在现有 ContentRepository/Service 上增加只读聚合和乐观锁保存；人工 revision 与 OCR 候选保持独立。管理员预览使用专用管理路由，仅读取草稿，不创建学习进度。

**Tech Stack:** FastAPI、SQLAlchemy async、Alembic、MySQL、Vue 3、TypeScript、Vitest、Playwright。

**Spec:** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`

## Global Constraints

- 保存请求携带 `expected_version`，冲突返回服务端当前 revision/version。
- 管理员预览不得创建用户进度或复用用户受限预览。
- 开放场景恰好 3 个；系列预览为 3–6 个且不能包含开放场景。
- V1.3 仅允许启用 `scene_learning`。

## Review Focus

- OCR 候选不得覆盖未显式选择的人工字段。
- revision 冲突时保留本地草稿，不自动重试覆盖。
- 下线被配置引用的场景返回明确冲突。
- 管理预览包含未发布草稿但不改变发布状态。
- 配置读取与写入使用同一版本，避免页面展示陈旧值。

---

### Task 1: 内容查询、编辑与配置仓储

**Files:**
- Create: `migrations/versions/0012_content_admin_editing.py`
- Modify: `src/juya_admin_api/modules/content/domain.py`
- Modify: `src/juya_admin_api/modules/content/repository.py`
- Modify: `src/juya_admin_api/modules/content/service.py`
- Test: `tests/integration/test_content_admin_editing.py`

**Interfaces:**
- Produces: `list_scenes/get_scene/get_revision/save_revision/get_discovery_config/save_discovery_config/admin_preview`。
- Consumes: 现有 scene、revision、entry、package、open/preview 配置表。

- [ ] **Step 1: 写分页、详情、乐观锁、配置约束和预览无副作用失败测试。**
- [ ] **Step 2: 运行测试确认方法缺失。**
- [ ] **Step 3: 追加必要索引/版本字段并实现 repository/service。**
- [ ] **Step 4: 重跑聚焦测试和内容发布回归。**
- [ ] **Step 5: 提交 `feat: 增加内容目录与编辑仓储`。**

### Task 2: 内容管理查询与编辑 API

**Files:**
- Modify: `src/juya_admin_api/modules/content/router.py`
- Modify: `src/juya_admin_api/infrastructure/runtime.py`
- Test: `tests/e2e/test_admin_content_editing_flow.py`
- Test: `tests/contract/test_admin_route_inventory.py`

**Interfaces:**
- Produces: 场景列表/详情、revision 详情/保存、配置读取/保存、管理员预览路由。
- Consumes: Task 1 service 和已有发布/下线命令。

- [ ] **Step 1: 写路由、422、409、no-store 预览和路由清单失败测试。**
- [ ] **Step 2: 确认测试因端点缺失失败。**
- [ ] **Step 3: 实现响应模型和路由，复用审计与统一错误。**
- [ ] **Step 4: 运行 admin-api 全量门禁并更新 OpenAPI。**
- [ ] **Step 5: 提交 `feat: 完成内容目录编辑接口`。**

### Task 3: A17/A20/A22/A23 前端真实接入

**Files:**
- Modify: `juya-admin/src/features/content/*`
- Modify: `juya-admin/src/features/content-editor/*`
- Modify: `juya-admin/src/features/discovery/*`
- Modify: `juya-admin/src/features/publishing/*`
- Modify: `juya-admin/src/pages/content/{content-list,scene-editor,publish-check,discovery-config}-page.vue`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.ts`
- Create: `juya-admin/tests/e2e/content-editing.spec.ts`

**Interfaces:**
- Produces: 四页完整加载/空/错误/冲突/成功状态，第四批 capability `available`。
- Consumes: Task 2 OpenAPI。

- [ ] **Step 1: 写 adapter、草稿冲突、配置恢复和预览失败测试。**
- [ ] **Step 2: 运行测试确认页面仍为占位。**
- [ ] **Step 3: 实现 adapters/composables 并替换四页占位。**
- [ ] **Step 4: 运行前端门禁、E2E、双视口截图和 detector。**
- [ ] **Step 5: 提交 `feat: 完成内容目录编辑页面`。**

