# V1.5 两 API 只读事实审计（2026-10-14）

范围：依据 `D:/个人/juya/doc/句芽英语V1.5开发交付包-20261011/句芽英语V1.5完整需求文档-开发交付整合确认版-20261011.md`，审计两 API 源码/契约/配置。本文路径均相对 `D:/个人/juya`。只写此证据报告，不修改产品、测试、依赖，不提交/部署、不操作真实用户/供应商。管理前端与小程序页面由其他审计覆盖。源码对应事实不等于端到端业务验收。

本次起点核实：admin-api clean `test` HEAD `b4de16ab5734973b6eac1c185af09c05cbb333cb`；miniapp-api clean `test` HEAD `d10f44bbcdbfed3c603dc3b953b9b07e6cde1877`。管理 HEAD 包含文档提交；产品/已部署基线 `4cb76c1674e57f848fdf8764c403c572c0d6ed81` 为父任务已取得证据，本文不冒充重新部署。父任务已有全量检查不重复执行。

## 已确认偏差与能力边界

### 注销到期后仍可撤回（确认源码及内存反例）

需求10.3:504-506及24.7:1335要求7天内撤回、届满删除/匿名化。`juya-miniapp-api/src/juya_miniapp_api/modules/accounts/service.py:40` 设置 now+7天，`:42-49` 撤回仅委派仓库；`repository.py:172-179` 内存撤回只拒绝DELETING后直接REVOKED/ACTIVE；SQL同文件`:365-392` 也只检查DELETING而无effective_at截止检查。定时清理在`:409-430` 仅处理仍PENDING且到期的申请。

本次纯内存运行，无数据库、网络、真实用户：2026-10-01T00:00:00Z申请，effective_at=2026-10-08T00:00:00Z，2026-10-09T00:00:00Z撤回返回REVOKED，user_status=ACTIVE，后续execute_due_deletions返回0。退出码0。说明到期而worker尚未开始清理时仍可撤回；SQL具相同源码缺口，SQL真实执行未验证。

可复现命令（在miniapp-api目录，`.venv/Scripts/python.exe`，设置PYTHONPATH=src，stdin执行）：

```python
import asyncio
from datetime import UTC, datetime, timedelta
from juya_miniapp_api.modules.accounts.repository import InMemoryAccountRepository
from juya_miniapp_api.modules.accounts.service import AccountLifecycleService
async def main():
    repository = InMemoryAccountRepository()
    repository.seed_user('diagnostic-only')
    async def revoke_sessions(*args): pass
    service = AccountLifecycleService(repository, revoke_sessions)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    request = await service.request_deletion('diagnostic-only', now)
    print('effective_at', request.effective_at)
    result = await service.revoke_deletion('diagnostic-only', now + timedelta(days=8))
    print(result.status, repository.user_status['diagnostic-only'])
    print(len(await service.execute_due_deletions(now + timedelta(days=8))))
asyncio.run(main())
```

### 长难句/语法未交付能力（确认源码，非已完成项）

需求17.1:928-939列出四类模块，并明确能力未完成默认关闭、可提示缺失能力。`juya-admin-api/src/juya_admin_api/infrastructure/runtime.py:245-249,325-329` 两种runtime均将long_sentence、grammar能力设为False；`modules/learning_features/modules.py:23-29`返回ready=False/reason=MODULE_NOT_IMPLEMENTED；`:76-77`保存启用返回LEARNING_MODULE_NOT_READY/409。不能把这两模块计为已交付学习功能；当前关闭与能力门禁符合939，完整四模块能力尚未完成。

## 契约和挂载

本次使用各项目既有虚拟test配置生成实际挂载OpenAPI，没有进入ASGI lifespan、没有数据库/HTTP/供应商请求：

| API | 源码生成 | 整体JSON深比 |
|---|---|---|
| admin | 186 paths / 205 operations / 219 schemas | 与`juya-admin-api/docs/contracts/v15-openapi.json`及`juya-admin/openapi/admin-api.json`均相等 |
| miniapp | 64 paths / 73 operations / 67 schemas | 与`juya-miniapp-api/docs/contracts/miniapp-api.json`及`juya-miniapp/tests/fixtures/contracts/v15-b38294b/openapi.json`均相等 |

生成依据：`juya-admin-api/scripts/export-v15-openapi.py:15-28`配置/构造，只取schema不执行写文件段；miniapp采用`tests/e2e/test_miniapp_core_flow.py:13-35`的runtime_settings并生成schema，后关闭所创建资源。两条本次命令均exit0。`juya-admin/openapi/admin-api-source.json:2-13`仍声明76da6a5及152paths/169ops/169schemas；这是已确认过期追溯元数据，不是当前DTO漂移，不得把旧152数字当当前接口规模。

