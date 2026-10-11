# V1.5 管理后端旧功能与重复流程审计

日期：2026-10-11。依据：`D:/个人/juya/doc/句芽英语V1.5开发交付包-20261011/句芽英语V1.5完整需求文档-开发交付整合确认版-20261011.md`，重点第16、17.1、18.2、19.2、30、32.1章。

本轮只核查管理后端和四仓源码消费者，前端验收范围为手机，不纳入平板。没有启动或重启Docker、执行SQL、种子、云调用、修改.env、实际数据、资源或历史迁移。仓库初始存在其他任务对runtime、short_reading、work_items、catalogue及生成契约的未提交改动，本轮未编辑或暂存这些改动。

## 判定标准

仅删除已被最新需求替换、生产源码零消费者且不涉及公开API/生成契约/历史引用的内部实现。源码搜索同时检查管理前端调用、BFF内部HTTP、runtime装配、Celery注册/派发和worker分支；单凭名字legacy、版本号旧、前端某页面未使用或新测试通过，不足以删除接口。

需求明确保留原多篇DOCX、独立音频/图片及词汇图包入口，并要求共用结构化草稿、版本、校对和发布（需求48、828、1796–1797行）。短文不得生成讲解、词汇、义项候选或词语/全文TTS；中央词库后台维护及场景本篇候选仍保留（18、323、330、1027行附近）。历史发布、收藏快照、资源引用及审计不能因退役而清除。

## 实现与真实消费者

下列代码路径相对本仓，其他仓明确注明；行号为本轮核查时位置。

| 实现 | 真实消费者/替换关系 | 结论 |
| --- | --- | --- |
| `content_import/parser.py`、`service.py:196–201`的统一ZIP与多篇DOCX | 管理端`features/content-import/content-package-adapter.ts:211`按ZIP/DOCX调用同`/content-imports`；worker `infrastructure/tasks/v15.py:138/155`消费VALIDATE/CONFIRM；导入最终共用`ProductionStore.save_revision_within` | 保留，两种输入汇合为同一批次manifest和草稿模型，并非重复发布流程 |
| `content/v15_materials.py`的独立素材ZIP | 管理端`content-materials-adapter.ts:26`与panel实际调用；runtime挂载；worker `tasks/v15.py:144`消费MATERIAL阶段；场景共用ProductionStore，短文共用ShortReadingStore.mutate | 保留；存在下述独立词汇表缺口，不能删入口掩盖 |
| `content/production_store.py`与`production_router.py` | 导入service、独立素材bind、content.repository的发布/全文读取均调用；BFF `integrations/admin_api/v15_client.py:60/73/140`消费同一权威open/preview/resources | 保留，旧名称不代表无人使用 |
| `production_router.py:119`的旧`POST /content/imports`图片导入 | runtime仍挂载；管理端`upload-adapter.ts:40`仍定义调用，未找到可达页面调用；生成契约仍有接口 | 当前不删。前端adapter零静态页面消费者不能证明公开HTTP API无外部调用；退役需协调契约与消费方 |
| `short_reading/schemas.py:17`、`store.py:77/418` | ReadingContent严格拒绝额外字段；详情仅纯阅读字段与可选FULL音频；统一导入`service.py:811`走独立ReadingContent；BFF `v15_client.py:68`及管理端short-reading-editor使用独立链路 | 保留；未发现正在运行的旧短文讲解或词汇候选实现，不为“清理”删除纯阅读能力 |
| `content/v15_candidates.py:112–129`、router及worker | `_draft`只读取当前scene_revision并校验SceneContent，候选仅AI_LEXICON/ASR_BOUNDARY；管理端scene-editor挂载候选panel；worker `tasks/v15.py:148`处理 | 保留，服务场景/词卡，无法把独立短文revision当场景候选，不是旧短文讲解 |
| `production_store.list_lexicon/write_entry/entry_references` | 管理端`lexicon-fields.vue:87/234`仅非modern、非candidate显示中央词库查询/保存；adapter实际GET/POST/PUT；schema1历史发布仍核对不可变lexicon_entry_version | 保留后台维护与历史验证。BFF当前详情没有中央词库HTTP调用 |
| `content/v15_entries.py:write_entries`与v15详情 | schema2写入content_entry/content_sense/content_source及不可变entry revision；ProductionStore.entry_references:884按schema分支；BFF learning/detail_aggregation.py将权威words/chunks与收藏状态汇合 | 本篇义项/来源替代学生逐词查库，中央库并非其可删除的重复实现；不按当前词库回填历史收藏 |
| `pronunciation/service.py:29`、`tasks.py:63` | 受控WORD/CHUNK目标，上传优先、首次腾讯TTS、预算/并发/固定字节缓存；BFF `v15_client.py:146/161`、管理端pronunciation-status-panel及worker v15派发真实消费 | 保留唯一实际受控词语TTS链路，不能因旧整段TTS禁用而一并删除 |
| `media/tasks.py:335/391/400`dispatcher与batch任务 | runtime装配CeleryMediaTaskDispatcher；media/router批任务入队；Celery beat与worker执行/恢复批次 | 保留整个有效批处理链路，不整文件删tasks.py |
| `media/router.py:482`的generate接口、`media/tasks.py:366`注册旧generate任务 | HTTP与任务均明确TTS_DISABLED；管理端audio-adapter/use-audio-versions仍导出生成调用，生成契约保留；注册任务可能消费旧队列消息 | 作为禁用响应保留。公开API/队列契约退役先协调，不按普通内部死代码删 |
| `MediaTaskService/PersistentMediaTaskService`及旧`integrations/tts/protocol.py` | 没有runtime生产装配；仍被unit/media、provider contract及`e2e/test_admin_media_jobs_flow.py`的旧fixture使用 | 属于后续内部清理候选，但当前不迁移fixture或删协议；先协调旧测试/fixture的替换清单 |

