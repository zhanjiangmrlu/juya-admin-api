# 管理后端云效测试制品部署

本次仅发布管理后端。小程序后端另行安排,小程序客户端编译后上传微信,不作为 ECS 静态站点发布。

## 云效控制台配置

1. 新建 `juya-admin-api-test` 流水线,通过现有 GitHub 服务连接选择管理后端仓库和 `test` 分支;仅配置 `test` 的推送触发器。
2. 使用 Linux/amd64 指定容器环境,保留云效官方 `build-steps/alinux3:latest`。测试脚本在任务容器中直接启动临时 MySQL 8.4、Redis 7.4 和 Python 3.13,运营测试额外使用独立空库,不会连接 ECS 数据库。首次运行会安装缺少的依赖、下载 MySQL 二进制并编译 Redis 与固定版本 FFmpeg,需要可访问系统软件源、MySQL CDN、Redis 下载站、FFmpeg 官方下载站、uv 下载源和阿里云 PyPI 镜像。建议 4C8G,超时保持 60 分钟。进入“变量和缓存 → 缓存设置”,启用 `/root/.cache` 云端缓存,以复用 Python、依赖包和已校验 FFmpeg;首次仍须准备工具。单独的测试任务关闭 Docker Daemon;镜像构建任务开启 Docker Daemon。

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
export JUYA_DOCKER_CACHE_DIR=/root/.cache/juya-admin-api-buildkit
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

## 2026-10-13 跨运行镜像缓存

第 12/13 次真实制品分别为 294,834,679 和 294,840,886 字节,87 个 Python 包及版本相同。
两次包内均包含本次运行新写入的 Python INSTALLER 和 APT 安装日志,FFmpeg 安装层没有复用。
任务初始化至 Python 包开始落盘分别为 25 分 21 秒、62 分 58 秒;FFmpeg 及系统依赖的实际解包、
配置只用了 13/12 秒。制品不能进一步区分此前的镜像拉取、缓存恢复与依赖下载耗时。

本次使用 Buildx 的 `type=local` 外部缓存,由云效文件缓存负责跨运行传输。
`mode=max` 同时保留 builder 中间阶段,从而让未变化的 Python 依赖和 FFmpeg 层可被下次源码构建复用。
仅增加 Dockerfile 的 `RUN --mount=type=cache` 或仅开启任务容器 `/root/.cache`,不能保证已有
Docker Daemon 内的缓存会跨公共 Runner 复用。

### 已存在的 Flow 流水线需要调整的两项

1. **变量和缓存 → 缓存设置**: 启用云端文件缓存 `/root/.cache`。如果已经启用,保持即可。
   缓存按任务隔离;测试任务的缓存不会自动传给镜像构建任务。
2. **变量和缓存 → 变量**: 增加普通变量 `JUYA_DOCKER_CACHE_DIR`,值为
   `/root/.cache/juya-admin-api-buildkit`。也可以在镜像构建命令中增加上面的 `export` 行,两种方式选一种。
   若用变量配置,原来的 `sh deploy/build-artifact.sh ...` 命令无需改动。

镜像任务继续使用官方 Alinux3、Linux/amd64、4C8G 和开启的 Docker Daemon。首次填充缓存仍可能
耗时较长,镜像任务超时可设 90 分钟以覆盖此前 66 分钟的冷构建;测试任务保持原超时。
保持当前测试 → 构建 → 部署顺序、test 分支限制、制品路径和主机部署命令。
`.aliyun-ci.yml` 是同步后的模板,修改仓库文件不会自动替代控制台已保存的流程。

### 脚本行为和验收

- 配置变量后,`deploy/cached-build.py` 先用不联网的 scratch 小镜像实际探测当前构建器的本地缓存
  导出/导入和 Docker tar 导出。若 Buildx 缺失、驱动不支持缓存导出或 Flow 代理拒绝接口,立即失败,
  不启动正式依赖下载,不生成可上传制品。该能力仍需首次真实云效运行确认。
- 正式构建显式导入 `current` 缓存,用 `--output type=docker` 直接导出镜像包,
  省去先导入 Daemon 再 `docker save` 的步骤。镜像身份、SHA256、压缩和 ECS 部署校验继续执行。
