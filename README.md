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
详见 [OSS 本地联调说明](docs/operations/aliyun-oss.md)。本地、测试和生产默认
`JUYA_CONTENT_SECURITY_ENABLED=false`，素材确认跳过内容审核并保留文件校验。
OCR/音频生产仍需配置对应外部服务。

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

完整本地 SQL/Redis 验收建议使用隔离运行器，不需要手工配置数据库口令：

```powershell
uv run python scripts/test-v13-isolated.py juya-admin-api --suite -q
```

运行器从既有本地 MySQL 容器读取连接配置，在内存中传递；每轮分别为一般用例和运营用例
创建 UUID 测试库、临时 Redis 容器和随机回环端口，先迁移到最新 schema，再运行测试，最后
只清理本轮创建的资源。不会清理主站库、旧测试库或共享 Redis，也不会拉取镜像或调用云测试。
本机需已有 `redis:7-alpine` 或 `redis:7.4-alpine` 镜像。默认自动命名；不要继承旧的
`JUYA_V13_TEST_DATABASE` 固定库名。只运行部分用例时省略 `--suite` 并传入 pytest 文件/选项。

2026-10-02 的本地收尾、云权限延期及素材验收边界见
[最新本地验收记录](docs/implementation/local-closeout/acceptance.md)。

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

## V1.3 内容与音频改版（2026-10-01）

实施方案与证据见 [V1.3 方案](docs/implementation/v13-content/design.md)、[需求对照](docs/implementation/v13-content/traceability.md) 和 [验收记录](docs/implementation/v13-content/evidence.md)。数据库需先增量迁移至 `0015`；两个后端最低 schema 版本为 15，不清库。人工录入、上传与 OCR 使用一个结构化草稿，发布固定词条、原图和音频版本，候选音频启用不会替换线上场景。音频使用整段文件与每句毫秒区间，缺文件、真实时长、逐句试听确认均阻止发布。TTS 本期关闭。

运行镜像包含 FFmpeg/ffprobe。本地、测试和生产均默认关闭内容审核
（`JUYA_CONTENT_SECURITY_ENABLED=false`），不调用云审核、不要求人工审核。
素材确认保留字节读回、格式/尺寸/时长和哈希校验，记录 `security_status=SKIPPED`，
不伪造 `PASSED` 或审核回执；关闭审核时该状态可用于导入、发布、批量任务和资源读取。
身份、权益、固定版本绑定与素材归属校验保持有效。

发布检查接口正常返回 HTTP 200 和 `ready/error_codes/warning_codes`，内容缺项通过检查结果展示。
只上传原图会生成空草稿，还需填写标题、对话、词汇、短语和来源，绑定整段音频并确认逐句时间。
实际发布命令仍会校验这些内容，未通过时返回 HTTP 409。

原阿里云审核实现保留。以后启用时设置 `JUYA_CONTENT_SECURITY_ENABLED=true`、
`JUYA_CONTENT_SECURITY_PROVIDER=aliyun` 及独立内容安全凭据并重启 API/Worker。
启用后 `SKIPPED` 素材不再可用，需要重新确认并完成审核；图片同步审核，音频异步提交/查询，
任务 ID 持久化后重试查询，不重复提交。`local` 提供方仅允许 local/test 环境且必须
显式开启 `JUYA_CONTENT_SECURITY_LOCAL_FIXTURES_ONLY=true`，仅接受 `/fixtures/` 合成素材。

百度识别需同时设置 `JUYA_OCR_PROVIDER=baidu`、API Key/Secret，并在管理页记录当月账户免费/付费额度核验、月度内部上限，再开启数据库 OCR 配置。OCR 默认关闭，上传不触发识别；重试与重识别使用新的管理员命令。数据库预占与 worker 一次认领保护重投递，未知/超时按消耗计数。安全素材确认、OCR 额度和内容发布各自独立校验。账户实际免费额度、付费状态和跨账号调用量需由控制台核验。

本地百度配置放在本仓库被 Git 忽略的 `.env`，不要把真实凭据写入 `.env.example`：

