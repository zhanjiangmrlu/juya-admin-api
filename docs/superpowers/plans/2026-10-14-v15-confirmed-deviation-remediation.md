# V1.5 已确认偏离整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development and dispatching-parallel-agents for independent repositories. Implement task by task; one writer per repository; dependent shared files run sequentially.

**Goal:** 将首轮事实清单中已确认的偏离落实为代码修复，按当前需求、真实设计和契约验收；缺证、冲突及用户暂缓项不擅定。

**Architecture:** 保留现有结构化草稿、发布版本、快照、任务和鉴权模型。恢复设计定义的页面与阶段，用真实契约驱动数据和动作；新增必要契约明确标为实施设计，不冒充已有接口。跨独立Git仓库可并行，单仓写入/提交/契约生成顺序执行。

**Tech Stack:** 现有 Vue3/Element Plus/uni-app/TypeScript/SCSS，FastAPI/Python3.13/MySQL/Redis；不升级依赖

**Spec:** `D:/个人/juya/doc/句芽英语V1.5开发交付包-20261011/句芽英语V1.5完整需求文档-开发交付整合确认版-20261011.md`；Figma `evoGqXQ4pI3Q3cdLpc6NOc`，正式后台2660:2/用户端2660:3；`docs/implementation/v15-fact-based-execution-20261014.md`及其三份审计证据

## Global Constraints

- 用户2026-10-14明确授权“开始按清单修改代码，V1.5已确认偏离都整改，缺依据或冲突先确认”；不再重复索要整个计划的执行许可
- 七条事实规则优先于技能中的自主业务裁定；用户可见功能同时有需求和真实设计依据；后台保护依赖追溯到功能与既有契约
- 留在四仓现有test分支；不新分支/工作树/push/PR/生产发布；只提交本任务文件，中文Conventional Commits
- 需求15.7不建立另一套发布/数据模型；保留原文、版本/fence/hash、幂等、冻结合格子集、失败-only与UNKNOWN续办、历史发布/收藏资源、迟到响应隔离
- 需求32/34统一ZIP顺序是上传→校验→对应预览→确认导入草稿→校对发布，不能把导入与发布合成一个写操作
- 原文件下载必须返回原ZIP/DOCX字节；不能从候选文本重新拼文件；分页不能用当前20行冒充整批统计
- 没有真实用量/价格/阶段证据不填0或画板示例；任务current=false/无任务不能推断阶段成功
- 反馈300字/最多1图；前置文本规则复用既有服务端规则，不扩大敏感词；前置检查不代替服务端审核与图片安全检查
- 需求10.3/24.7七天内可撤回，达到effective_at不得因worker延迟恢复ACTIVE；保持原注销/清空/权益清理职责
- 2026-10-14用户已明确暂缓“一次联系提示触发整改”，等需求文档与设计稿都更新；该项不实施、不反复提问
- 既有真实浏览器调试、完整试听、场景正式确认/发布仍暂缓；不代填audio_listened、不重新排查已取消的上传按钮问题、不付费云调用/真实账号清理
- 新/改Vue函数使用const箭头及业务参数注释；SCSS嵌套、kebab-case、复用已有组件；遵守各仓AGENTS

## Review Focus

- 七天边界、数据库时区与撤回/清理竞争：到期拒撤回且不产生REVOKED/ACTIVE或撤回事件
- 点击反馈提交/补充前即拦敏感正文：不先上传截图、不POST、不清空草稿；普通文本与既有后端分类一致
- 刷新/旧链接/换批次：保持真实批次与版本，迟到结果不跨批；历史已成功操作不得因页面拆分重执行
- 异常、选填缺省、不同内容模板：异常定位准确/排除明确；短文无词库候选；语块无图；原文与候选分离
- 已发布/失败/UNKNOWN/历史收藏：只改呈现和必要依赖，成功/冻结集合和旧资源不被重试、静默覆盖或误清

## Task 1: 注销七天截止与竞争控制

