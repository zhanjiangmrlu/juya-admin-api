# V1.5 管理后台前端事实审计（只读）

日期：2026-10-14。审计对象：D:/个人/juya/juya-admin，test，HEAD 73cac18fc8d38ca42d00aa821d058f11f81cb012；开始与结束 git status --short 均为空。本轮只新增本 canonical 的证据文件，未修改产品、提交、部署、发业务请求、运行测试或构建。

## 判定口径

- 权威需求 S：D:/个人/juya/doc/句芽英语V1.5开发交付包-20261011/句芽英语V1.5完整需求文档-开发交付整合确认版-20261011.md；下表 S:行号均指此文件。
- 权威设计：evoGqXQ4pI3Q3cdLpc6NOc，正式后台母版 2660:2。实际读取该 root 后确认 30 个顶层 A 页面，并取得全部 30 页高保真 get_design_context 与工具原图。27 页新增记录在 v15-admin-design-inventory.json；A17/A20/A24 见 strict-a20-a24-design-context.md。全部截图在 design-reference/，非产品截图。
- 已确认满足仅指列明的源码行为/字段与已读取设计中的对应可见功能，不等于整页像素、全部状态、真实服务或业务验收通过。
- 已确认偏离：需求或真实设计与当前可达实现有明确不同。设计没找到只列待确认，不能把“当前正式 root 未列出”写成“整个 Figma 不存在”。
- 待确认：缺少设计承接节点、运行证据、接口事实或完整异常态依据。本轮不自行决定兼容、业务或新设计。
- 组件可复用、路由可承载不同状态；不要求一画板必对应一文件/一路由。问题是用户实际看到的结构、操作和流程是否仍对应设计。
- 旧 PRODUCT.md、执行计划、工作单技术模型、测试断言均不能覆盖 V1.5 需求与设计。未把图层 name 当可见 characters。
- Root 先前由 XML 层名推断 A20/A24 顶栏是“内容列表”，已由本轮高保真截图纠正：实际分别“自动整理结果”“批量任务”。

## 已确认偏离摘要

| 编号 | 已确认事实及影响 | 双依据/代码 |
|---|---|---|
| F01 | A20 与 A24 合成“内容生产工作单”概览，并用顶部业务切换承载 A19/A22/A22P/R；A20 独立四列异常表改成篇目状态、混合问题说明和重复入口，附带任务/用量。 | S:850-859、875-886、909-914；A20 2662:649、A24 2662:520 高保真；src/pages/content/content-work-order-page.vue:515、522、541；src/features/content-production/work-order-overview.vue:92、105、150、175 |
| F02 | 统一 ZIP 当前入口不再呈现 A18U→A18UV→A18UP→A18UD 的独立可见阶段：上传后直接进工作单 import 视图，“校验与对应预览”以逐篇全文卡合并；确认后仍停留该视图，没有独立导入结果状态的设计内容。旧 workflow 文件仍在不代表主入口使用。 | S:782、1038-1041、1855-1877；A18U 2836:573 / UV 2836:689 / UP 2836:793 / UD 2836:930；content-import-page.vue:126；work-order-upload.vue:60；work-order-import.vue:79、98、201；content-work-order-page.vue:227（run 刷新同 view） |
| F03 | “添加音频或图片”和“导入词汇图包”都进入 source=materials 同一图包流程。现有页面具备目标/角色绑定，但没有 A18M 的独立素材匹配四统计与异常素材表；对应顶栏也按词汇图包阶段命名。 | S:824-834、782；A18M 2662:388 与 A18Z 2799:3378；content-import-page.vue:89、101、120；src/layouts/admin-layout/admin-layout.vue:76；content-materials-panel.vue:536、549、614 |
| F04 | 已有场景、短文编辑入口重定向到通用 work-item 页，后者无条件原文左栏/候选右栏，A30 短文和 A19E 条目编辑被套入同一对照外壳；不能把保留的旧独立编辑模板当成当前入口实际展示。字段功能部分存在，但页面组织不对应 A30/A19E。 | S:785-788、1634-1642；A30 2664:882、A19E 2836:1034、A20S 2663:158；scene-editor-page.vue:111-142；short-reading-editor-page.vue:122-133；content-work-item-page.vue:419-478 |
| F05 | A22 当前四项显示“本次正常、所需样本、已检查样本、当前范围”，第四项未按设计显示“样本发现问题”；正常抽样嵌入工作单顶部切换。实际人工检查与候选版本保护应保留。 | S:875-886；A22 2663:540；work-order-sampling.vue:169-180；content-work-order-page.vue:522 |
| F06 | 发布前/后共用的可见四项始终“本次范围、发布成功、发布失败/待核实、继续保留草稿”，未对应 A22P 的可发布/仍需校对/处理失败/保留草稿及 A22R 的已发布/发布失败/仍需校对/处理失败。结果表全量显示结果，具体失败原因统一转成泛化说明；外层结果出现后仍显示“选择当前合格子集”。 | S:899-906；A22P 2663:675、A22R 2663:802；work-order-publication.vue:42-83；content-work-order-page.vue:610（publication 分区，含“选择当前合格子集”） |
| F07 | A17 增加“生产进度”列与“尚未加入工作单”业务文案，并增加抽样/任务/开放配置三个相关链接；A17 高保真是五列内容表、底部单篇新建/批量导入。能力在需求中存在，不能据此认定新增 A17 入口布局有设计依据。 | S:782-789、877、909-915；A17 2662:140；content-list-page.vue:338-347、380-383 |
| F08 | A01 反馈超时/将超时按钮映射“处理”，用户补充映射“查看”，与需求和 A01 设计“立即处理/重新查看”不同。 | S:762-767；A01 2662:4；src/features/work-items/work-item-model.ts:35-65；dashboard-page.vue:89 |
| F09 | 限时权益开通主面板直接显示 detail.status、currentVersion.id；确认框 before 为活动原始状态，after 固定“开通后的权益状态由服务端返回”，对象/范围主信息使用内部 ID，且通用确认框一律提示高风险。限时操作页也在主摘要显示权益/user/campaign/version ID。需求已明确中文业务信息、真实状态、不以长编号为主、不对普通操作套统一高风险。当前 root 未定位 A07/A09 高保真，设计维度仍待补。 | S:731-743、1318；limited-grant-page.vue:188-189、282-289；limited-action-page.vue:169-180；src/components/confirm-dialog/confirm-dialog.vue:75-89。这是已确认需求偏离，不宣称已完成其设计对照 |

