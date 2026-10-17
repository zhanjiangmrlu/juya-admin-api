# V1.5 已确认偏离整改的源码交付与契约来源

本记录描述 2026-10-14 至 2026-10-15 已授权整改的源码职责、事实依据和交付边界。它不是最终门禁、云端部署或业务验收报告。既有事实审计见 [v15-fact-based-execution-20261014.md](v15-fact-based-execution-20261014.md)；该审计中的 HEAD、接口数量和诊断均属于当时快照。

## 需求与源码对应

完整需求为 `doc/句芽英语V1.5开发交付包-20261011/句芽英语V1.5完整需求文档-开发交付整合确认版-20261011.md`。正式设计文件为 `evoGqXQ4pI3Q3cdLpc6NOc`，后台根 `2660:2`、用户端根 `2660:3`。下面的代码路径相对各自仓库，不以测试数量代替功能或业务验收。

| 整改职责 | 需求与设计依据 | 对应实际源码与契约 | 交付边界 |
| --- | --- | --- | --- |
| 七天注销撤回截止 | 需求 10.3、24.7（504—508、1335—1339 行） | miniapp-api `modules/accounts/repository.py` 的内存及 SQL `revoke_deletion`，事务内比较持久化 `effective_at` | 原账户清理职责保留；未清理真实用户 |
| 反馈提交和补充前检查 | 需求 663—671、23 章；M40 `2480:1334`、M43 `2480:1496` | miniapp `features/feedback/feedback-form.ts`、`supplement-submit.ts` 和表单组件；规则来自 miniapp-api `modules/feedback/content_safety.py` | 客户端预检不替代服务端文本及图片审核；联系提示触发明确暂缓 |
| 待办文案和限时权益确认 | 需求 731—743、762—768、1318；A01、历史 A07/A09/A13 | admin `features/work-items/work-item-model.ts`、限时权益页面及 adapter；本 API `modules/limited_entitlements/router.py` 的只读 `preview-grant`、既有 `preview-operation` 与授予事务 | 授予预览是本轮必要契约设计；没有替真实用户授予权益 |
| 独立批次页面、统一 ZIP 四阶段和内容列表 | 需求 782—790、850—859、909—914、1038—1046、1855—1877；A17、A18U/UV/UP/UD、A20、A24 | admin 工作单、导入、内容列表和任务组件；本 API `modules/content_production/schemas.py` 的 `WorkOrderPhases`、`WorkOrderUsage` 及真实投影；原文件 GET 下载 | 保留上传→校验→对应预览→确认草稿→校对发布；阶段/用量依赖是必要新增契约，不填虚构价格 |
| 独立素材匹配和词汇图包 | 需求 782、824—834、1776—1799；A18M、A18Z/ZV/ZP/ZD | admin `features/content-import/content-materials-panel.vue`、材料 adapter/model；本 API 素材 manifest 与绑定请求/结果 | 目标、草稿版本、角色和条目身份固定；UNKNOWN 不推断已受理、未完成、成功或失败；有真实阶段仅说明已提交等待实际结果 |
| 原文对照、场景词条和纯短文编辑 | 需求 785—788、805—822、1634—1642；A20S、A19E、A30、A21 | admin `content-work-item-page.vue`、`scene-pairing-panel.vue`、`lexicon-media-fields.vue`、短文及原文组件；原 revision/hash/version/纠错/资产确权契约 | 原文与候选分离；语块无图、短文无词库/讲解；A30 保存次要、预览与发布检查主要；不代填人工试听 |
| 冻结发布范围和结果 | 需求 875—906，尤其 16.9；A22P `2663:675`、A22R `2663:802` | admin `work-order-publication.vue`、`work-order-publication-scope.ts` 和工作单页；本 API `PublicationState` 与 `content/publication_ranges.py` | 真实 PREVIEW 与结果阶段分开；只重试冻结 FAILED；UNKNOWN 原回执单列；成功数和已发布查看保留；A22 第四抽样指标明确暂缓 |

以上只涵盖本轮确认整改及必要依赖，不表示全部 V1.5 功能与每种业务状态已经验收。已有场景/短文版本、历史收藏、资源、正常抽样确认、预算和鉴权职责继续沿原模型。

## 必要新增实现设计与尚缺依据

- 本轮限时授予只读预览、工作单阶段/用量投影、素材分类和词汇表存在性等必要契约随真实源码导出；不可把首轮审计旧契约数量当成最终来源。
- 发布页主顶栏采用窄范围 Vue `provide/inject` 回调（admin `app/work-order-publication-heading.ts`）。页面只有在实际读取成功、当前工作单与 `PublicationState.work_order_id`/token 匹配时提供标题；路由、实例 owner 与加载代次隔离，未载入/读取错误时回到中性标题。这是必要呈现实现设计，没有新增业务接口或全局状态模型。
- A22R 主表只列明确 FAILED，未知及处理中结果分开，错误说明保持真实代码。A22P 仍列真实冻结候选；现有 `PublicationCandidate` 只有身份、版本、hash 和 warning codes，没有冻结的“正常抽样确认/异常单项确认”来源分类。该来源分组需要人类确认是否补历史来源快照，不能从当前候选状态猜历史分类；未实现新快照契约。
- A22 F05 第四统计与第三场景完成后的联系提示触发均由用户明确暂缓，须等需求/设计补齐。

## 最终契约来源规则

实际离线导出入口为 `scripts/export-v15-openapi.py`，从挂载应用取得完整 OpenAPI 和 method/path 清单，不打开外部连接。`scripts/generate-v15-schema.py` 属于 SQL 迁移，不用于 OpenAPI。

顺序为：先提交必要后端源码/契约/本记录，再在干净 backend HEAD 运行 `.venv/Scripts/python.exe scripts/export-v15-openapi.py`；如导出真的变化，提交变化并在新干净 HEAD 再导出核对。没有导出差异时不制造契约修改。admin 消费快照须与完整源 JSON 的全部值相等，路由清单须与全部 mounted method/path 集合一致，随后执行官方 `pnpm generate:api`，不能手编 DTO。

admin `openapi/admin-api-source.json` 的 `backend_commit` 为实际导出来源的已提交后端 HEAD；`source_sha256` 对源文件实际原始字节计算。`semantic_sha256` 对完整解析 JSON 按 Python `json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")` 计算 SHA256，不删除字段、不排序数组、无末尾换行。其余统计来自完整 `paths` 与 `components.schemas`；操作按 GET/POST/PUT/PATCH/DELETE/OPTIONS/HEAD/TRACE 统计，管理、internal、health 分组必须覆盖全部操作。

精确最终 HEAD、原始字节和语义哈希、数量、命令结果、生成 DTO 比较以及后续门禁/部署事实保存在协调侧忽略的 `task-8-report.md` 和最终交付记录；本记录不写自己的 commit 来形成循环引用。正式全量 check/unit/私有 SQL、构建、最终审查及已授权 TEST 发布由主任务执行。未变 miniapp/API 已持有官方检查产物，本任务不重复其门禁。

## 验收层次

本任务的组件和 Mock 浏览器反例/通过结果只支持本地呈现、行为、请求身份和隔离保护。官方检查、完整私有 SQL、构建产物、TEST 已部署版本、健康检查、真实浏览器、供应商素材、人工版权/完整试听、正式场景确认发布及微信硬件分别记录。没有执行的项目不得标为通过。上线后的证据写忽略的交付记录，避免改变已检查和待发布的源码 HEAD；最终 API 源码若再变，必须重新导出并同步消费端。
