# 两个后台项目本地收尾验收（2026-10-02）

用户明确选择“完成本地开发与可自动执行的验收”，并在测试身份权限不足后要求先跳过云权限。本轮完成本地工具修复、自动回归、真实管理 HTTP 与教学草稿准备；云端正向验收延期，素材人工确认保留待办。生产部署不在本轮范围。

此前 B01–B05、C01–C06、O01–O05 的实现和集成证据见 [V1.3 统一验收](../v13-unified/acceptance.md)。该文件是 10 月 1 日历史快照；本页及本轮 JSON 记录最新测试、草稿版本和延期决定。此次未发现新的后台产品功能缺口，修复集中在验收工具。

## 本轮变更

| 项目 | 完成内容 | 验证 |
|---|---|---|
| OSS CORS 工具 | 精确规划 127.0.0.1:5173、localhost:5173、127.0.0.1:18173；补 Content-Type、Range、ETag、x-oss-request-id 和 600 秒缓存；新增规则置前，保留旧规则字段与相对顺序 | 缺头、大小写、容量、幂等、旧规则保留及首条通配规则回归；默认只读，写入需显式参数且仅允许绑定测试桶 |
| 隔离验收工具 | 每阶段新建 UUID MySQL 库与独占 Redis 容器/随机回环端口；拒绝复用；运营分页测试与迁移回退分库；不继承运行时数据库/Redis URL | 成功、失败、资源归属、凭据脱敏、环境隔离和实际 SIGINT 离线回归；只清理本轮成功创建的资源 |
| 中断处理 | CREATE 与归属登记、DROP/RM 清理延迟 SIGINT；临界命令独立进程组，普通 pytest 保持可中断；提示准确表述 cleanup attempted | 数据库创建后、Redis 创建后、DROP 期间三个实际 SIGINT 用例先失败后通过；文案和进程组回归同样先失败后通过 |
| 教学草稿 | 六句中文译文、六句 ASR 标时候选真实保存并回读；设备预览保存后当前 `DRAFT v10` | 原句/词条固定版本和素材引用保留；所有 `timing_confirmed=false`，未发布，未绑定未听音的独立候选 |
| 交付文档 | 本地复跑命令、最小云策略、候选试听和素材缺口、脱敏 HTTP/浏览器证据 | 文档与当前草稿/测试结果一致 |

