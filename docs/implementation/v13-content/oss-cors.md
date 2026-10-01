# OSS 本地浏览器 CORS 核查与追加方案

核查日期：2026-10-01。当前 `http://127.0.0.1:5173` 已允许，旧的 5173 CORS 失败记录只能作为历史证据。`http://127.0.0.1:18173` 当前未允许。

## Bucket 范围与变更结果

使用现有本地容器提供的凭据，只检查绑定的 `juya-test`：`JUYA_ENVIRONMENT=local`、配置 Bucket 与 Expected Bucket 均为 `juya-test`，云端 ACL 为 `private`。凭据、Authorization 和签名 URL 没有打印或落盘。

已尝试授权范围内的测试 Bucket 最小追加，`PutBucketCORS` 返回 `AccessDenied`。没有成功修改云端规则；后续重新读取确认原规则保留。没有切换账号、扩大权限、修改生产 Bucket 或重新启动现有服务。

既有完整配置见 [oss-cors-plan.json](./evidence/oss-cors-plan.json)。`ResponseVary=true`，既有规则如下，必须完整保留：

```json
{
  "allowed_origins": ["http://localhost:18086", "https://juya-admin.vercel.app", "http://127.0.0.1:5173"],
  "allowed_methods": ["GET", "POST", "HEAD"],
  "allowed_headers": ["content-type"],
  "expose_headers": ["ETag"],
  "max_age_seconds": 300
}
```

需要有该测试 Bucket `oss:PutBucketCORS` 权限的操作者执行以下追加；不要以本片段覆盖全部 CORS 配置：

```json
{
  "allowed_origins": ["http://127.0.0.1:18173"],
  "allowed_methods": ["GET", "POST", "HEAD"],
  "allowed_headers": ["content-type"],
  "expose_headers": ["ETag"],
  "max_age_seconds": 300
}
```

