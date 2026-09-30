# OSS 本地联调与环境隔离

## 当前边界

本次只使用私有测试 Bucket `juya-test`（华南1深圳，`cn-shenzhen`）。
生产 Bucket `juya`不在联调请求范围内。测试 RAM 身份、凭据、配置不能用于生产。
2026-09-30 用户明确：先完成 OSS 上传、读取、删除，真实 OCR/TTS 后续验收。

上传只是对象入库，不等于业务素材已通过安全检查。管理端直传策略固定
`x-oss-meta-security_status=PENDING`、`x-oss-meta-decodable=false`，
浏览器不能自行声明 PASSED。素材确认仍需可信服务端实际解码、校验哈希和安全检查；
本批不伪造这些结果，也不以模拟 OCR/TTS 证明真实处理成功。

## 服务端配置

两个 API 使用同一组配置名：

| 配置 | 本地测试值或说明 |
| --- | --- |
| JUYA_ENVIRONMENT | test |
| JUYA_OSS_REGION | cn-shenzhen |
| JUYA_OSS_BUCKET | juya-test |
| JUYA_OSS_EXPECTED_BUCKET | juya-test；必须显式匹配，不匹配启动失败 |
| JUYA_OSS_ENDPOINT | https://oss-cn-shenzhen.aliyuncs.com |
| JUYA_OSS_CREDENTIALS_MODE | environment；生产 ECS 可选 ecs_ram_role |
| JUYA_OSS_RAM_ROLE_NAME | ECS 模式必须显式配置角色名，使用 IMDSv2 |
| JUYA_OSS_ACCESS_KEY_ID / JUYA_OSS_ACCESS_KEY_SECRET | 仅服务端环境注入；无静态默认密钥 |
| OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET | 未设置 JUYA 名称时兼容这组服务端变量 |
| JUYA_OSS_SESSION_TOKEN / OSS_SESSION_TOKEN | STS 安全令牌 |
| JUYA_OSS_CREDENTIALS_EXPIRES_AT | STS 模式必须提供带时区的未来时间 |

普通环境凭据每次操作重新读取；RAM 角色由受控 provider 刷新，不启用隐式凭据链。
策略最长 600 秒，STS 在到期前至少保留 30 秒余量。签名读取的对外到期时间
同时受权益和实际 OSS 签名到期时间限制。生产缺配置启动失败，不退回本地文件。

生产上线另行设置生产的 BUCKET/EXPECTED_BUCKET、独立 RAM 角色与来源。
EXPECTED_BUCKET 是配置误接防护；最终权限隔离仍靠各环境 RAM 身份的资源授权，
不是把测试策略重命名为 Production 就完成隔离。

不要把密钥放进 VITE_*、小程序源码、Git、日志、测试快照。用户提供的凭据文件
只在当前进程内读取，不复制到仓库。权限申请只针对所需 Bucket 和目录，不授予
AdministratorAccess 或 AliyunOSSFullAccess。

## 测试 RAM 策略

测试用户仅授予 `JuyaOssTestAccess`。对象动作 PutObject/GetObject/DeleteObject
只允许如下 Resource：

```text
acs:oss:*:*:juya-test/uploads/*
acs:oss:*:*:juya-test/feedback/*
acs:oss:*:*:juya-test/generated/audio/*
acs:oss:*:*:juya-test/oss-live-tests/*
```

Bucket 的 GetBucketAcl/GetBucketCORS/GetBucketLocation 只允许
`acs:oss:*:*:juya-test`。测试身份不得叠加其他全量 OSS 策略。
测试不会请求生产 Bucket 来验证隔离；上线前另审查全部直接/用户组/角色授权。

## CORS 与直传

在 juya-test → 数据安全 → 跨域设置添加规则：

- 来源：`http://localhost:5173`，不要使用 *。若访问 127.0.0.1 或其他端口，
  必须单独列出真实来源；localhost 与 127.0.0.1 并非同一来源。
- 方法：POST、GET、HEAD。
- 允许头：Content-Type、Range。
- 暴露头：ETag、x-oss-request-id。
- 缓存时间：600 秒。

