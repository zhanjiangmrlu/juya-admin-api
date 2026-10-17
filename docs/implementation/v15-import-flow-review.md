# V1.5 导入流程复核与隔离SQL验收

## 修正

- 词汇表的空parent或不属于内容表的来源行保留行号与parent标识，生成批次ERROR。纯阅读短文附词汇表同样明确ERROR。preview公开batch_issues；confirm批次阻断，不能选择正常正文后静默丢弃孤立词条。需要修正原表再上传。
- 按parent预分组词汇行；1000正文不反复扫描全词表。确认整批只读取、核对原文件一次，只展开ZIP一次；prepared items共享已验证files。DOCX同样复核原始文件SHA，原文件无法读取给明确错误。
- validate批量items、preview与stage SUCCEEDED在同一事务；末尾再按实时数据库时钟检查owner/fence/hash/config/itemID/lease，租约过期整个校验事务回滚。
- confirm在入口、每项preflight、内容写入事务末尾、失败结果事务以及最终receipt事务检查租约。取消或丢失租约属于fatal，不能吞掉后改写FAILED。已经成功的项目先读保存结果，不重新绑定媒体、不重复创建revision。取消后的preview不改manifest。
- 映射查询仅取CAS需要的稳定字段，移除created_at datetime，避免第二次导入预览无法JSON hash或存入manifest。content_identity.legacy_scene_id写scene公开ID而不是内部数字ID。
- 新明确请求key产生独立确认执行stage（hash含batch/payload/actor/key），同key仍按receipt重放。局部失败的新key可再次派发，相同已SUCCESS项直接恢复。
- bind_media新增强制第4个role：WHOLE_AUDIO/FULL_AUDIO/WORD_AUDIO/CHUNK_AUDIO/COVER_IMAGE/LEARNING_IMAGE/WORD_IMAGE。词汇与语块音频不能继承整段target角色；语块图片在服务层也防御拒绝。根runtime已按role装配。

## 验证

17项定向unit通过，包括真实DOCX/XLSX/ZIP解析、孤立行与短文禁用词表、1000item一次解包、旧lease无FAILED/receipt写入、媒体role分离。parser/service mypy与所属ruff通过。

新增tests/integration/test_v15_import_flow.py的5项隔离SQL测试：

1. create同key重放/异输入冲突、validate原子成功、preview、default ack、部分失败、SKIP、UPDATE_DRAFT、外部编辑后的目标CAS拒绝；
2. 孤立词条显示与整批确认阻断；
3. prepare期间cancel，旧worker不得写SUCCESS/FAILED或receipt，阶段不可复活；
4. 校验worker过期后被新owner抢占，旧worker不能提交items或preview，也不能改新owner；
5. 同payload新key独立重试stage，失败行重试后成功、成功行不再prepare媒体。

本环境未设置JUYA_TEST_DATABASE_URL，SQL5项skipped，不能宣称真实SQL验收通过；由根对话私有隔离runner执行。未调用云端、共享数据库，未改.env、schema、runtime或StageStore。
