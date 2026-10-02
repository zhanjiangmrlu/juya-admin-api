# juya-admin-api 接口文档

> 项目：句芽英语 V1.3 `juya-admin-api`
> 源码目录：`D:\个人\juya\juya-admin-api`
> 基线：2026-10-01 main V1.3 实施；历史第六批见其验收文档
> 文档日期：2026-10-01
> OpenAPI 操作数：107（92 管理端、13 内部、2 健康）

## 1. 文档范围

本文档根据当前 `juya-admin-api` 的 FastAPI 路由、Pydantic 模型、鉴权依赖和业务服务实现整理，覆盖：

- 2 个健康检查接口。
- 92 个管理端接口。
- 13 个内部服务接口。

FastAPI 还会默认提供 `/docs`、`/docs/oauth2-redirect`、`/redoc` 和 `/openapi.json`，这些为自动文档及元数据端点，不计入上述 107 个 OpenAPI 操作。生产环境是否对外暴露应由网关策略决定。原有操作见第 3 节，第二至六批的补全总览、查询参数及当前 DTO 见第 11 节；统计业务口径和验收边界见第 12 节。

## 2. 通用约定

### 2.1 请求与时间

- 示例基础地址：`https://admin-api.example.com`，实际地址以部署环境为准。
- JSON 请求使用 `Content-Type: application/json; charset=utf-8`。
- 时间字段使用 ISO 8601，服务端内部统一按 UTC 存储，例如 `2026-09-28T12:30:00Z`。
- 日期查询参数使用 `YYYY-MM-DD`。
- 请求 JSON 中未定义的额外字段会被拒绝，返回 `422 VALIDATION_ERROR`。
- 客户端可传入 `X-Request-ID`，长度不超过 128 个 ASCII 字符；未传或无效时由服务端生成。每个响应都会携带 `X-Request-ID`。

### 2.2 统一错误响应

```json
{
  "code": "SCENE_NOT_FOUND",
  "message": "场景不存在",
  "request_id": "f45a64fe18b643eda927c62e08d6ae24",
  "details": {}
}
```

| 字段         | 类型   | 说明                                         |
| ------------ | ------ | -------------------------------------------- |
| `code`       | string | 稳定的业务错误码，客户端应根据此字段分支处理 |
| `message`    | string | 面向用户或调试人员的中文说明                 |
| `request_id` | string | 请求追踪标识                                 |
| `details`    | object | 附加信息；参数校验失败时包含`errors` 数组    |

### 2.3 鉴权类型

| 标识            | 适用范围             | 要求                                               |
| --------------- | -------------------- | -------------------------------------------------- |
| `PUBLIC`        | 健康检查、管理员登录 | 无鉴权                                             |
| `ADMIN_READ`    | 管理端只读接口       | Cookie`juya_admin_session`                         |
| `ADMIN_WRITE`   | 管理端写接口         | Cookie`juya_admin_session` + Header `X-CSRF-Token` |
| `INTERNAL_HMAC` | `/internal/v1/**`    | VPC 内调用 + 4 个 HMAC 请求头                      |

OpenAPI 中 Cookie 和 CSRF Header 因 FastAPI 依赖的函数签名显示为可选，但除登录接口外，它们在业务上必须提供。

### 2.4 管理员登录流程

1. 调用 `POST /api/v1/admin/session` 验证账号密码。
2. 验证成功后，服务端通过 `Set-Cookie` 写入 `juya_admin_session`，响应体返回 `csrf_token`。
3. 后续只读请求携带 Cookie；写请求还需携带 `X-CSRF-Token`。

Cookie 的当前属性为 `Secure; HttpOnly; SameSite=Strict; Path=/api/v1/admin; Max-Age=28800`。

### 2.5 内部 HMAC 签名

所有 `/internal/v1/**` 接口都必须携带：

| Header             | 说明                                           |
| ------------------ | ---------------------------------------------- |
| `X-Juya-Service`   | 调用方服务名，当前默认只允许`juya-miniapp-api` |
| `X-Juya-Timestamp` | Unix 时间戳，单位秒，允许误差 300 秒           |
| `X-Juya-Nonce`     | 每次请求唯一的随机串，300 秒内不得重复         |
| `X-Juya-Signature` | HMAC-SHA256 的小写十六进制签名                 |

待签名串为：

```text
UPPERCASE_HTTP_METHOD + "\n" +
PATH_WITH_QUERY + "\n" +
TIMESTAMP + "\n" +
NONCE + "\n" +
SHA256_HEX(RAW_BODY)
```

最终签名：

```text
hex(HMAC-SHA256(JUYA_INTERNAL_HMAC_SECRET, canonical_string))
```

`PATH_WITH_QUERY` 必须与实际发送的路径和查询串完全一致；`RAW_BODY` 是实际发送的原始字节，不能在签名后再改变 JSON 空格或字段顺序。

### 2.6 幂等键

文档中标记 `X-Idempotency-Key` 的接口要求每次业务命令传入唯一键。网络超时后重试同一命令时必须复用原键；同一键不得用于不同请求内容。

## 3. 原有接口总览

本节保留原有操作总览；补全操作见第 11.1 节，两张清单的并集为当前完整接口集合。

### 3.1 健康检查

| 方法 | 路径            | 用途                          | 鉴权   | 成功状态 |
| ---- | --------------- | ----------------------------- | ------ | -------- |

### 3.2 管理安全与系统

| 方法  | 路径                           | 用途                   | 鉴权        | 成功状态 |
| ----- | ------------------------------ | ---------------------- | ----------- | -------- |

### 3.3 内容管理

| 方法 | 路径                                                             | 用途              | 鉴权        | 成功状态 |
| ---- | ---------------------------------------------------------------- | ----------------- | ----------- | -------- |

### 3.4 正式权益

| 方法 | 路径                                                     | 用途                     | 鉴权        | 成功状态 |
| ---- | -------------------------------------------------------- | ------------------------ | ----------- | -------- |

### 3.5 限时权益

| 方法 | 路径                                                                  | 用途         | 鉴权        | 成功状态 |
| ---- | --------------------------------------------------------------------- | ------------ | ----------- | -------- |

### 3.6 反馈管理

| 方法 | 路径                                                             | 用途               | 鉴权        | 成功状态 |
| ---- | ---------------------------------------------------------------- | ------------------ | ----------- | -------- |

### 3.7 管理端媒体

| 方法 | 路径                                  | 用途                    | 鉴权        | 成功状态 |
| ---- | ------------------------------------- | ----------------------- | ----------- | -------- |

### 3.8 运营查询

| 方法 | 路径                                                                   | 用途                   | 鉴权                      | 成功状态 |
| ---- | ---------------------------------------------------------------------- | ---------------------- | ------------------------- | -------- |

### 3.9 内部内容与访问策略

| 方法 | 路径                                                | 用途                       | 鉴权          | 成功状态 |
| ---- | --------------------------------------------------- | -------------------------- | ------------- | -------- |

### 3.10 内部反馈

| 方法 | 路径                                            | 用途                   | 鉴权          | 成功状态 |
| ---- | ----------------------------------------------- | ---------------------- | ------------- | -------- |

### 3.11 内部媒体与用户注销

| 方法 | 路径                                        | 用途                            | 鉴权          | 成功状态 |
| ---- | ------------------------------------------- | ------------------------------- | ------------- | -------- |

## 4. 详细接口

### 4.1 健康检查

#### GET `/health/live`

- 鉴权：`PUBLIC`
- 请求参数：无
- 成功响应：`200`

```json
{
  "status": "ok",
  "service": "juya-admin-api"
}
```

#### GET `/health/ready`

- 鉴权：`PUBLIC`
- 请求参数：无
- 成功响应：`200`

```json
{
  "status": "ready",
  "checks": {
    "mysql": true,
    "schema": true,
    "redis": true,
    "configuration": true
  }
}
```

- 主要错误：`503 SERVICE_NOT_READY`，`details.checks` 包含各检查项的布尔结果。

### 4.2 管理员会话、配置与审计

#### POST `/api/v1/admin/session`

- 鉴权：`PUBLIC`
- 请求体：`PasswordLoginRequest`
- 成功响应：`200 AdminSession`，同时通过 `Set-Cookie` 下发会话 Cookie。

```json
{
  "username": "admin",
  "password": "your-password"
}
```

```json
{
  "csrf_token": "opaque-csrf-token",
  "expires_at": "2026-09-28T20:30:00Z"
}
```

- 主要错误：`401 INVALID_ADMIN_CREDENTIALS`、`429 ADMIN_LOGIN_LOCKED`。

#### POST `/api/v1/admin/session/logout`

- 鉴权：`ADMIN_WRITE`
- 请求体：无
- 成功响应：`204`，响应体为空，服务端撤销会话并删除 Cookie。

#### GET `/api/v1/admin/settings`

- 鉴权：`ADMIN_READ`
- 成功响应：`200 SystemConfigList`

```json
{
  "items": [
    {
      "key": "feedback.sla",
      "value": { "hours": 48 },
      "version": 1
    }
  ]
}
```

#### PATCH `/api/v1/admin/settings/{key}`

- 鉴权：`ADMIN_WRITE`
- 路径参数：`key` 为配置键。
- 请求体：`ConfigUpdateRequest`，使用 `expected_version` 进行乐观锁校验。
- 成功响应：`200 SystemConfig`

```json
{
  "value": { "hours": 72 },
  "expected_version": 1
}
```

```json
{
  "key": "feedback.sla",
  "value": { "hours": 72 },
  "version": 2
}
```

- 主要错误：`409 CONFIG_VERSION_CONFLICT`。

#### GET `/api/v1/admin/audit-events`

- 鉴权：`ADMIN_READ`
- Query：`limit` 可选，默认 100；服务层最终限制在 1–200。
- 成功响应：`200 AuditEventList`。
- 列表按 `occurred_at` 倒序返回。

### 4.3 内容管理

#### POST `/api/v1/admin/content/scenes/{scene_id}/revisions`

- 鉴权：`ADMIN_WRITE`
- 路径参数：`scene_id`
- 请求体：`CreateRevisionRequest`；`source_revision_id` 可为 `null`。
- 成功响应：`201 SceneRevisionSummary`

```json
{
  "source_revision_id": null
}
```

```json
{
  "id": "01K6REVISION00000000000001",
  "scene_id": "scene_daily_greeting",
  "source_revision_id": null,
  "status": "DRAFT"
}
```

- 主要错误：`404 SCENE_NOT_FOUND`、`422 SOURCE_REVISION_INVALID`。

#### POST `/api/v1/admin/content/revisions/{revision_id}/publish-checks`

- 鉴权：`ADMIN_READ`
- 路径参数：`revision_id`
- 请求体：`PublishRevisionRequest`，可传已确认的警告码集合。
- 成功响应：`200 PublishCheckSummary`

```json
{
  "acknowledged_warning_codes": []
}
```

```json
{
  "revision_id": "01K6REVISION00000000000001",
  "ready": true,
  "error_codes": [],
  "warning_codes": []
}
```

- 主要错误：`404 REVISION_NOT_FOUND`。

#### POST `/api/v1/admin/content/revisions/{revision_id}/commands/publish`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 路径参数：`revision_id`
- 请求体：`PublishRevisionRequest`
- 成功响应：`200 PublishedRevision`

```json
{
  "scene_id": "scene_daily_greeting",
  "revision_id": "01K6REVISION00000000000001",
  "published_at": "2026-09-28T12:30:00Z"
}
```

- 主要错误：`404 REVISION_NOT_FOUND`、`409 PUBLISH_CHECK_FAILED`、`409 PUBLISH_WARNING_NOT_ACKNOWLEDGED`、`409 PUBLISHED_REVISION_IMMUTABLE`、`409 IDEMPOTENCY_KEY_REUSED`。

#### PUT `/api/v1/admin/content/open-scenes`

- 鉴权：`ADMIN_WRITE`
- 请求体：`OpenScenesRequest`，必须恰好包含 3 个不同的已发布场景。
- 成功响应：`200 OpenSceneConfig`

```json
{
  "scene_ids": ["scene_1", "scene_2", "scene_3"]
}
```

```json
{
  "version": 2,
  "scene_ids": ["scene_1", "scene_2", "scene_3"],
  "activated_at": "2026-09-28T12:30:00Z"
}
```

- 主要错误：`422 OPEN_SCENES_INVALID`、`409 OPEN_SCENE_NOT_PUBLISHED`。

#### PUT `/api/v1/admin/content/preview-configs/{series_id}`

- 鉴权：`ADMIN_WRITE`
- 路径参数：`series_id`
- 请求体：`PreviewScenesRequest`，必须包含 3–6 个不同的场景。
- 约束：场景必须已发布、属于该系列，且不能同时是开放场景。
- 成功响应：`200 PreviewSceneConfig`

```json
{
  "series_id": "series_a",
  "scene_ids": ["scene_4", "scene_5", "scene_6"],
  "updated_at": "2026-09-28T12:30:00Z"
}
```

- 主要错误：`422 PREVIEW_SCENES_INVALID`、`409 PREVIEW_SCENE_IS_OPEN`、`409 PREVIEW_SCENE_NOT_PUBLISHED`。

