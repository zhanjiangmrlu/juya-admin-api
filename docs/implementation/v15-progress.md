# V1.5 管理后端实施进度

- 分支：`feature/v1.5-20261011`；起点：`f4817cc914cab625af0927a0576f6a97883c8c70`。
- 已读取：20261011整合确认版完整需求、技术方案00、04、05、06、07、08。仓库及祖先目录未发现适用AGENTS.md。
- 当前阶段：继续闭合跨服务消费发现的剩余契约,状态实施中。此前READY与568项通过仅覆盖先前公开契约;本轮录音开关、短文排序、历史资源回填及管理契约尚在实施。计划见v15-contract-closeout.md。
- 约束：仅本仓库、增量迁移、显式暂存、中文提交；不改.env，不重启共享服务，不应用共享数据库迁移，不调用收费云任务。

## 任务与接口预检

| 任务 | 交付／消费 | 状态 |
| --- | --- | --- |
| P0 | 0017扩展结构、BFF快照／短文进度／引用表、内部schema2 | 完成；31张新增表在私有MySQL8.4应用并通过结构契约 |
| P1 | 固定namespace、ZIP/XLSX/DOCX安全解析、差异预览／幂等草稿、独立短文 | 完成；独立素材ZIP、稳定正文来源、导入身份不可覆盖、取消／租约、局部重试、磁盘展开均通过回归 |
| P2 | 条件式发布、义项来源、正常抽样、实际单句文件／受控TTS | 完成；实际FFmpeg切片、人工边界／听审／校对、条目批量确认、旧版本保留及云硬预算通过回归 |
| P3 | 当前内容授权、收藏凭据／复习资源、保留投影、历史回填 | 完成；实际SQL角色、归属、账号／模块／权益、收藏固定版本与签名时限通过；BFF最终同事务消费见契约 |
| P4 | OCR退役、null容量、真实权益预览、反馈／统计／生命周期 | 完成；运营真实SQL与旧版增量迁移通过；修复原因必填后内部可用操作探测 |
| P5 | 装配、OpenAPI、锁文件、CI／隔离SQL回归、最终评审 | 完成；147个挂载操作冻结、锁文件／静态门禁、部署配置测试和全套覆盖率门禁通过 |

音频与发音模块实现者仅新增独立模块／适配／测试，运营实现者独占运营模块及旧OCR任务注册；本对话独占迁移、config/runtime、content/access、导入与短文。模块实现汇总分别见v15-audio.md、v15-pronunciation.md、v15-operations.md。

Ruling: 以本进度文件承载计划ledger，不创建额外worktree／修改共享doc — 用户已明确固定分支和进度位置 — 若格式不充分，仅需补充本文件。

## 本轮可立即消费的中间契约

- 已装配并导出169个操作（此前147为上一阶段基线），包括录音/模块/排序、导入预览和恢复、音频、发音管理、发布范围、仅visible封面签名及真实批次阶段关联；见v15-contract-intermediate.md。最新profile_completion_enabled独立持久配置正在验证，尚非最终READY。
- 0017→0018实际私有MySQL升级验证通过：旧短文已发布快照/摘要与旧收费账本完整保留，新增sort_order默认0、旧attempt_number默认1；保留旧读接口，downgrade拒绝破坏不可变数据。

## 本轮最新验证（资料入口配置加入前）

- 独立MySQL8.4/Redis全套：general 624 passed / 4 skipped、ops 1 passed，合计625 passed / 4 skipped；覆盖率83.09%，退出0。四项跳过仍是未启用真实OSS/浏览器CORS，不计通过。
- 发布范围word_card/short_reading真实发布、最终hash/当前草稿护栏、响应丢失UNKNOWN回执恢复4项SQL通过；仅visible封面、实际PNG/WAV字节预览/Range、历史旧v1/旧类型/旧上传pair、收费预算并发和UNKNOWN拒绝均进入全套。
- 独立复核确认并修复延期重试P2：CONFIRM/MATERIAL_BIND重试入队、原阶段fence消费、专属回执和审计同事务；原入队键可作另一endpoint重试键，三并发仅一个替代执行，取消后原请求仍重放、同键改参拒绝。复核未确认其他P1/P2。
- E01–E22管理后端测试路径及跨服务/云/设备边界见v15-acceptance-matrix.md；千篇实际解析/999+1恢复、冻结抽20和完整大JSON已量化，原始证据见evidence/v15-thousand-import-metrics-20261011.json。HTTP详情/worker查询并发p95未测，不拿SQL-only p95替代。
- 全部静态门禁通过：uv sync --locked、Ruff、469文件格式检查、mypy 191源文件。0017生成器现拒绝覆盖已冻结迁移，实际执行确认历史文件SHA256未变化。
- FFmpeg9.0.2实际可执行路径和仅子进程注入方式见v15-ffmpeg-handoff.md。

