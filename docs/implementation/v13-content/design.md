# V1.3 内容生产与音频改版技术方案

日期：2026-10-01。用户已批准完整计划：三个项目 main 开发、小程序前端延期；不默认推送或部署。

## 数据与接口契约

复用 scene/scene_revision、audio_target/audio_version、media_asset 和版本快照。草稿 version 是乐观锁 edit_version；发布 content_version 独立，revision_id 固定不可变。同一版本固定所有资源版本，不追随 audio_target.active_version_id。

结构化 content 字段：title_en、title_zh、summary、tags、original_image_asset_id、cover_asset_id、copyright、source、audio（target_id/version_id/asset_id/duration_ms，允许 null）、dialogue、vocabulary、chunks。草稿全部允许未齐；发布强校验。

dialogue 行：id、speaker、english、chinese、start_ms、end_ms（允许 null）、audio_version_id（允许 null）、timing_confirmed（默认 false）、clickable_spans（服务端生成）。句子 id 由服务端或客户端创建稳定 ULID/UUID；移动或修改文字保留 id。更换 audio.version_id 清空全部标时与确认。

vocabulary/chunks 行：entry_id、entry_version、english、variants、phonetic、chinese、explanation、source_sentence_ids、icon_asset_id、audio_target_id、audio_version_id（后三项可空）。全局词库按类型与规范英文复用，版本不可变；草稿引用固定版本。片段含 entry_id/entry_version/source_locator/start/end，按 Unicode 字符偏移，语块优先，跨行人工配置落点。

管理 content 路由：GET/POST /series；POST /scenes（series_id/template_type）；既有 revisions GET/PUT（content 类型化）；GET/POST /lexicon、PUT /lexicon/{id}；preview/publish-checks/publish 使用同一草稿和实时事实。发布请求 expected_version 防检查后保存竞态。media 路由保留当前上传策略、确认、音频目标与版本接口，增加素材 metadata、管理员试听、安全额度与 OCR 状态。

public scene 使用 scene_id/revision_id/content_version/content，content 为上述公开白名单。词卡须携带 revision_id、entry_version、source_locator。新增场景资源签名接口必须确认资源属于该版本并重验权益。只读预览无原图/正文/音频/时间点/片段。

## 实施与验收

批次1模型与迁移；2统一编辑/词库/实际素材校验；3百度OCR及额度；4标时/预览/原子发布；5用户后端与统一枚举/不限量；6批量实际执行、统计生命周期、全链路。

百度OCR默认关闭。仅服务端读取管理员学习原图，调用 general，language_type=CHN_ENG、paragraph=true、probability=true。内部额度数据库事务预占；worker 认领一次，超时计数且不重试；人工重试使用新幂等键。候选字段选择采纳与 expected_version 校验。

可信素材验证计算字节哈希、真实解码尺寸/时长，独立内容安全未通过不得确认。整段音频和时间点必填；独立词条发音可空。本期禁止TTS执行。

测试：三个项目完整质量门禁、独立MySQL/Redis、真实loopback HTTP、浏览器1440×900/1280×800。外部凭据/额度/CORS/内容安全验收缺失独立报告，小程序前端与微信真机延期。