- 缓存成功导出后才替换旧版本,每次保留一代,避免 local cache 的 blob 无限累积。
  缓存目录必须专用且位于源码目录外。单代超过 1.5 GiB 时不更新缓存并输出提示;
  云效文件缓存的 2 GiB 限制针对该任务全部缓存目录的压缩总量,还需核对平台上传日志。
- 新制品包含 `build-metrics.json`,记录探测、正式构建耗时、缓存导入/导出状态和 `CACHED` 数量,
  并纳入 SHA256SUMS。此文件不包含缓存传输到云效、制品上传或 ECS 部署耗时。
- 首次运行应见 `cache miss`、`cache saved bytes=...`,随后核对云效的“缓存上传成功”。
  第二次普通源码变更应见缓存下载成功、`cache restore`、依赖层 `CACHED` 和新制品的
  `cache_imported: true`。只有这两次真实运行才能确认跨 Runner 加速,本地秒数不是云端承诺。
- 若需恢复旧构建方式,删除 `JUYA_DOCKER_CACHE_DIR` 变量/`export` 行即可;完整测试和部署校验仍保留。

本次本机验证使用云效官方 Alinux3 容器内的 Python 3.6.8、Buildx 0.14.0,连接本机 Docker
Daemon。完整构建及导出两次均成功,分别为 27.646 秒和 24.305 秒,压缩各约 3 秒;
第二次使用仅增加源码注释的临时上下文,日志确认导入外部缓存、5 个步骤 CACHED,
保存缓存约 354 MiB。两次共享本机构建器且复用了已有镜像层,不能作为云效冷构建或跨 Runner
缓存已经生效的证据。第二次制品的 20 项 SHA256、gzip 完整性及加载后镜像身份校验通过;
运行镜像的 UID 10001、项目导入、固定 FFmpeg 7.1.5 和实际 AAC 编码/探测/解码通过。
缓存及部署回归 32 项通过,独立代码审查未发现 P1/P2 问题。云效控制台配置尚未修改,
首次公共 Runner 能力探测及连续两次真实流水线验收仍待完成。