#### POST `/api/v1/admin/content/scenes/{scene_id}/commands/offline`

- 鉴权：`ADMIN_WRITE`
- 路径参数：`scene_id`
- 请求体：无
- 成功响应：`204`
- 主要错误：`404 SCENE_NOT_FOUND`、`409 OPEN_SCENE_CANNOT_OFFLINE`。

### 4.4 正式权益

#### POST `/api/v1/admin/formal-entitlements/preview-operation`

- 鉴权：`ADMIN_READ`
- Query：`operation` 必填，取值见 `EntitlementOperation`。
- 请求体：`EntitlementCommandRequest`
- 成功响应：`200 FormalEntitlement`
- 行为：只计算执行后的权益结果，不保存。

```json
{
  "user_id": "user_001",
  "package_id": "package_all",
  "term": "MONTH_3",
  "reason": "客服补发"
}
```

#### POST `/api/v1/admin/formal-entitlements/commands/{operation}`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 路径参数：`operation`，取值见 `EntitlementOperation`。
- 请求体：`EntitlementCommandRequest`。
- 约束：`GRANT` 和 `RENEW` 必须传 `term`；暂停、恢复、撤销操作会校验当前权益状态。
- 成功响应：`200 FormalEntitlement`

```json
{
  "id": "01K6ENTITLEMENT00000000001",
  "user_id": "user_001",
  "package_id": "package_all",
  "status": "ACTIVE",
  "term": "MONTH_3",
  "granted_at": "2026-09-28T12:30:00Z",
  "expires_at": "2026-12-28T12:30:00Z",
  "version": 1
}
```

- 主要错误：`404 ENTITLEMENT_NOT_FOUND`、`404 CONTENT_PACKAGE_NOT_FOUND`、`409 USER_NOT_ELIGIBLE`、`409 ENTITLEMENT_STATE_CONFLICT`、`409 ENTITLEMENT_EXPIRED`、`409 PERMANENT_ENTITLEMENT_CANNOT_EXTEND`、`422 ENTITLEMENT_TERM_REQUIRED`、`422 ENTITLEMENT_OPERATION_INVALID`、`409 IDEMPOTENCY_KEY_REUSED`。

### 4.5 限时权益

限时权益成功响应统一使用 `LimitedEntitlement`：

```json
{
  "id": "01K6LIMITED000000000000001",
  "user_id": "user_001",
  "campaign_version_id": "campaign_v1",
  "status": "PENDING",
  "granted_at": "2026-09-28T12:30:00Z",
  "start_deadline": "2026-10-01T12:30:00Z",
  "activated_at": null,
  "expires_at": null,
  "remedy_count": 0,
  "version": 1
}
```

#### POST `/api/v1/admin/limited-entitlements/commands/grant`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 请求体：`GrantRequest`
- 成功响应：`201 LimitedEntitlement`

```json
{
  "user_id": "user_001",
  "campaign_version_id": "campaign_v1"
}
```

- 主要错误：`404 CAMPAIGN_VERSION_NOT_FOUND`、`409 CAMPAIGN_NOT_OPEN`、`409 CAMPAIGN_CAPACITY_REACHED`、`409 LIMITED_ENTITLEMENT_EXISTS`、`409 USER_NOT_ELIGIBLE`、`422 CAMPAIGN_DURATION_INVALID`、`409 IDEMPOTENCY_KEY_REUSED`。

#### POST `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/remedy`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 路径参数：`entitlement_id`
- 请求体：`RemedyRequest`
- 成功响应：`200 LimitedEntitlement`

```json
{
  "mode": "EXTEND_START_DEADLINE"
}
```

- `EXTEND_START_DEADLINE`：对 `PENDING` 权益延长一个启动窗口。
- `RESTORE_START_WINDOW`：将 `START_EXPIRED` 权益恢复为 `PENDING`。
- 每条权益最多补救一次。
- 主要错误：`404 LIMITED_ENTITLEMENT_NOT_FOUND`、`409 LIMITED_ACTIVE_CANNOT_EXTEND`、`409 LIMITED_REMEDY_ALREADY_USED`、`409 LIMITED_REMEDY_STATE_CONFLICT`。

#### POST `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/pause`

- 鉴权：`ADMIN_WRITE`
- 请求体：`ReasonRequest`
- 成功响应：`200 LimitedEntitlement`
- 主要错误：`404 LIMITED_ENTITLEMENT_NOT_FOUND`、`409 LIMITED_STATE_CONFLICT`。

#### POST `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/resume`

- 鉴权：`ADMIN_WRITE`
- 请求体：无
- 成功响应：`200 LimitedEntitlement`
- 主要错误：`404 LIMITED_ENTITLEMENT_NOT_FOUND`、`409 LIMITED_STATE_CONFLICT`、`409 LIMITED_ENTITLEMENT_EXPIRED`。

#### POST `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/revoke`

- 鉴权：`ADMIN_WRITE`
- 请求体：`ReasonRequest`
- 成功响应：`200 LimitedEntitlement`
- 主要错误：`404 LIMITED_ENTITLEMENT_NOT_FOUND`、`409 LIMITED_STATE_CONFLICT`。

### 4.6 反馈管理

反馈命令和内部用户端接口的成功响应使用 `FeedbackTicket`；管理端详情 GET 使用包含该对象字段的 `FeedbackAdminDetailResponse`，并附带时间线、截图元数据、补充轮次、回复和内部备注：

```json
{
  "id": "01K6FEEDBACK00000000000001",
  "user_id": "user_001",
  "category": "CONTENT",
  "description": "例句中的翻译不准确",
  "source": { "scene_id": "scene_1", "entry_id": "entry_2" },
  "status": "PROCESSING",
  "deadline_at": "2026-09-30T12:30:00Z",
  "sla_remaining_seconds": null,
  "supplement_rounds": 0,
  "reopen_count": 0,
  "created_at": "2026-09-28T12:30:00Z",
  "updated_at": "2026-09-28T12:35:00Z",
  "resolved_at": null,
  "closed_at": null
}
```

#### GET `/api/v1/admin/feedback/{ticket_id}`

- 鉴权：`ADMIN_READ`
- 路径参数：`ticket_id`
- 成功响应：`200 FeedbackAdminDetailResponse`，包含上述 FeedbackTicket 基础字段和 `timeline[]`、`screenshots[]`、`rounds[]`、`replies[]`、`internal_notes[]`。截图项为 `security_status/delete_after/deleted_at`，没有对象键和访问 URL；临时地址必须单独申请。
- 主要错误：`404 FEEDBACK_NOT_FOUND`。

#### POST `/api/v1/admin/feedback/{ticket_id}/commands/start`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 请求体：无
- 前置状态：`PENDING` 或 `USER_SUPPLIED`
- 结果状态：`PROCESSING`
- 成功响应：`200 FeedbackTicket`

#### POST `/api/v1/admin/feedback/{ticket_id}/commands/request-supplement`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 请求体：`SupplementCommand`
- 前置状态：`PROCESSING`
- 结果状态：`NEED_MORE`；最多要求补充两轮，等待补充期间暂停 SLA 倒计时。

```json
{
  "request_text": "请补充出现问题时的操作步骤"
}
```

#### POST `/api/v1/admin/feedback/{ticket_id}/commands/resolve`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 请求体：`ResolveCommand`
- `template` 只允许 `RESOLVED` 或 `TEMPORARILY_UNAVAILABLE`。
- 前置状态：`PROCESSING` 或 `USER_SUPPLIED`
- 结果状态：`RESOLVED`

```json
{
  "template": "RESOLVED",
  "note": "内容已修正，下次发布生效"
}
```

#### POST `/api/v1/admin/feedback/{ticket_id}/commands/close-insufficient`

- 鉴权：`ADMIN_WRITE`
- Header：`X-Idempotency-Key` 必填。
- 请求体：`CloseCommand`
- 前置状态：`PROCESSING`、`NEED_MORE` 或 `USER_SUPPLIED`
- 结果状态：`CLOSED_INSUFFICIENT`

```json
{
  "reason": "多次未获得可复现信息"
}
```

反馈管理命令的共性错误：`404 FEEDBACK_NOT_FOUND`、`409 FEEDBACK_STATE_CONFLICT`、`409 FEEDBACK_SUPPLEMENT_LIMIT`、`422 FEEDBACK_REPLY_INVALID`、`422 FEEDBACK_TEMPLATE_INVALID`、`409 IDEMPOTENCY_KEY_REUSED`。

### 4.7 管理端媒体

#### POST `/api/v1/admin/media/upload-policies`

- 鉴权：`ADMIN_WRITE`
- 请求体：`UploadPolicyRequest`
- `asset_type` 只允许 `images` 或 `audio`。
- 成功响应：`200 UploadPolicy`，有效期 600 秒。

```json
{
  "asset_type": "images"
}
```

```json
{
  "upload_url": "https://example-bucket.oss-cn-hangzhou.aliyuncs.com",
  "object_key_prefix": "uploads/images/admin_001/",
  "max_bytes": 20971520,
  "expires_in": 600,
  "fields": {
    "key": "uploads/images/admin_001/${filename}",
    "policy": "...",
    "x-oss-signature": "..."
  }
}
```

- 图片上限 20 MiB；音频上限 50 MiB。
- 主要错误：`422 MEDIA_TYPE_INVALID`、`422 MEDIA_ACTOR_INVALID`。

#### POST `/api/v1/admin/media/uploads/confirm`

- 鉴权：`ADMIN_WRITE`
- 请求体：`ConfirmUploadRequest`
- 成功响应：`201 MediaAsset`

```json
{
  "asset_type": "images",
  "object_key": "uploads/images/admin_001/cover.webp"
}
```

```json
{
  "id": "01K6MEDIA0000000000000001",
  "asset_type": "images",
  "content_type": "image/webp",
  "size": 345678,
  "sha256": "d2a4...64-hex-characters...9f",
  "status": "CONFIRMED"
}
```

- 图片 MIME：`image/jpeg`、`image/png`、`image/webp`。
- 音频 MIME：`audio/mpeg`、`audio/mp4`、`audio/x-m4a`、`audio/wav`、`audio/x-wav`、`audio/aac`。
- 音频扩展名：`.mp3`、`.m4a`、`.wav`、`.aac`。
- OSS 对象元数据必须包含有效 SHA-256、`decodable=true` 和 `security_status=PASSED`。
- 主要错误：`422 MEDIA_OBJECT_KEY_INVALID`、`422 MEDIA_MIME_INVALID`、`422 MEDIA_SIZE_INVALID`、`422 MEDIA_HASH_INVALID`、`422 MEDIA_DECODE_FAILED`、`422 MEDIA_SECURITY_BLOCKED`、`422 MEDIA_EXTENSION_INVALID`。

### 4.8 运营查询

#### GET `/api/v1/admin/users`

- 鉴权：`ADMIN_READ`
- Query：`query` 可选，最长 64，对用户 ID 进行包含匹配；`contact_status` 可选，使用五种联系状态进行服务端筛选。
- 成功响应：`200 UserProjection[]`
- 响应包含完整联系投影并设置 `Cache-Control: no-store`。联系服务不可用且未筛选联系状态时，基础投影仍可返回，`contact=null`、`contact_degraded=true`；使用联系状态筛选时返回 `503`，不会伪造筛选结果。

#### POST `/api/v1/admin/users/search-by-wechat`

- 鉴权：`ADMIN_READ`
- 请求体：`WechatSearchRequest`
- 服务端会调用 `juya-miniapp-api` 查找对应用户 ID，再返回本服务的用户投影。
- 微信号只能出现在 POST JSON 正文，不得进入 URL、持久化存储、普通日志或统计事件。
- 成功响应：`200 UserProjection[]`

```json
{
  "wechat_id": "wx_example"
}
```

#### GET `/api/v1/admin/users/{user_id}`

- 鉴权：`ADMIN_READ`
- 成功响应：`200 UserDetail`

```json
{
  "user_id": "user_001",
  "account_status": "ACTIVE",
  "last_active_at": "2026-09-28T12:00:00Z",
  "formal_entitlement_count": 1,
  "limited_entitlement_count": 0,
  "open_feedback_count": 1,
  "contact_degraded": false,
  "contact": {
    "wechat_id": "wx_example",
    "contact_status": "CONTACTED",
    "change_pending": false,
    "verified_at": "2026-09-28T11:00:00Z",
    "verified_by": "7",
    "updated_at": "2026-09-28T12:00:00Z"
  },
  "learning_degraded": false,
  "open_scene_completed_count": 7,
  "learning_days": 12,
  "favorite_count": 4
}
```

- `contact` 可为 `null`；`contact_degraded=true` 表示联系方式上游查询已降级。
- 学习概况不可用时三个计数均为 `null` 且 `learning_degraded=true`，不使用 0 冒充真实统计。
- 主要错误：`404 USER_NOT_FOUND`、`502 MINIAPP_API_INVALID_RESPONSE`、`503 MINIAPP_API_UNAVAILABLE`。

#### POST `/api/v1/admin/users/{user_id}/commands/contact-status`