前端代理只代理 API，不代理 OSS。管理端 XHR 和小程序 uploadFile 直接提交
服务端给出的 V4 fields，包含绑定 MIME、key、大小和有效期；file 必须放在表单最后。
不能用前端代理的成功掩盖 Bucket 跨域缺失。浏览器上传与签名读取仍需实际 CORS 验证。
原生小程序不使用浏览器 CORS，但上线仍需配置微信 uploadFile/downloadFile 合法域名；
H5/WebView 来源按实际域名分别配置。无需等待自有域名备案即可用 OSS 测试服务地址联调。

签名算法与字段依据 [阿里云 POST V4 文档](https://www.alibabacloud.com/help/zh/oss/developer-reference/signature-version-4-recommend)。

## 生命周期与引用保护

业务只保存 object_key，不保存签名 URL。反馈截图清理只删除已到保留期、
工单处于 RESOLVED/CLOSED_INSUFFICIENT 的 feedback/ 对象。再次检查并锁定工单和截图，
保护其他未删除反馈、所有已登记 media_asset、封面、头像以及版本快照引用。
历史音频/版本也保留，不只保护当前活动版本。被保护或失败的记录不会标记 deleted_at。

审计先写删除意图再请求 OSS，完成后更新 DELETED/FAILED；保护时记录 PROTECTED。
审计只保存工单标识、结果、对象键 SHA256，不保存原始 key、URL、令牌或 SDK 异常正文。
若 OSS 已删除但数据库提交失败，重试幂等删除后重新落库；不假设跨数据库与 OSS 的原子性。

引用锁读取在未建立对象索引的表上可能扩大锁范围；每批最多 100 项，低峰执行。
共享对象宁可暂时保留，也不自动删除。禁止用 Bucket 自动生命周期规则绕开业务检查。
草稿清理保留前六批保留期与引用检查，不自动删除共享 media_asset 对象。

OCR 输入须在任务上传者的 uploads/images/ 前缀，并与持久化任务输入一致；
TTS 输出须在 generated/audio/，登记失败使任务终态 FAILED，错误不含私有 URL。
这些边界已测试；真实供应商接入、处理后的对象存在性和内容校验仍待后续验收。

## 可选真实测试

默认 pytest 不访问云端。只有 `JUYA_RUN_LIVE_OSS_TESTS=true` 才运行 live tests；
测试必须同时指定 BUCKET=EXPECTED_BUCKET=juya-test。
测试使用独立随机对象键，仅清理本次创建的对象；绝不批量列举或删除 Bucket。
真实测试的结果、CORS 检查、清理证据将在本节记录；未运行不得写成验收完成。

复现命令（在 admin-api 目录执行，凭据不写入脚本）：

```powershell
./scripts/test-oss-live.ps1 -CredentialFile 'D:\个人\juya\doc\oss信息.txt' -Target admin
./scripts/test-oss-live.ps1 -CredentialFile 'D:\个人\juya\doc\oss信息.txt' -Target miniapp
```

脚本固定测试环境和 juya-test，清除继承的 OSS 凭据配置，用文件中的测试凭据
临时注入当前子进程，结束后恢复原环境；不输出 AccessKey 值。

2026-09-30 真实非浏览器验证：

- admin-api live：2 passed / 1 failed。V4 上传、HEAD、签名 GET 内容一致、
  匿名 GET 403、删除后签名 GET 404 通过；错误前缀、MIME、超限大小、
  伪造 PASSED 元数据均被 OSS 拒绝（400/403）。随机测试对象已清理。
- miniapp-api live：1 passed。feedback/ 下的精确对象键、PNG 类型 V4 上传和
  HEAD 通过，本次截图对象已删除。
- Bucket ACL 实际返回 private。未请求生产 Bucket。
- CORS 测试未通过：localhost:5173 未获授权；用户确认尚未配置。
  这是待办，不是浏览器验收成功。之后配置规则，再复跑并补浏览器实测。
- OCR/TTS 按用户确认延期；生产环境、可信素材安全确认未验收。
