# 第六批执行与裁决记录

计划：`docs/superpowers/plans/2026-09-29-batch-6-analytics-acceptance.md`。2026-09-30，在用户授权的 main 执行，不创建 PR、不推送。

起点：admin-api `20bde0e`，admin `7c57d24`，miniapp-api `fb3dd96`。跨仓库分别记录提交，未改 miniapp-api。

## 完成情况

- Task 1 完成，`985552e`：匿名统计查询、生成 OpenAPI、失败关闭的隐私校验；缺失接口和个人维度接受问题均先 RED 后 GREEN；当时后端 208 项通过。
- Task 2 完成，admin `36db825`：A25 真实查询与导出、33 项能力全部 available、A13 请求竞态和 A26 冲突恢复；测试先 RED 后 GREEN，当时前端 172 项通过。
- Task 3 完成，admin-api `d82e566`、admin `094ff1d`：26 页路由、能力/OpenAPI 矩阵、89/89 接口文档、真实 HTTP/MySQL/Redis/Celery 验收、双视口。初次浏览器 90/92 暴露工作台高度错误，修正真实布局；图表单点可见性先 RED 后 GREEN。最终集中视觉确认完成，没有追加润色循环。
- 独立最终审查只有一次，没有 Critical；三项 Important 均接受：01:00 日聚合窗口、A26 初始读取保护、统计计数/比率/日期桶交叉一致性。
- 单次审查修正：三个跨年北京时间案例先失败再通过；统计 adapter 的八个不一致响应先失败再通过；配置挂起/失败禁止保存的单测和浏览器断言先失败再通过。加载恢复还覆盖 Element Plus 数字框初始 aria-disabled 不更新的问题，按 ready 状态重新挂载表单，重新读取后可编辑。
- 最终完整门禁：admin-api Ruff/check/format/mypy 通过，pytest 213；admin check/build 通过，Vitest 218，Chromium 93（41 业务/布局 + 52 双视口）；miniapp-api Ruff/check/format/mypy 通过，74 passed / 8 optional MySQL skipped。
- 原有 Starlette/httpx 废弃提醒不影响测试；媒体任务测试一次 ResizeObserver 通知记录为小问题。全导航和双视口的 pageerror 检查通过。

具体统计契约、复现条件和生产边界见 [验收报告](batch-6-analytics-acceptance.md)。复现测试保存在 Git，临时运行日志在完成提交后清理。

## Rulings I made

以下按发生顺序，包含选择以及错误时的代价。

1. Ruling: 多仓库计划不用单仓库执行脚本代替边界判断，分别保留起点并人工记录验证。代价：复审需要显式指定两个仓库的提交区间。
2. Ruling: 自然周用 ISO 周一，日字段按 Asia/Shanghai 民用日期处理，不按宿主机时区二次转换。代价：期待周日为起点的消费方需要适配。
3. Ruling: ACTIVE_USERS 跨日是快照计数合计，不宣称去重人数；比率必须有明确分子/分母。代价：旧计数数据在生产者补齐成对数据前没有比率。
4. Ruling: A13 陈旧请求和 A26 冲突关闭后的恢复属于计划明确的联动验收，按复现测试修复。代价：既有调用方会观察到更严格的异步生命周期行为。
5. Ruling: 真实生产 OSS/OCR/TTS 留在第七批。代价：本批不能证明生产媒体成功。
6. Ruling: 不发明比率、不增加学习事件采集或去重活跃人数；修正 01:00 统计日期属于现有数据正确性。代价：未提供源数据的指标继续缺失。
7. Ruling: 不宣称生产 HTTPS 浏览器会话通过；loopback 显式携带 Cookie 仅验证服务端。代价：部署仍需真实 HTTPS 会话验收。
8. Ruling: 事件计数重算前一完整日，账号状态快照单独记录执行当日；日期回补不伪造历史快照。代价：不同指标的采集日期并非同一日，旧事件计数需运营显式回补。

## Deferred minors

- minor (deferred): A25 离开页面未主动取消未完成查询；新查询会取消旧查询，不存在卸载后共享图表状态，但仍有可收敛的资源生命周期问题。
- minor (deferred): 媒体任务浏览器测试出现一次 ResizeObserver loop 通知，业务断言通过；本批不继续展开无关布局重写。