- 鉴权：`ADMIN_WRITE`
- 请求体：`{"status":"CONTACTED"}`，状态只能为 `NOT_PROVIDED`、`PENDING`、`CONTACTED`、`UNREACHABLE`、`DO_NOT_CONTACT`。
- 成功响应：`200 ContactProjectionResponse`，并写入脱敏审计 `contact.status.update`。

#### POST `/api/v1/admin/users/{user_id}/commands/verify-contact-change`

- 鉴权：`ADMIN_WRITE`
- 请求体：无。
- 成功响应：`200 ContactProjectionResponse`，只更新核对信息和待核对标志，并写入 `contact.change.verify`。

#### POST `/api/v1/admin/users/{user_id}/contact-copy-events`

- 鉴权：`ADMIN_WRITE`
- 请求体：无；成功响应 `204`。
- 前端必须先获得该接口成功响应，之后才能写入剪贴板。审计摘要不包含微信号。

#### GET `/api/v1/admin/contact-corrections`

- 鉴权：`ADMIN_READ`
- Query：`status` 可选，`page` 默认 1，`page_size` 默认 20、范围 1–100。
- 成功响应：分页对象 `items/total/page/page_size`；响应设置 `Cache-Control: no-store`。

#### GET `/api/v1/admin/contact-corrections/{correction_id}`

- 鉴权：`ADMIN_READ`
- 返回当前微信号、更正原因、申请状态和脱敏时间线，不返回不存在的“新微信号”字段。

#### POST `/api/v1/admin/contact-corrections/{correction_id}/commands/{command}`

- 鉴权：`ADMIN_WRITE`，并要求 `X-Idempotency-Key`。
- `command` 只能为 `approve` 或 `reject`。
- 批准仅重置一次用户自助修改机会，不直接改写微信号。
- 相同幂等键和相同请求安全重放；同一键用于不同申请或不同决定返回 `409 IDEMPOTENCY_KEY_REUSED`。
- 成功响应：`{"id":"COR-1","status":"APPROVED","processed_at":"2026-09-29T10:00:00Z"}`。

#### GET `/api/v1/admin/dashboard`

- 鉴权：`ADMIN_READ`
- 成功响应：`200 DashboardSnapshot`

```json
{
  "active_users": 120,
  "open_feedback": 8,
  "overdue_feedback": 1,
  "expiring_entitlements": 6,
  "failed_jobs": 0
}
```

#### GET `/api/v1/admin/work-items`

- 鉴权：`ADMIN_READ`
- 成功响应：`200 WorkItem[]`，按 `priority_rank`、`due_at`、`key` 升序。

```json
[
  {
    "key": "feedback:01K6FEEDBACK00000000000001",
    "kind": "FEEDBACK_OVERDUE",
    "priority_rank": 10,
    "due_at": "2026-09-28T11:30:00Z"
  }
]
```

#### GET `/api/v1/admin/analytics/export`

- 鉴权：`ADMIN_READ`
- Query：`start` 和 `end` 均必填，格式为 `YYYY-MM-DD`，数据区间包含起止日期。
- 成功响应：`200 AnalyticsRow[]`

```json
[
  {
    "day": "2026-09-28",
    "metric": "ACTIVE_USERS",
    "dimension": "all",
    "value": 120
  }
]
```

- 仅允许导出附录中的匿名汇总指标，维度不得包含用户或联系方式标识。
- 主要错误：`422 ANALYTICS_EXPORT_FORBIDDEN`。

### 4.9 内部内容与访问策略

本节全部接口均使用 `INTERNAL_HMAC`鉴权。

#### GET `/internal/v1/learning/modules`

- 请求参数：无
- 成功响应：`200 LearningModuleList`，按 `sort_order` 排序。

```json
{
  "items": [
    {
      "module_type": "SCENE",
      "display_name": "场景英语",
      "sort_order": 10,
      "entry_path": "/pages/learning/scenes"
    }
  ]
}
```

#### POST `/internal/v1/learning/catalog`

- 请求体：`UserQuery`
- 成功响应：`200 LearningCatalog`

```json
{
  "user_id": "user_001"
}
```

```json
{
  "items": [
    {
      "public_id": "scene_1",
      "title": "日常问候",
      "summary": "学习常见问候表达",
      "series": "日常交流",
      "cover_object_key": "published/covers/scene_1.webp"
    }
  ]
}
```

#### POST `/internal/v1/access/batch`

- 请求体：`AccessBatchRequest`，`scene_ids` 最多 100 个。
- 成功响应：`200 AccessDecisionList`，返回顺序与请求中 `scene_ids` 的顺序一致。

```json
{
  "user_id": "user_001",
  "scene_ids": ["scene_1", "scene_2"]
}
```

```json
{
  "items": [
    {
      "scene_id": "scene_1",
      "level": "FORMAL",
      "sources": ["formal:01K6ENTITLEMENT00000000001"],
      "earliest_expires_at": "2026-12-28T12:30:00Z"
    },
    {
      "scene_id": "scene_2",
      "level": "PREVIEW",
      "sources": ["preview:series_a"],
      "earliest_expires_at": null
    }
  ]
}
```

#### POST `/internal/v1/scenes/{scene_id}/open`

- 路径参数：`scene_id`
- 请求体：`UserQuery`
- 行为：如场景属于待激活的限时权益，此请求会触发激活，然后再计算访问级别。
- 成功响应：`200 SceneOpenResult`

```json
{
  "access": "LIMITED",
  "sources": ["limited:01K6LIMITED000000000000001"],
  "earliest_expires_at": "2026-10-03T12:30:00Z",
  "activated_at": "2026-09-28T12:30:00Z",
  "scene": {
    "public_id": "scene_1",
    "title": "日常问候",
    "entries": []
  }
}
```

- `PREVIEW` 权限只返回以下可见字段：`public_id`、`title`、`series`、`cover_url`、`introduction`、`preview_status`。
- `OPEN`、`FORMAL`、`LIMITED` 权限返回已发布版本的完整 `content_snapshot`，其内部字段由内容模型决定。
- 主要错误：`403 SCENE_ACCESS_DENIED`、`404 SCENE_NOT_FOUND`。

#### POST `/internal/v1/scenes/{scene_id}/entries/{entry_id}`

- 路径参数：`scene_id`、`entry_id`
- 请求体：`UserQuery`
- 要求：用户必须拥有完整场景访问权；`PREVIEW` 不可访问条目详情。
- 成功响应：`200 SceneEntry`

```json
{
  "stable_id": "entry_001",
  "entry_type": "SENTENCE",
  "normalized_english": "How are you?",
  "phonetic": "haʊ ɑːr juː",
  "chinese_text": "你好吗？",
  "explanation": "常用问候表达"
}
```

- 主要错误：`403 SCENE_ACCESS_DENIED`、`404 ENTRY_NOT_FOUND`。

### 4.10 内部反馈

本节全部接口均使用 `INTERNAL_HMAC`鉴权。

#### POST `/internal/v1/feedback`

- Header：`X-Idempotency-Key` 必填。
- 请求体：`FeedbackCreateRequest`
- 成功响应：`200 FeedbackTicket`，初始状态为 `PENDING`。

```json
{
  "user_id": "user_001",
  "category": "CONTENT",
  "description": "例句中的翻译不准确",
  "source": { "scene_id": "scene_1", "entry_id": "entry_2" },
  "screenshots": ["feedback/user_001/screenshot.webp"]
}
```

- `category`：`CONTENT`、`PRONUNCIATION`、`DISPLAY`、`FUNCTION`。
- `screenshots` 最多 1 个 OSS 对象键。
- 主要错误：`404 USER_NOT_FOUND`、`422 FEEDBACK_CATEGORY_INVALID`、`422 FEEDBACK_DESCRIPTION_REQUIRED`、`422 FEEDBACK_DESCRIPTION_TOO_LONG`、`422 FEEDBACK_SCREENSHOT_LIMIT`。

#### GET `/internal/v1/feedback/{ticket_id}`

- 路径参数：`ticket_id`
- 成功响应：`200 FeedbackTicket`
- 注意：当前代码只验证调用服务身份，没有从请求中校验反馈所有者；上游服务不得将该端点直接暴露给客户端。
- 主要错误：`404 FEEDBACK_NOT_FOUND`。

#### POST `/internal/v1/feedback/{ticket_id}/supplements`

- Header：`X-Idempotency-Key` 必填。
- 请求体：`SupplementRequest`
- 前置状态：`NEED_MORE`
- 结果状态：`USER_SUPPLIED`，重新开始 SLA 倒计时。

```json
{
  "user_id": "user_001",
  "text": "在 iPhone 微信 8.0.50 中点击播放后出现"
}
```

#### POST `/internal/v1/feedback/{ticket_id}/resolution`

- Header：`X-Idempotency-Key` 必填。
- 请求体：`UserResolutionRequest`
- 当 `action="REOPEN"` 时，进行重开；要求当前状态为 `RESOLVED`、解决后不超过 7 天，且每条反馈最多重开一次。
- 其他 `action` 值在当前实现中不执行状态变更，直接返回当前反馈。客户端应只传 `REOPEN`。
- 成功响应：`200 FeedbackTicket`

```json
{
  "user_id": "user_001",
  "action": "REOPEN",
  "reason": "新版本中问题仍存在"
}
```

- 主要错误：`404 FEEDBACK_NOT_FOUND`、`409 FEEDBACK_STATE_CONFLICT`、`409 FEEDBACK_REOPEN_LIMIT`、`409 FEEDBACK_REOPEN_WINDOW_EXPIRED`、`422 FEEDBACK_REOPEN_REASON_INVALID`。

### 4.11 内部媒体

#### GET `/internal/v1/media/{target_id}/signed-url`

- 鉴权：`INTERNAL_HMAC`
- 路径参数：`target_id`，媒体目标标识。
- Header：`X-User-ID` 必填，用于校验场景访问权。
- 成功响应：`200 SignedMedia`

```json
{
  "url": "https://example-bucket.oss-cn-hangzhou.aliyuncs.com/private/audio.mp3?...",
  "expires_at": "2026-09-28T12:35:00Z"
}
```

- 默认签名 URL 有效期为 300 秒；如权益更早到期，URL 有效期会截断到权益到期时间。
- 主要错误：`404 MEDIA_TARGET_NOT_FOUND`、`403 MEDIA_ACCESS_DENIED`、`403 MEDIA_ACCESS_EXPIRED`。

### 4.12 内部用户数据清理

#### POST `/internal/v1/account-deletions`

- 鉴权：`INTERNAL_HMAC`，含时间戳和一次性 nonce，按实际请求字节签名。
- 请求体：严格 `AccountDeletionRequest`；`user_id`、`deletion_request_id`、`event_id` 均为 26 字符标识。
- 用户及其注销请求必须处于 `DELETING` 或 `DELETED`；不接受尚未生效或其他用户的请求。
- 成功返回 `200`，含 `event_id`、`user_id`、`status: COMPLETED`、`completed_at`。这表示管理服务清理事务已提交，用户端最终状态仍需回调。
- 清理与 `DELETION_CLEANUP_RESULT` outbox 同事务持久化；管理 Worker 带 HMAC 回调用户服务 `/internal/v1/users/{user_id}/deletion-cleanup-result`，携带注销请求标识和 `succeeded: true`。
- 同事件、同用户、同注销请求可重放；跨用户/跨注销请求重用事件返回 `409 DELETION_EVENT_CONFLICT`。临时回调失败按退避重试，任务重新投递不会重复清理。
- 被本次注销捕获的截图须等待回调已发布且用户与请求均为 `DELETED` 才清理，不因反馈已结单或匿名字段为空绕过完成检查。

#### POST `/internal/v1/users/{user_id}/deletion`

- 鉴权：`INTERNAL_HMAC`
- 路径参数：`user_id`
- 请求体：`DeletionRequest`
- `event_id` 是上游注销事件唯一标识，同一事件可幂等重试。
- 成功响应：`200 DeletionResult`

```json
{
  "event_id": "delete-event-001"
}
```

```json
{
  "event_id": "delete-event-001",
  "user_id": "user_001",
  "status": "COMPLETED",
  "completed_at": "2026-09-28T12:30:00Z"
}
```

- 主要错误：`404 USER_NOT_FOUND`、`409 DELETION_EVENT_CONFLICT`。

## 5. 请求模型

### 5.1 登录与系统

| 模型                   | 字段               | 类型    | 必填 | 约束        |
| ---------------------- | ------------------ | ------- | ---- | ----------- |
| `PasswordLoginRequest` | `username`         | string  | 是   | 1–100 字符  |
|                        | `password`         | string  | 是   | 1–1024 字符 |
| `ConfigUpdateRequest`  | `value`            | object  | 是   | 配置 JSON   |
|                        | `expected_version` | integer | 是   | `>= 1`      |

### 5.2 内容

| 模型                     | 字段                         | 类型        | 必填 | 约束                          |
| ------------------------ | ---------------------------- | ----------- | ---- | ----------------------------- |
| `CreateRevisionRequest`  | `source_revision_id`         | string/null | 否   | 默认`null`                    |
| `PublishRevisionRequest` | `acknowledged_warning_codes` | string[]    | 否   | 元素唯一，默认`[]`            |
| `OpenScenesRequest`      | `scene_ids`                  | string[]    | 是   | 恰好 3 个；业务上必须互不相同 |
| `PreviewScenesRequest`   | `scene_ids`                  | string[]    | 是   | 3–6 个；业务上必须互不相同    |

