# SDD ledger — plan: docs/implementation/v13-content/plan.md

起点：admin-api e9d1e28；admin d7b3dff；miniapp-api b9b0224。

Ruling: 用户已批准计划且指定main，采用main直接实施。
Ruling: 技能建议重复方案确认被用户实施授权覆盖；不重复请求。
Pre-flight: 内容schema由主代理统一；media/前端/user分别限定文件所有权。

批次1：0015迁移在独立 juya_v13_root_test 完成 legacy 0008 → head 回归，2 passed；词库版本、素材尺寸/时长、OCR额度预占与匿名事件表只增量迁移。保留原文档，未清开发库。公共schema/片段/音频替换3测试通过。
