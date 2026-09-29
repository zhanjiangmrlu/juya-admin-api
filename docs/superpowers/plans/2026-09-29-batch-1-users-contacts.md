# 第一批：用户与联系资料实施计划

> **执行方式：** 当前会话原生执行。每项产品代码严格先写失败测试，再写最小实现；三个仓库分别提交中文 Conventional Commit。

**目标：** 完成 A02–A04 的用户学习概况、完整联系方式、联系状态筛选与修改、微信号变更核对、敏感复制审计、联系更正列表/详情/批准/拒绝，并移除对应待接入状态。联系状态统一为 `NOT_PROVIDED`（未填写）、`PENDING`（待联系）、`CONTACTED`（已联系）、`UNREACHABLE`（暂无法联系）、`DO_NOT_CONTACT`（用户不希望联系）。

**架构：** `juya-miniapp-api` 继续拥有用户、学习和联系方式；`juya-admin-api` 通过 HMAC 内部客户端聚合数据、执行管理命令并记录管理审计；`juya-admin` 只调用 `/api/v1/admin/**`。联系更正代表“批准用户重新修改一次”的请求，不虚构 schema 中不存在的“新微信号”字段。

**技术栈：** Python 3.13、FastAPI、Pydantic 2、SQLAlchemy 2 asyncio、MySQL 8.4、HTTPX、Vue 3、TypeScript、Element Plus、Vitest、Playwright。

**设计依据：** `docs/superpowers/specs/2026-09-29-juya-admin-missing-capabilities-design.md`。

## 文件结构

- `juya-admin-api/migrations/versions/0009_admin_contact_capabilities.py`：更正决定幂等字段和管理查询索引。
- `juya-miniapp-api/src/juya_miniapp_api/modules/contacts/{domain,repository,service}.py`：更正管理视图、查询和幂等决定。
- `juya-miniapp-api/src/juya_miniapp_api/modules/learning/admin_projection.py`：用户学习概况查询。
- `juya-miniapp-api/src/juya_miniapp_api/api/internal/v1/users.py`：联系投影、更正和学习概况内部契约。
- `juya-admin-api/src/juya_admin_api/integrations/miniapp_api/client.py`：签名 GET/POST 与内部 DTO。
- `juya-admin-api/src/juya_admin_api/modules/contacts/{service,router}.py`：管理端联系资料聚合、命令和审计。
- `juya-admin-api/src/juya_admin_api/modules/user_projection/{service,router}.py`：用户列表/详情的联系与学习聚合。
- `juya-admin/src/features/contacts/`：联系更正、状态命令和复制审计 adapter/composable。
- `juya-admin/src/features/users/`：用户列表与详情扩展。
- `juya-admin/src/pages/users/`、`src/pages/contacts/`：A02–A04 真实页面状态。

---

### Task 1：统一联系状态并增加更正幂等字段和查询索引

**Files:**
- Create: `juya-admin-api/migrations/versions/0009_admin_contact_capabilities.py`
- Modify: `juya-admin-api/src/juya_admin_api/infrastructure/config.py`
- Test: `juya-admin-api/tests/integration/test_admin_contact_schema.py`

**Interfaces:**
- Produces: `contact_correction_request.processed_by`、`decision_idempotency_key`、`decision_request_hash`。
- Produces: 唯一键 `(processed_by, decision_idempotency_key)`，以及 `(status, created_at)` 查询索引。
- Changes: `ck_user_contact_status` 使用需求规定的五种状态；历史 `VERIFIED` 迁移为 `CONTACTED`，历史 `INVALID` 迁移为 `UNREACHABLE`，`verified_at/verified_by` 保留为独立核对信息。
- Produces: `schema_version.version = 9`、`Settings.required_schema_version = 9`。

- [ ] **Step 1: 写失败的迁移测试**