### 5.3 权益

| 模型                        | 字段                  | 类型                   | 必填     | 约束                 |
| --------------------------- | --------------------- | ---------------------- | -------- | -------------------- |
| `EntitlementCommandRequest` | `user_id`             | string                 | 是       | 1–64 字符            |
|                             | `package_id`          | string                 | 是       | 1–64 字符            |
|                             | `term`                | `EntitlementTerm`/null | 条件必填 | `GRANT`/`RENEW` 必填 |
|                             | `reason`              | string/null            | 否       | 最长 500             |
| `GrantRequest`              | `user_id`             | string                 | 是       | 1–64 字符            |
|                             | `campaign_version_id` | string                 | 是       | 1–64 字符            |
| `RemedyRequest`             | `mode`                | `RemedyMode`           | 是       | 见枚举               |
| `ReasonRequest`             | `reason`              | string                 | 是       | 1–500 字符           |

### 5.4 反馈

| 模型                    | 字段           | 类型        | 必填 | 约束                                           |
| ----------------------- | -------------- | ----------- | ---- | ---------------------------------------------- |
| `FeedbackCreateRequest` | `user_id`      | string      | 是   | 1–64 字符                                      |
|                         | `category`     | string      | 是   | `CONTENT`/`PRONUNCIATION`/`DISPLAY`/`FUNCTION` |
|                         | `description`  | string      | 是   | 1–300 字符，不可全空白                         |
|                         | `source`       | object      | 否   | 默认`{}`                                       |
|                         | `screenshots`  | string[]    | 否   | 最多 1 个，默认`[]`                            |
| `SupplementRequest`     | `user_id`      | string      | 是   | 1–64 字符                                      |
|                         | `text`         | string      | 是   | 1–300 字符，不可全穽                           |
| `UserResolutionRequest` | `user_id`      | string      | 是   | 1–64 字符                                      |
|                         | `action`       | string      | 是   | 客户端应传`REOPEN`                             |
|                         | `reason`       | string/null | 否   | 重开时最长 300，空值使用默认原因               |
| `SupplementCommand`     | `request_text` | string      | 是   | 1–200 字符，不可全空白                         |
| `ResolveCommand`        | `template`     | string      | 是   | `RESOLVED`/`TEMPORARILY_UNAVAILABLE`           |
|                         | `note`         | string/null | 否   | 最长 200                                       |
| `CloseCommand`          | `reason`       | string      | 是   | 1–200 字符，不可全空白                         |

### 5.5 媒体、查询与清理

| 模型                   | 字段         | 类型     | 必填 | 约束                                     |
| ---------------------- | ------------ | -------- | ---- | ---------------------------------------- |
| `UploadPolicyRequest`  | `asset_type` | string   | 是   | `images`/`audio`                         |
| `ConfirmUploadRequest` | `asset_type` | string   | 是   | `images`/`audio`                         |
|                        | `object_key` | string   | 是   | 1–512 字符，必须位于当前管理员上传前缀下 |
| `WechatSearchRequest`  | `wechat_id`  | string   | 是   | 1–64 字符                                |
| `UserQuery`            | `user_id`    | string   | 是   | 1–64 字符                                |
| `AccessBatchRequest`   | `user_id`    | string   | 是   | 1–64 字符                                |
|                        | `scene_ids`  | string[] | 是   | 最多 100 个                              |
| `DeletionRequest`      | `event_id`   | string   | 是   | 1–128 字符                               |

## 6. 核心响应模型

### 6.1 `AuditEvent`

| 字段               | 类型        | 说明                                |
| ------------------ | ----------- | ----------------------------------- |
| `actor_public_id`  | string      | 操作人对外 ID，系统任务可为`system` |
| `action`           | string      | 操作类型                            |
| `object_type`      | string      | 对象类型                            |
| `object_public_id` | string      | 对象对外 ID                         |
| `before_summary`   | object      | 变更前的脱敏摘要                    |
| `after_summary`    | object      | 变更后的脱敏摘要                    |
| `reason`           | string/null | 操作原因                            |
| `request_id`       | string      | 请求追踪标识                        |
| `occurred_at`      | datetime    | 发生时间                            |

### 6.2 `FormalEntitlement`

| 字段         | 类型              | 说明                        |
| ------------ | ----------------- | --------------------------- |
| `id`         | string            | 权益对外 ID                 |
| `user_id`    | string            | 用户对外 ID                 |
| `package_id` | string            | 内容包对外 ID               |
| `status`     | string            | `ACTIVE`/`PAUSED`/`REVOKED` |
| `term`       | `EntitlementTerm` | 权益期限                    |
| `granted_at` | datetime          | 首次授予时间                |
| `expires_at` | datetime/null     | 到期时间；永久权益为`null`  |
| `version`    | integer           | 乐观锁版本                  |

### 6.3 `LimitedEntitlement`

| 字段                  | 类型          | 说明                                                          |
| --------------------- | ------------- | ------------------------------------------------------------- |
| `id`                  | string        | 限时权益对外 ID                                               |
| `user_id`             | string        | 用户对外 ID                                                   |
| `campaign_version_id` | string        | 活动版本 ID                                                   |
| `status`              | string        | `PENDING`/`ACTIVE`/`PAUSED`/`ENDED`/`START_EXPIRED`/`REVOKED` |
| `granted_at`          | datetime      | 授予时间                                                      |
| `start_deadline`      | datetime      | 最迟激活时间                                                  |
| `activated_at`        | datetime/null | 首次打开包含场景时的激活时间                                  |
| `expires_at`          | datetime/null | 激活后的到期时间                                              |
| `remedy_count`        | integer       | 已使用补救次数，最大为 1                                      |
| `version`             | integer       | 乐观锁版本                                                    |

### 6.4 `FeedbackTicket`

| 字段                    | 类型          | 说明                              |
| ----------------------- | ------------- | --------------------------------- |
| `id`                    | string        | 反馈 ID                           |
| `user_id`               | string        | 所有者用户 ID                     |
| `category`              | string        | 反馈分类                          |
| `description`           | string        | 反馈说明                          |
| `source`                | object        | 场景、条目、页面等来源信息        |
| `status`                | string        | 反馈状态，取值见第 7 节           |
| `deadline_at`           | datetime/null | 当前 SLA 截止时间                 |
| `sla_remaining_seconds` | integer/null  | 等待用户补充时冻结的 SLA 剩余秒数 |
| `supplement_rounds`     | integer       | 补充轮次                          |
| `reopen_count`          | integer       | 重开次数                          |
| `created_at`            | datetime      | 创建时间                          |
| `updated_at`            | datetime      | 最后更新时间                      |
| `resolved_at`           | datetime/null | 解决时间                          |
| `closed_at`             | datetime/null | 关闭时间                          |

### 6.5 `UserProjection`

| 字段                        | 类型          | 说明                                                     |
| --------------------------- | ------------- | -------------------------------------------------------- |
| `user_id`                   | string        | 用户对外 ID                                              |
| `account_status`            | string        | 账号状态                                                 |
| `last_active_at`            | datetime/null | 最近活跃时间                                             |
| `formal_entitlement_count`  | integer       | 正式权益数                                               |
| `limited_entitlement_count` | integer       | 限时权益数                                               |
| `open_feedback_count`       | integer       | 未完结反馈数                                             |
| `contact`                   | object/null   | 完整联系投影，含微信号、五态状态、待核对标志和核对元数据 |
| `contact_degraded`          | boolean       | 联系服务是否降级                                         |

用户详情在上述字段外增加 `learning_degraded`、`open_scene_completed_count`、`learning_days` 和 `favorite_count`。

### 6.6 `WorkItem`

| 字段            | 类型     | 说明               |
| --------------- | -------- | ------------------ |
| `key`           | string   | 待办唯一键         |
| `kind`          | string   | 待办类型           |
| `priority_rank` | integer  | 数字越小优先级越高 |
| `due_at`        | datetime | 截止时间           |

### 6.7 `AnalyticsRow`

| 字段        | 类型    | 说明         |
| ----------- | ------- | ------------ |
| `day`       | date    | 统计日期     |
| `metric`    | string  | 指标名       |
| `dimension` | string  | 匿名聚合维度 |
| `value`     | integer | 指标值       |

## 7. 枚举与状态

### 7.1 正式权益操作 `EntitlementOperation`

| 值       | 说明               |
| -------- | ------------------ |
| `GRANT`  | 首次授予或重新授予 |
| `RENEW`  | 续期               |
| `PAUSE`  | 暂停               |
| `RESUME` | 恢复               |
| `REVOKE` | 撤销               |

### 7.2 正式权益期限 `EntitlementTerm`

| 值          | 说明                    |
| ----------- | ----------------------- |
| `MONTH_1`   | 1 个自然月              |
| `MONTH_2`   | 2 个自然月              |
| `MONTH_3`   | 3 个自然月              |
| `MONTH_6`   | 6 个自然月              |
| `MONTH_12`  | 12 个自然月             |
| `PERMANENT` | 永久，`expires_at=null` |

自然月在 `Asia/Shanghai` 时区计算后转回 UTC；目标月无同日时取该月最后一日。

### 7.3 限时权益补救 `RemedyMode`

| 值                      | 说明                               |
| ----------------------- | ---------------------------------- |
| `EXTEND_START_DEADLINE` | 延长`PENDING` 权益的启动截止时间   |
| `RESTORE_START_WINDOW`  | 重置`START_EXPIRED` 权益的启动窗口 |

### 7.4 访问级别 `AccessLevel`

| 值        | 说明                     |
| --------- | ------------------------ |
| `OPEN`    | 开放场景，可访问完整内容 |
| `FORMAL`  | 由正式权益授权           |
| `LIMITED` | 由限时权益授权           |
| `PREVIEW` | 仅可见预览信息           |
| `HIDDEN`  | 无权访问                 |

### 7.5 反馈状态

| 值                    | 说明                   |
| --------------------- | ---------------------- |
| `PENDING`             | 待处理                 |
| `PROCESSING`          | 处理中                 |
| `NEED_MORE`           | 等待用户补充           |
| `USER_SUPPLIED`       | 用户已补充，待继续处理 |
| `RESOLVED`            | 已解决                 |
| `CLOSED_INSUFFICIENT` | 因信息不足关闭         |

### 7.6 待办类型和优先级

| `kind`                        | `priority_rank` |
| ----------------------------- | --------------: |
| `FEEDBACK_OVERDUE`            |              10 |
| `USER_SUPPLIED`               |              20 |
| `FEEDBACK_DUE_SOON`           |              30 |
| `CAMPAIGN_STARTING`           |              40 |
| `CAMPAIGN_START_EXPIRED`      |              50 |
| `NEW_FEEDBACK`                |              60 |
| `ACTIVE_ENTITLEMENT_EXPIRING` |             100 |

### 7.7 可导出统计指标

`NEW_USERS`、`ACTIVE_USERS`、`SCENE_COMPLETIONS`、`CONTACT_FUNNEL`、`FORMAL_ENTITLEMENTS`、`LIMITED_STARTS`、`LIMITED_COMPLETIONS`、`FAVORITES`、`REVIEWS`、`FEEDBACK_SLA`、`DELETIONS`。

### 7.8 联系状态与更正状态

- 联系状态：`NOT_PROVIDED`（未填写）、`PENDING`（待联系）、`CONTACTED`（已联系）、`UNREACHABLE`（暂无法联系）、`DO_NOT_CONTACT`（用户不希望联系）。
- 更正申请状态：`PENDING`、`PROCESSING`、`APPROVED`、`REJECTED`、`CANCELLED`。

## 8. 错误码速查

### 8.1 通用、鉴权与幂等

| HTTP | 错误码                       | 含义                         |
| ---: | ---------------------------- | ---------------------------- |
|  401 | `ADMIN_SESSION_INVALID`      | 管理员会话缺失、无效或过期   |
|  403 | `CSRF_INVALID`               | CSRF Token 缺失或不匹配      |
|  401 | `INVALID_INTERNAL_SIGNATURE` | 内部请求头不完整或签名不匹配 |
|  401 | `INTERNAL_SIGNATURE_EXPIRED` | 内部请求时间戳超出 300 秒    |
|  403 | `INTERNAL_SERVICE_FORBIDDEN` | 调用服务不在允许列表         |
|  409 | `INTERNAL_REQUEST_REPLAYED`  | Nonce 在有效期内重复使用     |
|  409 | `IDEMPOTENCY_KEY_REUSED`     | 同一幂等键被用于不同请求     |
|  409 | `IDEMPOTENCY_IN_PROGRESS`    | 同一幂等请求正在处理         |
|  422 | `VALIDATION_ERROR`           | 请求参数不符合模型           |
|  503 | `SERVICE_NOT_READY`          | 依赖或数据库版本未就绪       |

### 8.2 登录、内容与配置