参考: [云效文件与镜像缓存](https://help.aliyun.com/zh/yunxiao/user-guide/pipeline-cache)、
[Docker 外部缓存](https://docs.docker.com/build/cache/backends/)、
[Docker local cache 的目录增长行为](https://docs.docker.com/build/cache/backends/local/)。

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

### 外部 Dockerfile 解析器下载失败

2026-10-12 的 `raw-6f417bb1-9f09-4af9-a2ee-a371a5ef77a4_3.log` 在构建阶段解析 Dockerfile 第 1 行时失败。原 `# syntax=docker/dockerfile:1.7` 要求从 Docker Hub 下载外部解析器;对 `registry-1.docker.io` 的连接被拒绝,尚未拉取 Python 基础镜像或执行构建指令。Dockerfile 已移除该指令,改用 BuildKit 自带解析器,保留现有多阶段构建、APT 缓存挂载、固定 FFmpeg 版本、非 root 用户和健康检查。[Docker 官方解析器说明](https://docs.docker.com/build/buildkit/frontend/)。

本机实际 `docker build --check` 通过且没有外部解析器下载步骤;`deploy/build-artifact.sh` 完成镜像构建、导出及压缩,制品 SHA256、gzip 完整性及镜像 ID 校验通过。导出镜像的 UID 为 10001,项目导入和实际 AAC 音频生成/探测/完整解码检查通过;相关部署及容器回归测试 22 项通过。此次本机验证使用了已有构建缓存,不能证明云效的 Python 基础镜像、GHCR uv 镜像或系统软件源均可访问,后续须以新流水线日志确认。构建任务保持 Docker Daemon 开启,执行命令和制品/主机配置无需因此修改。

### Docker Hub 基础镜像拉取超时

`raw-d8430d17-4713-4dd1-a7cf-677e77c9f0ca_3.log` 已越过解析器阶段,但拉取 `docker.io/library/python:3.13-slim-trixie` 元数据时连接超时;同一日志中 `ghcr.io/astral-sh/uv:0.11.16` 元数据成功读取。当前账号的任务下拉列表只显示杭州公共构建集群,无需通过“新建构建集群”购买 VPC 资源。

Dockerfile 改用官方 `ghcr.io/astral-sh/uv:0.11.16-python3.13-trixie-slim`,固定镜像索引摘要 `sha256:d57fc364ed714127c162a01b316bfb7b9a3fd09d97c665781a5607acaa35d254`。builder 和 runtime 共用同一基础镜像,实际拉取核对为 Debian 13、Python 3.13.13 和 uv 0.11.16。基础镜像元数据及层均从 GHCR 获取,不再直接引用 Docker Hub;运行阶段移除自带 uv/uvx,保留原固定 FFmpeg、UID 10001、独立迁移和健康检查。镜像来源及版本标签方式见 [uv 官方 Docker 文档](https://docs.astral.sh/uv/guides/integration/docker/)。

构建阶段的 Python 生产依赖也从阿里云 PyPI 镜像下载:用 `uv export --locked --no-dev --no-emit-project` 导出锁定版本和哈希,通过 `uv pip sync --require-hashes` 安装,再以 `--no-deps` 非 editable 安装项目。业务依赖及 `uv.lock` 不变;依赖下载、哈希、软件安装和镜像导出失败仍会阻止后续部署。

本机实际拉取上述固定摘要的 Linux/amd64 镜像成功,不复用镜像构建层缓存的完整构建通过。运行镜像中的 87 个 Python 分发包逐一与 `uv.lock` 版本匹配,未包含 Ruff、Mypy 或 pytest,项目为非 editable 安装;UID 10001、固定 FFmpeg 版本、实际 AAC 生成/探测/完整解码均通过。相关部署与容器测试 22 项通过;实际 `deploy/build-artifact.sh` 完成镜像构建和制品导出,制品 SHA256、gzip 完整性与镜像 ID 校验通过。这些是本机验证结果,不能替代云效网络及真实主机部署验收。

继续使用杭州公共构建集群、Linux/amd64、现有 `build-steps/alinux3:latest` 和开启的 Docker Daemon。原构建命令、制品名称/下载路径、主机组及部署脚本保持原值;推送新提交后新建一次流水线运行并核对源码提交 SHA。GHCR 元数据在之前的云效日志中成功读取不代表这个组合镜像的全部层已在云效拉取成功,完整构建、制品上传及 ECS 部署仍须真实运行验收。

### 离线测试夹具的工作区依赖

2026-10-11 的 `raw-7c47490f-c209-4435-92a1-faa4207c190f_3.log` 已确认原生 MySQL/Redis 启动、迁移至 head、Ruff 和 Mypy 通过。
首轮 pytest 为 413 通过、4 失败、5 跳过,覆盖率 82.35%;失败均来自 `tests/scripts/test_v13_isolated.py`,提示 `Run uv sync --locked in ... first`。
这些离线用例模拟了命令执行,但原先遗漏文件系统夹具,仍检查开发机相邻仓库的 `.venv`。
云效单仓库工作区和临时 `UV_PROJECT_ENVIRONMENT` 不符合这一开发机布局,因此测试尚未调用命令替身便提前退出。
夹具现改为临时工作区并创建仅供路径检查的 Python 占位文件,保留实际脚本的缺少依赖检查、资源隔离和清理断言。
无需在管理后端流水线克隆小程序后端、创建真实跨仓库虚拟环境或忽略失败用例。
此失败发生在测试阶段,主机部署尚未执行;当前测试命令和域名配置无需因此修改。独立运营测试及后续镜像构建、部署仍待真实流水线验收。

### 软件源索引下载导致准备超时

`raw-3afe2b25-b8ba-438c-9690-be599c162f31_3.log` 停留在依赖安装前的仓库索引下载,`alinux3-plus` 的 41 MB 索引用时约 18 分钟;运行截图显示 30 分钟超时终止。该次尚未开始原生服务初始化或测试。
同款官方镜像中 GCC、Make、Tar、Gzip、XZ、ncurses-libs 和 libtirpc 已安装,只有 libaio、numactl-libs 未安装。
`deploy/ci-install-rpm.sh` 先用 RPM 本地查询筛选缺少的包,已有包不请求安装或主动升级;所需包齐全时完全跳过 DNF。
安装只启用 `alinux3-os` 的官方 HTTPS 地址,保留 GPG 校验并关闭弱依赖,避免刷新 plus、updates、powertools、EPEL 和 Docker 仓库。
MySQL/Redis 准备和 FFmpeg 编译准备共用此检查;软件安装、音频验证和测试失败仍阻止部署。
仓库筛选选项见 [DNF 官方命令说明](https://dnf.readthedocs.io/en/stable/command_ref.html)。不需要新增 ACR 或自定义构建镜像。
本机同款官方 Linux 镜像的实际安装验证退出码为 0:基础索引约 5.5 MB,仅安装两个包约 69 KB,总耗时 188 秒;原有 GCC 版本未改变。再次检查全部依赖及 FFmpeg 编译依赖时均直接跳过仓库刷新。
只延长超时不能消除下载开销;首次准备设为 60 分钟是下载及源码编译的余量,真实云效速度和端到端成功仍须新日志确认。

### FFmpeg 和 Python 下载导致 60 分钟超时

`raw-9aca5ad9-65cd-490b-bdb2-518d017c9fbc_3.log` 已确认上面的 RPM 优化生效:两项基础依赖约 2 分钟安装完成。随后 FFmpeg 官方源码下载约 10 分钟、配置及编译约 6 分钟,Python 下载约 3 分钟;21:33 开始安装 Python 包,22:05 仍只有部分下载完成,截图显示 22:10 因 60 分钟上限终止。该次仍未开始数据库迁移或 pytest,与域名、证书和 ECS 部署无关。

Python 安装现在使用 `uv export --locked --no-emit-project` 从原 `uv.lock` 导出完整依赖版本及哈希,再通过 `uv pip sync --require-hashes` 从 `https://mirrors.aliyun.com/pypi/simple/` 下载。当前项目使用 `--no-deps --editable .` 独立安装,保留源码测试方式;之后的命令使用 `uv run --no-sync`,避免重新按锁文件的 PyPI URL 下载。镜像缺少固定版本或文件哈希不一致时直接失败,不降级、不重新选版本。业务依赖声明和锁文件不变。机制见 [uv 官方命令说明](https://docs.astral.sh/uv/reference/cli/)。

`deploy/ci-prepare-ffmpeg.sh` 保留 7.1.5 官方源码 SHA256、原完整编译选项及实际生成/探测/完整解码检查。缓存键包含脚本内容、CPU 架构、系统发行版和 libc;同键用文件锁串行发布,只在编译完整后发布工具。缓存中的两个程序必须通过校验清单和版本验证,损坏或不完整时重新准备,不能将旧缓存视为测试通过。

默认缓存目录为 `/root/.cache/uv`、`/root/.cache/juya-ci-native/python` 和 `/root/.cache/juya-ci-native/ffmpeg-*`;临时虚拟环境、测试数据库和日志仍在本次 mktemp 目录中,不会跨运行复用或进入缓存。必须在 Flow 的“变量和缓存 → 缓存设置”启用 `/root/.cache`,下次同一任务才能恢复缓存;不同任务的缓存互相隔离,详见 [云效缓存说明](https://help.aliyun.com/zh/yunxiao/user-guide/pipeline-cache)。无需增加 ACR 或自定义构建镜像。

同款官方 Alinux3 容器的独立安装验证中,100 个适用于 Linux 的锁定依赖准备约 8 秒,Python 安装、依赖同步及本地项目安装共 16 秒并成功导入 SQLAlchemy、asyncmy 和项目。这个时间是本机 Docker 实测,不能作为云效实际耗时或完整部署成功的证明。

本机完整原生验证进一步越过下载、媒体编译、数据库迁移及静态检查后,发现新增测试的三项收集错误:两个迁移测试不能导入 `tests.integration` 中的共享夹具,另两个目录中的 `test_repository.py` 发生模块名称冲突。原配置在本机同样复现。pytest 现用 `pythonpath = ["."]` 使共享夹具可导入,给 system_config、work_items 两个测试目录增加包标记以区分同名模块,保留原导入模式、严格配置和测试选择。四个相关文件成功收集 13 个用例;未全局切换命名空间解析,其他现有测试的同目录导入方式保持兼容。行为见 [pytest 测试包与导入说明](https://docs.pytest.org/en/stable/explanation/pythonpath.html)。

最终同款 Linux 原生脚本验证退出码为 0:迁移、Ruff、485 个文件格式检查及 191 个源文件 Mypy 检查通过;主测试 656 通过、4 跳过,独立空库运营测试 1 通过,追加后覆盖率 83.29%。暖缓存 FFmpeg 准备不到 1 秒、Python 准备约 1 秒,脚本总耗时 505 秒。此前冷缓存验证已实际完成官方 FFmpeg 源码校验、完整编译和音频生成/探测/解码检查。最终本地验证因 Docker 软件源 DNS/下载不稳定,先在隔离容器内安装了从官方 HTTPS 下载并通过 GPG 签名校验的两个 RPM;该耗时不计入 505 秒,仓库脚本的软件安装失败仍会阻止检查。此结果不代表云效首次运行耗时、镜像构建或 ECS 部署成功,仍须推送新提交并在真实流水线验收。

## 2026-10-12 第 11 次运行: 构建耗时与镜像身份误判

截图显示构建任务约 89 分钟,但提供的 `raw-49ed04a5-6731-4f7b-a0d3-f262296fb2e1_0.log` 只有任务分配和步骤插件下载记录,没有命令执行详细日志,不能据此精确拆分每一步耗时。从这次真实制品读取的文件时间补充确认: Python 包的 INSTALLER 写入时间为北京时间 10:02:46 至 10:02:49;FFmpeg 的 APT 安装日志为 11:16:18 至 11:16:30。Python 依赖已完成后,运行镜像的系统包步骤仍在准备或下载,而解包安装仅约 12 秒。部署日志显示 295,027,124 字节制品从 11:17:49 下载至 11:21:48,平均 1.18 MB/s;构建耗时和 ECS 下载耗时应分别排查。

Dockerfile 现在使用阿里云 HTTPS Debian 主源和安全更新源,保留 Debian 原 Signed-By、APT 签名验证以及 `7:7.1.5-0+deb13u1` 固定版本;不存在忽略校验、降级版本或下载失败继续执行。镜像地址见 [Debian 镜像](https://developer.aliyun.com/mirror/debian) 和 [Debian security 镜像](https://developer.aliyun.com/mirror/debian-security)。同时把生产依赖安装放在 COPY src 之前,源码变更只重建项目包;增加 uv 缓存挂载,保留 APT 下载包及索引缓存,两者均用 sharing=locked。缓存挂载内容不进入运行镜像。构建脚本使用官方 Alinux3 已有的 pigz 并行压缩,缺失时回退 gzip;输出 Docker build 的 plain 日志及构建、导出、压缩阶段的秒数。

需要在 Flow 中确认当前镜像构建任务的缓存恢复/上传日志和后续 CACHED 步骤。`/root/.cache` 文件缓存只覆盖任务容器中的文件,不能单独证明 Docker Daemon 的 BuildKit 层已跨运行复用。公共镜像构建任务的 sidecar 缓存范围及限制见 [云效缓存说明](https://help.aliyun.com/zh/yunxiao/user-guide/pipeline-cache)。保留 Docker Daemon 开启,保持现有 test 分支、测试失败阻断和制品发布方式,无需新增 ACR 或私有构建集群。

部署失败的真实原因已经核对:包内 `image-id.txt` 与配置文件 SHA256 均为 `6805cd7e1ea8d988dfbf24c673df220fd06cd0cd73c72335d5f37dd4cc53056b`,包内 OCI manifest 摘要为 `3f777439381e59a42844638092418458c56485059143d123befb427ee2c21e90`。ECS Docker 29.8.1 使用 containerd 镜像存储,镜像加载后的 `.Id` 为后一个 manifest 摘要;本机 Docker 29.8.2 加载同一包得到相同结果。原脚本直接比较 config ID 和 manifest ID,因此误报 `image identity mismatch`;与改用 GHCR 基础镜像本身无关。这次已核对制品内全部 17 个 SHA256 条目,旧 API、Worker、Beat、MySQL 和 Redis 保持运行且 readiness 为 ready;失败发生在备份、停止应用及迁移之前。

`deploy/image-identity.py` 只使用 Python 标准库,兼容构建环境 Python 3.6 和服务器 python3。它读取镜像包元数据,验证指定标签、Linux/amd64、配置文件实际 SHA256 及 OCI manifest/index 的摘要引用关系。构建阶段统一记录 config 摘要;部署允许同一个镜像的 config、manifest 或 index ID,其他 ID 仍退出 65。部署仍先校验全部制品文件,再加载镜像并检查身份,通过后才备份、停应用和迁移;不会移除身份校验。新增 helper 必须包含在 SHA256SUMS 中,服务器需要 python3 命令。

修改后必须新建一次完整流水线运行,核对源码 SHA 和新制品版本。不要只重试第 11 次部署节点:旧制品内仍携带旧版 ecs-artifact-deploy.sh,不会自动使用仓库中的修复。新的构建命令、制品名称、下载路径和主机部署内联命令保持原值。本机缓存命中时间不能当作杭州公共 Runner 的耗时承诺,最终须记录新的详细构建日志与真实云效发布验收。

本次验证:23 项部署/制品回归通过,新 helper 在官方 Alinux3 的 Python 3.6.8、本机及 ECS 上读取第 11 次真实制品均通过;本机新构建的含 provenance 的 OCI index 身份也通过。使用阿里云 Debian 源的实际构建退出 0,复用已有基础镜像、Python 依赖与 APT 下载缓存,耗时约 78 秒;源码修改的独立临时构建上下文中依赖层与 FFmpeg 层均 CACHED,耗时 9.5 秒。非 root UID 10001、固定 FFmpeg、实际 AAC 编码/探测/完整解码通过。Linux 镜像环境全量 pytest 为 524 通过、143 跳过(未提供独立 MySQL/Redis 或真实云服务测试配置);Ruff、格式和 191 源文件 Mypy 检查通过。Windows 初次全量检查有两个音频用例因 PATH 缺少 ffprobe 失败,同一套代码在含 FFmpeg 的 Linux 运行镜像中两项均通过,未修改音频业务代码或跳过这两项用例。真实云效新运行及正式发布验收尚未完成。

官方 Alinux3 容器通过本机 Docker Daemon 实际执行完整 build-artifact.sh 的缓存命中验证也退出 0:构建 12 秒、docker save 导出 4 秒、pigz 压缩 2 秒;18 条制品 SHA256、gzip 完整性以及记录 config ID 与实际 containerd index ID 的身份校验均通过。这是本机已有缓存的实测,不代表公共 Runner 缓存已经生效。

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
# 测试服务器运行参数补充（2026-10-12）

管理端与小程序后端共享 `juya-admin-small_default` 网络时，
`/etc/juya/admin-api.env` 中的 `JUYA_MINIAPP_API_BASE_URL` 应为
`http://miniapp-api:8000`。容器内的 `127.0.0.1:8001` 无法访问小程序容器。
运行配置变化后必须重新创建应用容器；同时应将 `/etc/juya/compose.env` 的
`JUYA_ADMIN_API_IMAGE` 同步为当前已发布镜像，避免手动 Compose 操作使用旧镜像。

现有测试镜像的 Beat 在 160 MiB 上限下发生容器 OOM，测试 overlay 已改为
`${JUYA_ADMIN_BEAT_MEMORY_LIMIT:-256m}`，在 `/etc/juya/compose.env` 中设置
`JUYA_ADMIN_BEAT_MEMORY_LIMIT=256m`。原基础 Compose 的生产参数保留。
测试常驻服务上限合计因此增加至 1792 MiB，加上小程序服务需要持续观察实际内存，
不能把各容器上限当作这台服务器能够同时承受的峰值。

当前服务器旧制品未修改，通过 `/etc/juya/admin-test-runtime.override.yml` 追加相同
Beat 配置，已运行容器会保留该配置。下一次管理端云效发布需使用包含上述 overlay
修改的代码提交，避免旧版本把内存上限重新改回 160 MiB。