## 需求—源码—接口对应

以下“存在/有门禁”是读源码确认，不意味着全业务已通过。路径简写A=`juya-admin-api/src/juya_admin_api`，M=`juya-miniapp-api/src/juya_miniapp_api`。

| 需求 | 已读取的源码事实及接口 | 本次验收边界 |
|---|---|---|
| 5、9身份/昵称/头像 | M `modules/auth/service.py:46-77`通过wechat.exchange_code获取openid并加密/摘要建立用户和会话，拒DELETING/DELETED/SUSPENDED；`:79-96`轮换refresh；`modules/users/service.py:47-112`读取和修改本人profile；`users/router.py:83-113`提供头像upload-policy、GET /me、PATCH/POST /me/profile | 实际AppID/微信登录/头像OSS未调用；不能把虚拟配置生成契约当登录成功 |
| 5首页任务/有效学习日 | M `modules/learning/service.py:16-36`按未完成、新场景、收藏复习、历史选任务；`:63-81,100-104,123-126`进度和完成前鉴权；`checkins/service.py:6,16-22,25-55`按Asia/Shanghai去重和计算连续天数；`learning/repository.py:357,387,419,437`写相应完成分析事件 | 当前场景完成/复习触发路径有源码；首页真正请求及跨午夜业务未实跑 |
| 6、7、17学习权限/资源 | M `learning/access_service.py:215-250`管理API不可用返回authorization_pending，v2内容再次向权威接口取整包并核对revision/version、附本人收藏状态；`:253-269,297-325`检查签名过期/版本；`learning/v15_authority.py:33-84`核对资源属于当前整包引用后取址；/learning/modules、/learning/catalog、/scenes/{id}/open及资源路径在实际source契约中 | 已确认失败闭合和版本绑定；模块关闭、到期、并存权益的真实API矩阵未在此轮执行 |
| 9、13微信号同意/受控更正 | M `contacts/service.py:103-132`规范化微信号、加密与lookup_hmac；`:135-159`撤回/更正；`:249-269`产生can_self_edit/requires_correction及同意/核对信息；`contacts/repository.py:815-850`已联系/已核对禁止自改，self_edit_count限制，变更后PENDING/change_pending；`:886-889`撤回清除密文/HMAC/同意；A `contacts/router.py:169-319`更正处理、联系状态、新微信号核对、copy-events接口 | 源码存在相应控制；未读取或输出真实微信号，未执行后台联系操作 |
| 11、12权益/活动 | A `access_policy/service.py:93-137`合并OPEN/FORMAL/LIMITED，过滤已到期授权、取最早资源截止；`limited_entitlements/repository.py:284-322`事务锁活动版本，校验OPEN、3/5天、容量、ACTIVE用户与重复开通；`:324-352`固定期限和累计名额；内存实现`:163-178`首次激活/到期；`limited_entitlements/domain.py:30-114`最多一次启动补救、ACTIVE不可延长、恢复不改expires_at；formal/limited preview-operation及commands、campaign版本copy在契约中 | 固定版本、容量和时间计算有源码；并发SQL/真实用户开通未执行，本轮不重复全量集成检查 |
| 14反馈/SLA/消息 | A `feedback/service.py:125-145`300字/单张限制及deadline；`:163-175`状态推进；`:202-219`最多两轮补充、冻结剩余SLA并生成消息；`:255`限制截图属于feedback/{user}/；`:355-362`7天内最多一次重开；`:397-405`关闭后30天截图delete_after；`feedback/repository.py:496-509`工单锁及幂等记录。M `feedback/router.py:69-158`列表/创建/上传/详情/补充/处理结果，`messages/router.py:47-66`消息/已读 | 有状态流及用户消息实现；真实截图审核、供应商上传、消息送达未调用 |
| 10、20数据清空/注销清理 | M `accounts/router.py:52-88`独立learning-data、deletion、revoke接口；`accounts/service.py:29-40`确认词/7天；仓库到期事务`:409-459`锁申请并撤销session、本地清理、outbox；A `user_projection/deletion_service.py:258-281`删除snapshot引用/短文进度，`:294-365`撤销权益、截图提前排队删除、反馈user_id置空/审计匿名化、持久化清理回执outbox。M `infrastructure/tasks/schedules.py:13-25`每60秒执行到期、15秒派发outbox | 源码具跨域清理和异步回执；前述超7天仍撤回反例明确不符合。未执行真实用户清理，worker/Beat是否实时运行未核验 |
| 20反馈对象删除 | A `infrastructure/tasks/maintenance.py:329-395`按delete_after,id找100项并锁截图/工单，确认终态或注销完成、检查引用、先审计再OSS删除，异常延后重试、成功才deleted_at | 只确认代码策略；未删对象，无法声明真实OSS生命周期验收完成 |
| 30短文纯阅读 | M `short_reading/domain.py:21-86`安全preview DTO无正文，detail只有paragraphs_en/translation_zh/whole_audio/progress，无words/chunks/讲解；`service.py:24-75`open/save/complete均权威鉴权、独立repo，`:83-94`版本/段落/offset检查；`router.py:44-107`独立列表/preview/open/progress/complete/history/资源。A `short_reading/preparation.py:76-166`机器准备排除人工试听条件，已上传FULL资源仍检查固定asset/version/sha/duration/安全状态、非属主/非导入dedup拒绝；`store.py:416-446`从启用revision整包投射可选whole_audio | 无音频允许条件、试听/出版确认仍需完整发布门禁；机器ready不代表人工已听，真实播放/读完、场景统计隔离未在本轮请求验证 |
| 16、19、32统一生产/任务 | A `content_production/router.py:42-325`真实挂载work-orders上传、from-batch/manual、原始项/文件、import-preview/correction/confirm/retry、organize、候选决定/移除/确认、sampling/sample-action、publication-preview/confirm/retry、stage-retry/cancel/resume；`content_import/router.py:29-72`兼容批量导入上传/预览/确认/取消 | 接口闭环已存在；原始/候选分离、局部执行及失败结果将在下段给具体控制证据，实际人类听音/正式确认发布仍暂缓 |
| 16、19场景发布 | A `content/production_rules.py:25-66`确认sealed素材/音频归属；`:68-116`时间边界、切片确权及dependency_hash；`:150-193`v2对话必需中英文/说话者/音频/切片、来源版权；图片/词汇/语块选填且语块禁图。A `content/publication_ranges.py:193-226`人工确认、expected_version+collection_hash冻结范围、幂等摘要核对；`:263`同事务保存stage/outbox | 存在显式冻结范围与模板发布校验，不能据此声称所有存量内容通过或真实场景发布完成 |
| 21、31统计/用量 | A `analytics/service.py:183-208`匿名导出白名单，`:270-341`周期聚合/比例分母说明；`cloud_usage/store.py:46-108`硬预算、并发原子预留、已有非FAILED付费attempt不重复调用、北京时间日；`cloud_usage/router.py:16-108`读取实测/预算/tts估算与verified_actual，null保留未知且supplier_invoice_verified=False；`content_production/usage.py:16-61`工单stage关联实际attempt，返回token/音频毫秒/retry/unknown | 未调用付费供应商；没有当前账单、价格核验、真实usage数据，不做实际费用/优惠/预算剩余结论 |