| HTTP | 错误码                             | 含义                           |
| ---: | ---------------------------------- | ------------------------------ |
|  401 | `INVALID_ADMIN_CREDENTIALS`        | 用户名或密码错误               |
|  429 | `ADMIN_LOGIN_LOCKED`               | 登录失败超限，账号/IP 暂时锁定 |
|  409 | `CONFIG_VERSION_CONFLICT`          | 配置版本冲突                   |
|  404 | `SCENE_NOT_FOUND`                  | 场景不存在                     |
|  404 | `REVISION_NOT_FOUND`               | 内容版本不存在                 |
|  422 | `SOURCE_REVISION_INVALID`          | 来源版本无效或不属于该场景     |
|  409 | `PUBLISHED_REVISION_IMMUTABLE`     | 已发布/已替代版本不可原地修改  |
|  409 | `PUBLISH_CHECK_FAILED`             | 发布前存在阻断性错误           |
|  409 | `PUBLISH_WARNING_NOT_ACKNOWLEDGED` | 存在尚未确认的发布警告         |
|  422 | `OPEN_SCENES_INVALID`              | 开放场景不是 3 个不同场景      |
|  409 | `OPEN_SCENE_NOT_PUBLISHED`         | 开放场景未发布                 |
|  409 | `OPEN_SCENE_CANNOT_OFFLINE`        | 场景仍在当前开放配置中         |
|  422 | `PREVIEW_SCENES_INVALID`           | 预览场景数量或唯一性不合法     |
|  409 | `PREVIEW_SCENE_IS_OPEN`            | 场景同时是开放场景             |
|  409 | `PREVIEW_SCENE_NOT_PUBLISHED`      | 场景未发布或不属于指定系列     |

### 8.3 权益与访问

| HTTP | 错误码                                | 含义                         |
| ---: | ------------------------------------- | ---------------------------- |
|  404 | `ENTITLEMENT_NOT_FOUND`               | 正式权益不存在               |
|  404 | `CONTENT_PACKAGE_NOT_FOUND`           | 内容包不存在或不可用         |
|  409 | `USER_NOT_ELIGIBLE`                   | 用户不存在或当前不可授权     |
|  409 | `ENTITLEMENT_STATE_CONFLICT`          | 正式权益当前状态不允许该操作 |
|  409 | `ENTITLEMENT_EXPIRED`                 | 正式权益已到期，不可恢复     |
|  409 | `PERMANENT_ENTITLEMENT_CANNOT_EXTEND` | 永久权益不可叠加期限         |
|  422 | `ENTITLEMENT_TERM_REQUIRED`           | 授予或续期没有指定期限       |
|  422 | `ENTITLEMENT_OPERATION_INVALID`       | 不支持的正式权益操作         |
|  404 | `CAMPAIGN_VERSION_NOT_FOUND`          | 限时活动版本不存在           |
|  409 | `CAMPAIGN_NOT_OPEN`                   | 限时活动当前不可开通         |
|  409 | `CAMPAIGN_CAPACITY_REACHED`           | 限时活动名额已满             |
|  409 | `LIMITED_ENTITLEMENT_EXISTS`          | 用户已开通该活动             |
|  404 | `LIMITED_ENTITLEMENT_NOT_FOUND`       | 限时权益不存在               |
|  409 | `LIMITED_ACTIVE_CANNOT_EXTEND`        | 已激活的限时权益不能补救     |
|  409 | `LIMITED_REMEDY_ALREADY_USED`         | 该限时权益已使用过补救       |
|  409 | `LIMITED_REMEDY_STATE_CONFLICT`       | 当前状态不允许该补救方式     |
|  409 | `LIMITED_STATE_CONFLICT`              | 限时权益当前状态不允许该操作 |
|  409 | `LIMITED_ENTITLEMENT_EXPIRED`         | 限时权益已到期               |
|  403 | `SCENE_ACCESS_DENIED`                 | 用户无权访问场景或条目       |
|  404 | `ENTRY_NOT_FOUND`                     | 场景条目不存在               |

### 8.4 反馈、媒体与用户

| HTTP | 错误码                           | 含义                                   |
| ---: | -------------------------------- | -------------------------------------- |
|  404 | `FEEDBACK_NOT_FOUND`             | 反馈不存在，或请求用户不是所有者       |
|  422 | `FEEDBACK_CATEGORY_INVALID`      | 反馈分类无效                           |
|  422 | `FEEDBACK_DESCRIPTION_REQUIRED`  | 反馈描述为空                           |
|  422 | `FEEDBACK_DESCRIPTION_TOO_LONG`  | 反馈描述超过 300 字                    |
|  422 | `FEEDBACK_SCREENSHOT_LIMIT`      | 反馈截图超过 1 张                      |
|  409 | `FEEDBACK_STATE_CONFLICT`        | 反馈当前状态不允许该操作               |
|  409 | `FEEDBACK_SUPPLEMENT_LIMIT`      | 管理员要求补充已达 2 轮                |
|  422 | `FEEDBACK_SUPPLEMENT_INVALID`    | 用户补充内容为空或超长                 |
|  422 | `FEEDBACK_REPLY_INVALID`         | 管理员回复/关闭原因为空或超长          |
|  422 | `FEEDBACK_TEMPLATE_INVALID`      | 解决模板无效                           |
|  409 | `FEEDBACK_REOPEN_LIMIT`          | 反馈已重开过一次                       |
|  409 | `FEEDBACK_REOPEN_WINDOW_EXPIRED` | 解决后已超过 7 天重开期                |
|  422 | `FEEDBACK_REOPEN_REASON_INVALID` | 重开原因为空或超过 300 字              |
|  422 | `MEDIA_TYPE_INVALID`             | 媒体类型不是`images` 或 `audio`        |
|  422 | `MEDIA_OBJECT_KEY_INVALID`       | OSS 对象键不属于当前上传者或含路径穿越 |
|  422 | `MEDIA_MIME_INVALID`             | 媒体 MIME 不支持                       |
|  422 | `MEDIA_SIZE_INVALID`             | 文件为空或超出上限                     |
|  422 | `MEDIA_HASH_INVALID`             | SHA-256 元数据无效                     |
|  422 | `MEDIA_DECODE_FAILED`            | 媒体无法解码                           |
|  422 | `MEDIA_SECURITY_BLOCKED`         | 媒体未通过安全检查                     |
|  422 | `MEDIA_EXTENSION_INVALID`        | 音频扩展名不支持                       |
|  404 | `MEDIA_TARGET_NOT_FOUND`         | 媒体目标不存在                         |
|  403 | `MEDIA_ACCESS_DENIED`            | 用户无权访问媒体                       |
|  403 | `MEDIA_ACCESS_EXPIRED`           | 用户权益已到期，无法签发 URL           |
|  404 | `USER_NOT_FOUND`                 | 用户不存在                             |
|  404 | `CONTACT_CORRECTION_NOT_FOUND`   | 联系更正申请不存在                     |
|  422 | `CONTACT_STATUS_INVALID`         | 联系状态不在五种允许值中               |
|  409 | `DELETION_EVENT_CONFLICT`        | 同一注销事件 ID 被用于不同用户         |
|  502 | `MINIAPP_API_INVALID_RESPONSE`   | 用户服务返回的数据无效                 |
|  503 | `MINIAPP_API_UNAVAILABLE`        | 用户服务暂时不可用                     |
|  422 | `ANALYTICS_EXPORT_FORBIDDEN`     | 导出数据不是允许的匿名汇总数据         |

## 9. 调用示例

### 9.1 管理员登录与写操作

```bash
# 1. 账号密码登录并保存 Cookie；响应体中的 csrf_token 仅保存在内存
curl -sS -c cookies.txt -X POST 'https://admin-api.example.com/api/v1/admin/session' \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"your-password"}'

# 2. 写操作同时携带 Cookie 和 CSRF Token
curl -sS -b cookies.txt -X PUT \
  'https://admin-api.example.com/api/v1/admin/content/open-scenes' \
  -H 'Content-Type: application/json' \
  -H 'X-CSRF-Token: <step-1-response-csrf-token>' \
  -d '{"scene_ids":["scene_1","scene_2","scene_3"]}'
```

### 9.2 Python 生成内部 HMAC 请求头

```python
import hashlib
import hmac
import json
import secrets
import time

method = "POST"
path_with_query = "/internal/v1/access/batch"
body = json.dumps(
    {"user_id": "user_001", "scene_ids": ["scene_1"]},
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
timestamp = int(time.time())
nonce = secrets.token_hex(16)
secret = b"replace-with-shared-secret"

body_hash = hashlib.sha256(body).hexdigest()
canonical = "\n".join([method, path_with_query, str(timestamp), nonce, body_hash]).encode("utf-8")
signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()

headers = {
    "Content-Type": "application/json",
    "X-Juya-Service": "juya-miniapp-api",
    "X-Juya-Timestamp": str(timestamp),
    "X-Juya-Nonce": nonce,
    "X-Juya-Signature": signature,
}
```

## 10. 文档维护约定

- 新增、删除或修改 FastAPI 路由时，必须同步更新本文档的接口总览、详细定义、请求/响应模型和错误码。
- 以当前代码和 `/openapi.json` 为机器可读契约，本文档补充 OpenAPI 暂未表达的业务约束和联调说明。
- 每次更新文档时，应将顶部的“对应代码提交”更新为已校验的提交号。

## 11. 第二至六批补全接口和当前模型

以下补全原总览遗漏的 39 个操作（含会话探测）；本节和原总览合计覆盖当前挂载的 89/89 个 OpenAPI 操作，无额外隐藏业务路由。请求与响应类型名称可在 `juya-admin/openapi/admin-api.json` 的 `components.schemas` 查找，并由前端生成类型消费。未显式定义的 object 响应仍以相应详细接口中的字段说明为准。

### 11.1 补全操作清单

| 方法 | 路径                                                               | JSON 请求模型              | 成功响应模型                                        | 鉴权及幂等                |
| ---- | ------------------------------------------------------------------ | -------------------------- | --------------------------------------------------- | ------------------------- |

### 11.2 查询参数

路径变量均通过同名 path 参数传入；下面仅列补全操作的 query 参数。Cookie/CSRF 是否必需以通用业务规则为准，不以 OpenAPI 中依赖参数的 optional 标记放宽。

#### GET `/api/v1/admin/analytics`

| 参数     | 类型                     | 必填 | 默认和约束    |
| -------- | ------------------------ | ---- | ------------- |
| `period` | "day" / "week" / "month" | 否   | default="day" |
| `start`  | string (date)            | 是   | —             |
| `end`    | string (date)            | 是   | —             |

#### GET `/api/v1/admin/content/scenes`

| 参数        | 类型                                     | 必填 | 默认和约束                         |
| ----------- | ---------------------------------------- | ---- | ---------------------------------- |
| `page`      | integer                                  | 否   | minimum=1; default=1               |
| `page_size` | integer                                  | 否   | maximum=100; minimum=1; default=20 |
| `query`     | string / null                            | 否   | —                                  |
| `series_id` | string / null                            | 否   | —                                  |
| `status`    | "DRAFT" / "PUBLISHED" / "OFFLINE" / null | 否   | —                                  |

#### GET `/api/v1/admin/entitlements`

| 参数          | 类型                                                                           | 必填 | 默认和约束                         |
| ------------- | ------------------------------------------------------------------------------ | ---- | ---------------------------------- |
| `user_id`     | string / null                                                                  | 否   | —                                  |
| `type`        | "FORMAL" / "LIMITED" / null                                                    | 否   | —                                  |
| `status`      | "ACTIVE" / "PAUSED" / "REVOKED" / "PENDING" / "ENDED" / "START_EXPIRED" / null | 否   | —                                  |
| `package_id`  | string / null                                                                  | 否   | —                                  |
| `campaign_id` | string / null                                                                  | 否   | —                                  |
| `page`        | integer                                                                        | 否   | minimum=1; default=1               |
| `page_size`   | integer                                                                        | 否   | maximum=100; minimum=1; default=20 |

#### GET `/api/v1/admin/content-packages`

| 参数        | 类型    | 必填 | 默认和约束                         |
| ----------- | ------- | ---- | ---------------------------------- |
| `page`      | integer | 否   | minimum=1; default=1               |
| `page_size` | integer | 否   | maximum=100; minimum=1; default=20 |

#### GET `/api/v1/admin/campaigns`

| 参数        | 类型                                                                 | 必填 | 默认和约束                         |
| ----------- | -------------------------------------------------------------------- | ---- | ---------------------------------- |
| `status`    | "DRAFT" / "OPEN" / "PAUSED" / "ENDED" / "ARCHIVED" / "CLOSED" / null | 否   | —                                  |
| `page`      | integer                                                              | 否   | minimum=1; default=1               |
| `page_size` | integer                                                              | 否   | maximum=100; minimum=1; default=20 |

#### GET `/api/v1/admin/feedback`

