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
ID/Secret/Token/到期时间作为整组读取：JUYA_OSS_* 优先，缺少主凭据时才使用
OSS_* 整组，禁止逐字段混合；主凭据不完整时拒绝签发。轮换 STS 时同步更新
JUYA_OSS_CREDENTIALS_EXPIRES_AT。部署平台修改环境通常需要重启进程；
进程内环境刷新能力不等于自动读取宿主机更新或外部凭据文件。
策略最长 600 秒，STS 在到期前至少保留 30 秒余量。签名读取的对外到期时间
同时受权益和实际 OSS 签名到期时间限制。生产缺配置启动失败，不退回本地文件。

生产上线另行设置生产的 BUCKET/EXPECTED_BUCKET、独立 RAM 角色与来源。
EXPECTED_BUCKET 是配置误接防护；最终权限隔离仍靠各环境 RAM 身份的资源授权，
不是把测试策略重命名为 Production 就完成隔离。

不要把密钥放进 VITE_*、小程序源码、Git、日志、测试快照。用户提供的凭据文件
只在当前进程内读取，不复制到仓库。权限申请只针对所需 Bucket 和目录，不授予
AdministratorAccess 或 AliyunOSSFullAccess。

## 上传确认的独立内容安全配置

`POST /api/v1/admin/media/uploads/confirm` 会读回 OSS 对象、校验真实字节并执行
独立内容安全审核。仅配置 OSS 可以签发上传策略和直传，但不能完成素材确认；
默认 `disabled` 会返回 HTTP 503 和 `MEDIA_SECURITY_UNAVAILABLE`，提示
“未配置独立阿里云内容安全提供方”。

普通图片和音频需要已开通的阿里云内容安全服务及具备对应审核权限的服务端凭据：

| 配置 | 值或说明 |
| --- | --- |
| JUYA_CONTENT_SECURITY_PROVIDER | aliyun |
| JUYA_CONTENT_SECURITY_REGION | 已开通审核服务的区域；默认 cn-shanghai，与 OSS 区域分别配置 |
| JUYA_CONTENT_SECURITY_ACCESS_KEY_ID | 内容安全服务端凭据 ID |
| JUYA_CONTENT_SECURITY_ACCESS_KEY_SECRET | 与 ID 配套的 Secret |

本地 `docker-compose.dev.yml` 将这些配置传入 API、两个 Worker 和 Beat。
在启动 PowerShell 进程中注入配置后，重新执行 `scripts/start-local-oss.ps1`
并传入现有测试 OSS 凭据文件，即可重建应用容器并继续使用测试 Bucket；
无需重置数据库或管理员。已有镜像缺少审核实现时，先重新构建应用镜像。
`.env.example` 列出配置名称；不要将真实密钥写入该文件或提交到 Git。

不自动复用 OSS 凭据，也不自动开通服务或扩大 RAM 权限。
真实审核尚需核实账户开通、权限和区域；配置完成后再验收真实素材确认。
`local` 提供方仅用于显式开启的 local/test 合成 `/fixtures/` 素材测试，
不能用于放行普通上传图片和音频。

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

- 来源：分别添加 `http://127.0.0.1:5173` 和 `http://localhost:5173`，不要使用 *。
  若访问其他端口，必须单独列出真实来源；localhost 与 127.0.0.1 并非同一来源。
- 方法：POST、GET、HEAD。
- 允许头：Content-Type、Range。
- 暴露头：ETag、x-oss-request-id。
- 缓存时间：600 秒。

本地 Compose 默认签发的是占位 Bucket 地址，不能用于上传。已有本地服务运行时使用
`scripts/start-local-oss.ps1 -CredentialFile <测试凭据文件>` 切换到 `juya-test` 并重建应用容器。
脚本不修改云端 CORS；配置 CORS 需要测试 Bucket 的 `oss:PutBucketCORS` 权限。
默认 live CORS 测试来源为 `http://127.0.0.1:5173`，可通过 `JUYA_OSS_BROWSER_ORIGIN` 指定另一个实际来源。

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
候选按到期时间/id 排序。被保护或删除失败的截图保持未删除状态，将 delete_after
延后一天作为复查时间，避免前 100 项永远占满批次；复查仍执行完整引用检查。
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

2026-09-30 管理端上传跨域排查：

- 运行中的 Compose 原为杭州 `juya-local-placeholder` 和占位凭据，已通过新脚本切换为深圳 `juya-test`。
- 管理 API 实际登录并申请上传策略返回 HTTP 200，地址为 `juya-test.oss-cn-shenzhen.aliyuncs.com`。
  使用该策略上传 PNG、HEAD、签名读取一致均通过；仅删除本次对象，删除后读取返回 404。
- 两个本地来源的 POST 预检仍返回 403，GetBucketCORS 返回 NoSuchCORSConfiguration。
  尝试仅为测试 Bucket 增加上述规则时，PutBucketCORS 返回 AccessDenied 403，未修改任何规则。
  用户确认尚未配置；必须由有权限的账号完成控制台配置后再做浏览器验收。
- 后端默认测试 191 passed / 54 skipped；真实 OSS 测试 2 passed / 1 CORS failed。
  启动脚本回归覆盖应用容器配置、环境恢复、Docker 失败和不完整凭据拒绝。
