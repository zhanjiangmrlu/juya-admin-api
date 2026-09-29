# 第一批：用户与联系资料接口说明

> 核对基线：`juya-admin-api` OpenAPI，共 51 个业务操作，其中 38 个管理端操作、11 个内部操作、2 个健康检查。本文只描述第一批 A02–A04 实际接入的公开管理接口。

## 通用约定

- 只读接口要求管理员会话 Cookie `juya_admin_session`。
- 写接口还要求 `X-CSRF-Token`；联系更正决定额外要求 `X-Idempotency-Key`（1–128 字符）。
- 含完整微信号的响应统一携带 `Cache-Control: no-store`。
- 完整微信号搜索只接受 POST JSON 正文，不进入 URL。
- 联系状态固定为：`NOT_PROVIDED`、`PENDING`、`CONTACTED`、`UNREACHABLE`、`DO_NOT_CONTACT`。
- 联系更正批准仅重置一次用户自助修改机会，不直接改写微信号，也不存在“新微信号”响应字段。

## 路由清单

| 方法 | 路径                                                                   | 鉴权                      | 用途                             |
| ---- | ---------------------------------------------------------------------- | ------------------------- | -------------------------------- |
| GET  | `/api/v1/admin/users`                                                  | ADMIN_READ                | 用户列表、普通查询和联系状态筛选 |
| POST | `/api/v1/admin/users/search-by-wechat`                                 | ADMIN_READ                | 通过完整微信号查询用户           |
| GET  | `/api/v1/admin/users/{user_id}`                                        | ADMIN_READ                | 用户、完整联系元数据和学习概况   |
| POST | `/api/v1/admin/users/{user_id}/commands/contact-status`                | ADMIN_WRITE               | 更新联系状态                     |
| POST | `/api/v1/admin/users/{user_id}/commands/verify-contact-change`         | ADMIN_WRITE               | 核对微信号变更                   |
| POST | `/api/v1/admin/users/{user_id}/contact-copy-events`                    | ADMIN_WRITE               | 在前端写入剪贴板前记录复制审计   |
| GET  | `/api/v1/admin/contact-corrections`                                    | ADMIN_READ                | 分页查询联系更正申请             |
| GET  | `/api/v1/admin/contact-corrections/{correction_id}`                    | ADMIN_READ                | 查询申请详情和脱敏时间线         |
| POST | `/api/v1/admin/contact-corrections/{correction_id}/commands/{command}` | ADMIN_WRITE + IDEMPOTENCY | `approve` 或 `reject`            |

## 用户列表与搜索

`GET /api/v1/admin/users` 支持：

| Query            | 类型        | 说明                          |
| ---------------- | ----------- | ----------------------------- |
| `query`          | string/null | 用户公开编号包含匹配，最长 64 |
| `contact_status` | enum/null   | 服务端联系状态筛选            |

响应保持数组格式以兼容既有前端。每个列表项在原用户投影上增加：

```json
{
  "user_id": "USER-1",
  "account_status": "ACTIVE",
  "last_active_at": "2026-09-29T08:00:00Z",
  "formal_entitlement_count": 2,
  "limited_entitlement_count": 1,
  "open_feedback_count": 0,
  "contact": {
    "wechat_id": "wx_example",
    "contact_status": "CONTACTED",
    "change_pending": false,
    "verified_at": "2026-09-29T07:00:00Z",
    "verified_by": "7",
    "updated_at": "2026-09-29T08:00:00Z"
  },
  "contact_degraded": false
}
```

联系投影上游不可用且未使用联系状态筛选时，基础用户投影仍可返回，`contact=null`、`contact_degraded=true`；不会以空微信号伪装成功。使用联系状态筛选时无法安全判断结果，返回 `503 MINIAPP_API_UNAVAILABLE`。

`POST /api/v1/admin/users/search-by-wechat` 请求体：

```json
{ "wechat_id": "wx_example" }
```

## 用户详情

`GET /api/v1/admin/users/{user_id}` 在列表项字段外增加：

```json
{
  "learning_degraded": false,
  "open_scene_completed_count": 7,
  "learning_days": 12,
  "favorite_count": 4
}
```

学习上游不可用时三个计数均为 `null` 且 `learning_degraded=true`，不使用 0 冒充真实统计。

## 联系命令

更新联系状态请求体：

```json
{ "status": "CONTACTED" }
```

微信号核对命令无请求体。两者成功均返回完整 `ContactProjectionResponse`。复制审计成功返回 `204`；前端只有在该接口成功后才可调用剪贴板。

成功操作对应审计动作：

- `contact.view.list`
- `contact.view.detail`
- `contact.copy`
- `contact.status.update`
- `contact.change.verify`

审计摘要只包含公开用户编号、命中数量或状态，不包含微信号和更正原因正文。上游操作失败时不会写入成功审计。

## 联系更正申请

列表 Query：`status` 可选，`page` 从 1 开始，`page_size` 为 1–100。详情字段包括当前微信号、原因、状态、时间和时间线，不包含不存在的 `new_wechat_id`。

决定命令路径中的 `command` 只能为 `approve` 或 `reject`。相同幂等键和相同请求安全重放；同一键用于不同申请或不同决定返回 `409 IDEMPOTENCY_KEY_REUSED`。

成功决定响应：

```json
{
  "id": "COR-1",
  "status": "APPROVED",
  "processed_at": "2026-09-29T10:00:00Z"
}
```

## 主要错误码

| HTTP | 错误码                         | 说明                     |
| ---: | ------------------------------ | ------------------------ |
|  401 | `ADMIN_SESSION_INVALID`        | 管理员会话无效或过期     |
|  403 | `CSRF_INVALID`                 | 写请求 CSRF 校验失败     |
|  404 | `USER_NOT_FOUND`               | 用户不存在               |
|  404 | `CONTACT_CORRECTION_NOT_FOUND` | 联系更正申请不存在       |
|  409 | `IDEMPOTENCY_KEY_REUSED`       | 幂等键被不同请求复用     |
|  422 | `CONTACT_STATUS_INVALID`       | 联系状态不在五种允许值中 |
|  502 | `MINIAPP_API_INVALID_RESPONSE` | 用户服务响应结构无效     |
|  503 | `MINIAPP_API_UNAVAILABLE`      | 用户服务暂时不可用       |

## 敏感数据规则

- 微信号不得进入 URL、localStorage、sessionStorage、普通日志、统计事件或审计摘要。
- 页面离开时取消请求并清除内存中的更正详情。
- API 不返回 OpenID、密文、HMAC 密钥或 OSS 永久地址。
- 本批次不依赖生产 OSS；真实阿里云 OSS 接入按后续独立批次完成。
