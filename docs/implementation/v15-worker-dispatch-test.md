# V1.5 异步派发回归

根任务私有 MySQL runner 对稳定版本 `test_v15_worker_dispatch.py` 与 `test_v15_audio_authoring_flow.py` 实跑：3 passed in 5.31s。该次运行在同边界重试分支修改后启动，测试文件之后未改变。

派发用例使用实际服务组装、SQL outbox/stage 与真实 DOCX：模拟 broker 失败后保留 PENDING 和重试时间；QUEUED 租约超时后重新派发；VALIDATE 实际解析并保存预览，CONFIRM 实际保存完整短文草稿；重复投递不增加成功阶段的尝试次数。

材料、AI、ASR 分支只用隔离回调验证注册服务路由，不冒充云业务成功。未知阶段写 FAILED，缺失阶段安全返回；同 item 的过期旧 claim 在真实 SQL 中不能完成或写回，replacement 输出有效。

音频流程使用标准整段版本 ACTIVE，切片仍要求 CONFIRMED/PASSED 与显式试听。相同依赖的 FAILED 切片阶段可人工重新确认边界并回到 PENDING，实际 FFmpeg 成功后阶段完成；相同边界的 SUCCEEDED 阶段可重启并复用同版本缓存，之前的 claim 均不能再次完成。

没有连接真实 Celery broker 或付费云供应商；实际 broker delivery、云时间点、费用与审核仍属于外部验收。完整覆盖率由根最终隔离门禁统一报告。