| 参数        | 类型                                                                                                 | 必填 | 默认和约束                         |
| ----------- | ---------------------------------------------------------------------------------------------------- | ---- | ---------------------------------- |
| `status`    | "PENDING" / "PROCESSING" / "NEED_MORE" / "USER_SUPPLIED" / "RESOLVED" / "CLOSED_INSUFFICIENT" / null | 否   | —                                  |
| `category`  | "CONTENT" / "PRONUNCIATION" / "DISPLAY" / "FUNCTION" / null                                          | 否   | —                                  |
| `keyword`   | string / null                                                                                        | 否   | —                                  |
| `sla`       | "PAUSED" / "OVERDUE" / "DUE_SOON" / "ON_TRACK" / "COMPLETED" / null                                  | 否   | —                                  |
| `page`      | integer                                                                                              | 否   | minimum=1; default=1               |
| `page_size` | integer                                                                                              | 否   | maximum=100; minimum=1; default=20 |

#### GET `/api/v1/admin/media/audio-targets`

| 参数       | 类型          | 必填 | 默认和约束 |
| ---------- | ------------- | ---- | ---------- |
| `scene_id` | string / null | 否   | —          |

#### GET `/api/v1/admin/media/batch-jobs`

| 参数        | 类型    | 必填 | 默认和约束                         |
| ----------- | ------- | ---- | ---------------------------------- |
| `page`      | integer | 否   | minimum=1; default=1               |
| `page_size` | integer | 否   | maximum=100; minimum=1; default=20 |

### 11.3 补全接口模型字段

必填指字段必须出现在 JSON 中；类型中的 null 允许显式 null。请求对象的额外字段按模型配置拒绝。以下类型是当前 OpenAPI 的字段清单，嵌套类型同名引用。

#### `AdminPreviewResponse`

| 字段              | 类型   | 必填 | 约束 |
| ----------------- | ------ | ---- | ---- |
| `scene_id`        | string | 是   | —    |
| `revision_id`     | string | 是   | —    |
| `revision_status` | string | 是   | —    |
| `scene_title`     | string | 是   | —    |
| `series_title`    | string | 是   | —    |
| `content`         | object | 是   | —    |

#### `AnalyticsCountResponse`

| 字段        | 类型          | 必填 | 约束      |
| ----------- | ------------- | ---- | --------- |
| `day`       | string (date) | 是   | —         |
| `metric`    | string        | 是   | —         |
| `dimension` | string        | 是   | —         |
| `value`     | integer       | 是   | minimum=0 |

#### `AnalyticsRatioResponse`

| 字段          | 类型          | 必填 | 约束      |
| ------------- | ------------- | ---- | --------- |
| `day`         | string (date) | 是   | —         |
| `metric`      | string        | 是   | —         |
| `numerator`   | integer       | 是   | minimum=0 |
| `denominator` | integer       | 是   | minimum=0 |
| `rate`        | number / null | 是   | —         |
| `basis`       | string        | 是   | —         |

#### `AnalyticsResponse`

| 字段       | 类型                          | 必填 | 约束                    |
| ---------- | ----------------------------- | ---- | ----------------------- |
| `period`   | "day" / "week" / "month"      | 是   | —                       |
| `timezone` | "Asia/Shanghai"               | 否   | default="Asia/Shanghai" |
| `start`    | string (date)                 | 是   | —                       |
| `end`      | string (date)                 | 是   | —                       |
| `rows`     | array<AnalyticsCountResponse> | 是   | —                       |
| `ratios`   | array<AnalyticsRatioResponse> | 是   | —                       |

#### `AudioTargetListResponse`

| 字段    | 类型                       | 必填 | 约束 |
| ------- | -------------------------- | ---- | ---- |
| `items` | array<AudioTargetResponse> | 是   | —    |

#### `AudioTargetResponse`

| 字段                | 类型          | 必填 | 约束 |
| ------------------- | ------------- | ---- | ---- |
| `id`                | string        | 是   | —    |
| `stable_key`        | string        | 是   | —    |
| `target_type`       | string        | 是   | —    |
| `active_version_id` | string / null | 是   | —    |

#### `AudioVersionListResponse`

| 字段    | 类型                        | 必填 | 约束 |
| ------- | --------------------------- | ---- | ---- |
| `items` | array<AudioVersionResponse> | 是   | —    |

#### `AudioVersionResponse`

| 字段                  | 类型               | 必填 | 约束 |
| --------------------- | ------------------ | ---- | ---- |
| `id`                  | string             | 是   | —    |
| `target_id`           | string             | 是   | —    |
| `asset_id`            | string             | 是   | —    |
| `version_no`          | integer            | 是   | —    |
| `source`              | string             | 是   | —    |
| `status`              | string             | 是   | —    |
| `provider_request_id` | string / null      | 是   | —    |
| `processing_job_id`   | string / null      | 是   | —    |
| `created_by`          | string             | 是   | —    |
| `created_at`          | string (date-time) | 是   | —    |

#### `BatchJobItemResponse`

| 字段                | 类型           | 必填 | 约束 |
| ------------------- | -------------- | ---- | ---- |
| `id`                | string         | 是   | —    |
| `item_key`          | string         | 是   | —    |
| `target_id`         | string         | 是   | —    |
| `status`            | string         | 是   | —    |
| `attempt_count`     | integer        | 是   | —    |
| `error_code`        | string / null  | 是   | —    |
| `result_version`    | integer / null | 是   | —    |
| `processing_job_id` | string / null  | 是   | —    |

#### `BatchJobPageResponse`

| 字段        | 类型                    | 必填 | 约束 |
| ----------- | ----------------------- | ---- | ---- |
| `items`     | array<BatchJobResponse> | 是   | —    |
| `page`      | integer                 | 是   | —    |
| `page_size` | integer                 | 是   | —    |
| `total`     | integer                 | 是   | —    |

#### `BatchJobResponse`

| 字段                  | 类型                        | 必填 | 约束 |
| --------------------- | --------------------------- | ---- | ---- |
| `id`                  | string                      | 是   | —    |
| `business_key`        | string                      | 是   | —    |
| `job_type`            | string                      | 是   | —    |
| `status`              | string                      | 是   | —    |
| `total_count`         | integer                     | 是   | —    |
| `success_count`       | integer                     | 是   | —    |
| `failure_count`       | integer                     | 是   | —    |
| `created_by`          | string                      | 是   | —    |
| `created_at`          | string (date-time)          | 是   | —    |
| `updated_at`          | string (date-time)          | 是   | —    |
| `completed_at`        | string (date-time) / null   | 是   | —    |
| `cancel_requested_at` | string (date-time) / null   | 是   | —    |
| `items`               | array<BatchJobItemResponse> | 是   | —    |

#### `CampaignCommandRequest`

| 字段               | 类型           | 必填 | 约束      |
| ------------------ | -------------- | ---- | --------- |
| `expected_version` | integer        | 是   | minimum=1 |
| `capacity`         | integer / null | 否   | —         |

#### `CampaignListItemResponse`

| 字段                   | 类型                                                                         | 必填 | 约束 |
| ---------------------- | ---------------------------------------------------------------------------- | ---- | ---- |
| `id`                   | string                                                                       | 是   | —    |
| `name`                 | string                                                                       | 是   | —    |
| `status`               | string                                                                       | 是   | —    |
| `version`              | integer                                                                      | 是   | —    |
| `current_version_id`   | string / null                                                                | 否   | —    |
| `capacity`             | integer / null                                                               | 否   | —    |
| `granted_user_count`   | integer / null                                                               | 否   | —    |
| `created_at`           | string (date-time)                                                           | 是   | —    |
| `updated_at`           | string (date-time)                                                           | 是   | —    |
| `available_operations` | array<"open" / "pause" / "resume" / "end" / "archive" / "capacity" / "copy"> | 是   | —    |

#### `CampaignPageResponse`

| 字段        | 类型                            | 必填 | 约束 |
| ----------- | ------------------------------- | ---- | ---- |
| `items`     | array<CampaignListItemResponse> | 是   | —    |
| `page`      | integer                         | 是   | —    |
| `page_size` | integer                         | 是   | —    |
| `total`     | integer                         | 是   | —    |

#### `CampaignResponse`

| 字段                   | 类型                                                                         | 必填 | 约束 |
| ---------------------- | ---------------------------------------------------------------------------- | ---- | ---- |
| `id`                   | string                                                                       | 是   | —    |
| `name`                 | string                                                                       | 是   | —    |
| `status`               | string                                                                       | 是   | —    |
| `version`              | integer                                                                      | 是   | —    |
| `created_at`           | string (date-time)                                                           | 是   | —    |
| `updated_at`           | string (date-time)                                                           | 是   | —    |
| `current_version`      | CampaignVersionResponse / null                                               | 是   | —    |
| `available_operations` | array<"open" / "pause" / "resume" / "end" / "archive" / "capacity" / "copy"> | 是   | —    |

#### `CampaignSaveRequest`

| 字段                     | 类型                 | 必填 | 约束                       |
| ------------------------ | -------------------- | ---- | -------------------------- |
| `expected_version`       | integer / null       | 否   | —                          |
| `name`                   | string               | 是   | maxLength=200; minLength=1 |
| `duration_days`          | 3 / 5 / null         | 否   | —                          |
| `activation_window_days` | integer / null       | 否   | —                          |
| `capacity`               | integer / null       | 否   | —                          |
| `scene_ids`              | array<string> / null | 否   | —                          |

#### `CampaignVersionResponse`

| 字段                     | 类型                      | 必填 | 约束 |
| ------------------------ | ------------------------- | ---- | ---- |
| `id`                     | string                    | 是   | —    |
| `version_no`             | integer                   | 是   | —    |
| `status`                 | string                    | 是   | —    |
| `duration_days`          | integer                   | 是   | —    |
| `activation_window_days` | integer                   | 是   | —    |
| `capacity`               | integer                   | 是   | —    |
| `granted_user_count`     | integer                   | 是   | —    |
| `grant_starts_at`        | string (date-time) / null | 是   | —    |
| `grant_ends_at`          | string (date-time) / null | 是   | —    |
| `locked_at`              | string (date-time) / null | 是   | —    |
| `version`                | integer                   | 是   | —    |
| `scene_ids`              | array<string>             | 是   | —    |

#### `CreateAudioVersionRequest`

| 字段       | 类型   | 必填 | 约束                      |
| ---------- | ------ | ---- | ------------------------- |
| `asset_id` | string | 是   | maxLength=64; minLength=1 |

#### `CreateBatchJobRequest`

| 字段         | 类型          | 必填 | 约束                      |
| ------------ | ------------- | ---- | ------------------------- |
| `job_type`   | string        | 是   | maxLength=32; minLength=1 |
| `target_ids` | array<string> | 是   | maxItems=500; minItems=1  |

#### `CreateOcrJobRequest`

| 字段          | 类型   | 必填 | 约束                       |
| ------------- | ------ | ---- | -------------------------- |
| `asset_id`    | string | 是   | maxLength=64; minLength=1  |
| `object_key`  | string | 是   | maxLength=512; minLength=1 |
| `series_id`   | string | 是   | maxLength=64; minLength=1  |
| `template_id` | string | 是   | maxLength=64; minLength=1  |

#### `CreateTrashRequest`

| 字段          | 类型   | 必填 | 约束                      |
| ------------- | ------ | ---- | ------------------------- |
| `scene_id`    | string | 是   | maxLength=64; minLength=1 |
| `revision_id` | string | 是   | maxLength=64; minLength=1 |

#### `DiscoveryConfigResponse`

| 字段                | 类型                      | 必填 | 约束 |
| ------------------- | ------------------------- | ---- | ---- |
| `version`           | integer                   | 是   | —    |
| `open_scene_ids`    | array<string>             | 是   | —    |
| `preview_by_series` | object                    | 是   | —    |
| `learning_modules`  | object                    | 是   | —    |
| `updated_at`        | string (date-time) / null | 是   | —    |
| `actor_id`          | string / null             | 是   | —    |

#### `EntitlementListItemResponse`

| 字段          | 类型                      | 必填 | 约束 |
| ------------- | ------------------------- | ---- | ---- |
| `id`          | string                    | 是   | —    |
| `type`        | "FORMAL" / "LIMITED"      | 是   | —    |
| `user_id`     | string                    | 是   | —    |
| `status`      | string                    | 是   | —    |
| `granted_at`  | string (date-time)        | 是   | —    |
| `expires_at`  | string (date-time) / null | 是   | —    |
| `package_id`  | string / null             | 是   | —    |
| `campaign_id` | string / null             | 是   | —    |

#### `EntitlementPageResponse`

| 字段        | 类型                               | 必填 | 约束 |
| ----------- | ---------------------------------- | ---- | ---- |
| `items`     | array<EntitlementListItemResponse> | 是   | —    |
| `page`      | integer                            | 是   | —    |
| `page_size` | integer                            | 是   | —    |
| `total`     | integer                            | 是   | —    |

#### `FeedbackInternalNoteResponse`

| 字段         | 类型               | 必填 | 约束 |
| ------------ | ------------------ | ---- | ---- |
| `id`         | string             | 是   | —    |
| `admin_id`   | string             | 是   | —    |
| `content`    | string             | 是   | —    |
| `created_at` | string (date-time) | 是   | —    |

#### `FeedbackListItemResponse`

