# 第六批匿名统计接口与后台验收

2026-09-30，第六批基于前五批已提交实现增加匿名统计查询，并验收 A01–A26。管理端统计接口实现基线为 `985552e`，前端联动基线为 `36db825`。本报告覆盖业务接口和 local/test 媒体流程，不代表真实 OSS 或生产 OCR/TTS 供应商验收。

## 统计查询契约

`GET /api/v1/admin/analytics` 要求管理员会话 Cookie。查询参数 `start`、`end` 必填，格式 `YYYY-MM-DD`；`period` 为 `day|week|month`，默认 `day`。起止日期包含端点，须正序，最多包含 366 天；额外查询参数会被拒绝。

响应为 `AnalyticsResponse`，包含 `period`、`timezone=Asia/Shanghai`、原始 `start/end`、`activity_basis`、`rows` 和 `ratios`。计数行包含 `day/metric/dimension/value`；比率行包含 `day/metric/numerator/denominator/rate/basis`。完整操作清单及 47 个相关请求/响应类型见 [接口文档](../../../doc/接口文档/juya-admin-api-接口文档.md) 的第 11 节。

- 日聚合表的日期视为北京时间自然日，不随服务器本地时区转换。
- 自然周从周一开始；自然月从每月 1 日开始。跨年周的起点可以在前一年。
- 首尾周期只包含查询区间内的数据，不扩展到完整周/月；没有数据返回空数组。
- 行为日活以成功学习或复习操作按北京时间每日去重。完整自然周/月范围分别读取 WEEK_ACTIVE_USERS/MONTH_ACTIVE_USERS，并映射为 ACTIVE_USERS 独立人数；`activity_basis` 为 DAILY_USERS、CALENDAR_WEEK_USERS、CALENDAR_MONTH_USERS 或 PERSON_DAYS。自定义首尾未对齐自然周期的周/月范围合计日活，明确标为活跃人日，不能当作独立人数。自然周期计数来自每用户每周期首次活动事件；新采集启用之前的历史不伪造去重人数。
- 缺失图表桶使用 null，不补 0；不同序列共用同一日期轴，不错位。
- 比率采用累计分子 / 累计分母，不能对每日百分比取平均；分母 0 时 rate=null，显示“无分母”。分子大于分母、缺一项或无定义口径返回 `422 ANALYTICS_RATIO_INVALID`。

| 指标                | 分子 / 分母口径                          |
| ------------------- | ---------------------------------------- |
| CONTACT_FUNNEL      | 填写次数 / 提示曝光次数                  |
| LIMITED_STARTS      | 首次启动人数 / 开通人数                  |
| LIMITED_COMPLETIONS | 到期前完成人数 / 首次启动人数            |
| FEEDBACK_SLA        | SLA 内处理数量 / 纳入 SLA 统计的反馈数量 |

2026-10-01 V1.3 更新：比率来自明确的 NUMERATOR 和 DENOMINATOR 行，采用业务事件的开通、启动、反馈创建、提示曝光等 cohort 日期归属。限时 3/5 天分别提供 MODE_3/MODE_5 计数和比率组件。首次响应时长返回累计秒数 / 首次响应数量，`unit=seconds`；其他比率 `unit=ratio`。联系方式填写率与撤回率、三开放场景全部完成率、反馈解决/重开/超时率均由真实成功操作追加的事件生成。

`analytics_event` 的事件 ID 和业务 event_key 唯一；成功操作与追加事件在同一事务提交，重复操作不重复计数。事件类型覆盖账号新增、行为活跃、场景开始/完成/回看、开放场景启动/完成、联系方式提示/填写/撤回/状态、正式权益开通/状态/到期、限时权益开通/启动/到期/未开始失效/完成/状态、收藏、复习、反馈创建/首次响应/补充/解决/重开/超时/状态，以及注销申请/撤回/生效。

每天北京时间 01:00 重算前一个已结束自然日。`run_aggregate_daily(metric_day=date(...))` 可幂等重算任意已结束日期，日级锁与删除/重写在同一事务内；并按事件原始日期和 cohort 日期恢复计数与比率。定时执行还根据前日事件中的 cohort 日期自动刷新受影响历史转化率，避免后续启动、完成或响应使旧统计长期过时；显式回补不递归。CONTACT_STATES、FORMAL_STATES、LIMITED_STATES、FEEDBACK_STATES 是执行日状态快照，周/月展示采用周期最后一次实际快照，显式历史回补保留已经保存的历史状态快照，不拿当前状态伪造过去。日/周/月活跃计数均来自行为事件，账号 ACTIVE 状态不再参与活跃计算。

## 匿名边界

服务端查询和既有导出共用失败关闭的隐私校验；前端 adapter 在渲染和下载前再次校验。未知指标、负数或前端非安全整数、非法日期、未批准维度、比率不一致均不可展示/导出。

前端还校验日期桶位于请求区间对应的自然周期内、计数键和比率键唯一、每个比率与 rows 中的分子/分母严格一致，以及成对计数不存在遗漏比率。A26 在初次读取中或失败后禁止编辑/保存，失败可以重新读取；必须取得全部五个配置键的有效版本才能提交，409 仍保留本地草稿。

