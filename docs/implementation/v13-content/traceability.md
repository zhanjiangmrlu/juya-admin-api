# V1.3 需求实现与延期清单（第 1–29 章）

日期：2026-10-01。需求唯一基线为 `doc/句芽英语V1.3完整设计稿-内容上传与音频改版确认版-20260930/句芽英语V1.3完整需求文档-开发需求交付版-内容上传与音频改版确认版-20260930.md`。旧基线为 `doc/句芽英语V1完整设计稿-20260927/句芽英语V1完整需求文档-开发需求交付版-逻辑修改确认版.md`。原文逐行差异见 `01-需求逐条差异.md`，技术决策见 `02-技术方案.md`；本文件另以相同正文保存于 `juya-admin-api/docs/implementation/v13-content/traceability.md`。

本轮实施范围为 `juya-admin`、`juya-admin-api`、`juya-miniapp-api` 三个项目；`juya-miniapp` 前端按用户授权延期。表中“实现”表示可在代码和测试中定位，不能自动等同生产、平台审核或真机验收。既有规则沿用已有服务，不宣称本轮重做全部界面。完整测试数量、最后执行时间、失败/跳过项以 `juya-admin-api/docs/implementation/v13-content/evidence.md` 最终记录为准，本表不预填总数。

代码定位约定：A=`juya-admin-api/src/juya_admin_api`；U=`juya-miniapp-api/src/juya_miniapp_api`；W=`juya-admin/src`。测试定位给出项目和仓库相对路径，方便在三个独立仓库检索。测试名称仅用于追踪断言；执行结果须核对最终证据账本。

## 一、逐章对照矩阵

