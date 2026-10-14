# V1.5 小程序事实审计（只读，2026-10-14）

## 基线与结论边界

- 当前项目：`D:\个人\juya\juya-miniapp`；分支 `test`；HEAD `2132dbb4a01723589bb338caecdc8f40cf7ee071`。本轮 `git status --short` 为空，未修改产品、测试或配置，未提交、部署、购买云服务或写入真实业务数据。仅保存本报告和设计读取证据。
- 权威需求：`D:\个人\juya\doc\句芽英语V1.5开发交付包-20261011\句芽英语V1.5完整需求文档-开发交付整合确认版-20261011.md`。下文“需求 Lxxx”均为此文件；源码路径均相对 `juya-miniapp/`。
- 已读 `juya-miniapp/AGENTS.md` 与用户全局事实规则。当前 `src/pages.json` 注册4个一级页、10个普通子包、44个二级页；一级为首页、学习、收藏、我的，没有长难句/语法的完整页面注册。
- **已确认符合**仅表示本轮所列源码行为与条款对应，不能扩张为完整产品、真实接口、安全、真机或上线验收通过；**偏离**为需求明确要求而当前源码未实现的行为；**缺证/待确认**表示需要后端、运行、真实设备、云端、运营或设计进一步证据。
- 已确证两项源码偏离：完成第三开放场景后一次联系提示的触发时点；反馈提交前敏感内容检查。长难句与语法为未交付能力；需求17.1要求能力未完成默认关闭，本輪后端源码 `juya-admin-api/src/juya_admin_api/infrastructure/runtime.py:245-249` 的 module_capabilities 对两者为 False，但线上模块配置未读取。不把占位提示算上线，也不据此新增页面。

## 设计证据及日期

正式文件 `evoGqXQ4pI3Q3cdLpc6NOc`，最新用户根 `2660:3`，来源为需求 L1944。设计静态存在不等于程序实现，需求 L1934-L1938、L1995明确区分。

| 编号 | 实际设计证据 | 时点和限制 |
|---|---|---|
| D1 | `doc/审计/20261011-小程序最新设计需求/figma-learning-new.txt`、`figma-explore.txt` 实际 context 文字有开放场景、开始学习、探索更多、内容预览及安全封面说明；矩阵对应 M05N `2665:11`、M05 `2665:78`、M06 `2665:231` | 2026-10-11归档；本轮读取实际文本。不是2026-10-14页面渲染或像素验收 |
| D2 | `figma-scene.txt` context 可见人物名、中英文标题、“播放完整场景对话”“查看译文”“重点词汇”“语块”“进入逐句跟读”；M09 `2665:288` | 2026-10-11归档，正文示例不等于真实资料 |
| D3 | `figma-shadowing.txt` 可见全部对话、当前句、播放原音、开始/停止录音、当次回听、重录、不评分；M14 `2667:8`；M14R `2667:99`、M14L `2667:190`、M15 `2667:281`、M16 `2667:342` 由矩阵映射 | 2026-10-11归档。仅M14文本已读；三种真实设备状态、M15故障恢复未本轮运行 |
| D4 | `figma-word-bank.txt`、`figma-chunk-bank.txt`、`figma-favorite-detail.txt`、`figma-chunk-detail.txt`、`figma-review.txt` 实际文本有双银行、独立收藏、简明解释/来源句/返回原文、背面音标释义来源句、再看一次/下一个；M19 `2665:668`、M20 `2665:738`、M21 `2665:792`、M21C `2665:846`、M24 `2665:1138` | 2026-10-11归档。完整快照真实性/不同意思/过期授权须运行契约与后端证明 |
| D5 | `figma-profile.txt` context 可见纸白身份卡、隐私说明、句芽号、复制、微信号、权益/消息/反馈/账号入口；M27 `2665:900` | 2026-10-11归档；示例JU0001688/juya_demo_1688不能充当用户数据 |
| D6 | `figma-complete.txt` 有场景/词汇/语块统计、本次成果明细、返回首页和复习；`figma-feedback-result.txt` 有完整时间线、7天说明、仍有问题/可重开一次/已解决；M16 `2667:342`、M42 `2665:1025` | 2026-10-11归档；结果数值示例不是后端实账 |
| D7 | `figma-reading.txt` context 可见英文自然段、完整标题、生活短文/邻里·A2、“显示全文译文”；短文组 `2666:211` | 2026-10-11归档；`figma-reading-list.txt` 本轮未提取到实际文本，不据此声称列表设计已核实 |
| D8 | **本轮 live get_design_context + 默认截图**：M28 `2480:680` 有“完善联系资料”、用途说明、“稍后再说”“前往完善”；M40 `2480:1334` 有四分类、相关页面、最多300字、1截图；M43 `2480:1496` 有“内容未通过检查”“请修改问题说明，未提交的原文不会保存”“重新提交” | **2026-10-14**；实际画面/文字已读，非图层名称。记录见同目录 `v15-miniapp-figma-evidence.md` |