允许指标按 `analytics/service.py` 的 EXPORTABLE_METRICS 封闭清单校验，涵盖上述事件计数、状态快照、自然周期活跃及定义明确的比率。允许维度为封闭的汇总枚举，以及 `scene|series|package|campaign:<1–64 位字母数字下划线短横线>`；内容前缀仍拒绝 user、wechat、openid、phone、mobile、email、nickname、juya、screenshot、trajectory、wxid 等个人标识标记。事件 payload 仅允许 mode、匿名状态/分类、非负秒数/轮数、布尔归属标记和 ISO cohort 日期，不允许微信号、用户属性、截图或轨迹正文。账号注销时 user_id 置空、event_key 改为匿名 ID，保留匿名聚合及周期时间；无可追溯用户 token。

V1.3 本范围最终验证：用户 API Ruff、格式、mypy（96 个源文件）通过，隔离 MySQL/Redis 完整 pytest 102 通过，1 项真实 OSS 凭证测试跳过；管理后端本范围 terms/events/period/producer/maintenance/formal concurrency 隔离 33 通过。SQL 用例验证 125 条复习队列完整分页、不同修订来源快照、业务事件幂等、连续两天活动 2 日活/1 周活/1 月活、注销匿名化、跨日 cohort 比率、重算幂等和隐私拒绝。下表保留 2026-09-30 的历史验收证据；本期管理后端全套结果见 V1.3 交付报告。

## 覆盖证据

| 验收项              | 结果                                                                                                 |
| ------------------- | ---------------------------------------------------------------------------------------------------- |
| 当前 OpenAPI        | 89 个操作：76 管理端、11 内部、2 健康检查；文档 89/89，无隐藏业务路由                                |
| 前端能力            | 33/33 available；测试逐项关联真实 OpenAPI 操作，26/26 路由加载具体页面组件                           |
| admin-api           | Ruff、格式检查、mypy 通过；完整 pytest 213 通过，含隔离 MySQL 和真实 HTTP/Redis worker               |
| miniapp-api         | Ruff、格式检查、mypy 通过；pytest 74 通过，8 个需另行配置数据库的测试跳过，代码未改动                |
| admin               | check、生产构建通过；Vitest 218 通过                                                                 |
| Playwright Chromium | 93 通过：41 个业务/布局测试 + 26 页 × 2 个视口                                                       |
| 双视口              | 1440×900、1280×800，共 52 个页面截图；无根节点横向溢出、未命名纯图标按钮、未知 API 请求或 pageerror  |
| Impeccable          | 一次集中检查、一次修正、一次确认；detector 仅 2 个原有布局 advisory（5px 圆角、10px 字号），无阻断项 |

截图保存在 `juya-admin/.impeccable/review/A01-1440x900.png` 等本地忽略目录，可用 `pnpm exec playwright test --project=chromium` 重建。此次没有将截图作为像素黄金基线，检查为布局/可访问性自动断言加人工集中审阅。最终审查后的全套重跑是正确性回归，没有开启新的视觉润色循环。Ruff/pytest 的现有 Starlette/httpx deprecation warning 未影响执行结果。

一名独立审查者提出的三项重要问题已在一次修正中用 RED→GREEN 回归覆盖：日生产者时间窗口、配置初始加载/失败保护、统计 rows/ratios/日期桶交叉一致性。暂缓两项小问题：A25 离开页面未主动 abort 尚未结束的查询；最后一次媒体任务测试出现一次 ResizeObserver loop 通知，业务断言通过，全导航和 52 个视口的 pageerror 检查通过。本批不追加无关布局重写。

历史 2026-09-30 关键真实网络测试 `tests/e2e/test_full_admin_acceptance_http.py` 启动临时 loopback Uvicorn 和 solo Celery worker，实际连接隔离 MySQL/Redis：登录、未认证 401、日周月统计、CSRF 403、配置旧版本 409、反馈解决后工作台 open/overdue 计数各减 1 且待办消失、OCR 任务 SUCCEEDED 和候选 READY。测试显式种入已确认图片资产，没有连接 OSS。生产 Secure Cookie 未放宽；仅 loopback HTTP 测试客户端手工携带本次登录令牌，不构成生产 HTTPS 浏览器联调证明。

## 复现与边界

admin-api 普通门禁为 `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy src`、`uv run pytest -q`。SQL/worker 测试需显式设置 `JUYA_TEST_DATABASE_URL` 和 `JUYA_TEST_REDIS_URL` 为隔离测试资源；测试会迁移 schema 并创建、清理测试行，禁止配置为生产库。完整文档契约测试在包含 juya-admin 和 doc 的同级工作区验证生成快照与实际 app.openapi 相等及文档操作集合相等。

admin 门禁为 `pnpm check`、`pnpm test`、`pnpm build`、`pnpm exec playwright test --project=chromium`；miniapp-api 门禁同上 Python 四项。API DTO 快照来自实际 FastAPI OpenAPI，生成文件不手改。

历史第六批未触及生产 OSS 接入、生产资源权限或发布推送；V1.3 的业务事件采集及匿名统计已按上文补齐。批次验收完成不能替代第七批真实 OSS、OCR/TTS 供应商以及生产 HTTPS 全链路验证。

当前 V1.3 该测试改为 OCR 关闭情况下的真实批量任务执行，未调用百度。全套结果为 admin-api 283 passed/3 skipped、miniapp-api 102 passed/1 skipped、后台 234 单元和 97 Chromium 通过，当前接口 104 个。真实 OSS 和跨服务 HTTP、浏览器 CORS 阻挡及供应商边界见 [V1.3 交付报告](../implementation/v13-content/evidence.md)。
