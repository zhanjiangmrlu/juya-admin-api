# V1.5 E01–E22 管理后端自动化证据映射

对照技术方案06的E01–E22；本表映射实际测试，不以管理后端单仓测试声明BFF/设备/云验收。
最终执行结果与覆盖率见v15-progress.md。SQL用例由UUID隔离MySQL8.4/Redis执行，
收费供应商和OSS边界使用明确fixture；音频解码与切片使用实际FFmpeg/ffprobe。

| 编号 | 本仓自动化证据（tests下路径） | 验收边界 |
| --- | --- | --- |
| E01 | integration/test_v15_import_flow.py、test_v15_scene_flow.py、test_v15_fixed_resources_flow.py、test_publication_adapter_sql.py | 导入/草稿/校对/发布/权威读取；两仓真实HTTP由协调集成 |
| E02 | integration/test_v15_import_entry_identity.py、test_v15_import_flow.py、test_publication_adapter_sql.py | 输入幂等、导入身份和旧预览/hash拒绝 |
| E03 | unit/content/test_v15_import_parser.py、integration/test_v15_import_entry_identity.py | 父级ID、重复条目键、义项/来源身份 |
| E04 | unit/content/test_v15_import_parser.py、unit/content_import/test_v15_sources.py | 路径穿越/链接/压缩炸弹/附件和实际格式限制 |
| E05 | integration/test_v15_materials_sql.py、test_v15_import_admin_operations.py、test_v15_fixed_resources_flow.py | 可选素材与角色校验；词卡图/语块无图规则 |
| E06 | integration/test_v15_audio_pipeline.py、test_v15_audio_authoring_flow.py | 实际独立切片/SHA/时长、失效、刷新恢复；人工听审与设备另验 |
| E07 | unit/content/test_v15_proofreading.py、integration/test_publication_ranges_sql.py | N=0/1/20/100/1000公式及1000真候选固定抽20，重放不重抽 |
| E08 | integration/test_v15_thousand_import.py、test_v15_import_stage_concurrency.py、test_v15_worker_dispatch.py | 单批1000实际导入，999成功保留/1失败恢复、取消/租约/重放不读包 |
| E09 | integration/test_v15_access.py、test_v15_fixed_resources_flow.py、test_v15_favorite_legacy_resources.py | 上游本人有效快照/取消授权；收藏写入并发由BFF自有测试 |
| E10 | integration/test_v15_favorite_backfill.py、test_v15_favorite_legacy_resources.py | 已知旧资源同源成对固定、v1与当前v2、未知保持缺失 |
| E11 | integration/test_v15_fixed_resources_flow.py、test_v15_favorite_legacy_resources.py、test_admin_security.py | 他人/任意资源/旧身份/错误角色/失效签名拒绝 |
| E12 | integration/test_v15_fixed_resources_flow.py、test_v15_learning_features_sorting.py、test_v15_profile_completion_features.py | 撤权/下线/模块关闭禁全文；历史复习独立授权，录音与资料入口独立配置 |
| E13 | integration/test_v15_pronunciation_concurrency.py、test_v15_pronunciation_admin.py；unit/pronunciation | 上传优先、缓存身份、状态/竞争/本地留存恢复；零真实收费 |
| E14 | integration/test_v15_cloud_usage_sql.py、test_v15_candidate_paid_retry.py；unit/pronunciation/test_worker.py | 硬预算并发、UNKNOWN不重提、明确收费失败新账本、自动重试上限 |
| E15 | integration/test_v15_short_reading.py、test_v15_short_reading_upload_targets.py | 独立正文/译文/FULL音频；学生进度及纯阅读交互由BFF/前端 |
| E16 | integration/test_v15_fixed_resources_flow.py、test_v15_learning_modules_sql.py | 试看/套餐/关闭/下线权威、仅visible封面；学生开通与历史由BFF |
| E17 | integration/test_formal_entitlement_concurrency.py、test_entitlement_campaign_queries.py；unit/formal_entitlements/test_natural_month.py | 自然月/永久、续期/预览/幂等重放 |
| E18 | integration/test_limited_entitlement_concurrency.py、test_v15_operations.py；unit/limited_entitlements | 3/5日/自定义窗口、null容量与最后名额并发 |
| E19 | integration/test_admin_contact_schema.py、test_contact_legacy_backfill.py、test_admin_security.py；e2e/test_internal_core_flow.py | 联系状态与内部服务授权；真实微信登录由BFF/配置环境 |
| E20 | integration/test_feedback_concurrency.py、test_feedback_admin_queries.py；unit/feedback | SLA/往返/截图补充与清理；客户端展示另验 |
| E21 | integration/test_deletion_cleanup_sql.py；unit/user_projection/test_deletion_cleanup.py | 七日注销、取消/恢复、回调/引用清理；BFF入口另验 |
| E22 | integration/test_v15_closeout_migration.py、test_v15_favorite_backfill.py、test_admin_contact_schema.py、test_v15_bounded_json.py | 真实旧库升级，旧快照/hash/收费账本保留、旧读兼容、完整JSON回读；禁止破坏性downgrade |
| E23 | integration/test_v15_favorite_identity_migration.py、test_v15_schema.py；unit/content/test_favorite_identity_offline_migration.py | 收藏身份含内容类型/ID且不含版本；旧行/引用/账本保留，范围不明拒绝升级，碰撞拒绝降级，DDL中断可核验续跑，offline拒绝；BFF查询/UI隔离另验 |

仍须外部验收：版权与人工听音、真实AI/ASR时间和腾讯音色/收费实账、安全供应商、
真实OSS浏览器CORS/Range、微信AppID/设备与生产部署。不以fixture成功计为这些完成。

千篇量化证据：`evidence/v15-thousand-import-metrics-20261011.json`，说明见
`v15-thousand-import.md`。实际1000篇/1000句，ZIP整体53162 bytes/解压52918 bytes，
无媒体且已知fixture云调用0；validate166.255秒、confirm61.897秒、单项恢复2.065秒
均只有一次整体采样。metadata SQL p95为1.096ms(n20)，逐项保存SQL p95为44.771ms
(n1000、不含外围commit)。Python tracemalloc峰值95,985,104 bytes；进程中途系统峰值
另列，不能等同整个worker/数据库峰值。实际HTTP详情和worker查询并发p95仍未测。
