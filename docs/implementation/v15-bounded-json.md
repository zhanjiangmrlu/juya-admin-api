# V1.5 千项完整JSON读取兼容

真实1000正常候选SQL回归发现Windows asyncmy巨大JSON结果在接收buffer补充/扩容路径触发 Existing exports of data cannot be resized 并失联。保留1000数量、完整JSON写入和所有集合hash/身份规则，不修改安装驱动、不降级验收。

新增 infrastructure/db/bounded_json.read_json：仅固定表/JSON列/完整身份列白名单，参数绑定值；同session及行锁下先读UTF8字符长度，再以8192字符（最多32KiB）逐块FOR SHARE读取，拼回完整JSON。没有截断、摘要替代或返回缺字段。metadata查询先排除巨型JSON；writer在重组期间受同一行锁保护。

已接入proofreading create重复重放/get/lock_set、ImportService.batch manifest、publication range candidates/results及ProductionStore.creation_replay response_body。导入逐项current_batch仅需要状态与frozen preview.hash，因此在同排他行锁下仅读该小投影；完整预览/恢复默认完整JSON。避免1000×反复读取整份manifest。

新增unit完整Unicode千项重组与未授权SQL标识拒绝；SQL regression对超过2MB UTF8值验证proof/import/range/creation receipt读取完全一致、真实1000信息保留，锁查询和片段最大值。5个目标unit通过，ruff/mypy5源文件通过。

代码为可装配稳定基线；独占UUID MySQL容器的1000proof、1000import及大JSON真实SQL回归正在运行，完成后追加结果。第一次容器初始化切换期间socket未就绪，未进入测试，已清理；没有把基础设施故障计作代码回归结果。未写READY、未宣称真实SQL通过。

其他image-import与discovery直接响应重放点可能大，但本轮千项ZIP走creation_replay，未修改这些非必要路径。schema/runtime/config/安装驱动均未改。