| 字段                | 类型                                                                                          | 必填 | 约束 |
| ------------------- | --------------------------------------------------------------------------------------------- | ---- | ---- |
| `id`                | string                                                                                        | 是   | —    |
| `user_id`           | string                                                                                        | 是   | —    |
| `category`          | "CONTENT" / "PRONUNCIATION" / "DISPLAY" / "FUNCTION"                                          | 是   | —    |
| `description`       | string                                                                                        | 是   | —    |
| `status`            | "PENDING" / "PROCESSING" / "NEED_MORE" / "USER_SUPPLIED" / "RESOLVED" / "CLOSED_INSUFFICIENT" | 是   | —    |
| `deadline_at`       | string (date-time) / null                                                                     | 是   | —    |
| `sla_state`         | "PAUSED" / "OVERDUE" / "DUE_SOON" / "ON_TRACK" / "COMPLETED"                                  | 是   | —    |
| `supplement_rounds` | integer                                                                                       | 是   | —    |
| `created_at`        | string (date-time)                                                                            | 是   | —    |
| `updated_at`        | string (date-time)                                                                            | 是   | —    |

#### `FeedbackPageResponse`

| 字段        | 类型                            | 必填 | 约束 |
| ----------- | ------------------------------- | ---- | ---- |
| `items`     | array<FeedbackListItemResponse> | 是   | —    |
| `page`      | integer                         | 是   | —    |
| `page_size` | integer                         | 是   | —    |
| `total`     | integer                         | 是   | —    |

#### `FormalEntitlementDetailResponse`

| 字段                   | 类型                      | 必填 | 约束 |
| ---------------------- | ------------------------- | ---- | ---- |
| `id`                   | string                    | 是   | —    |
| `user_id`              | string                    | 是   | —    |
| `package_id`           | string                    | 是   | —    |
| `package_name`         | string                    | 是   | —    |
| `status`               | string                    | 是   | —    |
| `term`                 | string                    | 是   | —    |
| `granted_at`           | string (date-time)        | 是   | —    |
| `expires_at`           | string (date-time) / null | 是   | —    |
| `version`              | integer                   | 是   | —    |
| `available_operations` | array<string>             | 是   | —    |

#### `GenerateAudioRequest`

| 字段          | 类型   | 必填 | 约束                        |
| ------------- | ------ | ---- | --------------------------- |
| `stable_key`  | string | 是   | maxLength=128; minLength=1  |
| `target_type` | string | 是   | maxLength=32; minLength=1   |
| `text`        | string | 是   | maxLength=5000; minLength=1 |
| `voice`       | string | 是   | maxLength=64; minLength=1   |

#### `InternalNoteRequest`

| 字段      | 类型   | 必填 | 约束                       |
| --------- | ------ | ---- | -------------------------- |
| `content` | string | 是   | maxLength=200; minLength=1 |

#### `LimitedEntitlementDetailResponse`

| 字段                     | 类型                      | 必填 | 约束 |
| ------------------------ | ------------------------- | ---- | ---- |
| `id`                     | string                    | 是   | —    |
| `user_id`                | string                    | 是   | —    |
| `campaign_version_id`    | string                    | 是   | —    |
| `campaign_id`            | string                    | 是   | —    |
| `campaign_name`          | string                    | 是   | —    |
| `status`                 | string                    | 是   | —    |
| `granted_at`             | string (date-time)        | 是   | —    |
| `start_deadline`         | string (date-time)        | 是   | —    |
| `activated_at`           | string (date-time) / null | 是   | —    |
| `expires_at`             | string (date-time) / null | 是   | —    |
| `remedy_count`           | integer                   | 是   | —    |
| `version`                | integer                   | 是   | —    |
| `duration_days`          | integer                   | 是   | —    |
| `activation_window_days` | integer                   | 是   | —    |
| `scene_ids`              | array<string>             | 是   | —    |
| `available_operations`   | array<string>             | 是   | —    |

#### `OcrCandidateResponse`

| 字段                    | 类型          | 必填 | 约束 |
| ----------------------- | ------------- | ---- | ---- |
| `id`                    | string        | 是   | —    |
| `job_id`                | string        | 是   | —    |
| `asset_id`              | string        | 是   | —    |
| `status`                | string        | 是   | —    |
| `template_type`         | string        | 是   | —    |
| `structured_candidate`  | object        | 是   | —    |
| `confidence`            | number / null | 是   | —    |
| `error_code`            | string / null | 是   | —    |
| `confirmed_revision_id` | string / null | 是   | —    |

#### `OcrCommandRequest`

| 字段       | 类型          | 必填 | 约束 |
| ---------- | ------------- | ---- | ---- |
| `scene_id` | string / null | 否   | —    |
| `content`  | object / null | 否   | —    |

#### `OcrConfirmationResponse`

| 字段              | 类型    | 必填 | 约束 |
| ----------------- | ------- | ---- | ---- |
| `revision_id`     | string  | 是   | —    |
| `revision_status` | string  | 是   | —    |
| `version`         | integer | 是   | —    |

#### `PackagePageResponse`

| 字段        | 类型                   | 必填 | 约束 |
| ----------- | ---------------------- | ---- | ---- |
| `items`     | array<PackageResponse> | 是   | —    |
| `page`      | integer                | 是   | —    |
| `page_size` | integer                | 是   | —    |
| `total`     | integer                | 是   | —    |

#### `PackageResponse`

| 字段         | 类型    | 必填 | 约束 |
| ------------ | ------- | ---- | ---- |
| `id`         | string  | 是   | —    |
| `name`       | string  | 是   | —    |
| `status`     | string  | 是   | —    |
| `sort_order` | integer | 是   | —    |

#### `ProcessingJobResponse`

| 字段                  | 类型                      | 必填 | 约束 |
| --------------------- | ------------------------- | ---- | ---- |
| `id`                  | string                    | 是   | —    |
| `business_key`        | string                    | 是   | —    |
| `job_type`            | string                    | 是   | —    |
| `target_id`           | string                    | 是   | —    |
| `batch_id`            | string / null             | 是   | —    |
| `status`              | string                    | 是   | —    |
| `provider_request_id` | string / null             | 是   | —    |
| `error_code`          | string / null             | 是   | —    |
| `created_by`          | string                    | 是   | —    |
| `created_at`          | string (date-time)        | 是   | —    |
| `updated_at`          | string (date-time)        | 是   | —    |
| `cancel_requested_at` | string (date-time) / null | 是   | —    |

#### `RevisionResponse`

| 字段                  | 类型                      | 必填 | 约束 |
| --------------------- | ------------------------- | ---- | ---- |
| `id`                  | string                    | 是   | —    |
| `scene_id`            | string                    | 是   | —    |
| `source_revision_id`  | string / null             | 是   | —    |
| `version`             | integer                   | 是   | —    |
| `status`              | string                    | 是   | —    |
| `stable_sentence_ids` | array<string>             | 是   | —    |
| `stable_entry_ids`    | array<string>             | 是   | —    |
| `content`             | object                    | 是   | —    |
| `created_by`          | string                    | 是   | —    |
| `created_at`          | string (date-time) / null | 是   | —    |

#### `RollbackAudioRequest`

| 字段         | 类型   | 必填 | 约束                      |
| ------------ | ------ | ---- | ------------------------- |
| `version_id` | string | 是   | maxLength=64; minLength=1 |

#### `SaveDiscoveryConfigRequest`

| 字段                | 类型          | 必填 | 约束                   |
| ------------------- | ------------- | ---- | ---------------------- |
| `expected_version`  | integer       | 是   | minimum=0              |
| `open_scene_ids`    | array<string> | 是   | maxItems=3; minItems=3 |
| `preview_by_series` | object        | 是   | —                      |
| `learning_modules`  | object        | 是   | —                      |

#### `SaveRevisionRequest`

| 字段               | 类型    | 必填 | 约束      |
| ------------------ | ------- | ---- | --------- |
| `expected_version` | integer | 是   | minimum=1 |
| `content`          | object  | 是   | —         |

#### `ScenePageResponse`

| 字段        | 类型                 | 必填 | 约束 |
| ----------- | -------------------- | ---- | ---- |
| `items`     | array<SceneResponse> | 是   | —    |
| `page`      | integer              | 是   | —    |
| `page_size` | integer              | 是   | —    |
| `total`     | integer              | 是   | —    |

#### `SceneResponse`

| 字段                    | 类型                      | 必填 | 约束 |
| ----------------------- | ------------------------- | ---- | ---- |
| `id`                    | string                    | 是   | —    |
| `series_id`             | string                    | 是   | —    |
| `title`                 | string                    | 是   | —    |
| `series_title`          | string                    | 是   | —    |
| `summary`               | string / null             | 是   | —    |
| `cover_object_key`      | string / null             | 是   | —    |
| `status`                | string                    | 是   | —    |
| `draft_revision_id`     | string / null             | 是   | —    |
| `published_revision_id` | string / null             | 是   | —    |
| `updated_at`            | string (date-time) / null | 是   | —    |

#### `SignedFeedbackScreenshotResponse`

| 字段         | 类型               | 必填 | 约束 |
| ------------ | ------------------ | ---- | ---- |
| `url`        | string             | 是   | —    |
| `expires_at` | string (date-time) | 是   | —    |

#### `TrashEntryResponse`

| 字段              | 类型                      | 必填 | 约束 |
| ----------------- | ------------------------- | ---- | ---- |
| `id`              | string                    | 是   | —    |
| `scene_id`        | string                    | 是   | —    |
| `revision_id`     | string                    | 是   | —    |
| `status`          | string                    | 是   | —    |
| `trashed_by`      | string                    | 是   | —    |
| `trashed_at`      | string (date-time)        | 是   | —    |
| `retention_until` | string (date-time)        | 是   | —    |
| `restored_at`     | string (date-time) / null | 是   | —    |
| `cleaned_at`      | string (date-time) / null | 是   | —    |

#### `TrashListResponse`

| 字段    | 类型                      | 必填 | 约束 |
| ------- | ------------------------- | ---- | ---- |
| `items` | array<TrashEntryResponse> | 是   | —    |

## 12. 统计口径和最终验收边界

`GET /api/v1/admin/analytics?period=day|week|month&start=YYYY-MM-DD&end=YYYY-MM-DD` 使用 ADMIN_READ 鉴权。起止包含端点、须正序且最多 366 天，未知 query 参数返回 422。响应 `period/timezone/start/end/rows/ratios`，timezone 固定为 Asia/Shanghai。

- 自然日按北京时间；自然周从周一开始；自然月从 1 日开始。周起点可以跨年。首尾周期只统计请求区间内数据。
- 每周期的相同 metric/dimension 计数相加。缺失数据不补零，空区间返回空数组。
- ACTIVE_USERS 来自活跃状态账号的日级快照。跨日求和不是独立用户人数，也不是学习 DAU；前端明确显示未去重口径。
- rate 使用累计 numerator / 累计 denominator。denominator=0 时为 null；分子大于分母、缺少配对行或没有定义口径返回 `422 ANALYTICS_RATIO_INVALID`。
- CONTACT_FUNNEL 口径为填写次数 / 提示曝光次数；LIMITED_STARTS 为首次启动人数 / 开通人数；LIMITED_COMPLETIONS 为到期前完成人数 / 首次启动人数；FEEDBACK_SLA 为 SLA 内处理数量 / 纳入 SLA 统计的反馈数量。
- 只有明确的 NUMERATOR、DENOMINATOR 行才能产生比率。现有 daily producer 仅写入 NEW_USERS、ACTIVE_USERS、FEEDBACK_SLA、DELETIONS 的 ALL 计数；未采集指标及比率保持缺失，不将旧计数转换成比率。本批没有扩展学习行为采集。
- 每日北京时间 01:00 重算前一完整自然日的 NEW_USERS、FEEDBACK_SLA、DELETIONS；ACTIVE_USERS 按执行当日单独采集状态快照，保留真实 generated_at，不伪造历史状态。显式指定已结束日期仅回补三个事件计数，不改写快照或其他维度/比率行。历史错误计数需运营另行指定日期回补。
- 查询和旧导出只接受第 7.7 节指标。维度限定为汇总枚举及已批准的 scene/series/package/campaign 前缀，并拒绝个人标识标记。前后端都拒绝未知指标和个人维度；不返回用户 ID、微信号、截图或个人轨迹。

第二批权益/活动列表和第四批内容列表使用 page/page_size/total 分页。活动 `available_operations` 是服务端状态裁决，不由前端自行推定；修改活动、草稿、发现配置和系统配置须携带读取版本，409 后保留草稿并读取远端版本。第五批 OCR/audio/batch 命令使用 X-CSRF-Token 与 X-Idempotency-Key；OCR 人工确认写入草稿而非直接发布，音频切换保留可回滚版本。命令枚举及字段约束见第 11 节与 OpenAPI。

2026-09-30 验收：文档操作集合与实际 app.openapi 相等，89/89；前端生成快照与实际 OpenAPI 相等，33 项 capability 全部 available，A01–A26 均加载具体实现。Chromium 93 项通过，其中 52 项覆盖 1440×900 和 1280×800 双视口。真实 loopback HTTP 验证会话、匿名统计、CSRF、配置 409、反馈解决后的工作台计数/待办更新，以及 MySQL/Redis/Celery 的本地 OCR 闭环。

