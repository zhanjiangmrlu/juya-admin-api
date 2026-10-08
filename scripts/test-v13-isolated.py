"""Run local SQL/Redis tests in fresh resources owned only by this invocation."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
MYSQL_CONTAINER = "juya-admin-api-mysql-1"
OPERATIONS_TEST = "tests/integration/test_operations_v13.py"
DATABASE_PATTERN = re.compile(r"juya_v13_(?:general|ops|mini)_[a-f0-9]{32}\Z")
_interrupts_deferred = ContextVar("interrupts_deferred", default=False)


class RunnerError(RuntimeError):
    """A diagnostic that contains no command arguments or credentials."""


@contextmanager
def _defer_interrupts():
    # 功能:暂缓处理 SIGINT,确保关键资源创建登记或清理完成后再中断。
    # 参数:无。
    # 返回:临界区上下文;退出时恢复信号处理并处理待完成的中断。
    """Finish resource creation/registration or cleanup before handling Ctrl+C."""
    pending = False
    failed = False
    previous = signal.getsignal(signal.SIGINT)

    def remember(_signum, _frame):
        # 功能:记录待处理的中断信号,延迟到关键资源操作完成后处理。
        # 参数:
        #     _signum: 收到的系统信号编号,回调只记录中断而不立即退出。 当前替身保留该形参以兼容
        #       调用接口。
        #     _frame: 收到信号时的 Python 栈帧,回调保留该接口但不读取。 当前替身保留该形参以兼容
        #       调用接口。
        # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
        nonlocal pending
        pending = True

    signal.signal(signal.SIGINT, remember)
    token = _interrupts_deferred.set(True)
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        _interrupts_deferred.reset(token)
        signal.signal(signal.SIGINT, previous)
        if pending and not failed:
            raise KeyboardInterrupt()


def _command(args, *, env=None, cwd=None, input=None):
    # 功能:运行子进程并合并捕获输出,在关键阶段隔离进程组。
    # 参数:
    #     args: 可执行文件及命令行参数列表,直接传给 subprocess,不经 shell 拼接。
    #     env: 传给子进程的环境变量映射;None 表示继承当前环境。
    #     cwd: 子进程工作目录,确保命令在指定仓库中执行。
    #     input: 传给子进程标准输入的文本,如 SQL 或 Compose 配置。
    # 返回:包含退出码及合并标准输出的子进程完成结果。
    critical = _interrupts_deferred.get()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if critical:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        start_new_session=critical and os.name != "nt",
    )


def _redact(output, secrets):
    # 功能:移除命令输出中的敏感值、连接密码和带查询参数的 URL。
    # 参数:
    #     output: 子进程捕获的原始输出,待移除敏感信息。
    #     secrets: 需从诊断输出中删除的敏感值集合,仅在内存中用于脱敏。
    # 返回:删除敏感信息后的诊断文本。
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            output = output.replace(secret, "[redacted]")
            output = output.replace(quote(secret, safe=""), "[redacted]")
    output = re.sub(r"(\w+://[^/@\s]+:)[^/@\s]+(@)", r"\1[redacted]\2", output)
    return re.sub(r"https?://[^\s\"'<>]*[?][^\s\"'<>]*", "[redacted URL]", output)


def _checked(args, label, secrets, **kwargs):
    # 功能:运行命令,失败时以脱敏输出抛出 RunnerError。
    # 参数:
    #     args: 可执行文件及命令行参数列表,直接传给 subprocess,不经 shell 拼接。
    #     label: 命令失败时展示的诊断名称,不包含凭证。
    #     secrets: 需从诊断输出中删除的敏感值集合,仅在内存中用于脱敏。
    #     kwargs: 转交给命令执行器的关键字选项,例如 env、cwd 和 input。
    # 返回:成功命令捕获的标准输出文本;失败时抛出脱敏错误。
    result = _command(args, **kwargs)
    if result.returncode:
        raise RunnerError(f"{label} failed: {_redact(result.stdout, secrets)}")
    return result.stdout


def _database(name):
    # 功能:检查数据库名符合带阶段和 UUID 的独立测试库格式。
    # 参数:
    #     name: 带阶段及 UUID 的独立测试数据库名。
    # 返回:通过独立测试库命名检查的数据库名。
    if not DATABASE_PATTERN.fullmatch(name):
        raise RunnerError("A fresh juya_v13_<general|ops|mini>_<32 hex UUID> database is required")
    return name


def _mysql(sql, secrets):
    # 功能:通过本地 MySQL 容器执行独立测试数据库 SQL。
    # 参数:
    #     sql: 需要在独立 MySQL 测试环境执行的 SQL 文本。
    #     secrets: 需从诊断输出中删除的敏感值集合,仅在内存中用于脱敏。
    # 返回:SQL 命令捕获的输出文本。
    return _checked(
        [
            "docker",
            "exec",
            "-i",
            MYSQL_CONTAINER,
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot',
        ],
        "Isolated database operation",
        secrets,
        input=sql,
    )


def _python(repo):
    # 功能:定位目标仓库虚拟环境的 Python 可执行文件。
    # 参数:
    #     repo: 待运行迁移或测试的仓库根目录。
    # 返回:仓库虚拟环境 Python 可执行文件的路径。
    path = repo / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not path.is_file():
        raise RunnerError(f"Run uv sync --locked in {repo.name} first")
    return str(path)


def _mysql_connection(secrets):
    # Inspect is captured in memory; full environment and credentials are never printed.
    # 功能:读取本地 MySQL 密码和 IPv4 端口并将密码加入脱敏集合。
    # 参数:
    #     secrets: 需从诊断输出中删除的敏感值集合,仅在内存中用于脱敏。
    # 返回:MySQL 密码与端口的二元组,仅供后续本地测试连接。
    raw = _checked(["docker", "inspect", MYSQL_CONTAINER], "Local MySQL lookup", secrets)
    info = json.loads(raw)[0]
    variables = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    password = variables.get("MYSQL_ROOT_PASSWORD")
    if not password:
        raise RunnerError("The local MySQL root credential is unavailable")
    secrets.append(password)
    bindings = info["NetworkSettings"]["Ports"].get("3306/tcp") or []
    binding = next((item for item in bindings if item["HostIp"] in {"127.0.0.1", "0.0.0.0"}), None)
    if not binding or not binding["HostPort"].isdigit():
        raise RunnerError("Local MySQL must have an accessible IPv4 host port")
    return password, binding["HostPort"]


def _redis_image(secrets):
    # 功能:选择本机已有 Redis 镜像,不拉取远程镜像。
    # 参数:
    #     secrets: 需从诊断输出中删除的敏感值集合,仅在内存中用于脱敏。
    # 返回:本地可用的 Redis 镜像名称;找不到时抛错。
    for image in ("redis:7-alpine", "redis:7.4-alpine"):
        result = _command(["docker", "image", "inspect", image])
        if result.returncode == 0:
            return image
    raise RunnerError("A local Redis image is required; no image pulled")


def _phase(repo, phase, pytest_args, connection, redis_image, secrets, explicit_database=None):
    # 功能:创建独立数据库与 Redis,执行迁移及阶段测试,最后清理本次自有资源。
    # 参数:
    #     repo: 待运行迁移或测试的仓库根目录。
    #     phase: 隔离测试阶段名称,限定为 general、ops 或 mini。
    #     pytest_args: 当前阶段传给 pytest 的测试选择与运行选项。
    #     connection: 本地 MySQL 密码与 IPv4 映射端口的二元组。
    #     redis_image: 本机已存在的 Redis 镜像名称,用于创建独立测试容器。
    #     secrets: 需从诊断输出中删除的敏感值集合,仅在内存中用于脱敏。
    #     explicit_database: 可选独立测试数据库名,必须匹配当前阶段及 UUID 格式。
    # 返回:pytest 的退出码;自有资源在 finally 中清理。
    run_id = uuid4().hex
    database = _database(explicit_database or f"juya_v13_{phase}_{run_id}")
    if not database.startswith(f"juya_v13_{phase}_"):
        raise RunnerError("The named database must match this test phase")
    redis_name = f"juya-v13-test-{run_id}"
    database_created = redis_created = False
    password, port = connection
    try:
        # CREATE without IF NOT EXISTS refuses to reuse another run's database.
        with _defer_interrupts():
            _mysql(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
                secrets,
            )
            database_created = True
        with _defer_interrupts():
            _checked(
                [
                    "docker",
                    "create",
                    "--pull=never",
                    "--name",
                    redis_name,
                    "--label",
                    f"juya.isolated-test={run_id}",
                    "--publish",
                    "127.0.0.1::6379",
                    redis_image,
                    "redis-server",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                ],
                "Private Redis creation",
                secrets,
            )
            redis_created = True
        _checked(["docker", "start", redis_name], "Private Redis start", secrets)
        deadline = time.monotonic() + 10
        while True:
            ping = _command(["docker", "exec", redis_name, "redis-cli", "ping"])
            if ping.returncode == 0 and ping.stdout.strip() == "PONG":
                break
            if time.monotonic() >= deadline:
                raise RunnerError("Private Redis did not become ready")
            time.sleep(0.1)
        address = _checked(
            ["docker", "port", redis_name, "6379/tcp"], "Private Redis port lookup", secrets
        ).strip()
        if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", address):
            raise RunnerError("Private Redis must bind only to a random loopback port")
        url = f"mysql+pymysql://root:{quote(password, safe='')}@127.0.0.1:{port}/{database}"
        env = dict(os.environ)
        env.update(
            PYTHONIOENCODING="utf-8",
            JUYA_ENVIRONMENT="test",
            JUYA_TEST_DATABASE_URL=url,
            JUYA_TEST_REDIS_URL=f"redis://{address}/0",
            JUYA_V13_ISOLATED_DATABASE=database,
            JUYA_RUN_LIVE_OSS_TESTS="false",
            JUYA_RUN_LIVE_OSS_BROWSER_TESTS="false",
            JUYA_OCR_PROVIDER="disabled",
            JUYA_CONTENT_SECURITY_ENABLED="false",
        )
        # Module-level application construction must stay inert in unit/contract tests.
        # Socket/worker tests explicitly derive their runtime URLs from JUYA_TEST_*.
        env.pop("JUYA_DATABASE_URL", None)
        env.pop("JUYA_REDIS_URL", None)
        migration_env = dict(env, JUYA_MIGRATION_DATABASE_URL=url)
        admin_repo = ROOT / "juya-admin-api"
        _checked(
            [_python(admin_repo), "-m", "alembic", "upgrade", "head"],
            "Isolated schema migration",
            secrets,
            cwd=admin_repo,
            env=migration_env,
        )
        # Individual migration/backfill tests set their own target URL; do not override it.
        env.pop("JUYA_MIGRATION_DATABASE_URL", None)
        print(f"Running {repo.name} {phase} with an independent MySQL database and Redis")
        result = _command([_python(repo), "-m", "pytest", *pytest_args], cwd=repo, env=env)
        print(_redact(result.stdout, secrets))
        return result.returncode
    finally:
        # Cleanup is limited to UUID resources whose creation succeeded in this call.
        with _defer_interrupts():
            try:
                if database_created:
                    _mysql(f"DROP DATABASE `{_database(database)}`;", secrets)
            finally:
                if redis_created:
                    _checked(["docker", "rm", "-f", redis_name], "Private Redis cleanup", secrets)


def main(argv=None):
    # 功能:解析隔离测试参数,按阶段运行测试并以退出码报告结果。
    # 参数:
    #     argv: 显式命令行参数;None 时使用进程实际参数。
    # 返回:脚本退出码;未返回数值的入口通过输出或异常报告结果。
    argv = list(sys.argv[1:] if argv is None else argv)
    secrets = [
        value
        for key, value in os.environ.items()
        if value and any(word in key.upper() for word in ("PASSWORD", "SECRET", "TOKEN", "KEY"))
    ]
    try:
        if not argv or argv[0] not in {"juya-admin-api", "juya-miniapp-api"}:
            raise RunnerError("Usage: test-v13-isolated.py <repository> [--suite] [pytest options]")
        repo = ROOT / argv.pop(0)
        suite = "--suite" in argv
        argv = [arg for arg in argv if arg != "--suite"]
        if suite and any(arg.startswith("tests") or ".py" in arg for arg in argv):
            raise RunnerError("--suite accepts pytest options; use direct mode for file selection")
        explicit = os.getenv("JUYA_V13_TEST_DATABASE")
        if explicit:
            _database(explicit)
            if suite:
                raise RunnerError(
                    "--suite generates its own databases; unset JUYA_V13_TEST_DATABASE"
                )
        _python(repo)
        connection = _mysql_connection(secrets)
        image = _redis_image(secrets)
        if repo.name == "juya-admin-api" and suite:
            phases = [
                ("general", [*argv, f"--ignore={OPERATIONS_TEST}"]),
                ("ops", [*argv, OPERATIONS_TEST]),
            ]
        elif repo.name == "juya-admin-api":
            operations = any(
                "test_operations_v13.py" in arg and not arg.startswith("--") for arg in argv
            )
            phases = (
                [("ops", argv)]
                if operations
                else [("general", [*argv, f"--ignore={OPERATIONS_TEST}"])]
            )
        else:
            phases = [("mini", argv)]
        result = 0
        for phase, pytest_args in phases:
            code = _phase(repo, phase, pytest_args, connection, image, secrets, explicit)
            if code:
                result = code
        return result
    except KeyboardInterrupt:
        print("Interrupted; cleanup attempted for resources created by this invocation")
        return 130
    except (RunnerError, OSError, KeyError, ValueError, IndexError) as error:
        print(_redact(str(error), secrets))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
