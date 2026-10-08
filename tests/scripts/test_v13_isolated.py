"""Offline guards for the isolated suite runner; never start Docker or touch SQL."""

import importlib.util
import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner():
    # 功能:动态导入隔离测试脚本,不执行实际数据库或容器操作。
    # 参数:无。
    # 返回:动态加载的隔离测试脚本模块。
    script = Path(__file__).parents[2] / "scripts" / "test-v13-isolated.py"
    spec = importlib.util.spec_from_file_location("v13_isolated", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def commands(runner, monkeypatch):
    # 功能:替换隔离脚本命令执行并提供预设容器事实及调用记录。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:捕获的命令调用列表,元素包含参数和环境。
    calls = []

    def command(args, *, env=None, cwd=None, input=None):
        # 功能:记录隔离脚本命令并返回预设容器配置、端口或 Redis 就绪结果。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     env: 传给子进程的环境变量映射;None 表示继承当前环境。
        #     cwd: 子进程工作目录,确保命令在指定仓库中执行。
        #     input: 传给子进程标准输入的文本,如 SQL 或 Compose 配置。
        # 返回:本用例预设的调用结果或所构造的测试资源。
        calls.append((args, env, cwd, input))
        output = ""
        if args[:2] == ["docker", "inspect"]:
            output = json.dumps(
                [
                    {
                        "Config": {"Env": ["MYSQL_ROOT_PASSWORD=p@ss%secret"]},
                        "NetworkSettings": {
                            "Ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3306"}]}
                        },
                    }
                ]
            )
        elif args[:2] == ["docker", "port"]:
            output = "127.0.0.1:46379\n"
        elif args[-2:] == ["redis-cli", "ping"]:
            output = "PONG\n"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(runner, "_command", command)
    return calls


