# juya-admin-api

句芽英语管理与内容核心服务。服务基于 FastAPI、SQLAlchemy 2、MySQL 8.4、Redis、Celery
和阿里云 OSS，负责管理员认证、内容版本、正式/限时权益、反馈、媒体生产、后台投影与匿名统计。

## 本地开发

### 使用 Docker 启动（推荐）

先安装并打开 Docker Desktop，等待 Docker Engine 就绪。使用 Docker 时，不需要在宿主机
单独安装 Python、uv、MySQL 或 Redis。打开 PowerShell 执行：

```powershell
cd D:\个人\juya\juya-admin-api
docker compose -f .\docker-compose.dev.yml up --build -d
```

该命令会构建当前代码，启动 MySQL、Redis，执行数据库迁移和本地管理员初始化，再启动 API、
两个 Worker 和 Beat。已有本项目容器时也可使用此命令；首次构建需要下载镜像和依赖。
Compose 使用的本地默认配置已写在 `docker-compose.dev.yml` 中，无需复制 `.env.example` 即可启动。

执行以下命令检查就绪状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready
docker compose -f .\docker-compose.dev.yml ps
```

健康检查应返回 `status: ready`，且 `mysql`、`schema`、`redis`、`configuration` 均为 `true`。
`migrate` 和 `seed-local-admin` 是一次性任务，正常完成后退出；其他服务应保持运行。

| 服务     | 本地地址或端口                       |
| -------- | ------------------------------------ |
| 管理 API | `http://127.0.0.1:8000`              |
| 就绪检查 | `http://127.0.0.1:8000/health/ready` |
| MySQL    | `127.0.0.1:3306`                     |
| Redis    | `127.0.0.1:6379`                     |

也可以在端口 `8000`、`3306`、`6379` 都空闲时使用启动脚本，它会自动等待健康检查通过：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1
```

脚本会预先检查端口。如果本项目的 MySQL 或 Redis 容器已经运行，它也会报告端口占用，此时直接
使用上面的 `docker compose ... up --build -d` 命令。Compose 的端口映射以 YAML 配置为准。

### 登录管理后台

API 就绪后，在另一个 PowerShell 终端启动前端：

```powershell
cd D:\个人\juya\juya-admin
# 首次启动时安装依赖
pnpm install
pnpm dev --host 127.0.0.1 --port 5173 --strictPort
```

浏览器打开 <http://127.0.0.1:5173/>，登录成功后进入工作台。前端环境要求和代理配置见
[juya-admin README](../juya-admin/README.md)。本地默认登录信息为：

- 管理员账号：`admin`
- 管理员密码：`JuyaLocal@2026`

登录只需账号和密码，无需 TOTP 动态验证码。这些默认值只允许用于 `local` 或 `test`。
如需覆盖，在执行 Compose 命令或启动脚本前设置
`JUYA_LOCAL_ADMIN_USERNAME` 和 `JUYA_LOCAL_ADMIN_PASSWORD`。初始化是幂等的，再次启动会重置
同名本地管理员的密码、锁定状态和失败次数。数据库中的旧 TOTP 字段仅为兼容现有表结构保留，
不参与登录验证。

### 停止服务与排查启动问题

前端在运行 `pnpm dev` 的终端按 `Ctrl+C` 停止。后端执行以下命令停止容器并保留数据库 volume：

```powershell
docker compose -f .\docker-compose.dev.yml stop
```

启动脚本不会删除 volume。只有明确需要清空全部本地数据时才使用带 `--volumes` 的清理命令。

查看启动日志：

```powershell
docker compose -f .\docker-compose.dev.yml logs --tail 100 admin-api migrate seed-local-admin
```

- Docker Engine 不可用：先打开 Docker Desktop，确认 `docker info` 能正常返回
- 端口占用：先用 `docker compose -f .\docker-compose.dev.yml ps` 确认是否为本项目容器；如果是，使用 Compose 命令继续启动；如果是其他服务，需要先解决端口冲突
- 前端打开空白或提示服务不可用：先确认后端就绪，再刷新页面；前端首次访问工作台会检查登录会话
- 本地 Compose 默认使用 OSS 占位配置，可用于登录和页面查看；图片上传需要切换到真实测试 Bucket，并配置 Bucket CORS。已有本地容器运行时执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local-oss.ps1 -CredentialFile 'D:\个人\juya\doc\oss信息.txt'
```

