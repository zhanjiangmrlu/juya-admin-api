# 管理后端测试环境制品部署

目标：仅将 juya-admin-api 接入云效制品包发布，支持 8.163.84.24 的 HTTP 测试登录；小程序后端另行安排。

## 约束与接口

- main 开发、中文提交；确认的版本同步 test，只有 test 可以发布
- 云效构建 Docker 镜像，制品包含镜像包、SHA256、镜像标签/ID、deploy 配置；不用 ACR
- 保留 juya-admin-small 项目、现有 MySQL/Redis 数据卷和账号；单 Worker、单 Beat
- 新增 JUYA_ALLOW_INSECURE_HTTP=false，只允许 test 显式开启；保留 HttpOnly、SameSite 与 CSRF
- 使用真实数据库与 OSS；数据库迁移由管理后端负责，不自动执行结构降级
- 不部署小程序后端，不上传或提审小程序

## Task 1: HTTP 测试兼容

先写配置拒绝与 HTTP 会话恢复/退出测试并验证失败，然后实现 Settings 校验、路由 Cookie 参数与 runtime 注入。
前端共享 UUID 生成器在 randomUUID 缺失时使用 getRandomValues；覆盖请求 ID、幂等键、编辑行和上传队列。
验证：针对性 pytest/Vitest，随后后端 Ruff/mypy、前端 check/test/build。

## Task 2: 制品发布

先写 Shell 黑盒测试：拒绝错误分支/破损制品，成功发布，迁移失败和健康失败恢复前一版本。
新增打包与 ECS 脚本，锁定发布、校验制品、备份数据库、停止应用后迁移、健康检查与失败恢复；应用使用现有 2 GiB Compose 加测试覆盖配置。
改写云效定义为 test 门禁、隔离 MySQL/Redis 验证、Docker 构建/导出与制品上传、主机部署。
验证：脚本测试、Compose config、镜像构建与源码质量门禁。

## Task 3: 云效联通与验收

云效使用独立制品 juya-admin-api-test，主机组仅包含目标 ECS；确认 GitHub test 触发器。
前端继续发布 dist，部署模板保持现有静态目录和 API/health 同源代理；避免旧模板恢复 503。
真实流水线发布后验证浏览器 HTTP 登录、刷新、CSRF 写操作、退出及容器状态。
若云效会话不可访问，交付可粘贴的构建/部署步骤与变量，并明确云上流水线验收尚未完成，不能用手工发布冒充流水线。

## Review Focus

生产环境不能开启 HTTP Cookie；不弱化 CSRF；制品校验前不停止服务；回滚不能连接新建空数据卷；失败不标记成功；不删除共享 Docker 资源或输出凭据。