## 已清理的确证无消费者内部代码

1. `bc58547`：删除`media/tasks.py`的`LocalTtsProvider`和`_job_payload`及无用hashlib import，文件570→539行。两符号在src/tests/scripts中均只有定义，没有runtime、worker或测试调用。前者只拼出不存在的本地音频对象键，已被实际pronunciation缓存/上传字节链路取代。保留所有注册任务、dispatcher、PersistentProcessingJob返回类型及旧fixture依赖协议。
2. `bc2f407`：删除`modules/ocr_suggestions/service.py`、仅docstring的包初始化和专属旧算法单测`tests/unit/content/test_ocr_suggestions.py`，共222行。OCR分组建议只被3个专属算法case调用，未挂router/runtime/Celery；最新需求取消OCR，正文结构化由ZIP/DOCX parser承担。删除的测试只服务于退役算法，现有OCR退役防线及新导入测试保留。

历史ocr_candidate、媒体资源、已发布snapshot、favorite/revision/reference、审计及所有迁移均未删除或修改。清理后src/tests/scripts对上述删除符号的搜索无残留。

## 确认缺口与需要协调的具体清单

### F01：独立词汇图包的XLSX路径未实现

需求32.1明确“词汇表.xlsx + images/”，先选择已存在来源内容，可带独立词语音频。当前管理端`pages/content/content-import-page.vue:96–113`“词汇图包”实际进入ContentMaterialsPanel，并非旧`/media`图片导入。

实际API：`POST /api/v1/admin/content-materials` multipart只有`file/series_id/content_type`及幂等键；上传阶段没有source_content_id/revision。GET批次返回manifest.resources及每项path/sha256/allowed_roles/matching_candidates。POST bindings已有`revision_id/expected_version/manual_checked/bindings[{path,role,entry_id?}]`，panel与服务端会验证当前draft及series/type/CAS；这是明确绑定来源，不等于已支持表格解析。

worker `LegacyMaterialService.run:425/476`经read_container调用`material_manifest:37`；其59行拒绝`.xlsx`并返回`MATERIAL_FILE_TYPE_INVALID`。纯函数最小复现输入单个`.xlsx`条目即得到该错误，无数据库或外部调用。统一正文ZIP parser虽读取词汇表，但先要求内容表与正文，不能替代独立词汇图包。

最小协调范围：明确上传前/校验前来源内容或等价稳定revision映射；增加独立词汇表行解析、import_entry_id/父级映射和图/音频预览；保留现有纯素材手选bind兼容；同步materials请求/manifest DTO、管理端adapter/panel、生成OpenAPI/客户端契约和小样本集成fixture。现有MATERIAL worker及草稿/发布对象继续复用。本轮仅报告该公开契约缺口，不自行改API、OpenAPI或fixture，也不以删除入口处理。

### F02：长难句与语法仅具配置，能力尚未开发

本仓runtime在ContentService与LearningModulesStore两处明确传`long_sentence=False/grammar=False`。新管理路径`learning_features/modules.py:23/76`返回ready=false、reason=MODULE_NOT_IMPLEMENTED，enabled=true拒绝LEARNING_MODULE_NOT_READY/409；旧发现页保存`content/service.py:467–477`也拒绝MODULE_NOT_READY/409并列出缺失modules，原因是缺完整模板/路由/授权链路。

允许保持关闭、调整名称/顺序不能等同模块已实现，也不允许以开关替代学习功能。此次不开发新模块或删能力门禁；如后续要开放，需另行补完整内容模板、授权/BFF路由及手机学习页面后才标ready。

### F03：旧公开禁用接口及fixture依赖内部模型

若协调决定彻底退役旧整段generate HTTP/注册任务，需核对管理端audio-adapter/use-audio-versions、生成契约和可能旧队列消费，不可静默删除。若仅继续删除旧内存任务模型/旧TtsResult协议，则先迁移unit/media/test_processing_tasks.py、unit/test_v15_operations.py、contract/test_provider_contracts.py及e2e/test_admin_media_jobs_flow.py中依赖的旧fixture，保留实际批任务和“旧消息不得再次收费”的响应。本轮没有这些消费者/fixture变动。

## 验证与边界

根任务独立验证现有相关范围：unit/media、unit/pronunciation、provider contract、导入parser/sources、OCR退役、模块与生产规则，**114 passed，2.90秒**；仅一个既有Starlette/httpx弃用warning。Ruff通过，mypy检查清理文件及parser/sources/module四个源文件通过；没有运行SQL或重跑大套数据库测试。

此结论证明本次源码清理和调用关系，不替代手机Figma截图、独立词汇图包新增能力的真实HTTP验收、微信真机、云供应商/OSS、人工听音/版权或生产部署。已有最终小样本联调通过不代表它覆盖了本次发现的独立XLSX入口缺口。
