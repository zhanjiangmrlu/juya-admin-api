# 第七批阿里云 OSS 生产接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变前六批业务契约的前提下，完成私有阿里云 OSS 的生产凭据、CORS、浏览器直传、签名读取、任务对象流转、生命周期删除和真实环境验收。

**Architecture:** 复用现有 `OssProvider`、`AliyunOssProvider` 和 miniapp 上传服务，补齐 STS/RAM 受控凭据、环境校验和对象前缀策略。业务层继续只保存 `object_key`，local/test provider 保持可注入，生产禁止静默降级。

**Tech Stack:** Alibaba Cloud OSS SDK V2、RAM/STS、FastAPI、Celery、pytest、Playwright。

**Spec:** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`

## Global Constraints

- Bucket 必须私有；浏览器只获得受限、短时上传策略或签名 URL。
- 生产凭据不得写入仓库、日志、前端包或测试快照。
- CORS 仅允许明确的管理端/小程序 WebView 来源、方法和头。
- 生产缺少 OSS 配置或凭据时启动失败，禁止退回本地存储。
- 对象删除必须经过业务引用检查和审计。

## Review Focus

- STS token 过期、时钟漂移和凭据轮换时返回可诊断错误。
- 上传策略限制对象前缀、MIME、大小和过期时间。
- 签名 URL、AccessKey、security token 不进入结构化日志。
- OCR/TTS worker 只能访问授权前缀，失败不泄露对象 URL。
- 生命周期删除不会误删仍被发布版本或反馈引用的对象。

---

### Task 1: 生产凭据和配置硬化

**Files:**
- Modify: `src/juya_admin_api/infrastructure/config.py`
- Modify: `src/juya_admin_api/infrastructure/runtime.py`
- Modify: `src/juya_admin_api/integrations/oss/aliyun.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/infrastructure/config.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/integrations/oss/upload.py`
- Test: `tests/unit/media/test_aliyun_oss_adapter.py`
- Test: `juya-miniapp-api/tests/unit/test_oss_upload.py`

**Interfaces:**
- Produces: 环境凭据/RAM 角色 provider、严格生产配置校验、OSS V4 上传策略。
- Consumes: 现有 provider 协议和对象键规则。

- [ ] **Step 1: 写生产缺配置、STS token、V4 策略限制和日志脱敏失败测试。**
- [ ] **Step 2: 运行测试确认现有实现缺少受控凭据/约束。**
- [ ] **Step 3: 实现配置和 provider 硬化，不引入静态密钥默认值。**
- [ ] **Step 4: 运行两个后端的 OSS 聚焦与全量门禁。**
- [ ] **Step 5: 分仓提交 `feat: 完善阿里云OSS生产配置`。**

### Task 2: CORS、生命周期与媒体任务对象流转

**Files:**
- Modify: `src/juya_admin_api/modules/media/service.py`
- Modify: `src/juya_admin_api/infrastructure/tasks/maintenance.py`
- Modify: `src/juya_admin_api/modules/media/tasks.py`
- Create: `docs/operations/aliyun-oss.md`
- Test: `tests/integration/test_oss_object_lifecycle.py`

**Interfaces:**
- Produces: 前缀隔离、引用检查删除、OCR/TTS 输入输出和运维 CORS/RAM 配置说明。
- Consumes: 前六批稳定的 `object_key` 数据模型。

- [ ] **Step 1: 写引用保护、任务对象流转和日志脱敏失败测试。**
- [ ] **Step 2: 运行测试确认生命周期缺口。**
- [ ] **Step 3: 实现对象流转和删除保护，记录脱敏审计。**
- [ ] **Step 4: 运行 local/test provider 回归，证明业务契约未变化。**
- [ ] **Step 5: 提交 `feat: 完成OSS对象生命周期管理`。**

### Task 3: 真实阿里云环境端到端验收

**Files:**
- Create: `tests/live/test_aliyun_oss_e2e.py`
- Create: `juya-miniapp-api/tests/live/test_aliyun_feedback_upload.py`
- Modify: `docs/operations/aliyun-oss.md`

**Interfaces:**
- Produces: 使用外部环境变量启用的上传、head、签名读取、OCR/TTS 输入输出和删除验证。
- Consumes: 用户提供的真实私有 Bucket、RAM/STS 权限和允许来源。

- [ ] **Step 1: 写默认跳过、仅在 `JUYA_RUN_LIVE_OSS_TESTS=true` 时运行的 live tests。**
- [ ] **Step 2: 在真实阿里云配置下运行并确认上传/读取/处理/删除全链路。**
- [ ] **Step 3: 检查 Bucket 私有性、CORS、RAM 最小权限和日志无凭据。**
- [ ] **Step 4: 记录非敏感验证证据与对象清理结果。**
- [ ] **Step 5: 提交 `test: 完成阿里云OSS生产验收`。**

