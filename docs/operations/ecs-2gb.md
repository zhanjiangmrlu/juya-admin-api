# 单后端部署到 2GB ECS

适用于一个管理后端的低负载起步部署。操作系统使用 Ubuntu 24.04 LTS，MySQL 8.4、
Redis 7.4 与 API 同机运行。只启动一个 Celery Worker，消费全部七个队列，并发为 1。
这套配置需要实际监控验证容量，不保证高并发或大型媒体任务的吞吐量。

## 服务器准备

更换系统盘会永久清除原系统盘的数据，已有数据应先创建快照备份。在 ECS 控制台的广州地域，
进入实例详情，选择“全部操作 → 更换操作系统 → 更换系统盘”，选择公共镜像 Ubuntu
24.04 LTS 64 位。系统盘保持原容量，不需要为此次部署扩容。使用 SSH 密钥对登录；私钥只保存在
自己的电脑上，不能提交到仓库。更换完成后确认实例状态为运行中，操作系统显示 Ubuntu 24.04。

安装 Docker Engine 和 Compose 插件后，将本项目部署到 `/opt/juya/juya-admin-api`。
国内服务器的镜像下载需使用可达的可信镜像源或阿里云 ACR；依赖安装与镜像构建建议在本机或
流水线完成，再上传镜像，避免占用 2GB ECS 的运行内存。

## 配置与账号隔离

复制 `deploy/.env.ecs.example` 至 `/etc/juya/compose.env`，
复制 `deploy/.env.runtime.example` 至 `/etc/juya/admin-api.env`，均设置权限为 `600`。
填入实际镜像名、OSS 配置和随机密钥。不要使用本地 Compose 或本地默认管理员密码上线。

三个 MySQL 密码应分别使用独立的 48 位十六进制随机串。可在安全终端执行以下命令生成一项，
分别执行三次，不把输出贴到聊天或日志中：

```sh
openssl rand -hex 24
```

HMAC 密钥使用 `openssl rand -hex 32` 单独生成。运行时账号 `juya_admin_app` 仅获得数据库的
查询、写入和执行权限；迁移账号 `juya_migrate` 用于数据库迁移，API 容器不获得迁移或 root 密码。
MySQL 初始化脚本只在全新数据卷时创建账号，修改配置文件中的密码不会修改已有数据库密码。
已有部署改密时需要同步更新数据库账号，不能删除数据卷来换密码。

OSS 使用私有 Bucket，推荐为 ECS 绑定仅能访问选定 Bucket 的 RAM 角色，并在运行配置中填入角色名。
Bucket 的环境用途、地域和 CORS 应根据实际前端来源确认；不能直接将本地 `juya-test` Bucket 当作
正式环境。现有项目的生产 OCR/TTS 派发仍处于关闭状态，本部署不改变其能力边界。

## 启动顺序

在仓库根目录执行：

```sh
docker compose --env-file /etc/juya/compose.env -f deploy/docker-compose.ecs-2gb.yml config --quiet
docker compose --env-file /etc/juya/compose.env -f deploy/docker-compose.ecs-2gb.yml up -d --wait mysql redis
docker compose --env-file /etc/juya/compose.env -f deploy/docker-compose.ecs-2gb.yml run --rm migrate
docker compose --env-file /etc/juya/compose.env -f deploy/docker-compose.ecs-2gb.yml up -d admin-api admin-worker admin-beat
curl --fail http://127.0.0.1:8000/health/ready
```

只有迁移成功后才能启动 API、Worker 和 Beat。就绪检查应返回 `status: ready`，且数据库、schema、
Redis、configuration 均为 true。管理员需要独立创建正式账号；本套配置不会运行本地账号初始化器。
数据库默认空库，不会导入或修改现有本地开发数据库。

## 网络与前端连接

MySQL 和 Redis 不映射宿主机端口，只在 Compose 内部访问。API 仅绑定 ECS 的
`127.0.0.1:8000`，公网入口由 Nginx 和 HTTPS 提供；不要开放 3306、6379、8000 到公网。
同一机器上的其他容器仍属于受信任范围，不应在这台机器运行不可信容器。

API 使用安全 Cookie 和 CSRF 校验。若管理前端继续使用 Vercel，在后端 HTTPS 地址准备好后，
应将前端 `/api/` 转发至后端，并放在 SPA fallback 前，以保持同源登录流程。需实际验证 Cookie、
会话恢复和写入请求，不能仅以健康检查成功当作前后端联通成功。
广州属于中国内地，自有域名正式对外提供 Web 服务需在 ICP 备案完成后进行。

## 内存与持久化

| 服务 | 容器内存上限 | 调整 |
| --- | --- | --- |
| MySQL | 640 MiB | buffer pool 128 MiB、最多 40 个连接、关闭 X Plugin 和 performance schema |
| Redis | 192 MiB | 数据 maxmemory 96 MiB、AOF 持久化、noeviction |
| API | 320 MiB | 一个 Uvicorn 进程 |
| Worker | 384 MiB | 并发 1、预取 1、每 100 个任务回收进程 |
| Beat | 160 MiB | 唯一调度实例 |

长期容器上限合计 1696 MiB，需要给操作系统、Docker 和突发使用保留余量。
限制值不是实际常驻内存，Redis 的 AOF 重写与数据库初始化都可能产生额外峰值。
Worker 的进程回收限制在任务结束后检查，不能防止单个任务在执行中耗尽内存。
迁移容器单独运行，不与长期应用容器同时占用部署预算。

Redis 达到容量上限会拒绝写入，不能改用会随机淘汰队列或 nonce 的策略。任务结果与消息积压需要
监控和定期检查。MySQL 与 Redis 使用独立持久数据卷；Beat 的调度文件在 `/tmp`，重启可能重新
调度周期任务，因此任务本身仍需幂等。不能执行带 `--volumes` 的清理命令来更新服务。

部署后检查 `docker stats --no-stream`、`free -h`、容器 OOM 状态和 Worker 队列积压，并验证
管理员登录、列表查询、OSS 上传。数据库备份需要另行配置并验证恢复，可以存放到已有 OSS。
如果内存长期紧张、发生 OOM 或任务积压，再比较升级 ECS 4GB 与把 MySQL 迁到 RDS 的实际报价。

参考：[阿里云更换系统盘](https://help.aliyun.com/zh/ecs/user-guide/replace-the-operating-system-of-an-instance-1)、
[Celery 内存与预取优化](https://docs.celeryq.dev/en/stable/userguide/optimizing.html)。