| 最新章节 | 旧规则与本轮变化 | 实现位置（三项目） | 自动化/接口验收定位 | 尚需完成或明确延期 |
|---|---|---|---|---|
| 1 产品定位与目标 | 学习闭环沿用；内容闭环由图片→OCR，改为完整原图→人工或显式OCR→统一草稿→逐句标时→同版本发布 | A `modules/content/{schemas,production_store,production_rules}.py`；W `pages/content/scene-editor-page.vue`；U `integrations/admin_api/{content,schemas}.py` | admin-api `tests/integration/test_v13_content_transaction.py`；admin `src/features/content-editor/scene-form.spec.ts`；mini-api `tests/contract/test_v13_scene_contract.py` | 小程序新原图页、播放器、跟读和全部用户成功标准的真机整体验收延期 |
| 2 产品边界与合规原则 | 沿用无支付、无交易、无公开社交、录音不上传；新增明确百度辅助、上传音频及四模块内容边界 | A `integrations/ocr/baidu.py`、`modules/media/{quota,router}.py`；W OCR开关/额度和人工入口；U 保留身份/反馈能力，不增加支付 | admin-api `tests/unit/media/test_v13_media.py`、`test_content_security_gate.py`；mini-api `tests/unit/feedback/test_feedback_validation.py` | AppID主体、类目、录音及资料次级入口审核按第29章外部核验；不以技术实现代替合规验收 |
| 3 用户角色与权限 | 沿用普通/正式/限时用户与单管理员，联系方式与权益独立 | A `modules/access_policy/service.py`、formal/limited服务；U `modules/learning/{access_service,service}.py` | admin-api `tests/unit/access_policy/test_authorize.py`、`tests/integration/test_limited_entitlement_concurrency.py`；mini-api `tests/unit/learning/test_fail_closed.py` | 用户端角色展示本轮不改；真实设备到期后清除播放/正文缓存待接入 |
| 4 信息架构 | 沿用四个用户导航、唯一学习档案、M10/M11弹层、后台六模块；内容生产为基础入口 | W 路由及 `pages/content/*`、权益/反馈/用户页面；A/U现有路由 | admin `tests/e2e/content-editing.spec.ts`、`entitlements-campaigns.spec.ts`、`feedback-loop.spec.ts`；mini-api `tests/e2e/test_miniapp_core_flow.py` | 小程序旧路由兼容、弹层和唯一档案页面不能以本次后端交付标为完成 |
| 5 登录、首页与学习记录 | 登录/北京时间/有效学习日沿用；今日任务由最多10张改为不限总量 | U `modules/auth/repository.py`、`modules/checkins/{service,router}.py`、`modules/learning/service.py（select_today_task）`、favorites分页 | mini-api `tests/integration/test_concurrent_wechat_login.py`、`test_learning_idempotency.py`、`tests/unit/learning/test_today_task.py`、`test_beijing_learning_day.py`、`tests/integration/test_v13_events_and_favorites.py` | 首页无网络兜底与弹窗、火苗视觉、三个翻卡入口消费分页属于前端延期 |
| 6 学习列表、预览与权限 | 安全封面与完整原图明确分离；预览禁止原图、音频、时间点、词卡/片段，继续只显示元数据 | A `modules/access_policy/service.py`、`modules/content/production_store.py`；U `PreviewScene`双语DTO与full/preview联合校验；W预览配置 | admin-api `tests/integration/test_v13_content_transaction.py`、`tests/unit/content/test_v13_content.py`；mini-api `tests/contract/test_v13_scene_contract.py`（双语标题、旧title兼容、预览不写历史） | M06留在当前页弱提示、锁定标识与资料次级入口展示延期；测试OSS封面签名不等于生产CORS完成 |
| 7 场景详情、播放与跟读 | 原图缩略图替换主题图；一份整段音频+所有句子起止毫秒；仅已收录词可查，词/块独立发音可空 | A `schemas.py`、`text_spans.py`、`production_rules.py`；W `features/content-editor/{dialogue-fields,lexicon-fields,scene-preview}.vue`、`features/audio/segment-player.ts`；U严格正文DTO/词卡和资源代理 | admin-api `tests/unit/content/test_v13_content.py`；admin `src/features/audio/segment-player.spec.ts`、`tests/e2e/content-editing.spec.ts`；mini-api `tests/contract/test_v13_scene_contract.py` | M08/M09原图和播放器、M14完整句列表/录音权限/本地清理、M10/M11底部弹层、中文开关及滚动恢复延期；M14/15/16视觉独立确认与微信单句精度未验收 |
| 8 收藏、银行、成果与复习 | 去重/全部历史来源沿用；首页、收藏、成果翻卡统一不限张数；无发音条目不能显示无效控件 | U `modules/favorites/{domain,repository,service,router}.py`；新revision/version/source快照，不同修订保留；空context用权威英文 | mini-api `tests/unit/favorites/test_pinned_sources.py`、`test_normalization.py`、`tests/integration/test_favorite_merge.py`、`test_v13_events_and_favorites.py`、`tests/contract/test_v13_scene_contract.py` | 双标签独立滚动/筛选、成果整卡跳转、翻卡播放视觉本轮不改；SQL已验证125条分页，不能等同三个前端入口已接入 |
| 9 我的学习档案与联系方式 | 沿用本人完整微信号、用途同意、核对前一次真实修改、撤回、更正和三开放完成后一次提示 | U `modules/contacts/{service,repository,router}.py`、users/auth；A `modules/contacts`及user_projection；W用户详情 | mini-api `tests/unit/contacts/test_contact_rules.py`、`test_prompt_exposure.py`、`tests/integration/test_contact_concurrency.py`、`tests/contract/test_admin_contact_contract.py`；admin-api `tests/unit/contacts/test_admin_contact_service.py` | 新增认证曝光端点 `/api/v1/me/contact/prompt-exposures` 与幂等键；真实页面曝光触发、只弹一次提示及关闭后重入交互延期 |
| 10 数据清空与账号注销 | 沿用清空不删账号/权益、7天撤回、届满删除/匿名化、新身份不恢复权益；补匿名事件用户关联移除 | U `modules/accounts/{service,repository}.py`、`infrastructure/tasks/outbox.py`；A `modules/user_projection/deletion_service.py`、maintenance；跨域回调 | mini-api `tests/unit/accounts/test_deletion_state.py`、`test_outbox.py`、`tests/integration/test_account_deletion_concurrency.py`、`test_v13_events_and_favorites.py`；admin-api `tests/unit/user_projection/test_deletion_cleanup.py` | 清空/注销挽留界面本轮不改；生产保留政策、真实附件定时清理和提审材料需外部复核 |
| 11 统一权益模型 | 正式自然月算法沿用；期限wire/SQL/check/test统一小写 month_1/2/3/6/12、permanent，拒绝任意天数/日期 | A `modules/formal_entitlements/{domain,service,repository}.py`、`modules/limited_entitlements`；W `features/entitlements/formal-entitlement-model.ts`及grant/action页；U过期拒绝、保留摘要 | admin-api `tests/unit/test_v13_entitlement_terms.py`、`tests/unit/formal_entitlements/test_natural_month.py`、`test_state_machine.py`、`tests/integration/test_formal_entitlement_concurrency.py`；admin `formal-entitlement-model.spec.ts` | M36紧凑到期/成果视觉延期；精确时间边界后端可测，微信客户端缓存失效仍待接入 |
| 12 限时活动配置 | 沿用3/5天、活动版本首开锁定、唯一用户累计容量、撤销不释放、逐用户授予 | A `modules/limited_entitlements/{domain,service,repository}.py`；W活动/限时权益页面 | admin-api `tests/unit/limited_entitlements/{test_state_machine,test_campaign_service}.py`、`tests/integration/test_limited_entitlement_concurrency.py`、`tests/e2e/test_limited_command_idempotency.py`；admin `tests/e2e/entitlements-campaigns.spec.ts` | 不添加批量用户权益；真实运营参数需管理员核对 |
| 13 联系状态与后台联系方式 | 五种联系状态独立于权益；完整微信号仅本人/授权管理员可见，真实变更徽标及核对审计沿用 | A `modules/contacts`、user_projection；W `pages/users`、`features/users`；U联系人仓储 | admin-api `tests/integration/test_admin_contact_schema.py`；admin `src/pages/users/user-pages.spec.ts`、`tests/e2e/users.spec.ts`；mini-api `test_contact_concurrency.py` | 云日志/导出及运营权限最终检查不能由局部UI测试代替 |
| 14 问题反馈闭环 | 沿用四分类、单截图、48h可配置、等待补充暂停、两轮、七天一次重开、站内消息、关闭30天删图 | A `modules/feedback/{domain,service,repository}.py`、analytics lifecycle、maintenance；U `modules/feedback`、messages；W feedback页面 | admin-api `tests/unit/feedback/test_state_machine.py`、`tests/integration/test_feedback_concurrency.py`、`test_feedback_admin_queries.py`、`tests/e2e/test_admin_feedback_flow.py`；mini-api `test_feedback_orchestration.py`、`test_message_idempotency.py` | 实际云文字/截图审核未验收；M42按钮视觉延期。新增统计hook已补reopen_count查询及首次响应/并发唯一事件回归 |
| 15 后台页面需求 | 六模块沿用；内容基础入口增同稿人工/OCR、逐句标时、额度/候选/发布状态 | W `pages/{content,users,entitlements,feedback,analytics}`；A dashboard及相应业务服务 | admin `tests/e2e/{users,entitlements,feedback-loop,content-editing,settings}.spec.ts`；admin-api `tests/e2e/test_admin_sql_flow.py`、真实HTTP验收测试 | 两个后台视口的检查只覆盖浏览器，不代表小程序视觉或外部供应商验收 |
| 16 内容生产、人工录入、OCR与音频 | 本轮核心重构：两条入口同稿、OCR候选选择采纳、上传音频逐句标时、词库复用、整体版本发布、批量真实执行 | A `modules/content/{production_store,production_rules,batch_executor}.py`、`modules/media/{quota,inspection,tasks,repository}.py`、百度provider；W五个内容页面/统一表单；U统一内容契约 | admin-api `tests/integration/test_v13_{content_transaction,media_persistence,batch_operations,celery_batch}.py`、`tests/unit/media/{test_v13_media,test_batch_executor}.py`；admin form/revision/player测试及content-editing浏览器 | 百度真实识别/免费额度/关闭付费核实尚未执行；内容安全仅local合成fixture；真人对话音频质量、逐句试听准确度及微信真机尚未验收 |
| 17 开放场景与预览配置 | 原规则沿用：3开放、每系列3–6非开放预览、模块启用过滤、独立审核开关 | A content规则、access_policy、system_config；U catalog服务；W discovery-config页面 | admin-api `tests/unit/content/test_content_rules.py`、`tests/unit/access_policy/test_authorize.py`；mini-api `tests/unit/learning/test_catalog_cache.py`；admin `tests/e2e/content.spec.ts` | 用户学习列表重排与未启用页面直路由拦截属于小程序延期；未投产新内容 |
| 18 数据对象 | 新增原图/封面分离、稳定句子和片段、词条版本、OCR记录、整段音频+时间；收藏pin和不可识别事件 | A `migrations/versions/0015_v13_content_production.py`及content/media模型；U favorite快照和analytics事件 | admin-api迁移回归、`test_v13_content_transaction.py`、`test_v13_media_persistence.py`；mini-api `test_v13_events_and_favorites.py` | 只对隔离数据库执行本轮迁移验证；生产迁移、备份/恢复演练未执行 |
| 19 接口与权限原则 | 新同版本统一场景envelope；词卡必须revision/version/locator；每次资源取用重验权益；禁止浏览器/小程序直调百度 | A `modules/access_policy/router.py`、content/media路由、HMAC；U `integrations/admin_api/client.py`和learning access路由；W调用本项目后台 | mini-api `tests/contract/{test_v13_scene_contract,test_internal_signature_vectors}.py`、`tests/unit/learning/test_fail_closed.py`；admin-api `tests/unit/security/test_service_hmac.py`、`test_v13_content_transaction.py` | 小程序short URL刷新、失权停止播放/关闭全图/清缓存延期；后端已fail-closed不能代替端到端真机验证 |
| 20 隐私、安全与数据生命周期 | 原隐私规则沿用并明确受控完整原图/OCR凭据隔离；匿名业务事件无个人payload、注销断用户链接 | U字段加密、联系人/账号仓储、event白名单；A HMAC/admin session/audit、OSS生命周期和maintenance | mini-api `tests/unit/auth/test_identity_protection.py`、`tests/e2e/test_privacy_flow.py`；admin-api `tests/integration/test_admin_security.py`、`test_oss_object_lifecycle.py`、`tests/unit/media/test_oss_log_redaction.py` | 已注册/引用素材受引用保护；实际云安全判定、生产RAM/CORS/日志留存和注销外部附件删除仍需生产配置验证 |
| 21 汇总统计 | 原文规则未变；本轮补此前缺失的真实producer、不可变唯一事件、日/自然周/月行为去重、cohort比率和匿名注销 | A `modules/analytics/{events,lifecycle,service,router}.py`、maintenance；U各业务仓储事务hook；W `features/analytics`和analytics-page | admin-api `tests/unit/test_v13_analytics_events.py`、`tests/integration/{test_v13_analytics_pipeline,test_analytics_daily_producer,test_analytics_period_queries,test_task_maintenance}.py`；mini-api `test_v13_events_and_favorites.py`；admin analytics-model/adapter测试 | 曝光端点已就绪，小程序真实曝光触发延期；历史缺失事件不回填为虚构真实用户。详见第三节及batch-6统计文档 |
| 22 文案与视觉规范 | 新原图/双入口/可选发音等说明，保留无交易/无焦虑、统一播放和安全区 | W统一表单/预览/错误状态；A返回受控业务错误；U传输契约 | admin `tests/e2e/content-editing.spec.ts`；mini-api `test_fail_closed.py` | M14/M15/M16须各自独立视觉预览确认；小程序字号、按钮间距、颜色和文案不作为本轮已完成项 |
| 23 异常与降级处理 | 新增OCR关闭/额度不足/识别失败人工继续、音频标时和版本失败处理；其他网络/授权规则沿用 | A quota/provider/任务认领/乐观锁；W候选/保存/标时状态；U503 fail-closed/短期URL到期拒绝 | admin-api `test_v13_media.py`、`test_v13_media_persistence.py`、`test_v13_content_transaction.py`；admin revision-controller/segment-player测试；mini-api `test_fail_closed.py` | 百度真实限额、超时/计费回执及微信网络切换需外部/真机验收；不自动付费、不重试耗尽 |
| 24 核心验收标准 | 更新24.2不限量/音频/原图；24.3预览隔离；24.4小写期限；新增24.8双入口/统一版本/试听/设计确认 | 对应5–23章代码与下方验收组矩阵 | 下方第四节列出各验收组测试，最终结果从evidence读取 | 后端/后台测试通过不等于24章全项完成；所有小程序交互、M14/15/16设计与外部验收明确待完成 |
| 25 分阶段开发建议 | 内容生产阶段改为人工/OCR统一草稿、额度、标时、版本接口；阶段划分保留 | 本期 `docs/implementation/v13-content/{design,plan}.md`六批实施记录 | `evidence.md`逐批证据及最终门禁 | 用户端开发阶段与上线提审阶段延期；没有生产部署或推送 |
| 26 技术方案评审清单 | 增补同版本四模块/原图/标时/候选、百度限制/免费资源、安全封面隔离 | 1–13/15–16/18–19/23–25后端方案见design及矩阵；客户端14/17/20–22/23/25事项另见延期表 | 身份/权益/并发/隐私/发布/素材/统计对应测试；外部项单独记录 | 第14/17/20–22客户端路由、录音、播放、离页释放与第23/25客户端联动不能标为完成；第24账户资源仍未核验 |
| 27 优化意见追踪表 | 保留原20项/已确认四项/逻辑修改；新增27.4六项内容与音频追踪 | 27.4分别对应本表6/7/8/15/16/19章；旧界面项继承既有实现 | 以最新24章与本表定位，不将Pages缩略图计作额外需求 | 尚未确认的14项优化不纳入；M14/15/16最终设计不预先宣称已确认 |
| 28 已确认默认项 | 汇总本轮双入口、完整原图、整段标时、自建词库/可选发音、不限量、小写期限 | 固定枚举/发布规则/请求DTO/任务额度按最新值执行 | `test_v13_entitlement_terms.py`、`test_v13_content.py`、`test_v13_scene_contract.py`、`test_v13_events_and_favorites.py` | “默认项”是规则，不是生产配置已生效；现OCR关闭、TTS关闭，用户端实现仍待后续 |
| 29 参考依据与动态核验 | 新增百度接口/免费资源参考；其他AppID、主体、隐私和Pages基线沿用 | 凭据仅服务端，图片限制/额度/停止自动重试机制见quota/provider；原文保留 | HTTP provider mock验证参数；真实合成测试OSS字节路径；不以文档链接替代控制台证据 | 百度账户额度和关闭付费、微信当前类目/隐私/录音审核、生产OSS配置及真实云审核均未通过最终外部验收 |

