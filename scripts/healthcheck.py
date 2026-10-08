import os
import urllib.request


def main() -> None:
    # 功能:对 API 请求存活接口,对其他进程检查 PID 1 是否存在。
    # 参数:无。
    # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
    role = os.environ.get("JUYA_PROCESS_ROLE", "admin-api")
    if role == "admin-api":
        port = os.environ.get("PORT", "8000")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=2) as response:
            if response.status != 200:
                raise SystemExit(1)
        return
    os.kill(1, 0)


if __name__ == "__main__":
    main()
