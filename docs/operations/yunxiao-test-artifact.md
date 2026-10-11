# 管理后端云效测试制品部署

本次仅发布管理后端。小程序后端另行安排,小程序客户端编译后上传微信,不作为 ECS 静态站点发布。

## 云效控制台配置

1. 新建 `juya-admin-api-test` 流水线,通过现有 GitHub 服务连接选择管理后端仓库和 `test` 分支;仅配置 `test` 的推送触发器。
2. 使用 Linux/amd64 指定容器环境,保留云效官方 `build-steps/alinux3:latest`。测试脚本在任务容器中直接启动临时 MySQL 8.4、Redis 7.4 和 Python 3.13,运营测试额外使用独立空库,不会连接 ECS 数据库。首次运行会安装依赖、下载 MySQL 二进制并编译 Redis,需要可访问系统软件源、MySQL CDN、Redis 下载站和 uv 下载源。建议 4C8G、30 分钟超时。单独的测试任务关闭 Docker Daemon;镜像构建任务开启 Docker Daemon。

测试任务执行命令:

```sh
set -eu
test "${CI_COMMIT_REF_NAME}" = test
sh deploy/ci-verify.sh
```

镜像构建任务执行命令:

```sh
set -eu
test "${CI_COMMIT_REF_NAME}" = test
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

## 云效公共集群 Docker 限制

若公共构建集群在 `docker version` 输出版本后返回 `flow not support`,且任务尚未执行
`deploy/ci-verify.sh`,请删除测试和构建任务内联命令中的 `docker version`。
测试任务只调用 `sh deploy/ci-verify.sh`,构建任务只调用
`sh deploy/build-artifact.sh "$CI_COMMIT_REF_NAME" "$CI_COMMIT_SHA" artifact`,均保留前面的
`set -eu` 和 test 分支检查。构建脚本检查 Docker 命令是否存在,由后续实际构建操作判断可用性;
测试脚本使用下面的原生进程方式。
不要给测试、构建或制品导出添加 `|| true`;这些操作失败仍应停止流水线。

2026-10-10 的命令跟踪日志进一步确认 `docker network create` 返回 `flow not support`。
测试阶段已改为原生进程,不再使用 Docker 网络或子容器。临时服务仅绑定 `127.0.0.1`,
使用 13306/16379 端口、mktemp 数据目录和任务内固定测试凭据;结束后只停止本脚本启动的进程,
并清理本次临时目录。真实测试失败仍会阻止后续部署。
原生检查先清理继承的 `JUYA_*`、`OSS_*` 业务变量,明确关闭两项真实 OSS 测试开关,
再注入本任务的测试连接;构建任务中的业务凭据不会被用于检查。
构建阶段的镜像构建、导出仍需由真实运行日志验证,不能据此认定全部 Docker 操作已兼容。

## 服务器前提与行为

- 现有 `/opt/juya/juya-admin-api/deploy/docker-compose.ecs-2gb.yml` 和 `/etc/juya/compose.env` 必须存在;运行凭据保留在 `/etc/juya/admin-api.env`。
- 保留 `juya-admin-small` 项目名、MySQL/Redis 容器和数据卷。新版本只更新 API、单 Worker、单 Beat;不启动新的 MySQL/Redis。
- 校验制品 SHA256 和镜像 ID 后备份数据库,停止 Beat,给予 Worker 180 秒正常退出时间;如果被强制终止则中止发布并恢复旧应用。
- 应用停止后使用同一镜像运行一次性迁移;迁移及健康检查成功后记录 `current-release`。失败恢复前一应用 Compose 和镜像,不执行数据库降级。
- 备份位于 `backups/`,权限受 umask 077 限制;备份包含敏感数据,不进入制品或流水线日志。首次接管应记录旧部署、镜像及迁移版本。
- 保留旧镜像和数据库备份,不执行全局 `docker prune` 或 `down --volumes`。清理只针对经过确认的旧管理后端版本,避免影响随后的小程序后端。
- API 仅监听本机 8000,由现有 Nginx 代理 `/api/` 和 `/health/`;不开放数据库、Redis 或 8000 公网端口。

## 测试子域名 HTTPS 入口

2026-10-11 已核对测试域名解析、服务器 Nginx 和外网证书校验:

- 管理后台入口为 `https://test-admin.juyayingyu.com/login`,前端 API 地址保持相对路径,通过同域 `/api/` 访问管理后端。
- 独立管理接口入口为 `https://test-admin-api.juyayingyu.com/api/v1/admin/`,就绪检查为 `/health/ready`;根路径返回 404。
- 两个管理域名的 HTTP 请求跳转 HTTPS,登录页及健康检查从外网返回 200。
- 四个测试域名共用已安装的 Let's Encrypt 证书,服务器 Certbot 定时器处于 active 状态;小程序 API 和 H5 尚未部署,其域名返回 503。

测试覆盖配置设定 `JUYA_ENVIRONMENT=test`、`JUYA_ALLOW_INSECURE_HTTP=false`,保持 Secure、HttpOnly、SameSite=Strict Cookie、CSRF 和已有账号。
当前运行服务未启用 HTTP 登录;下一次制品部署也必须保持此 HTTPS 要求。主机组仍指向 ECS `8.163.84.24`,无需因域名切换而更换主机。
前端流水线发布静态文件时保留 `/etc/nginx/conf.d/juya-test-domains.conf` 及 TLS 片段,不能用旧 HTTP 模板覆盖。
服务器当前接入记录及 Nginx 模板见前端仓库 `docs/operations/test-domains-https.md`。

## 验收与待接入项

完成仓库检查和脚本测试不等于云效上线。必须记录真实 Flow 成功日志、镜像提交 SHA、公网健康检查、浏览器登录/刷新/写操作/退出、Worker/Beat 状态及 OOM 检查。
Nginx 代理必须保留在前端实际发布模板中,不能恢复旧 `/api/` 503 模板。小程序正式发布另需 HTTPS 合法域名及微信后台配置。
