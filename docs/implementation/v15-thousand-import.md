# E08 单批1000篇真实ZIP导入验收

新增 tests/integration/test_v15_thousand_import.py，只补验收，不改导入SQL/store/生产代码。复用现有隔离import_db fixture（真实MySQL、ProductionStore、ShortReadingStore与ImportService、无媒体binder拒绝任何调用），所有业务ID以本用例UUID隔离。

单ZIP包含实际XLSX内容表1000行和一个实际双语DOCX。每行独立import_content_id显式引用同一路径，验证相同英文正文不能合并不同内容身份。单次上传、实际验证后新建服务恢复原batch的1000项预览；VALIDATE重复投递不再读取/解包包。

明确一次确认全部1000个item ID及全部缺省确认。只注入一次save_item_within错误，在目标项真实落库之后抛出AppError，验证该项事务回滚、999其他项SUCCESS和唯一映射/场景/修订保留。第一次确认只解包一次SpoolFiles，不按item重开整包，媒体binder不允许调用。

新key只选择唯一失败item确认，真实save仅调用一次。最终1000独立映射、场景和修订，999既有scene/revision/version/edit_version逐项不变，无额外版本。再次重放原失败集合和新成功单项返回各自原保存结果；先删除原archive并设original/bind_media拒绝调用，证明成功幂等重放不依赖原包或媒体IO。spycounter分别确认验证外包一次、两次独立确认各一次，重放不增加。

阶段取消及旧worker fencing已有 tests/integration/test_v15_import_stage_concurrency.py真实SQL覆盖，本测试不重复降低规模的模拟。

本地仅收集检查（未提供隔离DB时1 skipped），ruff通过。真实1000解析/SQL测试由主对话私有runner执行，可花几十秒至两分钟；未运行前不计E08通过。此fixture不发布内容，不调用云端，也不宣称版权/人工听音/真机验收。


## 真实隔离执行结果

2026-10-11独占UUID MySQL 8.4容器、私有schema及Redis执行本文件、1000正常校对集合及大JSON回归，合计 `3 passed in 123.65s (0:02:03)`，退出0。1000 ZIP完整解析与SQL落库、999保留+1失败事务回滚、只重试失败项、最终1000唯一身份与旧999版本不变、原包删除后的无IO重放全部断言通过。自有容器/schema/Redis已清理；不将此范围结果扩展为云端/生产/真机通过。

## 第06方案本地量化口径

测试输出 `V15_THOUSAND_METRICS=<JSON>`，不含业务ID、主机名、用户标识、连接串或凭据。ZIP整体字节和成员压缩／解压字节均从实际生成容器测量；当前ZIP采用存储模式（不额外压缩），其中DOCX/XLSX自身为压缩文档。句数由最终1000份真实SQL修订的 `JSON_LENGTH(content_snapshot,'$.dialogue')` 求和断言；不将共享DOCX物理句数误当成1000篇的逻辑句数。

阶段耗时为单次真实验证、1000项预览读取、完整1000项确认和单失败项恢复，样本各n=1，不计算p95。批次元数据读取为同一会话串行执行20次实际SELECT，p95使用nearest-rank（第19个有序样本）；逐项保存为1000次真实 `save_item_within`（包括故障注入项的实际SQL），p95为第950个有序样本，计时不包含外层事务提交、整包解析或媒体处理，因此不是worker任务p95。

仅设置 `JUYA_THOUSAND_METRICS=true` 时启用Python tracemalloc，从测试正文开始至验证完成采集Python跟踪分配峰值；此值不包含MySQL/Redis容器、驱动原生缓冲区或进程WorkingSet，不能用于推导生产内存容量。正常全套不启用tracemalloc，仅增加20次轻量SQL读取。纯文档fixture未配置AI/ASR/TTS服务，媒体binder任何调用都会抛错；已知fixture云调用为0，不是外部账户账单测量。

本测试串行处理，队列／项目并发为1；没有HTTP详情、HTTP任务查询及读写同时负载，相关p95明确未采集。第06方案的API并发读取性能与生产并发容量仍须独立验收，不能用本地元数据SQL或单批耗时代替。

## 2026-10-11 量化执行结果

独占UUID MySQL 8.4容器、私有schema和Redis，启用tracemalloc，真实测试 `1 passed in 240.15s (0:04:00)`，退出0；自有资源全部清理。原始JSON输出已保存至 [量化证据](evidence/v15-thousand-import-metrics-20261011.json)。同一工作站另有其他验证工作，本结果不是空载性能基准，也不用于承诺生产容量。

| 指标 | 实际结果与口径 |
| --- | --- |
| 机器 | Windows 11；Intel64 Family 6 Model 183 Stepping 1，GenuineIntel；28逻辑CPU；RAM 68,487,553,024 bytes |
| 包体 | ZIP整体53,162 bytes；成员压缩52,918 bytes；成员解压52,918 bytes（ZIP存储模式） |
| 数据量 | 1000篇独立内容、1000逻辑句（真实SQL统计）、媒体0文件／0ms、串行并发1 |
| 云调用 | 已知fixture调用0；任何媒体binder调用都会失败；不声称外部账单核对 |
| 验证 | 166.255s，n=1，含实际1000行文档解析及SQL，tracemalloc开启 |
| 完整预览读取 | 2.158s，n=1，恢复服务读取1000项 |
| 全1000项确认 | 61.897s，n=1，含一次受控单项故障，保留999成功 |
| 单失败项恢复 | 2.065s，n=1，新key仅恢复1项，旧999版本不变 |
| 元数据SQL p95 | 1.096ms，n=20，真实batch元数据SELECT，同会话串行 |
| 逐项保存SQL p95 | 44.771ms，n=1000，实际save_item_within，排除外层提交及解析，非worker任务p95 |
| Python内存峰值 | tracemalloc 95,985,104 bytes；不包括进程全部原生分配／容器 |
| 进程中途采样 | WorkingSet 196,497,408 bytes；截至采样PeakWorkingSet 198,471,680 bytes，非测试结束峰值 |
| 解包次数 | 验证1次；1000项首次确认1次、失败项恢复1次；same-key重放0次 |
| HTTP详情／worker查询并发p95 | **未测**；整批任务p95也未测（n=1不能推导p95） |