## 二、本轮重点规则的关闭方式

| 条款 | 旧→新规则 | 代码和断言必须同时满足 | 交付界限 |
|---|---|---|---|
| 5.4、8.3、24.2.17 | 最多10张→全部可复习收藏，无总量硬上限 | today_task不再切片；`GET /api/v1/reviews/queue` cursor分页单页≤100，跨页125条无丢失/重复；复习完成幂等打卡 | 后端已接入；前端三个入口逐一接入分页延期 |
| 6.2–6.4、19.1 | 列表主题图→安全封面；完整原图只供授权详情 | 不把original_image作为预览封面；双语title；严格PreviewScene拒绝私有字段；无权限open不写学习历史 | 后端可测；M06显示/轻提示延期 |
| 7.1、16.1–16.2 | OCR主路径→人工/OCR双入口同稿 | 原图和结构化四模块分离；保存允许未齐；发布统一严格校验；两入口不产生两种用户DTO | 后台/API实现；用户四模块排版延期 |
| 7.3、16.5 | 默认生成/分句资源→上传一份整段对话音频，毫秒区间 | 真实解码duration，0≤start<end≤duration；句子audio version与同一场景快照一致；更换整段音频清空原标时/确认；暂停/切换清理监测 | 后台播放器可测；上传合成静音验证只证明字节/时长，未证明真人句首句尾质量 |
| 7.4、8.3、16.2 | 可泛查/默认发音→仅自建词库已收录可点，独立发音可空 | 服务端生成片段、语块优先、维护变体匹配；句子ID/entryID稳定定位；vocabulary/chunks列表入口授权；null音频保留；收藏空context取权威英文 | 后端与后台校对实现；底部弹层/无喇叭视觉延期 |
| 11.2、24.4.11–14 | 固定期限业务沿用，枚举统一为小写 | DTO/SQL约束/旧fixture/UI同为month_1/2/3/6/12、permanent；自然月月末、续期基准、永久转换和并发唯一不退化 | 服务与管理端可测；生产迁移尚未执行 |
| 16.3–16.4 | OCR自动草稿→主动候选、选字段人工采纳、免费资源防护 | 服务端读取私有bytes；CHN_ENG/位置/段落/置信度；预占/认领幂等；成功失败计尝试；失败不自动连试；候选按expected_version采纳，不覆盖未选字段 | mock参数/SQL并发可测；真实百度识别、账户额度/付费关闭未验收，OCR保持关闭 |
| 16.6、18.1、19 | 独立字段变化→原子启用统一不可变版本 | 发布校验实时事实和expected_version；全部媒体/词条固定版本；更新不追随audio.active version；句子重排保留稳定locator；资源必须属于请求revision并重验权限 | SQL事务/契约可测；客户端失权清缓存与真机同步待完成 |
| 16.7 | 批量任务占位→逐项实际业务执行 | 图片≤30/音频≤300/场景≤500；图片hash去重；真实标签/版权/包关系/校验/发布/下线/恢复/导出；worker单认领、取消未开始、失败重试及审计；不批量改用户资料 | SQL/真实Redis worker已具备验证；生产任务调度尚未上线 |

