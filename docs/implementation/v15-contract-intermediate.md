# V1.5 消费契约中间基线

本次实际运行时导出168个操作，见 `docs/contracts/v15-openapi.json`、`v15-routes.md`。
这是可供客户端立即生成的中间基线；新增SQL和全套门禁仍在运行，不标记最终READY。
最低schema为0018。共享环境升级、部署与其他仓库快照由协调对话处理，本仓未执行。

新增已装配接口包括：learning-features真实录音开关（缺失false）、learning-modules
名称/排序/实际readiness、short-readings运营sort-order、audio-upload-target/bind、
revisions/sentences/slice-state、content-imports/limits、resources/preview及受控字节URL、
content-stages状态与明确失败重试、pronunciations管理状态/固定预览/留存恢复，以及
publication-ranges预览token、显式范围确认与恢复结果。候选GET返回实际revision_id。

内部新增 `POST /internal/v1/content/{content_type}/{content_id}/preview-cover/signed-url`，
正文仅user_id；按当前本人visible权限、当前已发布COVER绑定、安全审核和实际HEAD
签发最多60秒URL。返回url/expires_at/asset_id/role=COVER，no-store。无封面404，
关闭模块/账号/内容不可见403，正文和音频仍需要full_access。学生不可直接调用。

管理写入要求现有管理员写权限/CSRF依赖以及Idempotency-Key，读取要求管理员会话。
导入预览只读原包中经校验引用的PNG/JPEG/WebP与实际音频，URL绑定管理员/批次/SHA，
60秒失效，不持久化URL，不等于素材安全审核或人工听审完成。

失败重试须当前fence/input_hash/config_version和manual_confirmed，音频/收费阶段另需
expected_edit_version；已成功不重置，UNKNOWN收费不盲目提交，明确已收费失败需
acknowledged_charged_retry。范围发布失败恢复仍使用原冻结范围的显式选中确认接口，
不让通用retry自动扩大选中范围。

发布范围冻结content_id/revision_id/版本/hash/warnings和管理员，确认显式选中IDs。
worker逐项结果保存，成功项保留；响应丢失后按固定逐项幂等回执恢复。最终发布事务
再次复核当前草稿、编辑版本和摘要。预览token15分钟过期，重放先于失效检查。

收藏历史旧版本及旧类型修复正在真实SQL验收；未知历史仍缺失，不拿当前词库补旧快照。
真实云/版权/人工听音/微信设备/生产验收保留外部边界。