测试 `test_contact_admin_migration_adds_idempotency_and_list_index`：升级到 head 后查询 `information_schema`，断言三个字段、唯一键、状态时间索引、新的五状态约束和 schema version 9 存在；准备旧状态数据后断言 `VERIFIED/INVALID` 分别迁移为 `CONTACTED/UNREACHABLE`。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/integration/test_admin_contact_schema.py -q`

Expected: FAIL，缺少 `0009` 字段或索引。

- [ ] **Step 3: 实现向前迁移**

只追加新迁移，不修改 `0001`；先删除旧状态约束、迁移旧值、再创建新约束。`downgrade()` 将 `UNREACHABLE/DO_NOT_CONTACT` 安全折叠为旧枚举可表达的 `INVALID`，然后按索引、唯一键、字段的反顺序清理。

- [ ] **Step 4: 验证 GREEN 与迁移链**

Run: `uv run alembic upgrade head; uv run pytest tests/integration/test_admin_contact_schema.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add migrations/versions/0009_admin_contact_capabilities.py src/juya_admin_api/infrastructure/config.py tests/integration/test_admin_contact_schema.py
git commit -m "feat: 增加联系资料管理迁移"
```

---

### Task 2：在 miniapp-api 提供联系更正管理查询和幂等决定

**Files:**
- Modify: `juya-miniapp-api/src/juya_miniapp_api/modules/contacts/domain.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/modules/contacts/repository.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/modules/contacts/service.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/api/internal/v1/users.py`
- Test: `juya-miniapp-api/tests/unit/contacts/test_contact_rules.py`
- Test: `juya-miniapp-api/tests/integration/test_contact_concurrency.py`
- Test: `juya-miniapp-api/tests/contract/test_admin_contact_contract.py`

**Interfaces:**
- Produces: `AdminCorrectionView(id, user_id, juya_number, nickname, wechat_id, reason, status, created_at, processed_at, timeline)`。
- Produces: `ContactService.list_corrections(status, page, page_size) -> tuple[tuple[AdminCorrectionView, ...], int]`。
- Produces: `ContactService.get_correction(correction_id) -> AdminCorrectionView`。
- Changes: `ContactService.decide_correction(..., idempotency_key: str)`；相同 key 和请求返回原结果，不同请求返回 `409 IDEMPOTENCY_KEY_REUSED`。
- Changes: `_CONTACT_STATUSES` 和 repository 只接受五种需求状态；状态更新不再改写 `verified_at/verified_by`，微信号核对仅由 `verify_change` 负责。
- Produces internal routes:
  - `POST /internal/v1/contact-corrections/search`
  - `GET /internal/v1/contact-corrections/{correction_id}`
  - `POST /internal/v1/contact-corrections/{correction_id}/decision`

- [ ] **Step 1: 写更正查询与幂等失败测试**

```python
async def test_admin_lists_and_reads_contact_corrections_without_inventing_new_wechat():
    page, total = await service.list_corrections("PENDING", 1, 20)
    assert total == 1
    assert page[0].wechat_id == "current_wechat"
    assert not hasattr(page[0], "new_wechat_id")


async def test_same_decision_key_replays_and_different_payload_conflicts():
    first = await service.decide_correction("COR-1", "APPROVED", "ADMIN-1", "idem-1", NOW)
    replay = await service.decide_correction("COR-1", "APPROVED", "ADMIN-1", "idem-1", NOW)
    assert replay == first
```

契约测试断言敏感响应为 `Cache-Control: no-store`，需要 `X-Admin-Id`，决定命令需要 `X-Idempotency-Key`，且响应不含 OpenID、密文或 HMAC。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/unit/contacts/test_contact_rules.py tests/contract/test_admin_contact_contract.py -q`

Expected: FAIL，缺少查询方法、DTO 和路由。

- [ ] **Step 3: 实现最小领域与路由能力**

列表按 `created_at DESC, id DESC` 稳定排序；页码从 1 开始，`page_size` 限制 1–100。详情时间线来自 `contact_status_history`，不返回敏感历史明文。批准只重置一次自助修改机会，不直接改微信号；拒绝只更新申请状态。

- [ ] **Step 4: 验证单元、契约和 MySQL 集成测试**

Run: `uv run pytest tests/unit/contacts/test_contact_rules.py tests/contract/test_admin_contact_contract.py tests/integration/test_contact_concurrency.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/juya_miniapp_api/modules/contacts src/juya_miniapp_api/api/internal/v1/users.py tests/unit/contacts tests/integration/test_contact_concurrency.py tests/contract/test_admin_contact_contract.py
git commit -m "feat: 提供联系更正管理接口"
```

---

### Task 3：在 miniapp-api 提供批量联系投影和学习概况

**Files:**
- Create: `juya-miniapp-api/src/juya_miniapp_api/modules/learning/admin_projection.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/api/internal/v1/users.py`
- Modify: `juya-miniapp-api/src/juya_miniapp_api/api/runtime.py`
- Test: `juya-miniapp-api/tests/unit/learning/test_admin_projection.py`
- Test: `juya-miniapp-api/tests/contract/test_admin_contact_contract.py`