## 三、第21章指标、事件与口径

详细口径和失败关闭边界以 `juya-admin-api/docs/api/batch-6-analytics-acceptance.md`（2026-10-01更新段）、A `modules/analytics/events.py` 的 EVENT_METRICS、`service.py` 的 EXPORTABLE_METRICS/RATIO_BASES 为准。W `features/analytics/analytics-model.ts` 与adapter再次校验指标/维度/分子分母，不导出个人轨迹。

| 需求指标 | 成功事务事件/真实状态来源 | 聚合指标和日期归属 |
|---|---|---|
| 新增用户 | USER_CREATED，首次幂等建档 | NEW_USERS，事件北京时间日期 |
| 日/周/月活跃 | USER_ACTIVE / USER_ACTIVE_WEEK / USER_ACTIVE_MONTH；成功授权open、进度保存、首次场景/复习完成 | ACTIVE_USERS / WEEK_ACTIVE_USERS / MONTH_ACTIVE_USERS；每用户每自然周期一次事件，完整自然周/月显示独立人数；部分周/月范围显示PERSON_DAYS而非独立人数 |
| 开放完成和三场景全部完成率 | OPEN_LEARNER_STARTED、OPEN_SCENE_COMPLETED、OPEN_ALL_COMPLETED | OPEN_LEARNERS、OPEN_SCENE_COMPLETIONS、OPEN_ALL_COMPLETIONS、OPEN_ALL_RATE；全部完成率归首次开放学习cohort |
| 联系提示/填写/撤回 | CONTACT_PROMPT_EXPOSED、CONTACT_SUBMITTED、CONTACT_WITHDRAWN | CONTACT_EXPOSURES、CONTACT_SUBMISSIONS、CONTACT_WITHDRAWALS；CONTACT_FUNNEL以提示cohort，CONTACT_WITHDRAW_RATE以填写cohort |
| 五种联系状态 | 执行日真实user_contact状态；CONTACT_STATUS_CHANGED仅表示变更 | CONTACT_STATES为快照，CONTACT_STATE_CHANGES为事件计数，不混作库存 |
| 正式开通/状态/到期 | FORMAL_GRANTED、FORMAL_STATUS_CHANGED、FORMAL_EXPIRED | FORMAL_ENTITLEMENTS、FORMAL_STATE_CHANGES、FORMAL_EXPIRATIONS；FORMAL_STATES为真实执行日快照 |
| 限时开通/启动/到期/完成 | LIMITED_GRANTED、STARTED、EXPIRED、START_EXPIRED、COMPLETED、STATUS_CHANGED | LIMITED_GRANTS、LIMITED_STARTS、LIMITED_EXPIRATIONS、LIMITED_START_EXPIRATIONS、LIMITED_COMPLETIONS、LIMITED_STATE_CHANGES；LIMITED_STATES快照 |
| 3/5天启动率/到期前完成率/未开始失效率 | 同上，payload仅mode=3/5、cohort_day、started_day、before_expiry | MODE_3/MODE_5和各自NUMERATOR/DENOMINATOR；启动/未开始失效归开通日，完成归启动日且必须到期前 |
| 场景开始/完成/回访 | SCENE_STARTED、SCENE_COMPLETED、SCENE_REVISITED；回访仅已完成场景再次成功open | SCENE_STARTS、SCENE_COMPLETIONS、REVISITS；scene维度为内容ID，不含用户轨迹 |
| 收藏/复习 | FAVORITE_CREATED、REVIEW_COMPLETED | FAVORITES、REVIEWS；去重业务事件，重复提交不重复计数 |
| 反馈新增/分类/状态 | FEEDBACK_CREATED、FEEDBACK_STATUS_CHANGED和真实ticket执行日状态 | FEEDBACK_NEW分类、FEEDBACK_STATE_CHANGES事件、FEEDBACK_STATES快照 |
| 首次响应/SLA/超时 | FEEDBACK_RESPONDED（每反馈首次）、FEEDBACK_OVERDUE | FEEDBACK_RESPONSES、FEEDBACK_RESPONSE_SECONDS加权平均秒数、FEEDBACK_SLA、FEEDBACK_TIMEOUTS/TIMEOUT_RATE；SLA/超时率归created_day |
| 补充轮次/解决/重开 | FEEDBACK_SUPPLEMENTED、RESOLVED、REOPENED | FEEDBACK_SUPPLEMENTS/FEEDBACK_SUPPLEMENT_ROUNDS、FEEDBACK_RESOLUTIONS/FEEDBACK_SOLVE_RATE、FEEDBACK_REOPENS/FEEDBACK_REOPEN_RATE；最多两轮/一次重开约束仍由反馈状态机执行 |
| 注销申请/撤回/生效 | DELETION_REQUESTED、WITHDRAWN、EFFECTIVE | DELETION_REQUESTS、DELETION_WITHDRAWALS、DELETIONS；生效时所有历史event.user_id置空，event_key替换anonymous:随机事件ID |

