# V1.5 OCR 接口与批操作退役

依据完整需求第16.1、19.2章，删除 media OCR额度/设置/创建/查询/候选/命令路由、请求响应模型与 dispatcher OCR方法；删除内容OCR建议与采纳路由。OpenAPI不再注册这些路径，HTTP请求返回404。

ContentBatchOperations 删除OCR回调与分支；BATCH_OPERATIONS不含OCR，入口校验在任何草稿查询、事务、历史回执重放之前拒绝不支持类型。因此旧OCR批次无法通过回执重放继续运行。独立手工音频、素材、批次、回收站与正文编辑保留；没有删除历史SQL表或历史已确认正文、引用。

测试改为验证退役：媒体OpenAPI及HTTP404、无作业/候选/派发/审计副作用、OCR批次422；内容建议与采纳404且不读取草稿或候选；批操作拒绝OCR且不接触内容/存储。原E2E通过OCR创建来测试音频和回收站的部分删除，剩余独立操作仍验证。

验证：定向14 passed（e2e媒体、内容退役、批执行、固定资源）；3个源文件mypy通过；全部所属源文件和测试ruff通过。路由库存契约当前被集成层 runtime.py 仍传入已删除 ocr_quota_service 参数阻塞，需要根对话移除调用并重新生成前端OpenAPI与接口文档后复核。没有调用云端、共享数据库、私有runner或重启服务。


## 运行装配收尾

删除 Settings 的 ocr_provider/baidu_ocr_api_key/baidu_ocr_secret_key；runtime 不再装配OCR额度、供应商或按百度密钥启用媒体dispatcher。dispatcher仅独立派发内容批任务，旧全篇TTS继续明确拒绝。删除 MediaTaskService/PersistentMediaTaskService 的OCR构造依赖与quota状态，删除 LocalOcrProvider、无调用的 enabled检查。历史候选类型与存储表保留。

本次定向27 passed（媒体E2E、旧TTS、运营OCR队列和配置、内容退役与路由库存，排除全量生成快照一致性1项）；config/runtime/tasks 的mypy通过，所属ruff通过。全量快照一致性待根对话再生成前端OpenAPI及文档。本轮未装配新增V15服务。
