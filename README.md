# juya-admin-api

句芽英语管理与内容核心服务。服务基于 FastAPI、SQLAlchemy 2、MySQL 8.4、Redis、Celery
和阿里云 OSS，负责管理员认证、内容版本、正式/限时权益、反馈、媒体生产、后台投影与匿名统计。

## 本地开发

要求 Python 3.13、uv、MySQL 8.4 和 Redis 7。

推荐使用 Docker Desktop 一键启动真实 MySQL、Redis、迁移、管理员初始化、API、Worker 和
Beat：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local.ps1
```

脚本会等待 `http://127.0.0.1:8000/health/ready` 返回就绪。本地默认登录信息为：

- 管理员账号：`admin`
- 管理员密码：`JuyaLocal@2026`
- TOTP 密钥：`JBSWY3DPEHPK3PXP`

生成当前 6 位 TOTP：

```powershell
uv run python -c "import pyotp; print(pyotp.TOTP('JBSWY3DPEHPK3PXP').now())"
```

这些默认值只允许用于 `local` 或 `test`。如需覆盖，在启动脚本前设置
`JUYA_LOCAL_ADMIN_USERNAME`、`JUYA_LOCAL_ADMIN_PASSWORD` 和
`JUYA_LOCAL_ADMIN_TOTP_SECRET`。初始化是幂等的，再次启动会重置同名本地管理员的密码、
TOTP、锁定状态和失败次数。

停止本地容器但保留数据库 volume：

```powershell
docker compose -f .\docker-compose.dev.yml stop
```

启动脚本不会删除 volume。只有明确需要清空全部本地数据时才使用带 `--volumes` 的清理命令。

不使用 Docker 时，需要自行准备 MySQL 8.4 和 Redis 7，再执行：

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
- 管理写请求使用安全 Cookie、TOTP 与 `X-CSRF-Token`。
- 对象仅保存 OSS object key，签名 URL 默认 5 分钟并受权益到期时间截断。