该脚本仅重建 API、Worker、Beat，使用私有 `juya-test`（深圳）；不重置数据库和登录账号，
不将密钥复制到仓库。后续重新执行普通 Compose `up` 会恢复默认占位配置，需再次运行此脚本。
在 OSS 控制台为测试 Bucket 添加 `http://127.0.0.1:5173` 和 `http://localhost:5173` 两个来源，
允许 POST、GET、HEAD，允许头 Content-Type、Range，暴露头 ETag、x-oss-request-id。
详见 [OSS 本地联调说明](docs/operations/aliyun-oss.md)。OCR/音频生产仍需配置对应外部服务。

### 不使用 Docker

需要 Python 3.13、uv，并自行准备 MySQL 8.4 和 Redis 7，再执行：

```powershell
uv sync --locked
$env:JUYA_MIGRATION_DATABASE_URL='mysql+pymysql://root:password@127.0.0.1:3306/juya'
uv run alembic upgrade head
uv run uvicorn juya_admin_api.main:app --reload
```

完整环境变量见 `.env.example`。OSS Bucket 必须设置为私有；生产环境建议给 ECS/ACK
绑定最小权限 RAM 角色，不在流水线或镜像中保存 AccessKey。

## 质量门禁

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=juya_admin_api --cov-report=term-missing
```

MySQL 集成测试需设置 `JUYA_TEST_DATABASE_URL`，该地址必须指向可清理的隔离测试库。

## 进程角色

同一镜像通过 `JUYA_PROCESS_ROLE` 启动四类长期进程：

- `admin-api`
- `admin-worker-content`
- `admin-worker-domain`
- `admin-beat`

数据库迁移是独立的 `migrate` 一次性进程，应用进程不会自动升级 schema。生产运行账号不应拥有 DDL 权限。

## 云效与 ECS 部署

单后端、2GB ECS 的起步配置见 [小内存 ECS 部署说明](docs/operations/ecs-2gb.md)，
使用 `deploy/docker-compose.ecs-2gb.yml`，包含自建 MySQL、Redis、单 Worker 与 Beat。
这套配置需先完成生产密钥、OSS 和管理员账号准备，并按说明执行迁移；不能直接运行本地 Compose 上线。

2026-09-30 的云上试运行状态与私有访问方式见 [ECS 部署记录](docs/operations/ecs-2gb-deployment.md)。

`.aliyun-ci.yml` 使用云效 Flow 的 YAML 结构完成质量门禁、ACR 镜像构建、人工审批和
ECS 主机组分批部署。首次接入时需在云效中配置文件顶部列出的服务连接、主机组和私密变量；
测试数据库必须是隔离库。ECS 上需安装 Docker、Compose 插件和 curl，并把生产环境变量以
`root` 可读权限保存到 `/etc/juya/admin-api.env`。ACR 登录名和登录密码应设为云效私密变量。

部署包中的 `deploy/ecs-deploy.sh` 会先用 `migrate` 角色执行 Alembic，再通过
`deploy/docker-compose.ecs.yml` 更新 API、两个 Worker 和唯一 Beat 实例。迁移失败会在长期
进程更新前终止发布；应用就绪探针失败会恢复上一镜像并让云效任务失败。流水线结构依据云效 Flow 的
`stages/jobs/steps`、ACR 镜像步骤和 ECS `VMDeploy` 主机部署组件编写。

## 接口与安全

- 管理接口：`/api/v1/admin`
- 服务间接口：`/internal/v1`
- 健康检查：`/health/live`、`/health/ready`
- 内部接口使用 VPC 与 HMAC-SHA256，并校验时间戳和一次性 nonce。
- 管理员使用账号密码登录；管理写请求使用安全 Cookie 与 `X-CSRF-Token`。
- 对象仅保存 OSS object key，签名 URL 默认 5 分钟并受权益到期时间截断。
