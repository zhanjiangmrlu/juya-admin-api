# 本地发布到现有 ECS

四个 Git 仓库保持在同一个父目录。Windows 入口是 `juya-admin-api/deploy/publish.ps1`，工作区的 `publish.ps1` 是快捷入口。不带参数运行会显示项目和环境选择菜单。

```powershell
# 从工作区父目录运行
.\publish.ps1

# 四个项目：全部测试、全部构建完成后，再逐个上传部署
.\publish.ps1 -Project all -Environment test

# 单项目发布；Project 也支持 juya-admin-api、juya-miniapp-api、juya-miniapp-h5
.\publish.ps1 -Project juya-admin -Environment test

# 预览、只读检查、仅本地测试打包、人工回滚
.\publish.ps1 -Project all -Environment test -Action plan
.\publish.ps1 -Project all -Environment test -Action doctor
.\publish.ps1 -Project all -Environment test -BuildOnly
.\publish.ps1 -Project juya-admin-api -Environment test -Action rollback
```

没有工作区快捷入口时，直接用 `juya-admin-api/deploy/publish.ps1`，参数相同。两个前端仓库各自的 `deploy/publish.ps1` 会默认选择本仓库项目。Linux 可直接用 Python 入口：

```bash
juya-admin-api/.venv/bin/python juya-admin-api/deploy/local-release/publish.py \
  --project all --environment test --action publish
```

## 本机准备

- Node.js 22.22.x、pnpm 11.22.0、uv、Git、OpenSSH 的 ssh/scp。
- 启动 Docker Desktop 并切到 Linux 容器；后端镜像固定构建为 Linux/amd64。
- 在两个后端仓库运行 `uv sync --frozen`。后台的虚拟环境也负责运行统一入口。
- FFmpeg 与 FFprobe 在 PATH 中；Windows 可在本地配置的 `local_tools.media_bin` 指定二者所在目录，PowerShell 入口只在本次执行期间加入 PATH。
- 完整后端测试使用 UUID 临时数据库/Redis，不重启共享开发数据库。预先拉取 `mysql:8.4`、`redis:7-alpine`，避免首次测试缺镜像。
- 测试发布要求 `test` 分支；生产要求 `main` 分支。选择的仓库须已提交且无未提交修改；脚本会再次检查构建期间的源码变化。

## 本地配置

把 `config.example.json` 复制为父目录的 `.local-release/config.json`。配置不进入任何 Git 仓库，支持 `-Config <路径>`。这里只保存地址、目录、项目名、域名和 SSH 密钥文件路径，不保存密码、私钥内容或业务环境变量。

`test` 和 `production` 必须分别填写 `ssh.host`。没有生产连接信息时，生产会停止，不借用测试配置。同一服务器上的生产目录、站点入口、Compose 项目和环境文件必须与全部测试项目隔离。SSH 使用密钥或 agent、BatchMode 和严格主机指纹检查；首次连接请正常核实服务器指纹。

ECS 前提：Python 3.12+、Docker Compose v2、Nginx、curl、现有域名证书和站点配置。后端的 `compose_env_file` 留在服务器，必须明确引用绝对路径的 `JUYA_RUNTIME_ENV_FILE`，且其 `JUYA_ENVIRONMENT` 与选定环境一致。

生产 Compose 文件是独立配置；生产端口、miniapp 的 Bucket 和共享网络必须明确指定。首次建立生产数据库、账号、运行时环境文件、依赖网络、站点和现有发布基线属于环境准备，发布脚本不替代这些步骤。确认这些资源后，再填写生产 SSH host 和真实域名；示例域名不能作为已上线证明。

## 流程和回滚

| 项目 | 本地检查和构建 | ECS 发布 |
| --- | --- | --- |
| juya-admin | frozen install、完整 check、Vitest；测试 build:test，生产 build | Nginx 静态站点 |
| juya-miniapp-h5 | frozen install、完整 check、Vitest；build:h5，显式 API 地址 | Nginx H5 静态站点 |
| juya-admin-api | uv sync、Ruff、mypy、完整隔离测试；Docker build/save | 先数据库备份、停止 beat/worker/API、迁移，再启动并检查 |
| juya-miniapp-api | uv sync、Ruff、mypy、完整隔离测试；Docker build/save | 独立 API/worker/beat；不执行数据库迁移 |

H5 的 API 地址按现有同源反向代理配置构建，readiness 使用独立 API 域名。微信小程序本体需通过微信开发者工具上传，不是 ECS 静态站点。

所有选中项目先完成检查与打包，任一失败则不上传。部署按后台 API、小程序 API、后台页面、H5 的顺序执行；某个项目部署失败，会停止后续项目。之前已成功部署的项目保留新版本，不进行跨四个项目的分布式事务回滚。

上传包有传输 SHA256、逐文件校验和、项目/环境/提交身份；拒绝损坏、未登记文件、符号链接和越界路径。包只含网站或镜像及白名单 Compose 辅助文件，服务器环境文件不会下载或打包。构建强制显式 API 地址，并检查生成的 JS。

前端切换站点链接后，检查本机 readiness、HTTPS 页面和入口 JS 是否与新包一致；失败或中断会恢复原链接和状态。首次发布保留原有云效目录/链接。与旧云效部署共享锁；不要用云效同时发布同一项目，避免发布顺序互相覆盖。

后端核对镜像身份、Linux 平台、Compose/runtime 环境和持久卷；admin 在迁移前备份数据库，恢复使用不可变镜像 ID。既有旧格式包会先转换为完整可回滚基线。失败恢复保留原 worker/beat 启停状态；成功发布会启动所有配置的应用服务，并检查 readiness、进程、容器健康和重启次数。

公网 readiness 失败或检查期间 Ctrl+C 会调用该项目回滚。手动 `-Action rollback` 在当前与上一版本之间切换；调用前会再次检查完整性。数据库升级不会自动降级；涉及不兼容 schema 的变更，应先设计兼容迁移，必要时由人工使用数据库备份恢复。

## 发布记录

本机包及仅包含阶段/版本身份的 `release.log` 位于 `.local-release/runs/<运行ID>`。前端服务端记录位于配置 root 的 `web-state.json`，版本位于 `releases/<发布ID>/site`；API 使用 `current-release`/`previous-release` 链接及 `backups` 数据库备份。

历史包、镜像、版本目录和数据库备份保留以便排错/恢复；定期检查 ECS 与本机磁盘。清理前至少保留当前和上一版本及其镜像、数据库备份。服务器突然断电或被强制 SIGKILL 时，需从版本记录检查状态并按需人工回滚。

`-Action doctor` 只验证连接、工具和现有路径可用，正式部署仍会执行镜像、运行环境和业务 readiness 检查。检查通过不代表某次版本已经上线。