**Interfaces:**
- Produces: `LearningOverview(open_scene_completed_count, learning_days, favorite_count)`。
- Produces: `LearningOverviewRepository.get(user_id: str) -> LearningOverview`，从完成事件、每日打卡和收藏表聚合。
- Produces internal routes:
  - `POST /internal/v1/users/contact-projections`
  - `GET /internal/v1/users/{user_id}/learning-overview`
- Contact projection fields: `user_id`、`wechat_id`、`contact_status`、`change_pending`、`verified_at`、`verified_by`、`updated_at`。

- [ ] **Step 1: 写聚合和批量投影失败测试**

断言重复完成事件不会重复计算开放场景，学习天数按 `daily_checkin` 去重，收藏按当前有效收藏计数；批量联系投影保持请求 ID 顺序并省略不存在的联系记录。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/unit/learning/test_admin_projection.py tests/contract/test_admin_contact_contract.py -q`

Expected: FAIL，缺少聚合类和路由。

- [ ] **Step 3: 实现 SQL 聚合并装配运行时**

查询只返回计数，不返回学习正文、收藏内容或其他用户敏感数据。批量联系投影最多接收 100 个用户 ID。

- [ ] **Step 4: 验证 GREEN**

Run: `uv run pytest tests/unit/learning/test_admin_projection.py tests/contract/test_admin_contact_contract.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/juya_miniapp_api/modules/learning/admin_projection.py src/juya_miniapp_api/api/internal/v1/users.py src/juya_miniapp_api/api/runtime.py tests/unit/learning/test_admin_projection.py tests/contract/test_admin_contact_contract.py
git commit -m "feat: 提供用户学习与联系投影"
```

---

### Task 4：扩展 admin-api 的 miniapp 内部客户端

**Files:**
- Modify: `juya-admin-api/src/juya_admin_api/integrations/miniapp_api/client.py`
- Test: `juya-admin-api/tests/contract/test_miniapp_client.py`

**Interfaces:**
- Produces: `_request_json(method, path, payload=None, extra_headers=None)`，GET 使用空 body 参与签名，POST 使用规范 JSON 字节。
- Fixes: 微信号查询调用 `POST /internal/v1/users/search`，解析 `items[*].public_id`。
- Produces: `get_contact_projections`、`get_learning_overview`、`list_contact_corrections`、`get_contact_correction`、`update_contact_status`、`verify_contact_change`、`decide_contact_correction`。
- 所有管理调用携带 `X-Admin-Id`；决定命令额外携带 `X-Idempotency-Key`。

- [ ] **Step 1: 写签名路径和 DTO 失败测试**

测试实际 HTTP method、path、body、`X-Admin-Id`、幂等键和签名；断言上游 `404/409/422` 保留安全错误码，网络超时转换为 `MINIAPP_API_UNAVAILABLE`，不会把写操作误判成成功。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/contract/test_miniapp_client.py -q`

Expected: FAIL，当前客户端路径与内部契约不一致且缺少方法。

- [ ] **Step 3: 重构单一签名请求入口并实现 DTO 解析**

删除旧的不存在路径依赖；响应字段缺失统一返回 `502 MINIAPP_API_INVALID_RESPONSE`。只有列表联系投影允许受控降级，写命令不降级。

- [ ] **Step 4: 验证 GREEN**

Run: `uv run pytest tests/contract/test_miniapp_client.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/juya_admin_api/integrations/miniapp_api/client.py tests/contract/test_miniapp_client.py
git commit -m "fix: 对齐用户服务内部契约"
```

---

### Task 5：实现 admin-api 联系资料管理与审计路由

**Files:**
- Create: `juya-admin-api/src/juya_admin_api/modules/contacts/__init__.py`
- Create: `juya-admin-api/src/juya_admin_api/modules/contacts/service.py`
- Create: `juya-admin-api/src/juya_admin_api/modules/contacts/router.py`
- Modify: `juya-admin-api/src/juya_admin_api/infrastructure/runtime.py`
- Test: `juya-admin-api/tests/unit/contacts/test_admin_contact_service.py`
- Test: `juya-admin-api/tests/e2e/test_admin_core_flow.py`

**Interfaces:**
- Produces public routes:
  - `GET /api/v1/admin/contact-corrections`
  - `GET /api/v1/admin/contact-corrections/{correction_id}`
  - `POST /api/v1/admin/contact-corrections/{correction_id}/commands/{approve|reject}`
  - `POST /api/v1/admin/users/{user_id}/commands/contact-status`
  - `POST /api/v1/admin/users/{user_id}/commands/verify-contact-change`
  - `POST /api/v1/admin/users/{user_id}/contact-copy-events`
