# V1.5 剩余契约闭合实施计划

> 实施采用既有子任务分工与根集成验证;执行技能为 subagent-driven-development / executing-plans。沿用用户授权的当前分支,不执行共享迁移、推送或部署。

**Goal:** 对照20261011完整需求补齐管理后端尚未公开的实际可用契约。

**Architecture:** 复用现有SQL内容与媒体权威、幂等命令、阶段租约及outbox;新增接口仅包装真实能力。0018由本仓唯一迁移作者编写,协调侧执行共享环境升级;私有测试运行器执行隔离升级。

**Tech Stack:** FastAPI、SQLAlchemy/asyncmy、MySQL8.4、Celery、FFmpeg。

**Spec:** 20261011完整需求16.9/19/29/32/34及管理前端v15-integration-ready未公开契约清单。

## 全局约束与评审重点

仅修改本仓库,不改历史0017;零真实收费调用。默认录音关闭,未知历史资源保持缺失,不得用当前英文词库猜配。失败重试保留成功项并拒绝未知收费结果重提。范围发布冻结管理员、选中ID、内容/编辑版本与hash,不发布未选项,重放先于当前状态检查。预览仅管理员授权范围,固定角色/版本/SHA审核核验,URL不落持久回执。排序属于运营元数据,不能修改旧文本快照。

## 任务

- [ ] 配置与短文: learning_features/store.py/router.py; system_config权威录音开关、CAS/幂等/审计和可信BFF DTO; short_reading全局sort_order与管理命令、内部稳定排序;私有SQL验证关闭/开启、冲突和排序。
- [ ] 素材与音频: v15_authoring slice-state恢复;短文FULL直接上传target及实际固定版本绑定;图片/音频待审核不能试听确认;真实SQL跨刷新与错误角色/CAS回归。
- [ ] 收藏与发音: v15_backfill exact历史资源;candidate归属revision_id;pronunciation admin状态、固定资源预览和有留存音频的本地恢复;未知禁止伪造或盲目收费;SQL与provider fake验证。
- [ ] 导入阶段: 实际limits只读契约、逐阶段retry的依赖/租约/未知收费防线;统一ZIP和素材ZIP受控预览,只读实际当前批次原始bytes,不把路径冒充URL;成功结果复用。
- [ ] 模块与发布: 模块名称/排序/readiness只读及真实CAS修改;范围预检查冻结token、显式选中确认、逐项发布/失败恢复、审计/幂等结果;现有场景/短文发布门槛保持。
- [ ] 根集成: 0018增量迁移、服务/worker装配、OpenAPI/internal字段及默认语义先冻结可消费版本;最终锁文件/Ruff/格式/mypy/私有SQL覆盖率>=80,独立只读复核后更新progress。

## 执行步骤

各任务先新增失败测试,再实现最小实际行为,执行目标测试与静态检查,只暂存自己文件并中文提交。根运行器集中执行真实SQL目标及全套;跳过的真实OSS/云/设备验收单列,不视为通过。P5此前READY只覆盖先前接口范围,本轮新缺口未闭合前重新标记实施中。