OSS 按规则顺序采用首个匹配项，因此已有通配规则不能被后面的完整规则修复；规划器同时考虑通配首匹配和精确首匹配。[官方 PutBucketCors 文档](https://www.alibabacloud.com/help/en/oss/developer-reference/putbucketcors)。

## 自动验收

最新完整结果见 [automated-validation.json](evidence/automated-validation.json)。

| 范围 | 结果 |
|---|---|
| juya-admin `pnpm check` | 格式、ESLint、Stylelint、Vue/TS 类型检查通过 |
| juya-admin `pnpm test` | 86 个文件，294 个用例通过 |
| juya-admin `pnpm build` | 构建通过 |
| juya-admin Chromium 回归 | 117 个用例通过，含业务路径和双视口；使用受控 API 响应 |
| juya-admin-api 静态检查 | Ruff check/format 通过；mypy 121 个源文件通过 |
| juya-admin-api 完整隔离 pytest | 通用阶段 387 passed / 4 skipped；运营阶段 1 passed，合计 388 passed / 4 skipped；两阶段各自使用新库和 Redis |

四个供应商/真实 OSS 浏览器用例在完整套件中按未启用条件跳过，不能视为通过。实际只读浏览器检查另外执行并如实记录失败来源。此次未修改用户 API 源码，也不将 10 月 1 日用户 API 的测试数冒充本轮结果。

后端复跑（已有本地 MySQL 和 Redis 镜像，不拉取镜像、不复用其他测试库）：

```powershell
cd D:\个人\juya\juya-admin-api
uv run python scripts/test-v13-isolated.py juya-admin-api --suite -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

前端复跑：

```powershell
cd D:\个人\juya\juya-admin
pnpm check
pnpm test
pnpm build
pnpm exec playwright test --project=chromium
```

## 实际本地联调

- 主站后台 [127.0.0.1:5173](http://127.0.0.1:5173/) 可访问；管理 API 8000、用户 API 8001 readiness 的 MySQL/schema/Redis 检查通过。管理 API 的配置检查也通过。
- 以本轮自建合成用户验证完整微信号 POST 查询、昵称/NEW_TODAY/联系状态组合查询、句芽编号查询、跨服务用户详情、工作台真实统计和 PAUSED 权益字段。见 [operations-main-http.json](evidence/operations-main-http.json)。头像结果另列为延期，没有混入这些通过项。
- 实际浏览器打开当前草稿、显示六句中文及标时候选；整段真实 WAV `duration=24.38`、`readyState=4`、`error=null`，播放时 currentTime 递增。此证据只证明浏览器播放链路，不能替代人的实际听音。见 [sample-final-readback.json](evidence/sample-final-readback.json)。
- 管理 API、两个 Worker、Beat、用户 API/Worker 保持运行。临时 CORS 前端与本轮隔离资源在验收后清理。历史 500 项杀进程/Beat 恢复和合成素材发布证据保留在统一验收中；本轮未重做该历史实测。

## 用户明确延期：云权限

实际测试身份的 `oss:PutBucketCors` 被 AccessDenied 拒绝；头像原图读取通过，归一化后的 `avatars/*` 固定写入 `oss:PutObject` 被资源组身份策略隐式拒绝，确认接口返回 `503 AVATAR_STORAGE_UNAVAILABLE`。代码没有把失败的头像保存为成功。见 [cloud-permission-blocks.json](evidence/cloud-permission-blocks.json)。用户回复“目前不方便弄权限，可以先跳过吗”后停止相关云写入，不再请求其补权限。

现状证据：[cors-current-and-plan.json](evidence/cors-current-and-plan.json)、[cors-browser-current.json](evidence/cors-browser-current.json)。127.0.0.1:5173 的实际既有合成对象 GET/HEAD 200、哈希匹配且 ETag 可读；localhost:5173 和 18173 仍被 CORS 阻止。带 Range 的预检仍有缺口。POST 本轮只做 OPTIONS，不宣称新来源浏览器直传已通过。

最小补权策略已经备好：[local-closeout-oss-policy.json](../../operations/local-closeout-oss-policy.json)。权限恢复后再使用 `scripts/oss-cors-audit.py --help` 所列显式写入参数应用计划，并重跑浏览器 Range/POST/GET/HEAD 和头像上传确认/读回。本轮没有变更现有云 CORS，也未访问生产桶。

## 正式素材仍需人工输入

详细记录和九段试听入口见 [local-closeout-materials.md](/D:/个人/juya/juya-admin/docs/implementation/local-closeout-materials.md)。

1. 正式版权/授权与来源尚空。图片中生成来源元数据不能替代使用权确认。
2. 六句时间是 ASR 候选，须逐句听原录音后确认；所有核对标记仍为 false。
3. 九段独立音频候选须实际听音确认，尤其 `improve` 候选识别结果涉及 `improved`。`coordinate our shifts` 和 `sanction the plan` 缺原录音。

当前发布检查仅剩 `COPYRIGHT_SOURCE_REQUIRED`、`SENTENCE_TIMING_INVALID`，`ready=false`。没有制造授权、人工听音或标时确认，没有发布正式教学内容。TTS 和付费云审核继续按既定决定关闭；本轮未新增真实 OCR/审核调用。生产上线、微信真实登录和 iOS/Android 真机验收仍不属于本次本地交付。

## 独立审查

独立审查发现两项实际问题：首条通配 CORS 匹配被遗漏，以及 Ctrl+C 导致资源创建/归属登记和清理之间的中断窗口。两项均通过失败回归重现、修复后定向通过，再执行本页完整隔离套件。没有把审查建议当作未验证的结论，也未借机扩大云权限或修改产品范围。