现有浏览器上传实现将 OSS V4 的 policy、credential、signature 等字段放在 multipart 表单体，HTTP 头不携带这些签名字段。当前预检实际请求头为 `content-type`；无需添加 `*`、PUT、DELETE 或 Authorization。以后上传实现若改变请求头，应重新核查。阿里云文档：[CORS 配置](https://www.alibabacloud.com/help/en/oss/user-guide/configure-cross-origin-resource-sharing/)、[POST Object](https://www.alibabacloud.com/help/en/oss/developer-reference/postobject)、[OPTIONS](https://www.alibabacloud.com/help/en/oss/developer-reference/options)。

## 实际验证

| 检查 | 5173 | 18173 |
|---|---|---|
| HTTP OPTIONS，GET/POST/HEAD，请求 content-type | 200，精确 ACAO | 403，无 ACAO |
| 实际 Chromium 页面来源读取既有 WAV | GET 200，16044 字节，SHA256 匹配，ETag 可读 | 浏览器阻止读取 |
| 实际 Chromium HEAD 预检 | OPTIONS 200，精确 ACAO | OPTIONS 403，无 ACAO |
| 实际 Chromium 签名 HEAD | 200，ETag 可读 | 浏览器阻止读取 |

额外负向检查：`https://untrusted.example` 的 GET/POST/HEAD 预检均 403，无 ACAO。实际浏览器证据见 [oss-cors-browser.json](./evidence/oss-cors-browser.json)。

浏览器测试使用已有 `/fixtures/v13-http/` 合成 WAV，仅证明存储/CORS/读取技术链路；本轮没有上传、删除对象。POST 仅检查 OPTIONS，没有发起 POST 上传。合成音频不能当作真实教学内容或阿里云内容安全验收。签名 HEAD 使用与请求一致的 Content-Type；签名和请求头必须匹配。

## 可复用命令

在 `D:\个人\juya\juya-admin-api` 下：

```powershell
# 默认只读，计划与前后规则对比
.\.venv\Scripts\python.exe scripts\oss-cors-audit.py --report docs\implementation\v13-content\evidence\oss-cors-plan.json

# 仅在测试 Bucket 操作者具备权限后执行：读当前规则、保留、追加、重读验证
.\.venv\Scripts\python.exe scripts\oss-cors-audit.py --apply-local-test-rules --report docs\implementation\v13-content\evidence\oss-cors-plan.json

# 实际浏览器只读验证：仅在内存中传递短时签名
.\.venv\Scripts\python.exe scripts\oss-cors-browser-check.py

# 无外部请求的保护/计划回归；浏览器 live 测试默认跳过
.\.venv\Scripts\python.exe -m pytest tests\live\test_oss_cors_plan.py tests\live\test_oss_cors_browser_readonly.py -q

# CORS 追加完成后，双来源严格验收（当前18173未允许，将失败）
$env:JUYA_RUN_LIVE_OSS_BROWSER_TESTS = "true"
.\.venv\Scripts\python.exe -m pytest tests\live\test_oss_cors_browser_readonly.py -q
Remove-Item Env:\JUYA_RUN_LIVE_OSS_BROWSER_TESTS
```

脚本使用现有本地容器 `juya-admin-api-admin-api-1` 的配置，不修改 `.env`；可通过 `--container` 指定另一个同样明确绑定测试 Bucket 的本地容器。Browser helper 和 Chromium runner 已放在仓库 `scripts/oss-cors-browser-*`，默认读取仓库内 [media-http.json](./evidence/media-http.json) 的既有合成对象路径，可用 `--fixture` 指定同格式证据文件，仍拒绝非 `/fixtures/v13-http/` 对象。要求 Node.js、相邻 `juya-admin` 项目的 Playwright 依赖及 Chromium，以及两个实际前端来源服务。缺少证据文件、Node.js、后台依赖或 Chromium 时，opt-in 测试会明确说明原因并跳过；Bucket 凭据或真实 HTTP 失败不会伪装成通过。默认只将脱敏结果输出到 stdout，需保存证据时显式传入 `--report`。配置脚本限制 private `juya-test`，原规则变化则中止，规则容量满则中止，不替换任何既有规则。

## 用户提供的真实素材（只读元数据）

用户授权目录：`D:\个人\图片+音频\图片+音频`。全部图片经 Pillow 解码，音频经 ffprobe 读取和检查；原文件没有修改。配对按名称确认，音频的教学内容对应和逐句标时仍需实际试听；没有按文字长度生成时间。

| 配对 | 图片实际尺寸 / 大小 | 音频实际格式 / 时长 / 大小 |
|---|---|---|
| A Better Way to Work（主任务已选） | PNG 1024×1536 / 1786755 B | WAV 24.380 秒 / 3900878 B |
| A Better Fit | PNG 1024×1536 / 2100826 B | WAV 31.000 秒 / 4960078 B |
| A Better Game Night | PNG 1024×1536 / 2047359 B | WAV 32.700 秒 / 5232078 B |
| A BOTANICAL GARDEN FIELD LESSON | PNG 4096×6144 / 23132282 B | WAV 61.080 秒 / 9772878 B |

选定文件为 `D:\个人\图片+音频\图片+音频\A Better Way to Work.png` 和 `D:\个人\图片+音频\图片+音频\A Better Way to Work.wav`，由主任务统一上传与 OCR，本任务不重复上传或识别。Garden 原图超过 OCR 的最大边 4096 和编码大小 8MB，需要另行允许的派生图片。Game Night 图片原名为 `.png.png`，未改名。完整路径、哈希、元数据见 [materials.json](./evidence/materials.json)。版权归属未独立核验；用户授权读取使用并不等于第三方版权验证。

## 仍需外部条件

- 18173 来源需要有测试 Bucket CORS 写权限的操作者追加上述规则，再运行双来源 live 验收。
- 真实教学 OCR、保存、标时、发布及终端读取由主任务和 UI 联调负责，本报告不能替代该链路证据。
- 本轮不涉及生产 Bucket CORS 或阿里云云审核验收。审核开关的当前部署状态由主任务报告。