## 上一阶段验证（本轮待最终刷新）

最新资料入口配置加入后的全套正在等待环境恢复后重跑：上一次运行遇到C盘余量不足20MB与Docker/containerd实际磁盘I/O错误，导致临时解包/SQL写入失败、worker fixture准备失败；不计通过。协调对话获用户授权负责重启Docker，本仓未另行重启。独立纯包展开单测随后通过，配置18项隔离目标此前通过。私有MySQL运行器退出现同时删除本次容器自动创建的匿名卷，只清理本次创建资源，不执行全局prune。

环境恢复期间，最新源码在D盘本次专属TEMP/TMP、仅进程UTF8及协调迁移后的D盘FFmpeg9.0.2条件下执行全部单元测试：359 passed，退出0；Ruff/格式472文件/mypy191文件通过。第一次补跑使用工具迁移前C路径及默认GBK，出现9项环境失败（FFmpeg不存在/UTF8日志按GBK解码），正确注入后同测试全部通过，未修改业务逻辑或弱化断言。Docker恢复后仍因C盘仅数百MB由协调要求暂不跑大规模SQL，待通知后最后全套重跑；当前不标记最终READY。

用户最新约束：后续测试联调只使用少量数据，停止重复千篇与大JSON专项；既有千篇证据已足够。本仓遵守，不再启动这些专项。待协调最终小样本联调后，若仍有必要，仅补一次最小fixture的变化范围SQL验证，严格清理本次资源。最终门禁口径据此调整，不再承诺重复全规模。

准确回传基线：14482完整运行为general624 passed/4 skipped + ops1 passed，覆盖率83.09%，退出0，资料入口配置加入前；更早2981有general621 passed/1 failed/4 skipped + ops1 passed、81.59%，不是完整通过。最新资料配置18项隔离general全部通过（对应scoped ops为空导致runner整体exit1），最新源码全部unit359 passed与静态门禁通过；最终变化范围小样本SQL/真实跨服务运行时仍待协调安排。源码/169操作JSON基线76da6a5，后续仅运行器匿名卷清理8e4449a及环境记录411f79f。

已核验并清理本次06:43中断遗留：私有MySQL a70fb3c942594bbd857e4b68997f092f（mysql:8.4、创建时间06:43:30），私有Redis 782349a5fa7442209f124915f1cf7d6c（redis:7-alpine、juya.isolated-test标签相同UUID）；仅对这两个确定拥有的容器执行rm-f-v，两份绑定匿名卷随后列表确认不存在。没有全局prune，没有清理协调的主站或其他任务资源。

- Git初始工作区干净，历史0016未修改；新0017在运行器独占MySQL8.4中成功应用。
- 最终 `uv sync --locked`、`ruff check .`、`ruff format --check .`、`mypy src` 通过（176个源文件；419个格式检查文件）。
- 最终私有运行器从空覆盖率记录运行全套：general 567 passed / 4 skipped；独立ops 1 passed；总计568 passed / 4 skipped。跳过项为未授权启用的真实OSS及浏览器CORS测试,不计作通过。
- general覆盖率82.05%,加入独立ops后82.55%,高于80%门槛。首次76.48%后通过真实资源、音频发布、独立素材、派发恢复和批量校对回归提高覆盖,未缩减覆盖范围或降低阈值。
- 真实Windows FFmpeg9.0.2验证独立音频文件、时长、SHA/英文/边界失效、同依赖失败恢复和旧版不可变。部署配置的12项测试通过；Linux实际镜像构建／启动没有在本轮执行。
- 独立评审发现并修复：ACTIVE音频校对兼容、同边界切片阶段恢复、QUEUED超时重派、标准词汇表来源唯一匹配／歧义校对、导入映射禁止重指向新义项／来源、旧素材无审核请求时恢复提交。修复后只读复核未发现新交付阻塞。
- 当前冻结0017新增31表，保留历史迁移且downgrade拒绝删除不可变快照；管理前端/BFF消费本仓库docs/contracts/v15-openapi.json。本轮不修改其他仓库快照。
- 接口冻结为147个操作,生产schema与本仓库快照／接口清单一致；不修改其他仓库快照。具体装配与消费边界见 `v15-integration.md`。
- `scripts/test-v15-isolated.py`仅创建自己的临时MySQL容器，委托既有运行器创建UUID数据库／私有Redis，并在结束清理自己的资源；不重启共享主站。

## 最终小样本重试夹具复核

2026-10-11真实浏览器联调的VALIDATE重试202后未完成，worker原始日志明确为`content_import_item(batch_id,row_no)`唯一键1062：夹具将已成功阶段改为FAILED，却保留已提交的解析预览条目。生产validate在同一事务写入条目、batch PREVIEW_READY与stage SUCCEEDED；人工制造上述组合不代表真实初次解析失败，不修改生产校验或采用覆盖式插入。