**Repository/BASE:** juya-miniapp-api，d10f44bbcdbfed3c603dc3b953b9b07e6cde1877
**Files:** `src/juya_miniapp_api/modules/accounts/repository.py`；`tests/unit/accounts/test_deletion_state.py`；`tests/integration/test_account_deletion_concurrency.py`；必要关联账户HTTP用例
**Authority:** 需求504—508、1335—1339；API审计明确内存第八天反例，SQL172/365相同源码缺口。账户删除/撤回是既有设计功能，本项不新增用户页面。
**Interfaces:** 保持`revoke_deletion(user_id: str, now: datetime) -> DeletionRequest`。守卫置于现有锁/事务内，比较持久化effective_at。若需新增错误码写明实施设计，保留通用AppError DTO/HTTP兼容，客户端正常失败路径不假成功。

- [ ] 先写/跑失败反例：截止前允许；exact cutoff、cutoff后、worker未开始的第八天均拒绝；状态/时间/审计不改
- [ ] 最小实现内存与SQL相同截止守卫，不依赖worker先转DELETING；保持已有processing拒绝
- [ ] 加强现有到期并发测试：此前允许REVOKED的断言不符合新确认边界，改为到期只允许清理最终态，并记录依据；不是放宽断言
- [ ] 在私有UUID数据库/Redis运行账户SQL与并发测试（既有`scripts/test-v15-isolated.py`）；真实执行0skip才算SQL验证，不能碰共享/测试线上账号
- [ ] 运行受影响unit/HTTP、Ruff/format/mypy；提交中文fix，报告实际命令/RED与GREEN、未验边界

## Task 2: 反馈前置敏感文本检查

**Repository/BASE:** juya-miniapp，2132dbb4a01723589bb338caecdc8f40cf7ee071
**Files:** `src/features/feedback/feedback-form.ts`、`components/feedback-form.vue`、现有补充表单消费、`src/shared/constants/feedback.ts`/`src/shared/types/feedback.ts`（必要时）；`tests/unit/features/feedback/feedback-form.spec.ts`及现有反馈组件/流程用例
**Authority:** 需求663—671、23章拦截/草稿保留；本轮真实M40=2480:1334、M43=2480:1496可见表单/拦截态，承接关系已保存；最新规则优先
**Interfaces:** 保持`validateFeedbackDraft(draft)`/`validateSupplement(text)`既有规范化和值/错误契约及create/supplement端点。唯一政策来源为`juya-miniapp-api/src/juya_miniapp_api/modules/feedback/content_safety.py`六个已存在regex；不新造政策/接口。当前服务器`api/runtime.py:254`按content_security_enabled启用审核，客户端预检不改其开关/云审核。

- [ ] 读取真实提交/补充、拦截返回/重新编辑路径及已有服务端规则，先证明现状允许敏感文本发请求
- [ ] 参数化验六类现有规则：URL、联系方式、银行/证件号、交易、现有敏感关键词；非敏感近似文本、换行、大小写、300字边界正常
- [ ] 在上传/POST前本地拒绝并复用已有M43拦截/修改返回；保留正文/来源/截图草稿，无敏感正文日志
- [ ] 验被拦文本create/supplement/upload调用为0，普通文本仍遵守既有接口；检查页面生命周期和重试
- [ ] scoped检查与相关unit/Mock流程（明确非真实业务）；提交中文fix并报告；联系提示整改严格跳过

## Task 3: 后台基础文案与限时权益真实确认

**Repository/BASE:** juya-admin73cac18；必要依赖juya-admin-api f85c47d
**Files:** `src/features/work-items/work-item-model.ts`及既有测试；`src/pages/entitlements/limited-grant-page.vue`、`limited-action-page.vue`、`src/features/entitlements/limited-entitlement-adapter.ts`/command/model、`src/components/confirm-dialog/confirm-dialog.vue`必要props及既有用例；若缺授予预览，限定API `modules/limited_entitlements/{router,service,repository}.py`及其测试/导出契约
**Authority:** F08/F09；需求731—743、762—768、1318；A01 2662:4；历史A07 2538:374、A09 2538:498、A13 2538:761。历史节点当前仅metadata，修改前必须取真实高保真context+截图，与最新规则冲突单独确认。
**Interfaces:** 既有`preview-operation`只用于已有权益；`LimitedEntitlementAdapter.preview`目前拒GRANT，这是事实，不假称授予已有服务端预览。若新增授予预览为完成735/742的必要接口设计，须先明确真实前态/目标态/时刻/版本和现有授予事务的过期校验，保持幂等回执；不能客户端编到期时间或固定“由服务端返回”。