- Produces: 所有响应 `no-store`；命令使用 `current_admin_write`，更正决定需要幂等键。
- Produces audit actions: `contact.view`、`contact.copy`、`contact.status.update`、`contact.change.verify`、`contact.correction.approve|reject`；摘要永不包含微信号或更正原因正文。

- [ ] **Step 1: 写路由与审计失败测试**

断言未登录为 401、缺 CSRF 为 403、缺幂等键为 422；成功命令调用内部客户端并写一条脱敏审计；内部调用失败时不写成功审计；复制审计成功后才返回 204。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/unit/contacts/test_admin_contact_service.py tests/e2e/test_admin_core_flow.py -q`

Expected: FAIL，缺少 contacts 模块和路由。

- [ ] **Step 3: 实现服务、路由和运行时装配**

从请求中读取 `request.state.request_id`；审计仅记录对象公开编号、动作、管理员和时间。联系状态只接受 `NOT_PROVIDED/PENDING/CONTACTED/UNREACHABLE/DO_NOT_CONTACT`，不接受旧值或任意字符串。

- [ ] **Step 4: 验证 GREEN**

Run: `uv run pytest tests/unit/contacts/test_admin_contact_service.py tests/e2e/test_admin_core_flow.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/juya_admin_api/modules/contacts src/juya_admin_api/infrastructure/runtime.py tests/unit/contacts tests/e2e/test_admin_core_flow.py
git commit -m "feat: 增加联系资料管理接口"
```

---

### Task 6：扩展 admin-api 用户列表和详情聚合

**Files:**
- Modify: `juya-admin-api/src/juya_admin_api/modules/user_projection/service.py`
- Modify: `juya-admin-api/src/juya_admin_api/modules/user_projection/router.py`
- Modify: `juya-admin-api/src/juya_admin_api/modules/user_projection/repository.py`
- Modify: `juya-admin-api/src/juya_admin_api/infrastructure/runtime.py`
- Test: `juya-admin-api/tests/unit/user_projection/test_user_projection.py`
- Test: `juya-admin-api/tests/e2e/test_admin_core_flow.py`

**Interfaces:**
- Changes: `UserProjectionService.search(query=None, wechat_id=None, contact_status=None)` 返回带联系投影的列表项。
- Changes: `UserDetail` 增加完整联系元数据和 `learning_overview`。
- User list remains an array for backward compatibility；新增 `contact_status` query，不改变已有字段名。
- User detail adds `change_pending`、`verified_at`、`verified_by`、`updated_at`、`open_scene_completed_count`、`learning_days`、`favorite_count`。
- Changes: 用户列表和详情成功返回含微信号的联系投影后写 `contact.view.list|detail` 管理审计；审计摘要只含公开用户编号和命中数量，不含微信号。

- [ ] **Step 1: 写聚合与筛选失败测试**

断言普通搜索批量读取联系投影、联系状态筛选由服务端执行、微信号仍只通过 POST body 搜索、用户服务超时时用户基础投影可降级但不伪造联系方式；详情学习概况成功时不再为 pending；只有真实返回敏感联系投影时才写脱敏查看审计。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/unit/user_projection/test_user_projection.py tests/e2e/test_admin_core_flow.py -q`

Expected: FAIL，缺少聚合字段和筛选。

- [ ] **Step 3: 实现列表与详情聚合**

先从本地投影获得用户集合，再一次批量读取联系投影；筛选后稳定保持本地查询顺序。微信号、昵称等敏感字段不写入日志。查看审计失败时敏感响应不得返回成功，避免出现未审计读取。

- [ ] **Step 4: 验证 GREEN**

Run: `uv run pytest tests/unit/user_projection/test_user_projection.py tests/e2e/test_admin_core_flow.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/juya_admin_api/modules/user_projection src/juya_admin_api/infrastructure/runtime.py tests/unit/user_projection/test_user_projection.py tests/e2e/test_admin_core_flow.py
git commit -m "feat: 完善用户联系与学习概况"
```

---

### Task 7：锁定 OpenAPI 路由清单并更新前端契约

**Files:**
- Create: `juya-admin-api/tests/contract/test_admin_route_inventory.py`
- Modify: `juya-admin/openapi/admin-api.json`
- Regenerate: `juya-admin/src/shared/contracts/generated/admin-api.d.ts`

