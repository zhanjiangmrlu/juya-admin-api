# 管理后端云效测试制品部署

本次仅发布管理后端。小程序后端另行安排,小程序客户端编译后上传微信,不作为 ECS 静态站点发布。

## 云效控制台配置

1. 新建 `juya-admin-api-test` 流水线,通过现有 GitHub 服务连接选择管理后端仓库和 `test` 分支;仅配置 `test` 的推送触发器。
2. 使用支持 Docker 的云效构建环境,构建脚本如下。检查和测试使用构建机临时 MySQL/Redis,运营测试额外使用独立空库,不会连接 ECS 数据库。

```sh
set -eu
test "${CI_COMMIT_REF_NAME}" = test
sh deploy/ci-verify.sh
sh deploy/build-artifact.sh "$CI_COMMIT_REF_NAME" "$CI_COMMIT_SHA" artifact
```

3. 添加“构建物上传”,绑定现有云效 Packages 服务连接,选择流水线软件包仓库。制品名称 `juya-admin-api-test`,版本 `${BUILD_NUMBER}`,打包路径 `artifact/`,勾选“制品中包含打包路径的目录”。
4. 添加“主机部署”,勾选下载制品,选择上一阶段的制品,选择只包含 `8.163.84.24` 的主机组。下载路径 `/home/admin/app/juya-admin-api-test.tgz`,执行用户 `root`。部署脚本:

```sh
set -eu
test "${CI_COMMIT_REF_NAME}" = test
staging="$(mktemp -d /tmp/juya-admin-api-test.XXXXXX)"
trap 'rm -rf "$staging"' EXIT
tar xzf /home/admin/app/juya-admin-api-test.tgz -C "$staging"
sh "$staging/artifact/deploy/ecs-artifact-deploy.sh" "$CI_COMMIT_REF_NAME" "$staging/artifact"
```

`.aliyun-ci.yml` 提供相同流程模板,应在真实 Flow 实例确认组件及制品服务连接后保存。前端继续使用原 dist 制品流水线,后端镜像无需 ACR。

## 云效 Docker 版本查询失败

若公共构建集群在 `docker version` 输出版本后返回 `flow not support`,且任务尚未执行
`deploy/ci-verify.sh`,请删除测试和构建任务内联命令中的 `docker version`。
测试任务只调用 `sh deploy/ci-verify.sh`,构建任务只调用
`sh deploy/build-artifact.sh "$CI_COMMIT_REF_NAME" "$CI_COMMIT_SHA" artifact`,均保留前面的
`set -eu` 和 test 分支检查。两份仓库脚本检查 Docker 命令是否存在,由后续实际操作判断可用性。
不要给测试、构建或制品导出添加 `|| true`;这些操作失败仍应停止流水线。

此修复只消除非必要版本查询造成的提前退出。公共集群是否支持网络创建、临时容器、镜像导出,
仍须由真实运行日志验证;不能据此认定全部 Docker 操作已兼容。

## 服务器前提与行为

- 现有 `/opt/juya/juya-admin-api/deploy/docker-compose.ecs-2gb.yml` 和 `/etc/juya/compose.env` 必须存在;运行凭据保留在 `/etc/juya/admin-api.env`。
- 保留 `juya-admin-small` 项目名、MySQL/Redis 容器和数据卷。新版本只更新 API、单 Worker、单 Beat;不启动新的 MySQL/Redis。
- 校验制品 SHA256 和镜像 ID 后备份数据库,停止 Beat,给予 Worker 180 秒正常退出时间;如果被强制终止则中止发布并恢复旧应用。
- 应用停止后使用同一镜像运行一次性迁移;迁移及健康检查成功后记录 `current-release`。失败恢复前一应用 Compose 和镜像,不执行数据库降级。
- 备份位于 `backups/`,权限受 umask 077 限制;备份包含敏感数据,不进入制品或流水线日志。首次接管应记录旧部署、镜像及迁移版本。
- 保留旧镜像和数据库备份,不执行全局 `docker prune` 或 `down --volumes`。清理只针对经过确认的旧管理后端版本,避免影响随后的小程序后端。
- API 仅监听本机 8000,由现有 Nginx 代理 `/api/` 和 `/health/`;不开放数据库、Redis 或 8000 公网端口。

## HTTP 测试与 HTTPS 切换

测试覆盖配置设定 `JUYA_ENVIRONMENT=test`、`JUYA_ALLOW_INSECURE_HTTP=true`,仍连接真实数据库和 `juya-test` OSS。
默认配置保持 Secure Cookie,非 test 环境开启 HTTP 开关会被拒绝。临时测试保留 HttpOnly、SameSite=Strict、CSRF 和已有账号;不创建本地默认管理员。
HTTP 会明文传输密码及会话,只使用测试账号和数据。备案完成后配置可信 HTTPS,将开关改为 false,重新登录并复验。

## 验收与待接入项

完成仓库检查和脚本测试不等于云效上线。必须记录真实 Flow 成功日志、镜像提交 SHA、公网健康检查、浏览器登录/刷新/写操作/退出、Worker/Beat 状态及 OOM 检查。
Nginx 代理必须保留在前端实际发布模板中,不能恢复旧 `/api/` 503 模板。小程序正式发布另需 HTTPS 合法域名及微信后台配置。