协调方只对独立retry_batch在同一事务修正夹具：先断言全部item仍PENDING，再删除该批次解析预览条目；batch设FAILED、preview_version=0、manifest仅含QA_INJECTED_RETRY；VALIDATE设FAILED并清空output/next_retry_at/lease_owner/lease_until。原archive、input_hash、config_version、fencing_version保留。已核对当前表结构没有其他批次预览字段需清理；创建回执及历史outbox保留，后续新fence防止旧执行回写。涉及的批次未确认入库或发布，不触碰已导入内容及映射。

按用户少量数据约束，仅执行一次变化范围SQL运行：资料入口/录音配置、短文排序、延期阶段重试、原文件预览/VALIDATE恢复、实际发布适配、0018升级；不包含千篇或大JSON专项。六个文件共11 passed，25.02秒，运行器退出0。该目标运行未重新测量全套覆盖率；覆盖率仍引用资料入口配置加入前的完整基线83.09%。

协调方恢复Docker后运行器独占新UUID MySQL/Redis与数据库，本次MySQL fa341875779c4105b3526df3423719f1、Redis feb79bb566a14c7e8a6cc0b2e218e15c，结束由运行器rm-f-v清理；再次按这两个精确名称查询，均不存在。最终真实浏览器小样本复测由协调方执行，结果尚未回传，因此不提前标记跨服务READY。

## 0019收藏内容身份修复

最终真实小样本联调发现不同文章复用相同entry/sense/source时误返回旧favorite_id。旧favorite_snapshot主表没有content_type/content_id，旧唯一键只含用户和条目身份。0019新增两个NOT NULL无默认值字段，分别为utf8mb4_bin VARCHAR(32)/VARCHAR(64)；最低启动schema升级19。索引名保留uq_favorite_snapshot_1，顺序user_id,content_type,content_id,entry_type,entry_id,sense_id,source_id；不含snapshot_version/saved_content_version/revision_id。

升级先验证所有current revision指针及favorite_id归属，再从明确保存的范围回填新增字段，不修改原snapshot/revision/public_id/ref/ledger。任何无法证明的旧范围在DDL前拒绝。仅online，必须停写、备份、独占执行；MySQL多DDL分别提交，已加列或已装索引但版本未完成的状态先核验形状及一致性后可续跑。downgrade先检查旧五列碰撞，存在则明确拒绝，无碰撞以单条ALTER恢复旧约束与结构；不删记录、不去重。

元数据契约先观察1 failed/3 passed（旧五列键不包含内容范围），修改后6项契约/offline行为通过；全部单元361 passed。最新uv sync --locked、Ruff、476文件格式、mypy191源文件通过。首次SQL11 passed/1 failed（164.66秒）：新测试读取cloud账本误按不存在的id排序，在迁移前失败；修正为实际public_id。独立复核补充PAD SPACE尾空格矛盾，以CAST AS BINARY逐字节校验，不trim/覆盖原事实；只重跑新增及受影响用例，5 passed/4 deselected，98.52秒，退出0。四项deselected为此前已通过的无碰撞降级/再升级及三种归属阻断，不能计为本次执行通过；其余首次通过项包括schema19、历史回填写入、旧上传复习、固定资源授权和0018历史升级。

最终5项明确覆盖旧所有列/两版revision/ref/收费账本保留、不同文章与类型独立收藏、同完整身份不同版本唯一约束、旧键碰撞降级拒绝、加列/装索引后中断续跑、两个字段各自尾空格矛盾在续跑DDL前阻断。0017/0018和169操作OpenAPI快照保持未修改，alembic heads仅0019。本仓不执行共享环境升级，BFF模型/写入/详情scope由BFF负责人独占修改。详细操作与恢复边界见v15-favorite-identity-migration.md。

两次实际SQL运行的私有MySQL/Redis容器和UUID数据库均已清理。运行前读取精确mount确认归属：第一次MySQL c18cfc4e2644476081c5dc788f093214/Redis fc220edb67f54980ae9d75f223d27c7f，第二次MySQL 020dcb98e68243fb9916b4c8c30d094c/Redis 2cd71f13d0b645398479e0763af6f575。MySQL匿名卷由helper自动rm-f-v；旧通用runner清理Redis未带-v，确认自有卷且无容器引用后精确删除这两份Redis卷，查询确认不存在，没有全局prune。此前临时目录删除被自动策略阻止，继续保留，不绕过。

## 外部待验收

真实版权／人工听音、真实AI／ASR时间信息与腾讯音色／收费实账、安全提供方、OSS浏览器CORS／Range、微信AppID／设备与生产验收仍待实际配置和授权环境。不以本地测试替代这些验收。