## 全量页面/功能矩阵

下表代码路径除特别注明均以 D:/个人/juya/juya-admin/ 为根。测试列表示已存在、已阅读相关用例标题/关键断言的证据入口，本轮未执行；不能当作当前 HEAD 新测试结果。

| 范围 | 精确需求依据 | 设计证据与实际可见功能 | 当前代码证据 | 分类与既有验证入口 |
|---|---|---|---|---|
| 导航/单管理员 | S:139-146、186-197、1310 | 30 页截图的核心管理六项与基础能力三项 | src/app/admin-navigation.ts:43-57；src/layouts/admin-layout/admin-layout.vue:122 | 已确认满足：核心/基础导航分组的源码定义对应截图；登录权限运行待确认。tests/e2e/admin-actor.spec.ts、layout.spec.ts |
| 工作台 | S:715-719 | A01 2662:4，六卡、紧急/普通分区、消息/导入页脚 | dashboard-page.vue:13-44、69-125；work-item-model.ts:79 | 结构/真实数据绑定满足；F08 动作文案偏离；用例 foundation-v15.spec.ts:41。日统计/SLA真实服务另验 |
| 用户列表 | S:721-725、637-645、1311/1316 | 当前 2660:2 未列 A02，待补承接设计 | user-list-page.vue:135-229、262-302；features/users/use-user-list.ts:56、82 | 需求字段/筛选/联系变更徽标源码存在；双依据完整合规待确认。users-figma.spec.ts:7、users.spec.ts |
| 用户详情 | S:727、637-645、1333-1339 | 当前 root 未列 A03 | user-detail-page.vue:129-199、213-283；features/users/user-related-records.vue | 身份/学习/联系核对/关联记录源码存在；设计缺证、注销/审计真实读取待确认。user-pages.spec.ts、audit-pagination.spec.ts |
| 联系更正 | S:474-485、637-645 | 当前 root 未列 A04 | contact-correction-page.vue:105-185，批准仅重置机会 | 已确认具备有限权限操作展示，不直接改微信号；设计待确认。contact-correction-page.spec.ts |
| 权益中心 | S:729-743、514-582 | 当前 root 未列 A05 | entitlement-center-page.vue:104-150、173-248 | 类型/版本/日期/状态筛选及期限显示源码存在；完整微信号等投影需结合组件/运行逐列验收，设计待补。entitlements-campaigns.spec.ts:97 |
| 正式开通 | S:733-742 | A06 2664:1354，当前页预览弹窗、昵称/句芽号/内容包、真实前后状态、期限、备注选填 | formal-grant-page.vue:153-257；entitlement-preview-dialog.vue:41-58、74-153 | 已确认功能骨架符合：服务端预览、动态实际动作、昵称读取、中文状态、备注选填。字体/视觉/所有状态待运行。action-page-identity.spec.ts |
| 正式续期 | S:733-742、520-533 | A08 2664:1487，续期自然月、保持生效时间、原/新到期 | formal-action-page.vue；formal-entitlement-model.ts；entitlement-preview-dialog.vue:90-153 | 已确认共享预览组件按 actualOperation 动态命名；自然月计算与真实前后期限由服务端证据确认，不在前端推定。batch-two-pages.spec.ts |
| 正式暂停/恢复/撤销/延期 | S:733-743 | A08P 2664:1616，原因、影响范围、数据保留；恢复/撤销/延期的专门状态图本轮未定位 | formal-action-page.vue；entitlement-preview-dialog.vue:104-125 | 暂停可见结构有依据；其余操作需求明确，具体设计状态待确认。不得将暂停画板自动当作所有动作的完整设计 |
| 限时开通 | S:535-582、615-623、731-743、1314/1318 | A07 本 root 未定位 | limited-grant-page.vue:51-57、188-192、282-289 | F09 已确认需求偏离；容量禁用条件源码存在；设计待补。entitlements-campaigns.spec.ts:43 |
| 限时延期/恢复/关闭 | S:561-572、731-743、1318 | A09 本 root 未定位 | limited-action-page.vue:127-149、169-180、268-278 | 固定预览/原因/补救次数源码存在，但主信息长编号 F09；设计与真实一次性限制待确认。entitlements-campaigns.spec.ts:12 |
| 活动列表与生命周期 | S:587-623、745-756 | A10 本 root 未定位；A11/A12见后两行 | campaign-list-page.vue；features/campaigns/campaign-model.ts | 保存/启停/复制/归档需以真实契约状态验收，列表设计待补。entitlements-campaigns.spec.ts:236 |
| 活动编辑 | S:747-756 | A11 2664:1121：标题搜已发布内容、3/5数量、拖动、预览/移除、默认不限、高级启动期限 | campaign-edit-page.vue:141-222、257-275；campaign-scene-selector.vue:145-202 | 已确认源码具备主要要求与设计功能；增加的状态详情/操作确认异常态需单独设计依据。use-campaign-editor.spec.ts、operations-v15.spec.ts:121 |
| 活动规则/容量 | S:597-613、753-755 | A12 2664:1248：核心锁定、独立容量、累计只读、复制编辑 | campaign-version-page.vue:130-188 | 已确认功能骨架满足；确认容量弹窗未在本画板出现，按真实契约必要性单独待核，不自动删除。entitlements-campaigns.spec.ts:186、206 |
| 消息中心 | S:758-770、1312-1313 | A13 本 root 未定位；A01只证明工作台待办区 | work-item-page.vue:48-105；work-item-model.ts:79-85 | 紧急/普通分组有实现，排序保留服务端顺序，不宣称前端已验证优先级；动作映射 F08。use-work-items.spec.ts |
| 反馈列表 | S:774、657-711、1322-1331 | A14 本 root 未定位 | feedback-list-page.vue；features/feedback/feedback-model.ts | 列表功能与真实状态/截图权限待运行，设计待补。feedback-loop.spec.ts:3 |
| 反馈详情内回复 | S:776-778、1327/1331 | A15 2664:1742：时间线、来源、截图、内部备注、模板、200字、回复解决/关闭、补充 | feedback-detail-page.vue:154-215；feedback-response-panel.vue:39-115 | 已确认主功能在详情；失败不清输入、需要补充独立动作源码存在。feedback-loop.spec.ts:46、69；真实通知与SLA另验 |
| 旧独立回复链接 | S:776、1331 | A15明确同页；无需保留独立回复页 | src/router/index.ts:6-14 | 已确认兼容链接重定向详情；feedback-respond-page.vue虽保留但不能据此误报它仍是当前可达独立流程 |
| 内容列表/新建/历史 | S:782-789、899-916 | A17 2662:140：内容标题/类型/分类/状态/操作、单篇新建/批量导入 | content-list-page.vue:197-274、319-389 | 标题/类型/生命周期字段支持；F07 增列/链接；“预览·发布”按钮调用edit且排除short_reading（355-361），其入口体验需与A17短文发布例核对，不能声称所有后续动作已符合。content-list-page.spec.ts、content-catalogue.spec.ts |
| 导入分类/入口 | S:782-790、805-822 | A18 2662:271：分类中文一次选、类型、ZIP优先、DOCX/素材/图包 | content-import-page.vue:58-135 | 分类/类型传props，四种入口能力有代码；F03两种入口共用图包路径；页脚“返回内容生产/开始导入”与设计“保存草稿/开始自动整理”不同，具体业务转换需Root确认。foundation-v15.spec.ts:41 |
| 统一ZIP上传 | S:1776-1799、1038 | A18U 2836:573：四阶段条、包结构/稳定ID说明、开始校验 | work-order-upload.vue:26-106 | F02；上传幂等键和大小策略存在，不重新排查用户已取消的上传错误 |
| ZIP校验 | S:1855-1877、1039 | A18UV 2836:689：行/ID、问题、处理动作、明确排除 | work-order-import.vue:79-119、182-201 | F02：当前合并预览卡；问题/范围/缺省确认字段仍有代码，不等于独立设计呈现。v15-content.spec.ts:59 |
| ZIP对应预览 | S:1040-1041、1855-1877 | A18UP 2836:793：正文译文、封面、音频、操作与差异 | work-order-import.vue:120-198 | 原文/候选/素材真实字段支持；F02合并结构。v15-content.spec.ts:111 |
| ZIP导入结果 | S:1041、1855-1877 | A18UD 2836:930：新增/候选更新/跳过/逐项结果/进入校对 | content-work-order-page.vue:227；work-order-import.vue:98-213 | F02：同view刷新，无独立结果态可见内容；不能用旧package-import-result.vue存在证明已接通 |
| 多篇DOCX | S:805-822、1343-1344 | A18 2662:271；A20 2662:649 | content-import-page.vue:84；work-order-upload.vue:52-65 | 文件、分类、类型传递满足；分篇/配对是API事实，本前端审计不判实际解析成功；呈现同受F01/F02影响 |
| 独立素材匹配 | S:824-834 | A18M 2662:388：4统计、文件/标题/时长问题/处理 | content-import-page.vue:89；content-materials-panel.vue:630-751 | F03；实际固定草稿/角色/条目选择有必要契约支撑，但不能当A18M视觉对齐。v15-authoring.spec.ts:112 |
| 词汇图包上传 | S:1776-1799、1818-1853 | A18Z 2799:3378：独立ZIP和稳定条目ID | content-materials-panel.vue:536-609 | 四阶段与上传字段源码存在；额外目标分类/模板选择在已传入上下文下是否重复，需按实际路径验收（不由模板出现就推定强制重选） |
| 词汇图包校验 | S:1855-1877 | A18ZV 2799:3485：ID/问题/处理、缺图确认/异常排除 | content-materials-panel.vue:614-707 | 已确认对应表格/选择/确认功能存在；恢复输入和固定草稿选择是附加可见流程，需补设计依据。content-materials-v15.spec.ts:230 |
| 词汇图包预览/结果 | S:1818-1853、1855-1877 | A18ZP 2799:3588、A18ZD 2799:3705 | content-materials-panel.vue:753-897 | 已确认有四阶段、真实结果判定和预览表；结果页新增“继续校对”入口不同于设计两个导入按钮，设计依据待补。content-materials-v15.spec.ts:65 |
| 自动整理/异常结果 | S:850-859、875-886 | A20 2662:649 | work-order-overview.vue:26-211；content-work-order-page.vue:541-568 | F01；计数使用order.counts不重算当前页，属于已确认正确的数据选择；筛选/位置/原文件footer待整改 |
| 原文对照 | S:805-822、857、1042 | A20S 2663:158：只读原文、可校候选、具体配对 | work-order-original.vue；work-order-model.ts:28-189；content-work-item-page.vue:419-529 | 已确认原文来自source_snapshot而非已改候选；A20S单处配对与当前全篇/元信息/词条大表单不等同，结构F04；work-order-original.spec.ts、content-work-order-review.spec.ts |
| 中央词库候选 | S:836-848、885、1044 | A19 2663:414：去重候选/涉及篇目/来源与义项对比/确认或不采用 | work-order-candidates.vue:28-145；content-work-order-page.vue:522 | 已确认词/类型/义项汇总、具体来源和明确决定存在；位于工作单tab属F01；跨篇一次确认是否完成须结合API另验，不以summary去重证明写入去重。work-order-candidates.spec.ts |
| 本篇词汇与语块 | S:785、836-848、1042/1047、1818-1853 | A19E 2836:1034：词汇有选图、语块无图、释义/解释/来源、发音资料 | scene-proofread-panel.vue:34-62；content-work-item-page.vue:469-478 | 字段/类型分区组件存在；页面外壳F04；逐条来源/发音策略需具体组件及实际交互验收。v15-authoring.spec.ts |
| 场景整段/单句音频 | S:861-873、1043、1347-1348 | A21 2663:273：整段、边界、真实单句文件、局部重生成 | scene-audio-panel.vue:48-126；features/audio/sentence-slices-panel.vue；scene-editor-page.vue:111、571 | 主要控件源码支持；不据此证明真实文件边界/声音/不叠播；本轮不试听、不paidcall。v15-authoring.spec.ts:320、v15-admin-fixes.spec.ts:225 |
| 正常抽样 | S:875-886、1350-1351 | A22 2663:540 | work-order-sampling.vue:44-147、169起；use-work-order.ts:53-81 | 实际资源身份/播放事件+人工勾选、候选哈希和重置保护存在；F05；不将play事件等同用户完整试听。work-order-sampling.spec.ts |
| 发布范围 | S:889-906、1045 | A22P 2663:675 | work-order-publication.vue:42-109；work-order-publication-scope.ts；page run('publication-preview') | 冻结合格子集和显式版权核对存在；F06；实际发布暂缓。content-work-order-publication-scope.spec.ts |
| 发布结果/重试 | S:899-906、1046 | A22R 2663:802 | work-order-publication.vue:25-100；work-order-model.ts:231；content-work-order-page.vue:360-389 | 成功保留/固定失败范围保护存在；UNKNOWN须保留恢复语义不能为贴图删除；F06的展示偏离；实际业务尚未验收 |
| 批次任务/用量/取消 | S:909-914、1046、1740-1746 | A24 2662:520 | work-order-overview.vue:150-210；page:406-429、663；typed WorkOrderUsage | F01及下述契约缺口；不能从本页任务或HTTP成功推出整批阶段完成 |
| 短文纯阅读编辑 | S:787、855-856、1634-1642、1683-1698 | A30 2664:882 | short-reading-editor-fields.vue:83-181；short-reading-editor-page.vue:122-133；content-work-item-page.vue:427-440 | 完整英文/全文译文/元信息/版权/封面/可选音频且无词库字段满足；当前编辑布局F04；音频正式确认入口不应由本审计代替人工。v15-authoring.spec.ts:4、86、210 |
| 开放/预览配置 | S:941-954、1660-1667 | A23 2664:1861：固定3场景、系列3-6预览、独立资料入口、短文逐篇不限数 | discovery-config-page.vue:146-220；discovery-model.ts；short-reading-trials-panel.vue | 已确认主要输入与校验/中文选项具备；预览图/简介与顺序是否完整可编辑需跟具体内容字段运行核验，不推定已完成。operations-v15.spec.ts:54 |
| 四类学习模块 | S:922-939、1052-1057 | A26 2664:999：四可反复开关、排序标记、停用说明、取消/保存 | learning-modules-page.vue:67-139；use-learning-modules.ts:42-78 | 已确认四开关不按类型永久禁用、服务端ready判断、版本独立保存；名称顺序折叠编辑与设计的具体交互差异待确认；新增“系统、审核与时限配置”链接在本画板未见依据。operations-v15.spec.ts:3 |
| 系统审核/SLA/总开关 | S:107-114、790、681-687、953、1081-1092 | A26本轮是模块配置；原系统审核配置页面设计承接未定位 | settings-page.vue:20-28、82-145；system-config-model.ts | 有需求支撑的设置代码存在；独立/settings页面的新版设计依据待补；未检验实际凭证/云配置，不显示或读取密钥 |
| 汇总统计/导出 | S:1129-1142、1740-1746 | A25 本 root 未定位 | analytics-page.vue:146-238；analytics-model.ts:3-56、220；cloud-usage-panel.vue:57-89 | 日周月/匿名白名单导出和多项统计支持；内容正常/异常/失败/抽样通过、缓存命中/可计算费用不应据现页面算齐，当前用量面板明确未知留空，缺项待API/设计对照。analytics-summary.spec.ts:3、65 |
| 回收/回退/批量标签等 | S:907、915-916 | 当前正式 root 未定位专用画板；A17含历史/恢复例 | content-list-page.vue:242-274、362-365仅直接历史/下线；content-adapter.ts:50 | 未从该目录入口找到完整回收站/30日恢复/回退/标签/包关联/导出界面证据，列待确认，不凭一次搜索断言全仓不存在，也不造新管理页 |
| OCR取消 | S:790、1345 | 新A19是中央词库，非OCR | router/index.ts:43→retired-content-page.vue；settings-page.vue无OCR调用UI | 已确认当前旧OCR路由落取消说明，入口/配置不恢复；运行/所有遗留接口由API审计确认。仅遗留.ocr-entry样式不等于功能仍启用 |
| 隐私与审计 | S:1096-1127、1142、1315 | 基础页面专用设计缺证；不以安全技术创造新业务入口 | user-adapter.ts:99-105敏感POST；user-detail-page.vue:98-109复制先审计；auth-store.ts:106；analytics-model.ts:220 | 已确认局部防泄漏做法；日志内容、角色鉴权、截图生命周期及注销匿名化属服务端/运行验收，不能由前端静态宣布完成 |