事件ID及业务event_key唯一，成功操作与event同事务提交。payload只允许匿名状态/分类、mode、非负时长/轮数、布尔标记和ISO日期，禁止个人信息；注销不留可追溯subject token。每天北京时间01:00重算前一已结束日，并根据该日后续结果的cohort日期自动幂等刷新历史转化率。显式回补只重算指定日，不伪造历史用户；历史状态快照保留，周/月取周期最后实际快照。当前日及未结束自然周期的统计有每日汇总延迟：只包括已生成的结束日期，不能把当前周/月截至昨日的独立人数标作全周期最终总数；新采集开始前缺失事件的周期也不具备完整历史口径。无分母rate=null；不对每日百分比求平均；首次响应时长unit=seconds。曝光端点已实现，但用户页面尚未发送真实曝光。

## 四、第24章验收组与第26章评审的归属

| 验收组 | 已可定位的服务/管理端证据 | 尚未覆盖的最终验收 |
|---|---|---|
| 24.1（身份/联系，1–11） | concurrent_wechat_login、contact_rules/contact_concurrency、admin_contact_schema、prompt_exposure、identity_protection | 唯一档案旧路由、一次提示、无网络首页重连、本人口径与复制的微信前端验收 |
| 24.2（学习/收藏/成果，1–17） | learning_idempotency、beijing_learning_day、pinned_sources、v13_events_and_favorites、v13_scene_contract；后台segment-player | 双标签与成果跳转、长句/按钮/中文开关、单音频状态、M14录音、三个不限量翻卡入口真机 |
| 24.3（预览，1–7）及24.3A（分区/模块，1–5） | content_rules、access_policy authorize、v13_content_transaction、PreviewScene合同、不写history及学习写入鉴权 | M06状态/弱入口/当前页拦截与用户阶段重排、客户端未启用模块直接路由阻断 |
| 24.4（权益，1–16） | natural_month、formal/limited state_machine、formal/limited concurrency、limited_command_idempotency、formal term UI模型 | M36视觉、用户端倒计时/到期缓存与端到端审核验收 |
| 24.5（后台/待办，1–7） | admin_sql_flow、dashboard/task查询、users/entitlements/feedback-loop浏览器，敏感审计测试 | 生产账号/RAM/日志访问及真实运维状态配置；最终门禁按evidence记录 |
| 24.6（反馈，1–9） | feedback state_machine/admin_queries/concurrency/admin_feedback_flow，message_idempotency，oss_object_lifecycle | 微信截图压缩/上传与云真实审核、M42紧凑按钮真机 |
| 24.7（注销/清空，1–5） | deletion_state/outbox/account_deletion_concurrency、user_projection deletion_cleanup、privacy_flow、v13事件匿名化 | 用户端注销挽留/撤回界面及生产外部附件删除/留存政策复核 |
| 24.8（内容/兼容，1–8） | v13_content_transaction、media_persistence、batch_operations/celery_batch、场景contract、统一编辑器浏览器/HTTP | 百度真实识别、真人标时质量、微信iOS/Android音频精度与布局、M14/15/16最终设计确认 |

