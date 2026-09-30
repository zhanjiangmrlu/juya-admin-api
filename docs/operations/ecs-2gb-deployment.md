# 2026-09-30 ECS 试运行部署记录

管理后端已部署在阿里云 ECS `8.163.84.24`，使用 Ubuntu 24.04.5 LTS、
2 vCPU 和 2 GiB 规格。系统实际可用物理内存约 1740 MiB，另配置 2 GiB Swap
和 `vm.swappiness=10`。Swap 用于缓冲峰值，不能代替实际容量监控。

## 部署与配置

- 后端版本：`3a74cb4`，镜像 `juya-admin-api:ecs-2gb`。
- Docker Engine：29.8.1；Docker 软件包使用阿里云镜像源和 Docker 官方签名密钥。
- 项目目录：`/opt/juya/juya-admin-api`。
- Compose 文件：`deploy/docker-compose.ecs-2gb.yml`，项目名 `juya-admin-small`。
- 配置文件：`/etc/juya/compose.env`、`/etc/juya/admin-api.env`，均为 root 所有、权限 `600`。
- 长期进程：MySQL 8.4、Redis 7.4、API、一个并发为 1 的 Worker、一个 Beat。
- MySQL 与 Redis 使用持久数据卷，未映射公网端口；API 仅监听 `127.0.0.1:8000`。
- 私有后台静态页面位于 `/opt/juya/juya-admin-web`，Nginx 仅监听 `127.0.0.1:8080`。

应用使用 `production` 运行配置启用真实数据库路由和严格配置检查。本次仍是云上试运行，
按用户选择显式绑定深圳私有测试 Bucket `juya-test`，并未访问正式 Bucket `juya`。
OSS 暂用已有服务器端凭据；正式运行应切换到独立生产 Bucket 和限定权限的 ECS RAM 角色。

数据库是独立新建的云上空库，完成 0001 至 0014 迁移，没有导入本地开发数据。
已独立创建 `admin` 账号，密码随机生成，不使用本地默认密码。管理员密码和部署配置的
本机副本位于仓库之外，文件权限仅授予当前 Windows 用户，未进入 Git。

## 私有试用入口

当前机器已启动 SSH 转发，访问 `http://localhost:18086/login`。
只有保留这条 SSH 连接的本机可以使用此入口；电脑关闭或转发进程退出后需重新连接。
连接命令示例：

```powershell
ssh -N -L 18086:127.0.0.1:8080 `
  -i '<本机私钥文件路径>' `
  -o ExitOnForwardFailure=yes `
  -o ServerAliveInterval=30 `
  -o ServerAliveCountMax=3 `
  -o StrictHostKeyChecking=yes `
  -o 'UserKnownHostsFile=<已保存的 known_hosts 文件路径>' `
  root@8.163.84.24
```

连接时会校验已记录的 SSH 主机密钥。网页流量通过 SSH 加密传输到 ECS。
验证中 Chromium 在 localhost 上正常保留 Secure、HttpOnly 会话 Cookie；此试用方式
不能替代公网域名的 HTTPS 配置。

`juya-test` 原来没有 CORS 规则，当前应用 AccessKey 也不具备 `oss:PutBucketCors` 权限。
用户已在阿里云控制台配置以下精确来源，预检验证通过：

```text
http://localhost:18086
https://juya-admin.vercel.app
```

允许 GET、POST、HEAD，允许请求头 `content-type`，暴露 `ETag`，缓存 300 秒，
多来源配置启用 `Vary: Origin`。Bucket 仍保持私有。

## 已完成验证

- 云上健康检查返回 `ready`，MySQL、schema、Redis、configuration 均为 true。
- 独立管理员密码登录成功；浏览器会话刷新恢复成功，CSRF 退出成功，退出后会话返回 401。
- Chromium 加载真实云上前后端，页面运行错误和意外 API 错误均为 0。
- 浏览器从管理 API 获取签名策略并实际直传 OSS 成功。
- OSS HEAD、签名读取、匿名读取拒绝、两个来源的 GET/POST/HEAD 跨域预检均通过。
- 只删除本次创建的临时测试对象，已用签名读取返回 404 确认清理完成。
- Worker 的周期任务实际执行成功；部署后观察到五个服务均未发生 OOM 或异常重启。

一次低负载快照中，五个长期容器共使用约 680 MiB。此数值不包含宿主机及 Nginx，
也不是并发容量或大型媒体任务的负载测试结论。

## 后续接入

域名备案仍在审核中，公网域名、HTTPS 和 Vercel 到后端的转发尚未配置。
当前 Vercel 地址 `https://juya-admin.vercel.app/login` 不会自动连接这台后端。
备案通过后，配置后端 HTTPS 域名，并将 Vercel 的 `/api/` 转发放在 SPA fallback 之前，
再次验证 Cookie、会话恢复和写入请求。

自动数据库备份与恢复演练尚未配置，正式使用前需要完成。真实 OCR/TTS、可信素材扫描与
解码链路仍按现有代码边界单独验收；OSS 对象上传成功不等于业务素材已通过安全检查。
本次未部署 `juya-miniapp-api`，依赖该服务的功能需要后续联调。

日常查看状态：

```sh
cd /opt/juya/juya-admin-api
docker compose --env-file /etc/juya/compose.env -f deploy/docker-compose.ecs-2gb.yml ps
curl --fail http://127.0.0.1:8000/health/ready
docker stats --no-stream
free -h
```

更新应用时保留数据卷，不能以删除 MySQL/Redis 数据卷的方式升级或修改密码。