```dotenv
JUYA_OCR_PROVIDER=baidu
JUYA_BAIDU_OCR_API_KEY=<百度应用 API Key>
JUYA_BAIDU_OCR_SECRET_KEY=<百度应用 Secret Key>
```

修改后需要同时重新构建并启动 API 和内容 Worker；已运行的容器不会自动读取 `.env`：

```powershell
docker compose -f .\docker-compose.dev.yml up --build -d --no-deps admin-api admin-worker-content
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

打开后台 `/content/import` 保存 OCR 设置。月度内部上限 `0` 会阻断全部识别；设置不能超过已核验免费额度。
先上传图片形成草稿，再在场景编辑页点击“保存并识别原图”；在 OCR 候选页选择标题、对话、词汇和语块后采纳。
原图中的背景文字、图标和说话人标记可能被识别为独立行，采纳前要对照原图校对。
2026-10-01 本地验收的内部上限为 `3`，识别消耗 `1` 次；这只是当时的内部预算，不代表百度控制台的账户余量。

实际 OSS 浏览器上传需为后台来源配置 bucket CORS；验收时分别记录浏览器直传、服务端字节读回、内容安全和百度账户，不将 HTTP 测试替代外部供应商验收。本轮未推送或部署生产，小程序前端待最终设计稿。

## 接口与安全

- 管理接口：`/api/v1/admin`
- 服务间接口：`/internal/v1`
- 健康检查：`/health/live`、`/health/ready`
- 内部接口使用 VPC 与 HMAC-SHA256，并校验时间戳和一次性 nonce。
- 管理员使用账号密码登录；管理写请求使用安全 Cookie 与 `X-CSRF-Token`。
- 对象仅保存 OSS object key，签名 URL 默认 5 分钟并受权益到期时间截断。


## V1.3 统一主站刷新（2026-10-01）

本轮隔离 MySQL 回归使用以下命令，测试连接从现有本地 MySQL 容器读取且不输出凭据。两个仓库需先 `uv sync --locked`；隔离 Redis 需在 6398 端口可用。脚本只允许 `juya_v13_` 前缀数据库，避免落到共享开发库。

```powershell
uv run python scripts/test-v13-isolated.py juya-admin-api -q --ignore=tests/integration/test_operations_v13.py
$env:JUYA_V13_TEST_DATABASE = 'juya_v13_ops_20261001'
uv run python scripts/test-v13-isolated.py juya-admin-api -q tests/integration/test_operations_v13.py
Remove-Item Env:JUYA_V13_TEST_DATABASE
uv run python scripts/test-v13-isolated.py juya-miniapp-api -q
```

当前本地统一主站：后台 <http://127.0.0.1:5173/>，管理 API <http://127.0.0.1:8000/docs>，用户 API <http://127.0.0.1:8001/docs>。两个 API 使用真实共享 MySQL/Redis，schema 最低版本16。

已有本机 Docker 栈刷新源码并保留运行时配置：

```powershell
cd D:\个人\juya\juya-admin-api
uv run python scripts/refresh-v13-local.py
Invoke-RestMethod http://127.0.0.1:8000/health/ready
Invoke-RestMethod http://127.0.0.1:8001/health/ready
```

该脚本已在本机实际执行，重建统一镜像、迁移并刷新两个 API、管理内容/领域 Worker、管理 Beat 和用户 Worker。它读取既有本地容器环境，不写 `.env` 或输出凭据；依赖既有管理栈及用户 API 环境，不能代替首次安装。直接运行默认 Compose up 可能重新采用默认配置，当前带 OSS 配置的栈用本节刷新命令。

停止但保留数据库卷：

```powershell
cd D:\个人\juya\juya-admin-api
docker stop juya-main-mini-api juya-main-mini-worker
docker compose -f docker-compose.dev.yml stop
```

前端保留现有5173终端；如未运行，在 `juya-admin` 执行 `pnpm dev --host 127.0.0.1 --port 5173 --strictPort`，停止按 Ctrl+C。服务异常先分别检查两个 ready 与容器状态。完整验收与外部边界见[统一验收](docs/implementation/v13-unified/acceptance.md).