## 现有契约可支撑与缺口（仅前端必要边界）

1. WorkOrder.counts 提供整批 total/normal/review_required/failed/publishable 等；当前 useWorkOrder 的 order 与分页 items 分开读取是正确选择。禁止用20条当前页重算整批统计。
2. WorkIssue 已有 id/code/message/location/correction_target，可渲染独立“问题位置/问题/修正入口”；通用 message 不能擅自猜成具体业务原因，位置缺失必须忠实标缺失。需要人类可读代码映射时只映射已核实语义。
3. /items page_size 最大100，status 仅单个production_status；A20“全部问题”组合范围不能简单用某一页本地过滤冒充全批。全量有界分页或额外契约的决定留给Root计划，不改API。
4. 当前 WorkTask 为逐篇/逐句阶段事实，无现成全批业务阶段汇总字段。既有调查见 strict-a20-a24-design-context.md/父任务消息：导入阶段完成后 current 可false；ASR_BOUNDARY/ASR_POLL/SENTENCE_SLICE不能凭显示标签一对一当作A24四个业务阶段；没有任务不代表完成。
5. WorkOrderUsage attempts 有 input_tokens/output_tokens/measured_audio_ms/actual_units（可null）、retry_count、unknown_count；cost为null。reserved_units是逐次预留，不能叫整批预计；历史关联记录不等于本次新增收费。设计预计文本/音频与费用仍缺权威字段，不能填示例或0。
6. 正式原文件 GET 已在既有父任务API调查确认；当前FE adapter没有方法，ApiClient readSuccessBody读取text/JSON，不支持二进制原字节下载。以后若实现需复用鉴权/错误处理与Blob读取，不用候选文本冒充原ZIP/DOCX。
7. stage-retry 提交具体stage_id/fence/input_hash/config_version/edit_version，并有收费重试明确确认。A24“重试失败阶段”如何涵盖多篇不得自行变成重跑全部；SUCCESS/UNKNOWN恢复等保护必须保留。
8. 无id /content/jobs 与 /content/proofreading 当前进入自创工作单列表（page:470-514），设计A24是单批详情。不能擅自选最新批次、发明选择器或删除旧deep link。需要业务/导航依据再确定兼容路径。

