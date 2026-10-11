# V1.5 场景人工确认与切片

`SceneAuthoringStore(sessions, proofreading, stages, prepare_slice=None, encoding_config=None)` 仅操作当前未发布 schema_version=2 场景，所有写入要求 edit_version CAS。接口由 `create_authoring_router(store, *, current_admin_write)` 注册，沿用管理员写权限与 CSRF。

`confirm_entry(revision_id,entry_id,expected_version,actor,manual)` 不采信客户端 confirmed；复核服务端 details_hash、entry／sense／source 固定身份，保存人工确认人与时间。每次确认建立新 immutable pronunciation_target，完整读音与 reading_variant、WORD／CHUNK、允许合成、上传固定目标与版本来自服务器当前条目；上传资源必须实际 CONFIRMED／安全 PASSED。保存英文长文本不按腾讯上限截断。释义编辑与 wording／new_sense 身份分配继续由根 v15_entries 保存助手执行。

`confirm_boundary(revision_id,sentence_id,expected_version,start_ms,end_ms,actor,manual)` 使用实际固定原音频数据库 SHA、object_key、duration、资源归属及审核状态，边界不能超过实际时长。保存人工确认人与 source version，清除旧 slice 引用，撤销旧校对确认；同事务建立 SENTENCE_SLICE StageStore 阶段和 CONTENT_SENTENCE_SLICE admin_outbox 事件。阶段身份绑定 revision＋sentence，避免不同内容中同名句子互相取消。

生产 worker 根据 payload.stage_id 获得阶段租约／fencing，核对 payload.input_hash、encoding_config 后调用 AudioPipelineService.split；必须由后台异步运行，不在 HTTP 接口运行 FFmpeg。`current_input(revision_id,sentence_id)` 与 `locked_input(session,revision_id,sentence_id)` 可直接注入 SQLAudioRepository，返回 SliceInput 实际固定资源及人工边界。SQLAudioRepository.commit_if_current 拒绝旧依赖写入。worker 应在预览前完成素材解码／安全审核准备，并提供可试听引用；不能让用户试听尚未安全通过的文件。

`confirm_slice(revision_id,sentence_id,expected_version,dependency_hash,actor,listened)` 要求人工实际试听。`prepare_slice(asset_id,resource_version)` 由根生产装配，必须复用真实 prepare_fixed_asset／审核／confirm_audio_version，确认接口本身不执行音频切片。回到锁事务再次校验 source／english／边界依赖，实际素材 CONFIRMED、PASSED、object_key 与 duration 后才能将 slice、audio version 标记 CONFIRMED 并绑定 snapshot.slice_target_id／slice_version／slice_dependency_hash。所有确认更新草稿版本并撤销旧校对，审计同事务提交。

单测覆盖边界实际时长与必须人工检查；隔离 MySQL 用例验证客户端 confirmed 无效、人工确认固定目标、身份不漂移、条目修订事实更新和重复旧 CAS 拒绝。真实云审核、实际试听与真机播放仍需外部验收。

管理员可通过 GET `/{revision_id}/sentences/{sentence_id}/slice-preview?dependency_hash=...` 试听尚未人工确认的 READY 切片。构造函数 `sign_slice(asset_id, expires_in)->str` 由根生产装配签私有链接，TTL固定60秒；`current_admin` 可单独注入读取权限，未提供时仍要求管理员写权限。响应 `Cache-Control: no-store`。服务在准备解码／审核前后两次重验当前源音频／正文／边界hash，JOIN实际切片、媒体、audio_version与target身份；仅实际 CONFIRMED/PASSED 固定对象可签发。返回READY不会写入snapshot或伪造已人工试听确认。