## 收藏、缓存发音、生产及历史补充证据

- 需求8/19.5：M `modules/favorites/snapshots.py:26-45`六元组身份为content_type/content_id/entry_type/entry_id/sense_id/source_id；`:68-88`新确认快照必需版本/权威字段，语块不能携图；`:119-121`新收藏只接受dialogue/word_card，不允许短文新建。内存`:201-227`同一有效收藏保留原数据、取消后重建时递增snapshot_version；SQL对应行为及历史迁移链未在本轮逐状态实跑。M `favorites/resources.py:40-90`读取本人有效快照固定图片、上游取址后再次核对绑定、解析URL/期限；M `short_reading/history.py:21-40,87-164`本人ACTIVE才查独立阅读历史，只返回安全标题/时间/计数/can_open，不含正文/定位。
- 需求7.4/19.6：A `modules/pronunciation/service.py:53-67`上传资源优先，其次cache_key缓存，最后允许合成目标；`tasks.py:50-77`确认目标/允许合成/启用与价格配置检查、记录供应商计费字符；`:132-140`只在可重试且结果非unknown并attempt<2时自动重试。M `pronunciation/service.py:112-154`从当前内容或本人收藏取得服务端授权目标，不接受任意文本替代上下文。本次只确认代码路径，缓存并发合并SQL、手动冷却/全部失败分类、实际TTS付费及真机播放未执行。
- 需求32.5安全包路径：A `content_import/parser.py:82-106`拒绝绝对/空段/./../冒号/控制字符/Windows保留名；`:140-159`拒大小写重复路径、符号链接、加密/特殊文件及压缩比/单文件规模超限；`:853-860`明确document/image/audio扩展白名单、引用不存在/类型不符报问题。实际内容解码与完整总规模边界已有代码，但本轮不运行全部恶意包/大规模夹具。
- 原始与候选：A `content_production/service.py:438-453`预览同时返回candidate和source_snapshot；`:458-466`读取原上传文件；`:520-553`纠错校验当前draft/edit_version及candidate_hash，短文正文/音频变动清空旧听音确认并失效校对。`router.py:136-140`的/original路由返回完整WorkItemDetail（含原始/候选），不是另一个隔离DTO；原始完整保真、特殊DOCX层级、旧原始revision在全部输入形态的表现未在此轮逐一验证。
- 任务恢复/局部发布：A `content_import/stages.py:33-67`以item/stage/input_hash/config_version创建阶段及失效旧版本，`:80-106,139-149`租约+fence控制领取/回写。`content_production/commands.py:312-318`恢复使用冻结原预览；`:421-459`publication-preview逐项inspect，确认检查token属于工作单、selected_revision_ids属于冻结范围，再传manual_confirmed/range_version/collection_hash。`content/v15_proofreading.py:26-29`样本数为5%上取整、至少1最多20且不超过正常项数；`:205-207`由服务器随机选正常样本。异常逐篇、正常批量确认、系统性失败传播和修订后的所有结果分类，当前仅定位代码，沿用父任务既有检查证据，不另宣称本轮全状态通过。
- 草稿回收/资源保护：A `media/service.py:1538-1607`具有trash/restore/cleanup入口；`media/repository.py:1300-1322`草稿引用检查包括发布版本、内容包、活动、开放/预览、派生revision。该检查本身没有favorite_resource_reference条件，不据此推断整个收藏资源GC有缺陷；实际resource删除入口、全部有效收藏/历史版本保护需沿资源取址与删除链进一步验证，列为待确认而非通过项。
- 短文隔离：M `short_reading/sql_repository.py:77,143`产生SHORT_READING_STARTED/COMPLETED分析事件，独立short_reading_progress。需求30.4:1655-1658明确不算场景完成/第5.5有效学习日；本轮确认了存储/事件分离，未重跑所有汇总SQL，因此“未混入统计”的端到端结论仍待确认。M `integrations/admin_api/v15_facts.py:47-85,145-162,212-230`直接查最新模块/ACTIVE账户/PUBLISHED/试读和有效权益，授权链存在，真实取消试读/关闭/下线后的请求矩阵待确认。
- 清空与注销分离：M `accounts/repository.py:672-704`清空仅删短文/快照/复习/收藏/打卡/场景事件与进度，注销再删联系更正/联系历史/微信号/消息/profile；清空本身未列入账号或权益表。本轮不执行真实清空，头像物理对象和全部审计/统计匿名化留存边界仍待确认。