以上内容使用已读typedcontract与初始范围必要API事实，不声称本轮重复验证API或线上行为。

## 设计证据覆盖与不足

本轮正式root 30页均已读取高保真及截图：
A01、A17、A18、A18M、A24、A20、A20S、A21、A19、A22、A22P、A22R、A12、A06、A08、A30、A26、A11、A08P、A15、A23、A18Z、A18ZV、A18ZP、A18ZD、A18U、A18UV、A18UP、A18UD、A19E。

A02/A03/A04/A05/A07/A09/A10/A13/A14/A25在该root未列出。可能承接其它正式旧画板，但本轮未获取其权威承接关系，不能凭仓库A编号或旧V1.3计划替代。恢复/撤销/延期、各页加载/无数据/失败/冲突等状态亦不能由单张主状态截图认定全覆盖。

工具原图通常1024×640，设计逻辑1440×900；用于确认层级、可见字符、主要功能，不是产品在1440×900/1280×800的像素对照结果。带动态图片的A18UP/A18ZP/A19E资源只作为设计参考；本轮未下载或植入产品。

## 既有验证证据与当前限制

补核历史节点线索：Root提供的2660:63已实时get_metadata核实，是包含A12/A06/A08/A30/A26/A11/A08P/A15/A23的“V1.5｜后台简化”容器，不是A10活动列表画板。其子页正是本轮已读的高保真页面；没有据此补足A10设计缺证。S:1934/1936的已有规则保留与最新规则优先仍适用，缺画板不构成删除旧功能的依据。