本批媒体验证使用 local/test provider 和隔离库中的已确认资产，没有执行真实 OSS 浏览器直传、生产私有媒体访问或线上 OCR/TTS 供应商调用；这些属于第七批，不能据此声明生产媒体链路完成。完整门禁、复现方式和限制见 [第六批验收报告](../../juya-admin-api/docs/api/batch-6-analytics-acceptance.md)。

## V1.3 结构化内容与固定版本契约（2026-10-01）

本节及当前 OpenAPI 替代历史内容/OCR/音频章节中的旧字段与流程。沿用原鉴权、CSRF、审计和业务状态规则，TTS 入口返回 `409 TTS_DISABLED`。内容数据库最低迁移版本 `0015`。

- 创建系列、创建场景和图片导入均要求 `X-CSRF-Token` 与 `X-Idempotency-Key`；同一键与相同输入重放返回同一对象，输入变化返回冲突。场景与初始结构化草稿在同一事务创建，客户端失败重试保留原命令键。
- 草稿 `content` 为严格 `SceneContent`：`title_en/title_zh/summary/tags/original_asset_id/cover_asset_id/copyright_note/source_note/dialogue/vocabulary/chunks/whole_audio`。草稿允许不完整，未知字段被拒绝；编辑提交 `expected_version`，冲突不覆盖人工输入。
- 对话包含稳定 `id/speaker/english/chinese/start_ms/end_ms/audio_version_id/timing_confirmed/clickable_spans`。词条含稳定 `entry_id/entry_version`、词形、人工变体、释义、音标、解释、可空发音目标和来源句 ID。词库修改产生不可变版本，发布引用版本不追随最新词库。
- 可信上传读取私有 OSS 实际字节，保存 SHA-256、图片尺寸及真实音频时长。确认状态、检查结果和任务返回前端；客户端哈希或元数据不能证明检查通过。每批图片最多 30 张、音频最多 300 份。图片导入仅建草稿，OCR 需明确触发。
- OCR 配额独立查询/配置，默认关闭。账户额度核验记录、内部上限、数据库预占和 worker 一次认领保护调用；失败及未知结果保守计数。采纳需 `job_id/expected_version/selected_fields/content`，只写选中字段，不能覆盖更新后的草稿。候选保留位置、置信度、原始行及分组建议。
- 整段音频 `whole_audio` 固定 `target_id/version_id/asset_id/duration_ms`。全部句子校验 `0 ≤ start_ms < end_ms ≤ duration_ms`，且确认试听；替换整段音频使原标时和试听确认失效。词卡发音可空。音频候选确认或回退不直接替换已发布内容。
- 检查与发布实时读取素材事实和词条引用，发布再次校验草稿版本并事务切换发布指针。缺双语标题、原图、对话、词汇、语块、版权来源、完整音频或任一句标时均阻断。安全预览封面不能使用受限原图。
- 内部场景接口为有类型的完整/预览响应。完整响应固定 `scene_id/revision_id/content_version/content`；预览仅返回许可、双语标题、系列、安全封面和简介。资源签名必须携带场景修订，验证该修订真实引用及当次权益；签名有效期受权益到期截断。旧无场景资源签名入口拒绝使用。
- 词卡携带 `revision_id/entry_version/source_locator`。来源为 `sentence:{稳定句子ID}:entry:{词条ID}` 或明确词卡列表来源；跨行语块上下文由全部匹配行拼接。片段索引采用 Unicode 码点偏移，客户端需按码点切分。
- 正式期限统一 `month_1/month_2/month_3/month_6/month_12/permanent`；复习分页不限制总量。批量标签、版权、归属、校验、发布、下线、恢复、导出由实际 worker 逐项执行；恢复也在事务内重新检查素材事实。

完整实施与供应商待验收边界见 [V1.3 交付报告](../../juya-admin-api/docs/implementation/v13-content/evidence.md)。

<!-- V13_CURRENT_INVENTORY -->
## V1.3 当前源码接口清单

当前 OpenAPI 操作数：107。草稿 `content` 为 SceneContent；发布强制 `expected_version`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health/live` | Live |
| GET | `/health/ready` | Ready |
| POST | `/api/v1/admin/content/imports` | Import Images |
| GET | `/api/v1/admin/content/series` | List Series |
| POST | `/api/v1/admin/content/series` | Create Series |
| POST | `/api/v1/admin/content/scenes` | Create Scene |
| GET | `/api/v1/admin/content/scenes` | List Scenes |
| GET | `/api/v1/admin/content/lexicon` | List Lexicon |
| POST | `/api/v1/admin/content/lexicon` | Create Lexicon |
| PUT | `/api/v1/admin/content/lexicon/{entry_id}` | Update Lexicon |
| GET | `/api/v1/admin/content/revisions/{revision_id}/ocr-suggestions/{job_id}` | Ocr Suggestions |
| POST | `/api/v1/admin/content/revisions/{revision_id}/ocr-adoptions` | Adopt Ocr |
| GET | `/api/v1/admin/content/revisions/{revision_id}/resources/{resource_id}/signed-url` | Draft Resource |
| GET | `/api/v1/admin/analytics` | Query Analytics |
| POST | `/api/v1/admin/session` | Create Password Session |
| GET | `/api/v1/admin/session` | Get Session |
| POST | `/api/v1/admin/session/logout` | Logout |
| GET | `/api/v1/admin/settings` | List Settings |
| PATCH | `/api/v1/admin/settings/{key}` | Update Setting |
| GET | `/api/v1/admin/audit-events` | List Audit Events |
| GET | `/api/v1/admin/content/scenes/{scene_id}` | Get Scene |
| GET | `/api/v1/admin/content/revisions/{revision_id}` | Get Revision |
| PUT | `/api/v1/admin/content/revisions/{revision_id}` | Save Revision |
| GET | `/api/v1/admin/content/discovery-config` | Get Discovery Config |
| PUT | `/api/v1/admin/content/discovery-config` | Save Discovery Config |
| GET | `/api/v1/admin/content/revisions/{revision_id}/preview` | Admin Preview |
| GET | `/api/v1/admin/content/scenes/{scene_id}/revisions` | Revision History |
| POST | `/api/v1/admin/content/scenes/{scene_id}/revisions` | Create Revision |
| POST | `/api/v1/admin/content/revisions/{revision_id}/publish-checks` | Validate Publish |
| POST | `/api/v1/admin/content/revisions/{revision_id}/commands/publish` | Publish Revision |
| PUT | `/api/v1/admin/content/open-scenes` | Replace Open Scenes |
| PUT | `/api/v1/admin/content/preview-configs/{series_id}` | Replace Preview Scenes |
| POST | `/api/v1/admin/content/scenes/{scene_id}/commands/offline` | Offline Scene |
| GET | `/api/v1/admin/formal-entitlements/{entitlement_id}` | Get Entitlement |
| POST | `/api/v1/admin/formal-entitlements/preview-operation` | Preview Operation |
| POST | `/api/v1/admin/formal-entitlements/commands/{operation}` | Apply Operation |
| GET | `/api/v1/admin/limited-entitlements/{entitlement_id}` | Get Entitlement |
| POST | `/api/v1/admin/limited-entitlements/commands/grant` | Grant |
| POST | `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/remedy` | Remedy |
| POST | `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/pause` | Pause |
| POST | `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/resume` | Resume |
| POST | `/api/v1/admin/limited-entitlements/{entitlement_id}/commands/revoke` | Revoke |
| GET | `/api/v1/admin/entitlements` | List Entitlements |
| GET | `/api/v1/admin/content-packages` | List Packages |
| GET | `/api/v1/admin/campaigns` | List Campaigns |
| POST | `/api/v1/admin/campaigns` | Create Campaign |
| GET | `/api/v1/admin/campaigns/{campaign_id}` | Get Campaign |
| PUT | `/api/v1/admin/campaigns/{campaign_id}` | Update Campaign |
| POST | `/api/v1/admin/campaigns/{campaign_id}/versions/copy` | Copy Version |
| POST | `/api/v1/admin/campaigns/{campaign_id}/commands/{operation}` | Command Campaign |
| GET | `/api/v1/admin/feedback` | List Feedback |
| GET | `/api/v1/admin/feedback/{ticket_id}` | Detail |
| POST | `/api/v1/admin/feedback/{ticket_id}/screenshot-url` | Screenshot Url |
| POST | `/api/v1/admin/feedback/{ticket_id}/internal-notes` | Add Internal Note |
| POST | `/api/v1/admin/feedback/{ticket_id}/commands/start` | Start |
| POST | `/api/v1/admin/feedback/{ticket_id}/commands/request-supplement` | Request Supplement |
| POST | `/api/v1/admin/feedback/{ticket_id}/commands/resolve` | Resolve |
| POST | `/api/v1/admin/feedback/{ticket_id}/commands/close-insufficient` | Close Insufficient |
| GET | `/api/v1/admin/contact-corrections` | List Corrections |
| GET | `/api/v1/admin/contact-corrections/{correction_id}` | Correction Detail |
| POST | `/api/v1/admin/contact-corrections/{correction_id}/commands/{command}` | Decide Correction |
| POST | `/api/v1/admin/users/{user_id}/commands/contact-status` | Update Contact Status |
| POST | `/api/v1/admin/users/{user_id}/commands/verify-contact-change` | Verify Contact Change |
| POST | `/api/v1/admin/users/{user_id}/contact-copy-events` | Audit Contact Copy |
| POST | `/api/v1/admin/media/upload-policies` | Create Upload Policy |
| POST | `/api/v1/admin/media/uploads/confirm` | Confirm Upload |
| GET | `/api/v1/admin/media/assets/{asset_id}` | Get Asset |
| GET | `/api/v1/admin/media/assets/{asset_id}/signed-url` | Preview Asset |
| GET | `/api/v1/admin/media/ocr/quota` | Get Quota |
| PUT | `/api/v1/admin/media/ocr/settings` | Configure Ocr |
| POST | `/api/v1/admin/media/ocr/jobs` | Create Ocr Job |
| GET | `/api/v1/admin/media/ocr/jobs/{job_id}` | Get Ocr Job |
| GET | `/api/v1/admin/media/ocr/jobs/{job_id}/candidate` | Get Ocr Candidate |
| POST | `/api/v1/admin/media/ocr/jobs/{job_id}/commands/{operation}` | Command Ocr Job |
| POST | `/api/v1/admin/media/audio-targets` | Create Audio Target |
| GET | `/api/v1/admin/media/audio-targets` | List Audio Targets |
| GET | `/api/v1/admin/media/audio-targets/{target_id}/versions` | List Audio Versions |
| POST | `/api/v1/admin/media/audio-targets/{target_id}/versions` | Create Audio Version |
| POST | `/api/v1/admin/media/audio-targets/{target_id}/commands/generate` | Generate Audio |
| POST | `/api/v1/admin/media/audio-versions/{version_id}/commands/confirm` | Confirm Audio Version |
| POST | `/api/v1/admin/media/audio-targets/{target_id}/commands/rollback` | Rollback Audio Version |
| GET | `/api/v1/admin/media/batch-jobs` | List Batch Jobs |
| POST | `/api/v1/admin/media/batch-jobs` | Create Batch Job |
| GET | `/api/v1/admin/media/batch-jobs/{batch_id}` | Get Batch Job |
| POST | `/api/v1/admin/media/batch-jobs/{batch_id}/commands/{operation}` | Command Batch Job |
| GET | `/api/v1/admin/media/trash` | List Trash |
| POST | `/api/v1/admin/media/trash` | Create Trash Entry |
| POST | `/api/v1/admin/media/trash/{entry_id}/commands/{operation}` | Command Trash Entry |
| GET | `/api/v1/admin/users` | Search Users |
| POST | `/api/v1/admin/users/search-by-wechat` | Search Users By Wechat |
| GET | `/api/v1/admin/users/{user_id}` | User Detail |
| GET | `/api/v1/admin/dashboard` | Dashboard Snapshot |
| GET | `/api/v1/admin/work-items` | Active Work Items |
| GET | `/api/v1/admin/analytics/export` | Analytics Export |
| GET | `/internal/v1/learning/modules` | Learning Modules |
| POST | `/internal/v1/learning/catalog` | Learning Catalog |
| POST | `/internal/v1/access/batch` | Access Batch |
| POST | `/internal/v1/scenes/{scene_id}/open` | Open Scene |
| POST | `/internal/v1/scenes/{scene_id}/entries/{entry_id}` | Get Entry |
| POST | `/internal/v1/scenes/{scene_id}/resources/{resource_id}/signed-url` | Signed Resource |
| POST | `/internal/v1/feedback` | Create Feedback |
| GET | `/internal/v1/feedback/{ticket_id}` | Feedback Detail |
| POST | `/internal/v1/feedback/{ticket_id}/supplements` | Supply |
| POST | `/internal/v1/feedback/{ticket_id}/resolution` | User Resolution |
| GET | `/internal/v1/media/{target_id}/signed-url` | Signed Url |
| POST | `/internal/v1/account-deletions` | Cleanup Account |
| POST | `/internal/v1/users/{user_id}/deletion` | Cleanup User |