本轮对 `2660:3` 读取metadata，未找到M28/M40/M43。随后 **只读 use_figma 查询父链**得到：M28 → `2480:623`“04 学习档案与联系资料 / 用户端 5屏” → `2475:158`“小程序页面”；M40/M43 → `2480:1223`“06 消息与问题反馈 / 用户端 6屏” → 同`2475:158`。最新M27 → `2665:898` → `2665:7` → `2665:2` → `2660:3`。因此D8是同文件承接旧母版节点，**不是最新根内新增画板**。需求34.1保留登录/权益/反馈/隐私生命周期规则；不恢复与第33章冲突的旧档案视觉。

## 逐项事实矩阵

| 功能与需求条款 | 设计实际证据 | 当前源码证据 | 已确认事实/偏离 | 缺证与验收边界 |
|---|---|---|---|---|
| 静默身份/失败原页重连，5.1 L203-L210 | D1；登录错误独立状态不在已读文本中 | `src/services/startup.ts:8-46,57-98`；`src/features/home/use-home-page.ts:67-82,104-107`；`src/features/home/components/home-entry-page.vue:140` | 已有单飞会话恢复/微信登录，失败清首页与目录，当前页网络弹层可重试 | OpenID服务端不下发、幂等用户建档、真实微信无网恢复由API/设备确认 |
| 北京问候/昵称/连续卡，5.2-5.3 L214-L223 | D1；连续卡细节未live核实 | `src/features/home/home-presenter.ts:49-62`；`src/features/home/components/streak-card.vue` | 问候使用北京工具，昵称自定义→微信→学习者；checkins来自DTO，缺值为null | 有效学习日计算、自然日去重/断签/累计最长不由前端证明 |
| 今日任务，5.4 L229-L234；有效学习日5.5 L238-L243 | D1；任务具体选取算法不由设计证明 | `src/features/home/home-presenter.ts:19-43`；`src/features/home/use-home-page.ts:28-49,92-95` | 使用服务端today_task，收藏复习传完整card_ids，场景任务必须匹配可访问摘要；不自行伪造推荐 | 恢复/首个未完/复习/最久未复习优先级与北京有效日是服务端职责，待API审计 |
| 新/老用户内容分区，6.1 L251-L256 | D1 | `src/features/learning/catalog-presenter.ts:53-95` | 过滤关闭模块和HIDDEN；有权益/非开放进度优先current/entitled并收起开放为复习入口；preview排除可访问场景 | 固定3开放、系列3-6预览、所有已开始非开放状态需真实目录矩阵 |
| 安全封面/预览卡，6.2 L260-L264；6.4 L279-L283 | D1 | `src/features/learning/catalog-presenter.ts:25-49,72-75`；`src/features/learning/components/scene-card.vue` | 预览与学习使用摘要模型，有access状态和不可进入行为 | 安全白名单、原图不泄露及服务端每次鉴权不能由卡片隐藏证明 |
| 无权仍停M06/一次弱入口，6.3 L268-L275 | D1 | `src/features/learning/use-learning-page.ts:31-39,50-95` | 无权限打开notice而不进入学习；资料入口走`contact-prompt`，会话记录显示机会；弱入口不授予权益 | 真实预览开关、刷新/多页面会话红点/入口时序待运行验证 |
| 同版本详情一次资料/默认译文/空块，7.1 L291-L299 | D2 | `src/stores/scene.ts:24-82`；`src/features/scene/scene-model.ts:35-59`；`src/features/scene/components/scene-reading-view.vue:11-55` | 现主页面直接显示详情正文、译文、words/chunks；空数组区块不显示；译文初态false | DTO当前内容版本与实际后端资料、完整时间边界及切片发布前提待API审计 |
| 词汇双列留白/语块无图，7.1 L295-L297；33.1 L1897-L1898 | D2/D4 | `src/features/scene/components/scene-word-grid.vue:25`；`scene-word-card.vue:58-85,93-179`；`scene-chunk-list.vue` | 双列minmax、卡min-height246、词汇图片容器无图保留；chunk不显示图片区；pending禁用收藏 | 长词/长释义整行等高和375/390/414微信真机未本轮验 |
| 人物名/中文排版，7.2 L303-L305；33.1 L1893-L1894 | D2 | `src/features/scene/components/scene-reading-view.vue:27-40,101-104,124-131` | 原顺序遍历、姓名与台词同行，#96523a/700，译文22px行高；全文译文独立 | 本轮不作完整像素/字体平台一致性判定 |
| 整段/单句资源和防叠播，7.3 L309-L315 | D2/D3 | `src/features/scene/components/scene-reading-page-view.vue:36-65,196-205`；`src/features/audio/audio-controller.ts:37-59,98-151`；`src/features/scene/scene-service.ts:16-29` | 整段固定“播放完整场景对话”，同版本边界定位，受控签名；播放claim所有权、取消旧请求/监听 | 无均分切句证据；真实切片内容、首尾完整/串音、人类听音未验且正式内容确认暂缓 |
| 独立发音/受控TTS/缺资源隐藏，7.4 L319-L330；19.6 L1085-L1088 | D2/D4 | `src/features/audio/pronunciation-service.ts:10-24,60-74`；`pronunciation-controller.ts:16-38,80-109`；`components/pronunciation-button.vue:66-107` | 只传受控ID与content/favorite授权上下文；按钮20px图/44px点击区/透明底；目标不可用隐藏；准备/失败轻量状态和有限等待 | 云端同键缓存/并发/2次自动重试/预算/费用/供应商配置并非前端已验 |
| 主学习不逐词查义，7.4 L319,330；24.2 L1242-L1243 | D2 | `src/features/scene/components/scene-reading-view.vue:11-55`；`src/features/scene/use-scene-page.ts:284-354` | 最新正文/词汇直显主组件存在；源码仍留legacy inspectEntry/inspectSentence兼容分支，不能仅因文件存在认定当前主流程调用 | 不扩大为删除旧public/兼容接口；各模板实际callsite运行覆盖待确认 |
| 全文译文切换，7.5 L335-L338 | D2 | `src/features/scene/scene-model.ts:59`；`src/stores/scene.ts:82`；`scene-reading-view.vue:32-42` | 默认隐藏、一次切整篇中文，words/chunks中文独立，不作完成前提 | 保持滚动/音频/收藏与离页再入需真机验 |
| 全句跟读/任句/不上传，7.6 L342-L346 | D3 | `src/sub-packages/scene/shadowing.vue:44-51,76-134,146-164,177-200`；`src/features/recording/recording-controller.ts:105-137,200-235,269-289`；`native-recording-port.ts:27,135-159` | 原音/本地录音/回听切换，任句选择，拒绝权限仅禁录，服务端recording_enabled；离页释放临时录音；无录音上传调用 | iPhone/安卓权限、自动停止、回听、临时文件实际删除与审核降级未本轮真机验 |
| 阅读位置/失权清理，7.7-7.8 L350-L356；19.6 L1083-L1084 | D2/D4 | `src/features/scene/use-scene-page.ts:107-175,403-412,461-485`；`scene-reading-page-view.vue:109-157,185-186,239-293` | 稳定位置队列和请求代次，失权清正文/图片/媒体，onShow重新核验；底部safe-area与空间 | 系列/旧来源映射、旧缓存到期、公网短签名真失效由后端及设备确认 |
| M15失败/M16完成，7.9 L360-L362；8.4 L407 | D3/D6 | `src/sub-packages/scene/audio-failed.vue`；`src/sub-packages/scene/completed.vue:29-78`；`src/features/learning-result/result-service.ts:7-9` | 完成页读取权威result，失败明确ERROR；详情/复习实际导航，完整收藏复习队列，非固定成果数字 | M15实际音频失败注入、M16五态与成果口径待运行，不作设计截图即可业务验收 |
| 收藏提交/取消/失败恢复，7.10 L368-L374；8.2 L388-L393 | D2/D4 | `src/features/scene/use-scene-page.ts:70-105,365-401`；`src/features/favorites/favorite-service.ts:15-49`；`src/stores/favorites.ts:145-215` | 稳定引用提交，不传任意释义，pending禁重；取消等待真实最终状态才更新；共享状态同步来源/银行/队列 | 幂等唯一性、陈旧版本冲突与旧快照不改写是服务器证据，待API审计 |
| 银行双标签/不同意思/独立操作，8.1 L382-L384 | D4 | `src/features/favorites/components/favorite-bank-page.vue:42-109,127-163`；`favorite-list.vue:18-56`；`favorite-presenter.ts:22-35` | 独立tab筛选/滚动/复习状态；按有效favorite_id成组，不按英文聚合；发音/取消stop不跳详情 | 同词不同义/多来源/跨设备实账未本轮验证 |
| 无限总数翻卡完整快照，8.3 L397-L403 | D4 | `src/stores/favorites.ts:238-278`；`favorite-bank-page.vue:97-109`；`components/review-card.vue:30-59`；`review-page-view.vue:337` | 分页读至cursor结束；全部可见收藏进队列；背面用同一item快照、无解释块；上方发音与英文点击stop；卡内滚动 | 首页/成果/银行入口大集合及长文本真实设备覆盖待验；服务端是否截断队列需API验证 |
| 收藏到期保留/返回当前原文，8.5 L411-L415；7.8 L354-L356 | D4 | `src/features/favorites/favorite-service.ts:31-49`；`source-resolution.ts:6-32`；`src/sub-packages/favorites/detail.vue:42-58` | 返回前请求source-resolution；NO_ACCESS不跳；仅EXACT带定位，更新来源显示“来源内容已更新”；target_content_version与原快照分开 | 失权仍本人收藏发音可用、源模块关闭不绕权限、版本唯一映射需API/运行证明 |
| 收藏详情样式/入口，8.6 L419-L423；33.2 L1906-L1911 | D4 | `src/sub-packages/favorites/detail.vue:83-118,137,146-203` | 详情保留解释/来源/收藏/返回；摘要min-height120，#FFFDF7/#E0E3D5/15圆角、单词26/音标13/语块17、中文22行高，自然换行 | 最长内容、字体、具体26/17视觉和下区顺推未本轮截图验 |
| 唯一档案/本人真实编号联系方式，9.1-9.4 L431-L453；33.3 L1915 | D5；当前parent实证 | `src/pages/profile/index.vue`；`src/sub-packages/profile/contact-prompt.vue:2-15`；`profile-dashboard.vue:62-74,141-159` | 同一ProfileDashboard；本人profile/contact动态填充，nickname兜底、wechat未填提示，不用设计示例 | profile接口所有权/编号唯一性/后端不泄他人未前端证明；编辑/头像真微信同步待设备验 |
| **第三开放场景完成一次联系提示，9.5 L457-L460** | **D8：M28弹层与可关闭/完善两动作；背景为档案；没有完成即触发的可见说明** | **`src/features/contact-profile/contact-prompt.ts:2-6`；唯一消费`profile-dashboard.vue:75-104`；`completed.vue:29-99`没有联系资格或曝光逻辑** | **偏离：提示在打开档案且eligible后才出现，完成第三场景不会立即触发。已有真实曝光持久标记/幂等补记，不能伪造完成/重复弹窗** | **设计背景档案与需求完成触发的时点存在边界：修复触发以9.5为准，复用既有弹层，不恢复旧档案视觉；服务端eligible/一次跨设备需API验证** |
| 用途同意/撤回/受控修改，9.6-9.7 L468-L483 | D8只证明用途说明；管理画板未本轮context | `contact-form.ts:20-30,46-63`；`components/contact-form.vue:21-40,60-75`；`contact-service.ts:10-20`；`contact-manage.vue:36-87` | 校验同意才存；明确用途；can_self_edit来自服务端；保存/删除/更正复用本人联系接口；离页清草稿 | 同值不消耗次数/1次修改/已核对更正/撤回后审计摘要和后台标记需API审计 |
| 清学习数据不删账号权益，10.1-10.2 L491-L498 | 未本轮读取M44/M45实际context | `src/sub-packages/account/clear-confirm.vue:32-48,54-79`；`account-service.ts:9-10`；`local-data-cleaner.ts:22-36` | 专门确认页说明不可恢复及保留账号权益；服务端成功才清学习缓存，不clearSession | 实际数据库删除收藏/进度/打卡、联系方式权益保留未操作真实数据 |
| 注销保留提示/7天撤回，10.3 L502-L508 | 未本轮读取M46/M47实际context | `src/sub-packages/account/delete-confirm.vue:46-92,104-178`；`deletion-pending.vue:62-166,197`；`account-service.ts:12-13` | 注销页加载真实统计/权益，提供保留/继续注销；受理成功清本地学习草稿而保留会话；撤回后重新加载 | 7天到期删除/匿名化/新用户不恢复旧权益、任务与外部截图清理是服务器/定时作业证据 |
| 三类权益分区与准确时钟，11.1-11.4 L516-L559 | D5权益入口；M32-M37具体实际context未本轮读取 | `entitlement-service.ts:8`；`entitlement-presenter.ts:18-77`；`entitlement-page-view.vue:78-111,212-247,327-344` | DTO formal/limited分区，duration_days来自DTO，时钟从服务端，ACTIVE/ENDED/EXCEPTION映射；unknown/paused/revoked收紧；正式生效/到期检查 | 授予自然月/多个包/续期/永久/原子激活/重叠授权/30日阈值需API审计；前端无管理员授予能力 |
| 一次补救/权益并存/结束成果，11.5-11.7 L563-L581 | D6成果参考；M36最新重设计未本轮实际读取 | `entitlement-page-view.vue:138-211,250-274,304-344`；`src/shared/constants/entitlements.ts:47-54` | 用户端不改变倒计时，结束/异常保留银行和历史入口；绝对时间24h结束标签；成果用响应值，统计数字/标签 | 管理员一次补救计次、到期缓存、准确结束时刻及M36真视觉需API/运行确认 |
| 四类反馈/上下文/300字/压缩单图，14.1-14.2 L659-L669 | D8 M40 | `feedback-form.ts:28-58`；`components/feedback-form.vue:119-157`；`screenshot-picker.ts:16`；`feedback-service.ts:10-27` | 分类白名单、长度、MIME/5MiB/单图校验，需scene/page_label；系统压缩选图、上传受控凭证 | 截图内容安全/二维码拒绝、各种入口自动附带精确句子/词条、压缩实大小需后端/设备证明 |
| **反馈前后端敏感检查，14.2 L670-L671** | **D8 M43有拦截态，M40只有普通表单；没有敏感标准/前置时序** | **`feedback-form.ts:28-58`仅常规字段；`components/feedback-form.vue:125-157`直接上传/create，166-171处理服务端FEEDBACK_CONTENT_BLOCKED；全src查敏感/CONTENT_BLOCKED没有提交前检查实现** | **偏离：客户端没有提交前敏感文本检查，不能因服务端拦截/设计拦截页即认前后端完成** | **具体规则需对齐既有服务端策略与需求；本轮不造敏感词表或新接口。服务端库/日志不保留拦截原文由API审计** |
| 反馈状态/补充/SLA，14.3-14.5 L675-L694 | D6/D8 | `feedback-form.ts:67-83`；`feedback-service.ts:25-27`；`components/feedback-detail-view.vue` | 补充入口按NEEDS_SUPPLEMENT且管理员reply显示；补充/回复在同条反馈详情/时间线 | 最大2轮、等待暂停、重新48h、管理员200字模板由后台/API证明；未深入每个状态运行 |
| 站内消息/红点/7天一次重开，14.6 L698-L703 | D6 M42；D8 | `message-service.ts:10-11`；`profile-dashboard.vue:71-74,151-158`；`feedback-detail-view.vue:94-98`；`feedback-form.ts:88-107`；`feedback-resolution-actions.vue:47-58` | 已读用markRead不删消息；源feedback未读计算；RESOLVED后7天和reopen_count<1展示，两行重开文案/原因；最终动作走API | 后台outbox实际投递、首页铃铛/多端红点清除、服务端窗口拒绝需全链路验 |
| 截图30天/注销提前删，14.7 L707-L709 | D8不含保存期限 | `src/features/feedback/upload-service.ts:13-37`；`feedback-service.ts:14-19` | 有图片上传/受控凭证消费，生命周期不由客户端计时 | 关闭后30天清理/重开计时/注销实际删除不在前端审计结论 |
| 四模块配置/全关/直接路由，17.1 L933-L939 | D1；画板不证明功能能力 | `catalog-presenter.ts:53-95`；`use-learning-page.ts:98-103`；`src/features/short-reading/use-short-reading-page.ts:31-48,54-91` | 前端按服务端enabled/顺序；短文直接路由先重取模块，关闭拒绝；场景受限请求委托服务器 | 清缓存/服务端即时拒绝/全关UI真实状态待API/运行；长难句grammar只toast无完整路由，**未交付**，17.1允许能力未完成默认关闭 |
| 开放3篇/预览配置，17.2-17.3 L943-L954 | D1 | `catalog-service.ts`；`catalog-presenter.ts:56-94` | 使用服务端catalog/preview开关结果，不需客户端重新打包替换开放内容 | 配置当前值/三篇先替换后下线/预览总开关与二级入口未线上读取 |
| 契约与资源鉴权，19.1 L1026-L1032；19.5 L1072-L1079 | D2/D4/D7 | `scene-service.ts:12-37`；`favorite-service.ts:15-49`；`short-reading-service.ts:24-140`；`src/shared/contracts/scene-snapshot.ts` | 引用ID/版本，收藏快照端点，source-resolution，独立短文路由/进度与音频取址；客户端并非直接云AI文本调用 | BFF真实DTO/每次鉴权/任何public资源泄露须独立API审计；不因类型定义认接口已上线 |
| 隐私最小化/日志/临时录音，20.1-20.2 L1098-L1110 | D8用途说明；D3当次录音不评分 | `components/contact-form.vue:60-75`；`src/stores/feedback-draft.ts:7`；`recording-controller.ts:269-289`；`src/shared/utils/redact.ts:5-34` | 表单明确主动同意，草稿不持久化敏感正文；微信日志字段脱敏工具；录音会话清理存在 | 工具存在不能证明所有日志/导出完整脱敏；供应商处理留存/隐私说明所有期限字段、截图用途与存储生命周期未全面运行 |
| HTTPS/密钥/会话/资源角色，19.6/20.4 L1083-L1090,L1119-L1127 | 无对应实际安全设计，规则以需求为准 | `src/services/runtime.ts:36-74`；`src/services/http/client.ts:26-31,47,80-99`；`scene-service.ts:16-29`；`pronunciation-service.ts:10-24` | HTTP会话单飞刷新/身份代次防旧请求，受限资源短签名，受控ID；test构建不含localhost且mockfalse见manifest | 未进行包内所有凭据系统扫描、TLS生产覆盖/真实云资源/CORS测试；不能称安全验收全过 |
| 异常与降级，23 L1180-L1211 | D3/D6/D8有失败/拦截参考，网络错误本轮仅源码 | `use-home-page.ts:67-82`；`use-scene-page.ts:163-175`；`feedback-form.vue:144-173`；`short-reading-controller.ts:67-79,244-287` | 登录失败空学习数据，受限失权清正文/音频；上传失败保草稿、无伪成功；短文播放失败不作为读完门槛 | 所有23章异常没有逐一注入，离线恢复/签名过期/服务器故障/版本冲突真设备仍待验 |
| 核心验收24.1-24.7 L1216-L1339 | D1-D8按已读范围 | 上述对应源码和下述日志 | 本矩阵逐功能映射，不用自动化总通过数替代每个业务验收 | 身份联系/多义快照/到期授权/音频听验/反馈投递/清理任务仍需完整业务与设备验收 |
| 短文独立纯阅读列表/安全预览，30.1 L1628-L1632；30.5 L1662-L1667 | D7；列表实际context缺证 | `src/pages.json` short-reading子包；`short-reading-service.ts:24-87`；`use-short-reading-page.ts:31-48`；`src/sub-packages/short-reading/preview.vue:18-25` | 独立list/detail/preview；安全summary白名单，previewOnly不open正文/写进度；模块路由闸门 | 默认关闭配置/逐篇无限试读与未授权不返正文/定时生效由API确认 |
| 短文译文/标签/全文播放，30.3 L1646-L1651；33.4 L1923-L1926 | D7 | `short-reading-controller.ts:30-48,185-193,217-241`；`components/short-reading-page-view.vue:104-125,146-211,284-287` | 标签两项独立本地toggle不筛选跳转；译文一次整篇；自然段；v-if whole_audio仅一个全文播放器；无音频无占位，无词卡/点词/单词发音/短文收藏主组件 | 标签播放/译文时保留再入reset、真实长文和有无音频版式待设备运行；平板不在本轮范围 |
| 短文独立进度/读完/版本/失权，30.4 L1655-L1658；30.5 L1664-L1666 | D7 | `short-reading-service.ts:91-140`；`short-reading-controller.ts:82-177,197-214,244-293`；`use-short-reading-page.ts:54-91` | 独立progress/complete，命令带content_version，读完无播放译文前置；revoke/suspend停播清正文；来源更新仅确证定位 | 短文不计场景/三开放里程碑/有效日、重读幂等、发布/回退同版资料需API与迁移实账 |
| 短文发布与四项目职责，30.2/30.6/30.7 L1636-L1642,L1669-L1696 | D7只涉及前端阅读 | 前端service/schema校验，后台发布不是本项目职责 | 小程序已具纯阅读主链的源码能力，不能因模块开关或截图直接宣布上线 | 独立模型迁移/版权人工校对/有音频确认/无词库候选等由后台/API审计；真实内容未正式确认发布 |
| 全部界面附录，33 L1887-L1926 | D2-D7为20261011实际context；D8当前承接节点 | 上述场景、银行、详情、翻卡、profile、completed、short-reading组件 | 最新关键视觉参数和交互结构可在源码对应；需求390基准/375自适应，不以静态高度强行裁切 | 本轮未新截图、未当前H5/真机逐画板像素验收；旧39 phone matrix不能作为全部状态当前视觉合格，平板除外 |