**Interfaces:**
- Produces: 第一批所有公开管理路由必须出现在 `app.openapi()["paths"]`。
- Produces: 固定快照与当前 FastAPI OpenAPI 一致；生成文件只由 `pnpm generate:api` 产生。

- [ ] **Step 1: 写失败的路由清单测试**

断言 Task 5 的六组路由和扩展用户路由全部存在，且写路由声明 CSRF/幂等 Header，敏感响应 DTO 不含 `openid`、密文字段或永久媒体 URL。

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest tests/contract/test_admin_route_inventory.py -q`

Expected: FAIL，新增路由尚未进入受控清单或 schema 信息不完整。

- [ ] **Step 3: 完善响应模型并导出 OpenAPI**

从运行中的本地 `juya-admin-api` 获取 `/openapi.json` 写入前端固定快照，然后运行：

Run: `pnpm generate:api`

- [ ] **Step 4: 验证契约测试和生成结果**

Run: `uv run pytest tests/contract/test_admin_route_inventory.py -q`

Run: `pnpm exec tsc --noEmit -p tsconfig.node.json`

Expected: PASS，且重新生成后 `git diff` 稳定。

- [ ] **Step 5: 分仓提交**

```powershell
# juya-admin-api
git add tests/contract/test_admin_route_inventory.py
git commit -m "test: 锁定首批管理接口清单"

# juya-admin
git add openapi/admin-api.json src/shared/contracts/generated/admin-api.d.ts
git commit -m "chore: 更新用户联系接口契约"
```

---

### Task 8：实现前端用户与联系资料 adapter/composable

**Files:**
- Modify: `juya-admin/src/features/users/user-adapter.ts`
- Modify: `juya-admin/src/features/users/use-user-list.ts`
- Modify: `juya-admin/src/features/users/use-user-detail.ts`
- Modify: `juya-admin/src/features/contacts/contact-capabilities.ts`
- Create: `juya-admin/src/features/contacts/use-contact-corrections.ts`
- Test: `juya-admin/src/features/users/user-adapter.spec.ts`
- Test: `juya-admin/src/features/users/use-user-list.spec.ts`
- Test: `juya-admin/src/features/users/use-user-detail.spec.ts`
- Test: `juya-admin/src/features/contacts/contact-capabilities.spec.ts`
- Test: `juya-admin/src/features/contacts/use-contact-corrections.spec.ts`

**Interfaces:**
- `UserProjectionDto` 增加 `contact`；`UserDetailDto` 增加学习概况和联系核对元数据。
- `ContactCapabilities` 产生真实 `listCorrections`、`getCorrection`、`decideCorrection`、`updateStatus`、`verifyChange`、`auditCopy` 方法。
- 命令通过 `createIdempotencyKey()` 生成键；409 保留页面数据并提示刷新。
- copy 流程只有审计接口成功后才调用 `navigator.clipboard.writeText`。

- [ ] **Step 1: 将 pending 测试改为失败的真实请求测试**

断言 adapter 请求准确 method/path/body/header；列表取消旧请求；详情学习区块成功；复制审计失败时剪贴板未写入；更正命令成功后重新读取详情。

- [ ] **Step 2: 运行测试确认 RED**

Run: `pnpm exec vitest run src/features/users src/features/contacts`

Expected: FAIL，当前 contact adapter 返回 `pending` 且不发送请求。

- [ ] **Step 3: 实现 adapter 和控制器**

继续在业务 adapter 层校验未知响应，不让页面直接依赖生成 DTO。敏感字段不写入 URL、Pinia、localStorage 或 sessionStorage。

- [ ] **Step 4: 验证 GREEN**

Run: `pnpm exec vitest run src/features/users src/features/contacts`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/features/users src/features/contacts
git commit -m "feat: 接入用户与联系资料接口"
```

---

### Task 9：完成 A02–A04 页面真实交互

