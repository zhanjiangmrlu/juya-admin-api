# V1.5 句级音频与云候选

## 已实现

- `FFmpegSlicer.slice(bytes, source_sha256, Boundary)`：私有临时目录、50 MiB 输入上限、固定允许 MP3/M4A/WAV/AAC demuxer，禁止网络协议与播放列表。源 SHA256 一致后探测并严格完整解码；重编码 PCM 24 kHz 单声道 WAV 独立文件；再次 ffprobe、完整解码和实际时长检查。编码容差 30ms，不用压缩流 copy。
- `AudioPipelineService.split(revision_id, sentence_id)`：只处理已人工核验边界，依赖包含英文正文、句身份、源版本与 hash、边界、编码配置。先复用同依赖结果；缺失则在工作线程生成并冻结私有不可变对象。事务内重验当前输入，拒绝过期工作结果；保留旧版文件。
- `SQLAudioRepository`：在同事务登记 media_asset、audio_target、audio_version 与 sentence_audio_slice，源为既有约束支持的 MANUAL。同候选内容行锁下幂等，切片复用资产 hash，内部 BIGINT 外键通过 public_id 查找。`confirm` 必须听音 reviewer 和当前依赖 hash 一致。产出 READY 不等于听音 CONFIRMED。
- `AliyunASR`：DashScope 文件转录 submit/poll/result，任务 ID 校验、显式配置音频/结果 HTTPS 域名白名单、关闭重定向、限制响应大小、统一安全错误。供应商实际时间信息保存为候选；时间存在仍不声明与管理员句 ID 可靠匹配，必须人工核验。不均分、不用标点制造切点。
- `AliyunContentAI`：qwen-flash 非思考 JSON 候选；校验 schema、来源 ID、英文确为源文本子串、重复与输入上限。原管理员文本保留，候选需确认。依赖只有现有 httpx，无新 Python SDK。

## 集成接口

`create_audio_pipeline(sessions=..., oss=..., current_input=..., locked_input=..., ffmpeg_path=..., ffprobe_path=...)` 返回 AudioPipelineService。

当前内容持有者实现两个回调：

1. `current_input(revision_id, sentence_id) -> SliceInput` 从 canonical 候选读取全英文、已固定整段音频 object_key/version/hash、句边界、核验者、content_type/content_id。
2. `locked_input(session, revision_id, sentence_id) -> SliceInput` 使用传入同事务 SQLAlchemy AsyncSession `SELECT ... FOR UPDATE` 锁对应 canonical 候选行，并重新读取输入。只允许可编辑候选；不能将已发布版本作为边界编辑对象。

局部边界编辑只改变该句输入 hash。源音频或英文影响匹配的修改让相关句 hash 全部改变；旧 hash 的文件与确认不匹配新输入，不会复用。旧 revision 永久保持原引用。生产发布门槛必须重新计算当前每句 hash、检查完整 sentence ID 集合与 CONFIRMED 状态，并验证素材安全审核。

切片新资产的 security_status=PENDING，不能据可解码直接认为内容安全通过。根集成须执行既有审核策略后将素材改为 CONFIRMED/PASSED 或按已授权策略 SKIPPED，才能受控试听和发布。现有媒体接口要求 asset.status=CONFIRMED；不可直接把切片 READY 当作该状态。

worker 阶段调用 split，API 请求只出队任务，不等待 FFmpeg。现有通用 stage 的 input_hash/fencing/lease 配合句级 dependency hash 可承担队列重投；写入最终检查仍由 locked_input 完成。未通过最后检查产生的孤儿不可变对象只能按保护窗口清理，不能删除历史版资源。

## 配置与外部验收

worker 镜像固定 FFmpeg/ffprobe 版本，服务端配置 ffmpeg_path、ffprobe_path。云端 API key 仅从服务端注入；ASR base_url/model/audio_hosts/result_hosts 和文本 model/base_url/config_version 由服务器配置，禁止管理员任意输入 URL。供应商调用须由生产任务的额度/重试/费用账本包围。

官方契约已核验：[ASR 文件转录](https://help.aliyun.com/zh/model-studio/recording-file-recognition-qwen)、[结构化输出](https://help.aliyun.com/en/model-studio/qwen-structured-output)。本轮没有云调用；实际 ASR 句首句尾可靠性、语音听音、云费用与真实账号需要外部验收。

## 验证

先建立失败导入测试再实现；真实 FFmpeg 9.0.2 Windows 临时工具测试通过（无仓库二进制）。单元覆盖实际独立文件/时长、源替换、边界非法/越界、截断音频、播放列表禁止、过期任务、未核验边界、缺少时间点、未知来源和供应商异常。MySQL 集成用例 `tests/integration/test_v15_audio_pipeline.py` 验证并发幂等、实际外键注册、边界变化失效与旧记录保留；须由根集成隔离 MySQL runner 执行。