## 两项必要后续整改范围（设计/需求/源码三方依据）

1. **一次联系提示时点**：以需求9.5 L457的第三开放场景完成事件为目标，复用既有M28用途文案与稍后/完善动作、既有服务端资格和真实曝光幂等接口。保持其他完成成果/导航/权益规则；不将填微信变为权限条件。M28实际背景在档案，没有静态完成触发信息，不能声称Figma已规定触发挂载页；不回退最新M27身份卡。
2. **反馈前置敏感检查**：需求14.2 L670明确前后端均检查；M40/M43证明表单和拦截态存在。当前前端只有分类/长度/单图格式与服务端拒绝处理。后续先读取并对齐既有服务端规则/安全契约，再落实提交前检查与提示；不能自造敏感词表、新接口或把静态画板当安全服务。

不把以上项扩大成长难句/语法页面建设、其他视觉重构、付费云调用或真实用户删除操作。本轮仍只读，**没有修复代码**。

## 本地检查证据与版本环境

本轮不重复全量检查，仅读取已有同HEAD证据。

| 证据 | 实际内容 | 能证明/不能证明 |
|---|---|---|
| `D:\个人\juya\.tools\v15-phone-20261013\miniapp-build-manifest.json:2-8` | commit=2132dbb4a01723589bb338caecdc8f40cf7ee071；output为隔离构建source的dist/build/mp-weixin；test_api_present=true、localhost_present=false、mock_false_present=true | manifest记录与当前源码HEAD相同的test构建；本轮没有重新校验每个产物sha256，没有推断当前线上部署版本 |
| `miniapp-check.log:1-7` | format:check、eslint、stylelint、vue-tsc；All matched files use Prettier code style，日志到typecheck | 已有本地静态检查日志；本轮未重跑。不代表设计/真机/服务器契约完备 |
| `miniapp-test.log:231-234` | 141个测试文件、746个测试通过，Start20:13:51，Duration18.64s | 20261013目录历史自动化结果；含单元、Mock流程、静态契约，不能称真实API/微信E2E |
| `miniapp-test-build-direct.log:1-5` | Compiler5.26（vue3）；Build complete；16页page-meta均固定首节点、组件无page-meta | test微信包生成/结构检查已有成功证据；非微信开发者工具实际运行、真机音频/网络、云端验收 |

`miniapp-build.log`不存在，真正读到的构建日志是`miniapp-test-build-direct.log`，不编造不存在日志。此前engine-mismatch文件不是最终成功日志，不能用它把当前同HEAD最终测试写成失败，也不能隐藏环境差异。

## 尚未验收与不可混淆

- 源码审计：已完成本报告范围的矩阵和两项偏离确证；个别深层状态已明确缺证，没有借历史描述作current proof。
- 本地：已读同HEAD既有静态/141文件746测试/test包日志。本轮未再运行全量、浏览器或真机。
- 云端：未部署；未读线上模块配置、权益、数据库、截图生命周期和云音频/AI账单；无付费调用。
- 业务：未提交真实反馈、未删除账号/学习数据、未开通权益、未发布场景或短文；人类听音、版权/人工校对、场景正式确认发布继续暂缓。
- 微信iPhone/安卓375/390/414、审核降级、播放切换/回听首尾完整、长文本安全区、无网重连、到期缓存、跨账号越权和消息闭环均需后续专项业务验收。
- 无须恢复已取消的“上传按钮不存在”问题；本报告没有以旧设计名称或metadata冒充可见按钮/功能。