**Files:**
- Modify: `juya-admin/src/components/sensitive-value/sensitive-value.vue`
- Modify: `juya-admin/src/components/sensitive-value/sensitive-value.spec.ts`
- Modify: `juya-admin/src/pages/users/user-list-page.vue`
- Modify: `juya-admin/src/pages/users/user-detail-page.vue`
- Modify: `juya-admin/src/pages/contacts/contact-correction-page.vue`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.ts`
- Modify: `juya-admin/src/shared/capabilities/capability-registry.spec.ts`
- Test: `juya-admin/src/pages/users/user-pages.spec.ts`
- Test: `juya-admin/src/pages/contacts/contact-correction-page.spec.ts`

**Interfaces:**
- A02：直接显示完整微信号和五种联系状态；启用联系状态筛选；“联系资料列表”切换到更正申请表格。
- A03：展示开放场景完成数、学习天数、收藏数；提供状态更新、核对变更和审计后复制。
- A04：显示当前微信号、更正原因、状态和脱敏时间线；批准/拒绝二次确认并处理 409。
- 将 `contacts.copy-audit`、`contacts.correction-command`、`contacts.correction-list` 标记为 `available`；不提前修改第二批权益能力。

- [ ] **Step 1: 写页面失败测试**

断言三个页面不再出现联系相关“接口待接入”；完整微信号使用普通易读字体直接显示；状态修改和更正决定按钮可用；loading/empty/error/409 均有明确状态；A04 不显示不存在的“新微信号”。

- [ ] **Step 2: 运行测试确认 RED**

Run: `pnpm exec vitest run src/components/sensitive-value src/pages/users src/pages/contacts src/shared/capabilities`

Expected: FAIL，页面仍为 pending 且按钮禁用。

- [ ] **Step 3: 实现页面交互并保持现有视觉结构**

只替换数据和行为，不重做布局。离开敏感页面时取消请求并清空详情状态；复制成功显示短反馈，失败不触碰剪贴板。

- [ ] **Step 4: 验证 GREEN**

Run: `pnpm exec vitest run src/components/sensitive-value src/pages/users src/pages/contacts src/shared/capabilities`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/components/sensitive-value src/pages/users src/pages/contacts src/shared/capabilities
git commit -m "feat: 完成用户与联系资料页面"
```

---

### Task 10：完成第一批端到端验收与文档

**Files:**
- Modify: `juya-admin/tests/e2e/fixtures/admin-api.ts`
- Modify: `juya-admin/tests/e2e/users.spec.ts`
- Modify: `juya-admin/README.md`
- Create: `juya-admin-api/docs/api/batch-1-users-contacts.md`
- Modify: `doc/接口文档/juya-admin-api-接口文档.md`

**Interfaces:**
- Produces E2E flows: 联系状态筛选、用户详情学习概况、审计后复制、更正列表进入详情、批准/拒绝、409 保留页面。
- Produces docs: 第一批新增路由、请求/响应、鉴权、幂等、错误码和敏感数据规则。

- [ ] **Step 1: 写失败的 E2E 测试和夹具**

测试真实页面行为与请求记录；断言微信号不进入 URL 或 storage、写命令携带 CSRF 和幂等键、联系相关页面不再渲染“接口待接入”。

- [ ] **Step 2: 运行关键 E2E 确认 RED**

Run: `pnpm exec playwright test tests/e2e/users.spec.ts --project=chromium`

Expected: FAIL，夹具和页面尚未覆盖新流程。

- [ ] **Step 3: 完成夹具、接口文档和 README**

接口文档必须由实际路由/OpenAPI 核对，不手写不存在的能力。仓库内新增第一批接口说明并提交；同步更新工作区根目录的汇总接口文档（该目录不是 Git 仓库，在交付说明中单独列明）。README 将三个联系能力列为已接入。

- [ ] **Step 4: 执行三个仓库第一批完整门禁**

Run in `juya-miniapp-api`:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Run in `juya-admin-api`:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Run in `juya-admin`:

```powershell
pnpm check
pnpm test -- --maxWorkers=1
pnpm build
pnpm exec playwright test tests/e2e/users.spec.ts --project=chromium
git diff --check
```

Expected: 所有命令退出码 0。若 MySQL/Docker 或 Chromium 不可用，记录明确的未验证项，不以聚焦测试替代全量成功声明。

- [ ] **Step 5: 分仓提交验收与文档**

```powershell
# juya-admin
git add tests/e2e/users.spec.ts tests/e2e/fixtures/admin-api.ts README.md
git commit -m "test: 完成用户联系流程验收"

# juya-admin-api
git add docs/api/batch-1-users-contacts.md
git commit -m "docs: 更新用户联系接口说明"
```

## Review Focus

- 联系更正批准只能重置一次用户自助修改机会，不能直接写入一个不存在的新微信号。
- 相同更正决定幂等键必须安全重放，不同 correction 或 decision 复用同一键必须返回 409。
- 用户服务降级只能影响联系/学习区块，不能把空字符串或零值伪装成真实数据。
- 完整微信号可按需求直接展示，但不得进入 URL、持久化、日志、统计或审计摘要。
- 管理审计成功记录必须发生在真实操作成功之后；上游失败不得留下成功审计。