- 当前repo下测试文件存在；上表列出匹配功能用例，不表示本轮执行或已经覆盖设计要求。检查“无横向溢出/元素可见”不能自动证明画板结构一致。
- canonical/task-5-fe-unit-output.txt末尾记载118文件/447测试通过；task-5-fe-e2e-final-output.txt末尾记载16通过。日志自身没有当前73cac18完整HEAD绑定；仅能作为历史测试证据，不能宣传当前HEAD重新通过。
- canonical/test-release-acceptance.md为历史混合记录，涉及多组旧API/FE commit与局部真实UI；本轮没有复用其早期“动态列approved”等描述作为设计授权，也没有把旧记录当本轮上线/业务结果。
- 当前HEAD只读已复核；本轮未跑单测、E2E、typecheck、lint、build，未连接实际云服务、未启动/改变浏览器用户会话。
- 完整试听、正式确认、实际发布以及小程序设备/播放/学习结果仍暂缓。没有填audio_listened；没有云调用、上传重排错或账号操作。
- 已确认满足的源码功能后续仍需限定真实浏览器状态、1440×900与1280×800设计对照、契约响应和必要业务流验证；Root负责整合任务后决定修复范围，本报告不含自行新增的页面方案。

## 建议交给Root的下一步分组（不是实施授权或业务裁定）

