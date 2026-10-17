# V1.5 收藏内容范围迁移计划

> 执行技能：systematic-debugging、test-driven-development、executing-plans、verification-before-completion。沿用本仓当前分支，不执行共享数据库迁移。

**Goal:** 同一用户在不同内容中收藏相同entry/sense/source时拥有独立收藏ID，内容版本更新仍复用同一身份。

**Architecture:** 0019新增favorite_snapshot.content_type/content_id，严格从该收藏明确指向且归属一致的当前revision回填。替换唯一索引，保留所有现有ID、revision、引用和账本。0017/0018保持冻结。

**Tech Stack:** Alembic、SQLAlchemy、MySQL8.4。

**Spec:** 2026-10-11最终小样本联调发现第二篇收藏误复用第一篇favorite_id；协调修复要求包含content_type/content_id且不把内容版本加入identity。

## 契约与约束

- 索引名保持`uq_favorite_snapshot_1`，列顺序为`user_id,content_type,content_id,entry_type,entry_id,sense_id,source_id`。
- 新增两列NOT NULL、无默认值；身份不包含snapshot_version、saved_content_version或revision_id。
- 回填使用原始当前revision的content_type/content_id，不改历史revision、不猜配缺失内容；指针缺失、归属不符或空范围明确拒绝升级。
- 降级先检测旧五列键碰撞，存在时明确拒绝，禁止去重或删除数据；无碰撞时以单条ALTER恢复旧索引并删除新增列。
- 共享环境升级和BFF写入/详情查询由协调方负责，本仓负责唯一迁移、元数据、历史回填写入兼容和最小SQL回归。
- upgrade和downgrade均只允许online执行；offline无法验证当前revision归属或旧键碰撞，明确拒绝且不输出DDL。

## 执行与中断恢复

执行前停止所有收藏写入方（包括BFF API、回填及相关worker），备份favorite_snapshot、favorite_snapshot_revision、引用/映射/回执表及迁移版本，独占执行`alembic upgrade head`。本迁移的多条MySQL DDL各自提交，不声称全流程事务原子；schema19应用只能在迁移完成后启动。

若进程在新增两列后或替换索引后中断，保持停写，先核验alembic_version/schema_version、SHOW CREATE TABLE以及current revision归属。再次online升级会先做全部归属预检，然后验证两列必须同时存在且为utf8mb4_bin的VARCHAR(32)/VARCHAR(64)、已填值必须与原current revision逐字节相同、索引必须为旧五列或新七列；只补齐NULL字段并安装最终NOT NULL唯一键，完成版本更新。比较使用CAST AS BINARY，避免PAD SPACE规则把尾空格矛盾视为相同；矛盾不会被trim或自动纠正。不改历史revision或旧字段。异常形状/相互矛盾的值会拒绝续跑，由备份与原始事实人工核查，不自动覆盖。

已完成0019时，如必须恢复0018应用，保持停写并执行online `alembic downgrade 0018`；存在旧五列身份碰撞将先拒绝，无碰撞时一条ALTER恢复旧索引并删除新两列，保留所有原表行。若中断时Alembic仍标0018，优先核验后续跑完成0019再按上述路径降级；不要只改版本标记、手工删收藏、去重或先DROP唯一键。

## 步骤

- [x] 用收藏元数据契约回归观察旧五列键不满足新增内容范围，确认失败原因。
- [x] 添加0019、schema19启动门槛、最新元数据及本仓回填写入/fixture兼容。
- [x] 实际SQL验证旧行完整保留、不同文章/类型独立、相同完整身份受唯一键约束、升级后冲突降级拒绝、无冲突可逆及缺失范围拒绝。
- [x] 执行静态门禁与相关单元/SQL小样本，精确清理本次资源，中文提交并回传契约。

## 验证结果

元数据契约先失败1项/通过3项，修改后6项契约/offline行为通过。全部单元361 passed；uv sync --locked、Ruff、476文件格式、mypy191源文件通过。

首次实际SQL11 passed/1 failed，失败为测试把cloud账本按不存在的id排序，发生在新迁移前；修正后只重跑新增/受影响5项，5 passed/4 deselected（98.52秒、退出0）。其余4项此前已通过：无碰撞降级/再升级、缺失指针、错归属、空范围在DDL前阻断。最后5项覆盖保留所有旧行/引用/账本、跨文章/类型隔离且版本不独立成身份、碰撞降级保护、两类中断续跑及两个字段尾空格矛盾阻断。并非重新跑全套覆盖率；之前83.09%的全套基线仍只适用于资料入口配置加入前。

本仓新迁移及相关调用方可以提供给协调环境消费，学生端收藏创建/详情prefill/取消隔离及最终浏览器运行仍由BFF与协调方验收。