第26章25项按三类关闭：身份/授权/状态/幂等/版本/隐私/数据维护由上述后端及SQL测试验证；管理员上传/草稿/候选/标时/预览/发布由W页面与浏览器验证；用户路由、网络提示、词卡弹层、单播放器、录音和失权清理由延期的小程序前端承担。第24项百度账户免费资源属于外部验收，不能由内部额度表证明；第25项后端封面隔离与用户原图设计分别验收。

## 五、延期与外部验收清单

| 项目 | 状态与验收方法 | 不得误称完成的原因 |
|---|---|---|
| 小程序新正文/完整原图/同版本播放 | 明确延期；接入typed场景、revision词卡和短期resource URL；验证M09/M14同句列表 | 本轮未修改juya-miniapp，旧前端不能自动适配新契约 |
| 三个翻卡入口和无发音状态 | 明确延期；分页拉全、保留筛选、独立发音null隐藏控件 | 后端125条分页不是前端三入口验收 |
| 一次联系方式提示和曝光 | 明确延期；真实展示时调用已实现认证幂等曝光端点 | API事件能力不会自动产生页面曝光 |
| M14/M15/M16独立视觉预览 | 仍需产品负责人分别确认，之后才可进入正式设计稿 | 需求授权实施不等于三页最终视觉已获确认 |
| 微信iPhone/Android真机 | 未验收；真人对话句首/句尾、暂停/切换防叠播、录音权限/离页清理、长文本安全区 | 浏览器和合成静音/图片不能证明微信音频定位精度 |
| 百度真实OCR与费用控制台 | 未验收；核实账户免费资源、已关闭后付费、实际请求/日志ID、实际图片识别结果 | provider mock+内部配额不等于百度外部免费额度；本地测试OCR_DISABLED未产生百度调用 |
| 阿里云真实内容安全 | 未验收；image/audio/text真实云provider判定和回执 | 当前真实测试HTTP使用local synthetic fixtures gate，不能称云审核通过 |
| 测试OSS数据路径 | 已有实际测试Bucket合成fixture字节证据，详`.tools/v13-media-http-result.json`及helper | 仅证明真实上传/读流/hash/解码/签名下载、401/非法音频拒绝，不等于云安全或生产配置验收 |
| 生产OSS CORS/RAM/日志 | 未完成最终配置验收；浏览器直传遇到CORS阻碍，HTTP替代上传记录分开保留 | HTTP可上传不证明浏览器CORS；测试Bucket不是生产权限审计 |
| 微信主体/类目/隐私与提审 | 未验收；使用当前AppID和最新后台规则逐项核验 | 原文参考链接、本地开关和技术鉴权均不能替代平台结论 |
| 生产迁移/发布/部署 | 未执行；本次在隔离MySQL/Redis及本地服务验收，不清真实用户数据库 | 本轮不推送、不部署生产；隔离测试通过不能称线上已生效 |

真实后台浏览器证据由 `.tools/v13-browser-live.mjs` 与 `.tools/v13-browser-evidence/report.json` 记录；其中fixture自动化、真实登录/草稿/词库/预览及CORS失败、HTTP上传替代路径应分别读取。媒体真实HTTP由 `.tools/v13_media_http.py` 和 `.tools/v13-media-http-result.json` 记录：测试OSS合成音频/图片真实上传及下载、OCR/TTS关闭拒绝、Redis真实worker批量标签写入。最终状态、总测试数量与残余项由 `evidence.md` 收口；本文件不把未执行项写成通过。
