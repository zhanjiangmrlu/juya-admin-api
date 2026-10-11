# E08 单批1000篇真实ZIP导入验收

新增 tests/integration/test_v15_thousand_import.py，只补验收，不改导入SQL/store/生产代码。复用现有隔离import_db fixture（真实MySQL、ProductionStore、ShortReadingStore与ImportService、无媒体binder拒绝任何调用），所有业务ID以本用例UUID隔离。

单ZIP包含实际XLSX内容表1000行和一个实际双语DOCX。每行独立import_content_id显式引用同一路径，验证相同英文正文不能合并不同内容身份。单次上传、实际验证后新建服务恢复原batch的1000项预览；VALIDATE重复投递不再读取/解包包。

明确一次确认全部1000个item ID及全部缺省确认。只注入一次save_item_within错误，在目标项真实落库之后抛出AppError，验证该项事务回滚、999其他项SUCCESS和唯一映射/场景/修订保留。第一次确认只解包一次SpoolFiles，不按item重开整包，媒体binder不允许调用。

新key只选择唯一失败item确认，真实save仅调用一次。最终1000独立映射、场景和修订，999既有scene/revision/version/edit_version逐项不变，无额外版本。再次重放原失败集合和新成功单项返回各自原保存结果；先删除原archive并设original/bind_media拒绝调用，证明成功幂等重放不依赖原包或媒体IO。spycounter分别确认验证外包一次、两次独立确认各一次，重放不增加。

阶段取消及旧worker fencing已有 tests/integration/test_v15_import_stage_concurrency.py真实SQL覆盖，本测试不重复降低规模的模拟。

本地仅收集检查（未提供隔离DB时1 skipped），ruff通过。真实1000解析/SQL测试由主对话私有runner执行，可花几十秒至两分钟；未运行前不计E08通过。此fixture不发布内容，不调用云端，也不宣称版权/人工听音/真机验收。


## 真实隔离执行结果

2026-10-11独占UUID MySQL 8.4容器、私有schema及Redis执行本文件、1000正常校对集合及大JSON回归，合计 `3 passed in 123.65s (0:02:03)`，退出0。1000 ZIP完整解析与SQL落库、999保留+1失败事务回滚、只重试失败项、最终1000唯一身份与旧999版本不变、原包删除后的无IO重放全部断言通过。自有容器/schema/Redis已清理；不将此范围结果扩展为云端/生产/真机通过。
