# V1.3 内容改版实施计划

> For agentic workers: 使用 executing-plans，独立领域按 dispatching-parallel-agents 协调，关键行为执行测试先失败再通过。

Goal: 三仓新需求闭环，小程序前端延期。
Architecture: 现有双后端、MySQL/Redis/Celery/OSS，固定发布快照。
Spec: design.md

## Global Constraints

main 中文提交；不清库、不修改小程序前端、不推送、不部署生产。

## Review Focus

额度并发与worker重复投递；发布与草稿保存竞态；音频候选确认不改线上；跨版本资源鉴权；无权限预览字段泄漏。

## Tasks

- [ ] 1. 需求对照、结构化 schema、词库与素材/OCR迁移；typed-content 测试 RED/GREEN。
- [ ] 2. 内容目录创建、词库版本、稳定句子/来源、可信素材、上传独立于OCR；HTTP保存/素材测试 RED/GREEN。
- [ ] 3. 百度HTTP provider、内部额度预占与认领、候选字段采纳；并发/重复调用测试 RED/GREEN。
- [ ] 4. 毫秒标时与真实预览、事实检查、事务发布固定引用；无音频/竞态测试 RED/GREEN。
- [ ] 5. 用户版本契约、资源签名、期限小写、不限量复习；跨版本权限/自然月/>10卡测试 RED/GREEN。
- [ ] 6. 批量实际执行、统计生产、生命周期与联调；真实MySQL/Redis/HTTP/browser 验证。

每项提交前检查差异与运行验证，记录 evidence.md；完成后整体独立审查。