- [ ] 取A07/A09/A13高保真并读当前limited完整真实契约/正式权益已实现预览模式；F08先对齐立即处理/重新查看等明确文案，保留目的地与排序
- [ ] 中文状态、昵称/句芽号、活动名、真实前后期限/状态为主，内部ID/version移详情；普通开通不套统一高风险，其他确认调用不受影响
- [ ] 缺授予预览时先保存可审接口方案及精确依据、缺失业务规则消息Root；规则明确的必要只读预览可实施，真正冲突待人类确认
- [ ] 有效期/容量/版本变化后预览需刷新；重复成功授予不新增权益/名额；原因必填/选填沿既有规则
- [ ] 运行有关组件/adapter/命令/HTTP/SQL检查；必要契约同步（整个source schema比较，不手编DTO）；提交并报告，不实际开通用户

## Task 4: 内容生产独立页面、统一ZIP四阶段与内容列表

**Repositories:** juya-admin（单写者）；必要缺契约由同任务明确方案后限域juya-admin-api
**Files:** `src/pages/content/content-work-order-page.vue`、`content-import-page.vue`、`content-list-page.vue`；`src/features/content-production/{work-order-upload,work-order-import,work-order-overview,use-work-order,work-order-adapter,work-order-model}.ts/vue`实际既有后缀；必要新增职责明确的视图组件；`src/router/index.ts`、`src/layouts/admin-layout/admin-layout.vue`、`src/app/admin-navigation.ts`；受影响现有unit/Playwright用例
**Authority:** F01/F02/F07；需求782—790、850—859、909—914、1038—1046、1855—1877；A17 2662:140、A18 2662:271、A18U/UV/UP/UD 2836:573/689/793/930、A20 2662:649、A24 2662:520；历史A24 2538:1737只供无ID恢复入口追溯，须context核对，不能推定新旧承接规则。
**Interfaces:** 沿现有`/content/work-orders/:id?view=...`及真实WorkOrder/ImportPreview/item/commands恢复页面，内部工作单模型可保留，用户界面按设计分开。已有overview/import/candidates/sampling/publication旧链接不得跨批猜最新或创造另一发布模型；确无承接的无ID页面单独确认。

- [ ] 逐项读高保真和当前用户路径；拆掉无依据的四tab合成工作单外壳，按对应画板设置标题、统计、表格、页脚动作；A17回到设计五列与两主要入口
- [ ] ZIP上传后先到独立校验：行/导入ID/具体问题/排除；再到对应预览：正文、译文、图片角色、音频、变更/缺省；明确确认草稿后显示真实新增/候选更新/跳过/失败结果
- [ ] A20四统计用整批counts；问题位置/问题/修正入口独立，仅真实issue，不重复泛化问题。全部问题跨页范围读取完整，不能20条重算统计
- [ ] A24单独显示权威任务/用量，不能current=true/单句任务推全批阶段。缺phase/预计用量/费用字段先明确必要契约方案与数据来源；无依据不造零。费用单价/估算假设或业务阶段归属缺证需确认，继续其余UI
- [ ] 原文件下载沿既有GET original-file；ApiClient必要Blob模式保留会话/CSRF/error，不复制绕过共用请求层
- [ ] 覆盖刷新、旧链接、换批迟到结果、预览后变更、取消/失败ONLY、1000逻辑批次分页范围；本地产品截图对照四阶段/A20/A24（设计不作整页资产）
- [ ] scoped检查/Mock E2E+真实隔离契约/必要SQL；提交报告与未验项，不调用供应商或真实确认发布

## Task 5: 独立素材匹配与词汇图包入口

**Files:** admin `content-import-page.vue`、`content-materials-panel.vue`、材料adapter/model、layout必要标题；相关`content-materials-v15.spec.ts`/`v15-authoring.spec.ts`等
**Authority:** F03；需求782、824—834、1776—1799；A18M 2662:388；A18Z/ZV/ZP/ZD 2799:3378/3485/3588/3705
**Interfaces:** 继承已选中文分类/类型；保留目标草稿/版本/素材角色/条目ID确权。独立音频图片与xlsx词汇图包不能共同冒充同一种业务流程。

