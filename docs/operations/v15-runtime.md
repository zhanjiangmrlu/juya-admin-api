# V1.5 导入、音频与云服务运行

## 私有导入卷

API 和内容 worker 必须挂载相同的命名卷 `juya_imports:/var/lib/juya/imports`。开发 Compose、独立 ECS Compose、2GB ECS Compose 均已声明；测试覆盖文件合并时继承。固定容器路径 `JUYA_IMPORT_STORAGE_PATH=/var/lib/juya/imports`，勿单独改某个进程。

镜像创建此目录及 `.tmp`，owner/group 为 10001:10001、权限 0700；空命名卷首次创建时由 Docker 复制镜像目录元数据。API 和内容 worker 使用 `.tmp` 作 TMPDIR；原包和真实音频临时文件不占小容量 `/tmp` tmpfs。最终音频仍冻结到私有 OSS；不挂载公开 Web 根目录。

对已有 root-owned 卷或 bind mount，先停写入者并维护权限。运行以下明确预检时使用该部署自己的 Compose 参数，示例为本地；不要使用 `down -v`，避免删掉导入与数据库。

```sh
docker compose -f docker-compose.dev.yml run --rm --no-deps --user 0:0 --entrypoint sh admin-api -ec 'mkdir -p /var/lib/juya/imports/.tmp; chown 10001:10001 /var/lib/juya/imports /var/lib/juya/imports/.tmp; chmod 0700 /var/lib/juya/imports /var/lib/juya/imports/.tmp'
docker compose -f docker-compose.dev.yml run --rm --no-deps --entrypoint sh admin-api -ec 'test "$(id -u)" = 10001; test -w /var/lib/juya/imports; test -w "$TMPDIR"; stat -c "%u:%g %a" /var/lib/juya/imports /var/lib/juya/imports/.tmp'
```

如果既有包文件属于 root，需要针对该卷内已确认属于本服务的文件单独修复 owner，不对未知宿主目录递归 chown。API 和内容 worker 写读同一测试哨兵可验证共享卷；验证后删除本次哨兵。生产迁移必须保留旧批次原包，数据库备份与该卷备份应同步。清理只移除超过保护窗口且无批次/发布引用的对象，不能按年龄删除全部包。

2GB 单 worker 仍须串行处理长音频并监控内存、磁盘与队列；挂载磁盘避免 tmpfs 上限不等于无限容量或1000篇耗时承诺。中断留下的 `.tmp` 文件只在无运行任务时按保护窗口清理。

## FFmpeg 与 CI

运行镜像使用 `python:3.13-slim-trixie`，固定 Debian 包 `ffmpeg=7:7.1.5-0+deb13u1`，安装后核对 dpkg 版本并执行 ffmpeg/ffprobe 版本检查。此包号已查证 [Debian trixie 官方包页](https://packages.debian.org/trixie/amd64/ffmpeg)。仓库更新使包号不可下载时，构建会失败；维护者核验官方新版本并更新版本/验证记录，禁止静默退回未固定版本。

实际云效路径为 `.aliyun-ci.yml → deploy/ci-verify.sh`，仅 test 分支可运行打包/发布。原生 CI 对 apt/dnf runner 准备固定 FFmpeg 7.1.5：缺失或版本不匹配时从 [官方 releases](https://ffmpeg.org/releases/) 获取 tar.xz，校验 SHA256 `de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f`，使用本任务私有工具目录编译。随后生成短 WAV、ffprobe 探测、严格完整解码，失败即停止。

CI 保持 `uv sync --locked`、Ruff check、Ruff format --check、mypy、完整隔离 MySQL/Redis pytest 与覆盖率 >=80；主套和专用运营套均阻断失败，禁用真实 OSS 外部测试。FFmpeg 准备不能跳过真实音频测试；不以 mock 证明可播放。

## 云服务默认关闭

`.env.example` 和 `deploy/.env.runtime.example` 中 ASR、内容 AI、TTS 均为 false，长期 key 为空。启用必须由服务器配置，客户响应不返回长期 key。ASR 的音频域名仅配置本服务已授权私有 OSS 签名域，结果链接只允许提供商已核验域名，禁止任意 URL 与重定向。

TTS 默认字符额度与微元额度均为0，价格版本 unconfigured；没有预算不可合成，即使管理员单独改 `JUYA_TTS_ENABLED=true`。实际启用需设置经确认的价格版本、每日字符限额和每日微元限额，并在上线前证明首次调用成功、第二次缓存、并发预占与预算拒绝。价格单位、地域与核验日必须记账，不把示例值当供应商最新价格。

ASR 默认 `JUYA_ASR_DAILY_SECONDS_LIMIT=0`，文本候选默认 `JUYA_CONTENT_AI_DAILY_TOKEN_LIMIT=0`、`JUYA_CONTENT_AI_MAX_OUTPUT_TOKENS=4096`；无每日预算必须前置拒绝付费调用，最大输出token本身不是每日额度。生产阶段预占最大用量边界并记录提供商actual usage，不能用预估冒充真实计量。只配 key 和 enabled 开关不足以作为上线验收；在未完成可验证预算拒绝前保持 disabled。转录结果只作为候选，不据其标点或总时长制造句边界；真实文件切片成功仍要求句首句尾试听和当前依赖确认。

本轮配置检查不构建镜像、不部署、不运行云效、不调用付费云服务。Linux 镜像内UID/卷复制、ECS升级权限、官方APT完整依赖可解析、原生编译与真实云预算/费用须在对应环境验收。