## 首轮结论与未覆盖事项

已确认：两端当前source和消费snapshot完全一致；后台追溯元数据过期；注销截止控制有内存可运行反例且SQL存在同缺口；长难句/语法尚无可启用能力。没有依据给出“全部V1.5通过”或百分比。

推断：若定时任务已及时转DELETING，到期撤回会被拒绝；但执行存在间隙/停机/积压时当前代码的7天截止依赖worker执行顺序。这是依据源码和内存反例推断运行风险，未对线上账户操作。

待确认：上述矩阵明确标出的真实微信/OSS/供应商、SQL并发/故障恢复、完整收藏GC/历史资源、匿名化保留边界、全部学习状态/统计口径；人类真实听音、场景正式确认及发布、真机音频、防叠音和真实业务操作。本轮不接管管理前端/小程序界面审计，不把Figma/本地构建/HTTP健康替代这些验证。需求涉及法律/平台/价格事实时本轮仅读取需求范围，没有产生新的法务或价格建议。

本次实际运行：2条虚拟配置source OpenAPI深比较（exit0）；1条纯内存7天注销撤回反例（exit0）。没有重跑父任务已有admin 1132passed/45skipped等全量输出；该数字仅属于父任务先前验证，不是本次新增运行。无数据库连接、无HTTP请求、无供应商调用、无产品/测试/依赖变更、无提交/部署。结束复查两API branch=test、HEAD未变、git status --short为空（本报告位于忽略的.superpowers目录）。