def test_import_is_side_effect_free(monkeypatch):

    # 功能:验证导入隔离测试脚本不会执行外部操作。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    def forbidden(*args, **kwargs):
        # 功能:当禁止的外部调用发生时立即令测试失败。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise AssertionError("import must not run Docker, SQL or pytest")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    script = Path(__file__).parents[2] / "scripts" / "test-v13-isolated.py"
    spec = importlib.util.spec_from_file_location("v13_isolated", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


@pytest.mark.parametrize("database", ["juya", "juya_v13_test", "juya_v13_共享", "juya_v13_../x"])
def test_rejects_reusable_or_unsafe_database_before_docker(runner, commands, monkeypatch, database):
    # 功能:验证不安全或可复用数据库名在调用 Docker 前被拒绝。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     database: 参数化测试输入的数据库名称,用于检验资源命名约束。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    monkeypatch.setenv("JUYA_V13_TEST_DATABASE", database)
    assert runner.main(["juya-admin-api", "-q"]) != 0
    assert commands == []


def test_suite_uses_fresh_separate_databases_and_private_redis(runner, commands, monkeypatch):
    # 功能:验证整套测试使用新的独立数据库及私有 Redis。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    monkeypatch.delenv("JUYA_V13_TEST_DATABASE", raising=False)
    monkeypatch.setenv("JUYA_DATABASE_URL", "mysql://shared-db/juya")
    monkeypatch.setenv("JUYA_REDIS_URL", "redis://shared-redis/0")
    monkeypatch.setenv("JUYA_RUN_LIVE_OSS_TESTS", "true")
    monkeypatch.setenv("JUYA_RUN_LIVE_OSS_BROWSER_TESTS", "true")
    assert runner.main(["juya-admin-api", "--suite", "-q"]) == 0
    runs = [row for row in commands if "pytest" in row[0]]
    assert len(runs) == 2
    general, operations = runs
    assert "--ignore=tests/integration/test_operations_v13.py" in general[0]
    assert "tests/integration/test_operations_v13.py" in operations[0]
    databases = [row[1]["JUYA_V13_ISOLATED_DATABASE"] for row in runs]
    assert len(set(databases)) == 2
    assert databases[0].startswith("juya_v13_general_")
    assert databases[1].startswith("juya_v13_ops_")
    for _args, env, _cwd, _input in runs:
        assert "JUYA_MIGRATION_DATABASE_URL" not in env
        assert "JUYA_DATABASE_URL" not in env
        assert "JUYA_REDIS_URL" not in env
        assert env["JUYA_TEST_REDIS_URL"] == "redis://127.0.0.1:46379/0"
        assert env["JUYA_RUN_LIVE_OSS_TESTS"] == "false"
        assert env["JUYA_RUN_LIVE_OSS_BROWSER_TESTS"] == "false"
    creates = [row for row in commands if row[0][:2] == ["docker", "create"]]
    assert len(creates) == 2
    assert all("127.0.0.1::6379" in row[0] for row in creates)
    assert all("--pull=never" in row[0] for row in creates)
    assert all("flushdb" not in str(row) and "flushall" not in str(row) for row in commands)
    migrations = [row for row in commands if "alembic" in row[0]]
    assert len(migrations) == 2 and all(row[0][-2:] == ["upgrade", "head"] for row in migrations)
    assert commands.index(migrations[0]) < commands.index(general)
    assert commands.index(migrations[1]) < commands.index(operations)
    drops = [row[3] for row in commands if row[3] and "DROP DATABASE" in row[3]]
    assert all(any(database in sql for database in databases) for sql in drops)
    assert len(drops) == 2


def test_miniapp_uses_admin_migrations_and_keeps_pytest_arguments(runner, commands, monkeypatch):
    # 功能:验证小程序测试使用管理端迁移且保留 pytest 参数。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    monkeypatch.delenv("JUYA_V13_TEST_DATABASE", raising=False)
    assert runner.main(["juya-miniapp-api", "-q", "tests/unit"]) == 0
    migration = next(row for row in commands if "alembic" in row[0])
    test = next(row for row in commands if "pytest" in row[0])
    assert migration[2].name == "juya-admin-api"
    assert test[2].name == "juya-miniapp-api"
    assert test[0][-2:] == ["-q", "tests/unit"]


def test_failed_migration_cleans_only_owned_resources_and_never_runs_pytest(
    runner, commands, monkeypatch, capsys
):
    # 功能:验证迁移失败只清理自有资源且不运行 pytest。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     capsys: pytest 标准输出与错误捕获工具,用于检查诊断及脱敏内容。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    original = runner._command

    def failing(args, **kwargs):
        # 功能:在用例指定命令上返回失败结果以验证停止及清理。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:本用例预设的调用结果或所构造的测试资源。
        result = original(args, **kwargs)
        if "alembic" in args:
            return SimpleNamespace(returncode=2, stdout="p@ss%secret p%40ss%25secret")
        return result

    monkeypatch.delenv("JUYA_V13_TEST_DATABASE", raising=False)
    monkeypatch.setattr(runner, "_command", failing)
    assert runner.main(["juya-admin-api", "-q"]) != 0
    assert not any("pytest" in row[0] for row in commands)
    assert sum(bool(row[3] and "DROP DATABASE" in row[3]) for row in commands) == 1
    assert sum(row[0][:3] == ["docker", "rm", "-f"] for row in commands) == 1
    output = capsys.readouterr().out
    assert "p@ss%secret" not in output and "p%40ss%25secret" not in output


def test_existing_named_database_is_never_dropped(runner, commands, monkeypatch):
    # 功能:验证已有同名数据库不会被删除。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    original = runner._command

    def existing(args, **kwargs):
        # 功能:模拟创建数据库遇到同名已有库,检查禁止误删。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:本用例预设的调用结果或所构造的测试资源。
        result = original(args, **kwargs)
        if (kwargs.get("input") or "").startswith("CREATE DATABASE"):
            return SimpleNamespace(returncode=1, stdout="database exists")
        return result

    monkeypatch.setenv("JUYA_V13_TEST_DATABASE", "juya_v13_general_" + "a" * 32)
    monkeypatch.setattr(runner, "_command", existing)
    assert runner.main(["juya-admin-api", "-q"]) != 0
    assert not any(row[3] and "DROP DATABASE" in row[3] for row in commands)


def test_suite_refuses_file_selection_before_docker(runner, commands):
    # 功能:验证整套测试模式在调用 Docker 前拒绝指定测试文件。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert runner.main(["juya-admin-api", "--suite", "tests/unit"]) != 0
    assert commands == []


@pytest.mark.parametrize("interrupt_at", ["database_created", "redis_created", "database_drop"])
def test_sigint_at_resource_boundaries_finishes_owned_cleanup(
    runner, commands, monkeypatch, interrupt_at
):
    # 功能:验证资源创建边界收到 SIGINT 后仍完成自有资源清理。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     interrupt_at: 模拟 SIGINT 到达的资源创建或清理边界。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    original = runner._command
    owned = set()
    interrupted = False

    def interrupting(args, **kwargs):
        # 功能:在指定资源边界模拟 SIGINT 并记录命令。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:本用例预设的调用结果或所构造的测试资源。
        nonlocal interrupted
        sql = kwargs.get("input") or ""
        result = original(args, **kwargs)
        event = None
        if sql.startswith("CREATE DATABASE"):
            owned.add(sql.split("`")[1])
            event = "database_created"
        elif args[:2] == ["docker", "create"]:
            owned.add(args[args.index("--name") + 1])
            event = "redis_created"
        elif sql.startswith("DROP DATABASE"):
            event = "database_drop"
        if event == interrupt_at and not interrupted:
            interrupted = True
            signal.raise_signal(signal.SIGINT)
        # A DROP interrupted before completion must finish before removing Redis.
        if sql.startswith("DROP DATABASE"):
            owned.remove(sql.split("`")[1])
        elif args[:3] == ["docker", "rm", "-f"]:
            owned.remove(args[-1])
        return result

    monkeypatch.setattr(runner, "_command", interrupting)
    previous = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner._phase(
                runner.ROOT / "juya-admin-api",
                "general",
                ["-q"],
                ("test-password", "3306"),
                "redis:7-alpine",
                ["test-password"],
            )
        assert signal.getsignal(signal.SIGINT) == previous
    finally:
        signal.signal(signal.SIGINT, previous)
    assert interrupted
    assert owned == set(), "no owned resource may remain after a caught SIGINT"


def test_interrupt_message_does_not_claim_cleanup_is_verified(
    runner, commands, monkeypatch, capsys
):

    # 功能:验证中断提示不会声称清理已验证完成。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     commands: 隔离脚本命令替身记录的调用列表,供断言外部操作顺序和范围。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     capsys: pytest 标准输出与错误捕获工具,用于检查诊断及脱敏内容。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    def interrupted(*args, **kwargs):
        # 功能:在 SQL 执行边界注入中断以验证事务回滚。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise KeyboardInterrupt()

    monkeypatch.delenv("JUYA_V13_TEST_DATABASE", raising=False)
    monkeypatch.setattr(runner, "_phase", interrupted)
    assert runner.main(["juya-admin-api", "-q"]) == 130
    output = capsys.readouterr().out
    assert "cleanup attempted" in output
    assert "were cleaned up" not in output


def test_only_critical_commands_use_a_separate_process_group(runner, monkeypatch):
    # 功能:验证仅关键命令创建独立进程组。
    # 参数:
    #     runner: 动态加载的隔离测试脚本模块,供替换内部命令和调用入口。
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert hasattr(runner, "_defer_interrupts")
    calls = []

    def fake_run(*args, **kwargs):
        # 功能:捕获子进程选项并返回正常完成结果。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:本用例预设的调用结果或所构造的测试资源。
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner._command(["pytest"])
    with runner._defer_interrupts():
        runner._command(["docker", "create"])
    runner._command(["pytest"])
    if os.name == "nt":
        group = subprocess.CREATE_NEW_PROCESS_GROUP
        assert not calls[0]["creationflags"] & group
        assert calls[1]["creationflags"] & group
        assert not calls[2]["creationflags"] & group
    else:
        assert not calls[0]["start_new_session"]
        assert calls[1]["start_new_session"]
        assert not calls[2]["start_new_session"]