1. 先固定已确认偏离F01—F09与路由的实际用户路径；逐项修复应具有需求和设计双依据。
2. 缺设计承接的基础页、无id批次入口、跨页问题范围、全批阶段/预计用量字段单独提问或补证；不将它们混入“随手优化”。
3. 可直接保留的保护：权威整批计数、原文快照、候选版本、固定发布子集、失败-only与UNKNOWN续办、路线迟到响应隔离、费用明确确认、敏感信息只读/审计。
4. 不以一次全suite绿色替代这份功能/设计核对，也不为贴图删除保护性检查或更改测试预期掩盖不一致。

## 补证：第29章历史母版真实引用（仅元数据定位）

Root提供S:1617的正式历史母版引用2534:818，本轮实际读取该节点，返回canvas“后台页面”，含30个顶层画板。以下10个此前未在2660:2当前增补root中定位到的基础页，已在历史母版中定位到准确节点；此前表格中的“缺节点/设计承接待确认”应更新为“历史节点已定位，尚未读取其高保真context与截图、尚未完成可见功能与当前源码对照”。不能仅依据元数据名称判定满足，也不能恢复已由最新需求取消的旧功能。S:1934/1936最新规则优先。

| 历史图层名称（不是已核可见文字） | 真实node |
|---|---|
| A02 用户列表 | 2538:17 |
| A03 用户详情 | 2538:103 |
| A04 联系资料更正处理 | 2538:164 |
| A05 统一权益中心 | 2538:225 |
| A07 开通限时学习权益 | 2538:374 |
| A09 限时权益操作 | 2538:498 |
| A10 限时活动列表 | 2538:559 |
| A13 消息中心 | 2538:761 |
| A14 问题反馈列表 | 2538:823 |
| A25 汇总统计 | 2538:1809 |

完整顶层节点索引保存在`v15-admin-historical-node-inventory.json`。本轮仍只有2660:2下30个节点完成高保真context与截图核对；历史母版这10页不计入该30页，不标设计通过。历史A16/OCR等旧画板虽存在，不代表当前V1.5授权。
