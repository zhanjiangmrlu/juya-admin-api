# 第六批匿名统计接口与后台验收

2026-09-30，第六批基于前五批已提交实现增加匿名统计查询，并验收 A01–A26。管理端统计接口实现基线为 `985552e`，前端联动基线为 `36db825`。本报告覆盖业务接口和 local/test 媒体流程，不代表真实 OSS 或生产 OCR/TTS 供应商验收。

## 统计查询契约

`GET /api/v1/admin/analytics` 要求管理员会话 Cookie。查询参数 `start`、`end` 必填，格式 `YYYY-MM-DD`；`period` 为 `day|week|month`，默认 `day`。起止日期包含端点，须正序，最多包含 366 天；额外查询参数会被拒绝。

响应为 `AnalyticsResponse`，包含 `period`、`timezone=Asia/Shanghai`、原始 `start/end`、`rows` 和 `ratios`。计数行包含 `day/metric/dimension/value`；比率行包含 `day/metric/numerator/denominator/rate/basis`。完整操作清单及 47 个相关请求/响应类型见 [接口文档](../../../doc/接口文档/juya-admin-api-接口文档.md) 的第 11 节。

- 日聚合表的日期视为北京时间自然日，不随服务器本地时区转换。
- 自然周从周一开始；自然月从每月 1 日开始。跨年周的起点可以在前一年。
- 首尾周期只包含查询区间内的数据，不扩展到完整周/月；没有数据返回空数组。
- 同周期、指标和维度的计数相加。跨日 ACTIVE_USERS 是日级快照计数合计，不是跨周期去重人数或学习 DAU。
- 缺失图表桶使用 null，不补 0；不同序列共用同一日期轴，不错位。
- 比率采用累计分子 / 累计分母，不能对每日百分比取平均；分母 0 时 rate=null，显示“无分母”。分子大于分母、缺一项或无定义口径返回 `422 ANALYTICS_RATIO_INVALID`。

| 指标                | 分子 / 分母口径                          |
| ------------------- | ---------------------------------------- |
| CONTACT_FUNNEL      | 填写次数 / 提示曝光次数                  |
| LIMITED_STARTS      | 首次启动人数 / 开通人数                  |
| LIMITED_COMPLETIONS | 到期前完成人数 / 首次启动人数            |
| FEEDBACK_SLA        | SLA 内处理数量 / 纳入 SLA 统计的反馈数量 |

比率必须来自明确的 NUMERATOR 和 DENOMINATOR 行。现有 daily producer 仅生成 NEW_USERS、ACTIVE_USERS、FEEDBACK_SLA、DELETIONS 的 ALL 计数，没有上述成对数据。本批不扩展事件采集器、不将旧计数伪造成比率；生产者尚未写入的指标或比率不展示。实际 worker 的 ACTIVE_USERS 查询为 `account_status='ACTIVE'` 的账号快照，不是学习行为活跃人数。HTTP 比率验收使用隔离测试库的明确种子数据。

每天北京时间 01:00 的 worker 对已结束的前一个自然日重算 NEW_USERS、FEEDBACK_SLA、DELETIONS，不再只统计执行当天的凌晨一小时。ACTIVE_USERS 单独按执行当天写入实时状态快照，保留实际 generated_at；不会用今天的状态伪造昨天快照。`run_aggregate_daily(metric_day=date(...))` 可显式重算指定已结束日期的三个事件计数，不改写历史 ACTIVE_USERS 或其他生产者的维度/比率行。已有错误历史计数需要运营明确指定日期回补，不能由本批验收自动修改线上数据。

## 匿名边界

服务端查询和既有导出共用失败关闭的隐私校验；前端 adapter 在渲染和下载前再次校验。未知指标、负数或前端非安全整数、非法日期、未批准维度、比率不一致均不可展示/导出。

前端还校验日期桶位于请求区间对应的自然周期内、计数键和比率键唯一、每个比率与 rows 中的分子/分母严格一致，以及成对计数不存在遗漏比率。A26 在初次读取中或失败后禁止编辑/保存，失败可以重新读取；必须取得全部五个配置键的有效版本才能提交，409 仍保留本地草稿。

允许指标为 NEW_USERS、ACTIVE_USERS、SCENE_COMPLETIONS、CONTACT_FUNNEL、FORMAL_ENTITLEMENTS、LIMITED_STARTS、LIMITED_COMPLETIONS、FAVORITES、REVIEWS、FEEDBACK_SLA、DELETIONS。允许维度为封闭的汇总枚举，以及 `scene|series|package|campaign:<1–64 位字母数字下划线短横线>`；内容前缀仍拒绝 user、wechat、openid、phone、mobile、email、nickname、juya、screenshot、trajectory、wxid 等个人标识标记。具体维度枚举以 analytics/service.py 和前端 analytics-model.ts 为准。接口不接受用户维度筛选，不返回微信号、用户 ID、截图或个人轨迹。

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

关键真实网络测试 `tests/e2e/test_full_admin_acceptance_http.py` 启动临时 loopback Uvicorn 和 solo Celery worker，实际连接隔离 MySQL/Redis：登录、未认证 401、日周月统计、CSRF 403、配置旧版本 409、反馈解决后工作台 open/overdue 计数各减 1 且待办消失、OCR 任务 SUCCEEDED 和候选 READY。测试显式种入已确认图片资产，没有连接 OSS。生产 Secure Cookie 未放宽；仅 loopback HTTP 测试客户端手工携带本次登录令牌，不构成生产 HTTPS 浏览器联调证明。

## 复现与边界

admin-api 普通门禁为 `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy src`、`uv run pytest -q`。SQL/worker 测试需显式设置 `JUYA_TEST_DATABASE_URL` 和 `JUYA_TEST_REDIS_URL` 为隔离测试资源；测试会迁移 schema 并创建、清理测试行，禁止配置为生产库。完整文档契约测试在包含 juya-admin 和 doc 的同级工作区验证生成快照与实际 app.openapi 相等及文档操作集合相等。

admin 门禁为 `pnpm check`、`pnpm test`、`pnpm build`、`pnpm exec playwright test --project=chromium`；miniapp-api 门禁同上 Python 四项。API DTO 快照来自实际 FastAPI OpenAPI，生成文件不手改。

本批没有触及第七批 OSS 生产接入、生产资源/权限、永久媒体地址、学习行为采集扩展、账号体系升级或发布推送。批次验收完成不能替代第七批真实 OSS、OCR/TTS 供应商以及生产 HTTPS 全链路验证。
