# 第五批 OCR 音频与批量任务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 A18、A19、A21、A24 的 OCR、人工校对、音频版本、批量任务和草稿回收站闭环。

**Architecture:** 复用现有 MediaService、OCR/TTS provider 和 Celery 任务入口，将任务状态与结果持久化到 SQL。所有对象只持久化 `object_key`；任务单项隔离失败，人工版本永远优先于重试结果。

**Tech Stack:** FastAPI、SQLAlchemy async、Alembic、Celery、Redis、OSS provider、Vue 3、Vitest、Playwright。

**Spec:** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`

## Global Constraints

- 图片单批最多 30 张且必须同系列、同模板。
- 音频单批最多 300 个；通用批量任务最多 500 项。
- 重试不得覆盖人工 revision；取消只对未完成任务生效。
- 草稿被发布版本、活动或配置引用时不可清理。

## Review Focus

- 同一对象重复确认上传必须幂等且校验 size/content-type/sha256。
- Celery 重投递不得创建重复任务或重复音频版本。
- 批次单项失败不得把成功项回滚成失败。
- 人工确认与 OCR 回写并发时人工版本获胜。
- 回收站清理必须先验证引用并记录审计。

---

### Task 1: 媒体任务、音频版本和回收站 SQL 模型

**Files:**
- Create: `migrations/versions/0013_media_job_admin.py`
- Modify: `src/juya_admin_api/modules/media/repository.py`
- Modify: `src/juya_admin_api/modules/media/service.py`
- Create: `src/juya_admin_api/modules/media/domain.py`
- Test: `tests/integration/test_media_job_repository.py`

**Interfaces:**
- Produces: OCR/audio/batch job repositories、audio target/version repository、trash repository。
- Consumes: 现有 media_asset、processing_job、ocr_candidate 等表。

- [x] **Step 1: 写任务幂等、单项隔离、人工优先、版本回退和引用保护失败测试。**
- [x] **Step 2: 运行测试确认仓储能力缺失。**
- [x] **Step 3: 追加兼容迁移、索引和 repository/service。**
- [x] **Step 4: 运行聚焦 MySQL 测试。**
- [x] **Step 5: 提交 `feat: 增加媒体任务持久化能力`。**

### Task 2: Celery 任务与管理 API

**Files:**
- Modify: `src/juya_admin_api/modules/media/tasks.py`
- Modify: `src/juya_admin_api/modules/media/router.py`
- Modify: `src/juya_admin_api/infrastructure/runtime.py`
- Test: `tests/unit/media/test_processing_tasks.py`
- Test: `tests/e2e/test_admin_media_jobs_flow.py`
- Test: `tests/contract/test_admin_route_inventory.py`

**Interfaces:**
- Produces: OCR 创建/查询/候选/确认/重试/取消，音频目标/版本/生成/上传/确认/回退，批量任务和回收站路由。
- Consumes: Task 1 repositories、现有 upload policy 和 provider 协议。

- [x] **Step 1: 写任务重投递、取消、部分失败、路由鉴权和清单失败测试。**
- [x] **Step 2: 运行测试确认命令和查询缺失。**
- [x] **Step 3: 实现任务编排、路由、审计和幂等。**
- [x] **Step 4: 使用 local/test provider 跑真实 HTTP + worker 集成流程。**
- [x] **Step 5: 提交 `feat: 完成媒体任务管理接口`。**

### Task 3: A18/A19/A21/A24 前端真实接入

**Files:**
- Modify: `juya-admin/src/features/content-import/*`
- Modify: `juya-admin/src/features/ocr/*`
- Modify: `juya-admin/src/features/audio/*`
- Modify: `juya-admin/src/features/batch-jobs/*`
- Modify: `juya-admin/src/pages/content/{content-import,ocr-review,audio-version,batch-jobs}-page.vue`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.ts`
- Create: `juya-admin/tests/e2e/media-jobs.spec.ts`

**Interfaces:**
- Produces: 上传/OCR/校对/音频/批量/回收站完整页面状态，第五批 capability `available`。
- Consumes: Task 2 OpenAPI。

- [x] **Step 1: 写 adapter、队列取消、部分失败、人工确认和回退失败测试。**
- [x] **Step 2: 运行测试确认页面待接入。**
- [x] **Step 3: 实现 adapters/composables 和四页真实交互。**
- [x] **Step 4: 运行前端全量门禁、worker E2E 和双视口检查。**
- [x] **Step 5: 提交 `feat: 完成媒体任务后台页面`。**

