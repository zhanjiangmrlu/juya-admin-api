# V1.5 正常抽样后的显式批量校对确认

生产顺序为：结构化与实际资源准备 → 显式entry批量确认（SceneAuthoringStore.confirm_entries_bulk / POST content/revisions/entry-confirmations）→ 获取新草稿版本/资料hash → proof.create冻结集合 → 人工检查服务器选择的样本 → 显式normal/bulk-confirm接受正常结构化结果 → complete合格集合 → 人工发布。不能proof.create之后再改entry而沿用旧candidate_hash；校对不自动确认词条，不伪造人工试听或版权。

集合分类由服务端实际facts及content_import_item.issues生成并冻结。请求中的normal、异常类别等字段不参与分类；N是正常候选数量，抽样数为N=0时0，否则min(N,20,max(1,ceil(N*5%)))。默认facts将结构完整(normal)与人工/资源已准备(ready)分开；结构正常但尚未确认词条或适用音频的项目不能批量放行。异常项继续逐项manual，不能通过删字段或客户端normal=true隐藏。

失败联动分组由服务端固定为content_type；客户端group_id仅可重复该固定值，不匹配会拒绝。当前同集合内同类型采用保守的统一分组，不能将有问题的同类候选拆成孤立组绕过重新校对。

新增POST /api/v1/admin/content/proofreading/{set_id}/normal/bulk-confirm，要求管理员写权限/CSRF、Idempotency-Key，body为collection_hash、revision_ids和accept_normal_results=true。服务锁集合，先重放同键同载荷已提交回执，然后校验固定集合摘要、全部服务端样本的MANUAL确认及hash。每个选中项须在冻结正常集合、无同类失败，重新检查当前draft/hash及实际资源/条目ready。逐项写BATCH_NORMAL记录，样本已有MANUAL记录保持MANUAL。确认、审计、幂等回执同事务；新载荷复用同键409，重放不会把已complete状态降回PENDING。

complete不会隐式确认其他项目。默认仍要求全部候选已显式batch/manual确认；新可选body allow_abnormal_drafts=true只允许冻结异常且未确认项目留草稿，仍要求全部正常、抽样及失败受影响项目通过。留草稿ID进入审计，没有confirmation记录的异常不能借set.status=CONFIRMED发布。

真实SQL tests/integration/test_v15_bulk_proofreading_sql.py使用3正常+1实际不完整异常短文：N=3且仅1样本、clientnormal忽略、样本前batch拒绝、样本人工后normal记录2项BATCH_NORMAL、异常batch拒绝、默认complete拒绝、显式异常留草稿complete后仅正常可发布、同键重放保留CONFIRMED状态、新载荷同key拒绝。当前无JUYA_TEST_DATABASE_URL而跳过，须由root隔离runner执行；纯分类与既有抽样单元已通过。