- [ ] 阅读真实素材上传/匹配契约、高保真；区分入口与标题，素材匹配按真实四统计/异常表，图包保留四阶段
- [ ] 修正绑定必须明确目标/角色/稳定ID，不用标题猜配或自行上传触发OCR；语块禁图、词汇缺图需明确确认
- [ ] 测可达入口、上传后校验/预览/确认、失败重试/旧资料与分类一次选择；必要scoped检查、本地设计对照；提交

## Task 6: 正文对照、场景词条与短文独立编辑

**Files:** admin `content-work-item-page.vue`、`scene-editor-page.vue`、`short-reading-editor-page.vue`、现有scene-proofread/short-reading-fields/original组件及相关测试
**Authority:** F04；需求785—788、805—822、1634—1642；A20S 2663:158、A19E 2836:1034、A30 2664:882、A21 2663:273
**Interfaces:** 读取现有source_snapshot与candidate，保留真实revision/edit/hash和现有纠错/资产确权写接口；不能把已改候选当原文或绕过发布保护。

- [ ] 高保真+当前入口：A20S只对照/配对，A19E条目编辑与A30短文各按设计布局，不无条件嵌左原文右通用候选
- [ ] 短文仅完整英中文/元信息/版权/安全封面/可选FULL上传试听替换；无词库/讲解；场景词汇/语块字段按类型，语块无图
- [ ] 改正文/素材失效旧确认；换项/版本冲突不串数据；原文下载/返回对应修正上下文稳定
- [ ] 相关unit/组件/Mock E2E、必要API检查与本地手机/平板预览；人工试听继续暂缓，不代写确认；提交

## Task 7: 抽样、发布范围与发布结果按画板呈现

**Files:** admin `content-work-order-page.vue`、`work-order-sampling.vue`、`work-order-publication.vue`、现有scope/model及相关测试
**Authority:** F05/F06；需求875—906；A22 2663:540、A22P 2663:675、A22R 2663:802
**Interfaces:** 当前真实SamplingState、PublicationState、冻结范围token/version/hash/selected_revision_ids、failed-only/UNKNOWN恢复。全batchCounts与特定publication范围语义不可混用。

- [ ] 抽样第四统计为真实“样本发现问题”，不得冒称当前范围；没有聚合字段先对齐服务端来源，再补必要依赖
- [ ] 发布前可发布/仍需校对/处理失败/保留草稿；结果已发布/发布失败/仍需校对/处理失败，各自按冻结范围及真实状态，不拿其他集合凑卡
- [ ] 结果阶段不继续显示范围选择；失败原因忠实映射已确认错误，不全换成泛化说明；UNKNOWN保留未决/续办，不能伪造失败或重发成功
- [ ] 样本关键变更失效、人工检查、选定合格子集、部分成功/失败、重试冻结集合、刷新/迟到结果有meaningful用例；本地对照画板；提交

## Task 8: 契约来源、最终门禁与交付边界

**Files:** 必要真实契约导出/前端生成及`juya-admin/openapi/admin-api-source.json`；事实整改交付记录；各仓实际变更文件
**Authority:** 接口19/职责34/用户事实规则7；旧source metadata过期已确证，不改真实接口含义来迁就快照

- [ ] 最终冻结四HEAD，修正source_commit/counts/哈希到真实生成来源；source/OpenAPI/消费schema完整语义比较
- [ ] 对改动仓执行其规定静态检查、完整unit/必要隔离SQL/构建；未变仓不重复全量，失败按第一真实错误归因，不删检查
- [ ] 每task独立范围审查（spec+quality）通过，最终一次四仓全变更审查；不重复相同HEAD已通过检查
- [ ] 改动网页在本地规定视口与实际设计对照，并验证可交互路径；Mock/本地/真实服务/云/真机分开
- [ ] 用户已有test发布授权有效，只有正式门禁/依赖兼容完成后沿既有官方发布入口发布已改项目；不得生产发布、真实用户清理或替人试听/确认
- [ ] 最终逐项列已改、依据、实际验证、已部署与未验/待确认/明确暂缓；未处理的确认偏离不得声明清单全部完成
