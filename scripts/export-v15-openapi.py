"""Freeze the owned OpenAPI and route inventory without connecting to external services."""

import asyncio
import json
from pathlib import Path

from pydantic import SecretStr

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.runtime import build_runtime
from juya_admin_api.main import create_app


def main() -> None:
    settings = Settings(
        environment="test",
        database_url=SecretStr("mysql+pymysql://root:test@127.0.0.1/contract"),
        redis_url=SecretStr("redis://127.0.0.1:6379/15"),
        internal_hmac_secret=SecretStr("test-internal-secret"),
        oss_access_key_id=SecretStr("test-access-key"),
        oss_access_key_secret=SecretStr("test-access-secret"),
        oss_region="oss-cn-test",
        oss_bucket="juya-test",
        oss_expected_bucket="juya-test",
    )
    runtime = build_runtime(settings)
    try:
        schema = create_app(settings, runtime=runtime).openapi()
        root = Path(__file__).resolve().parents[1] / "docs/contracts"
        root.mkdir(parents=True, exist_ok=True)
        (root / "v15-openapi.json").write_text(
            json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        rows = [
            (method.upper(), path)
            for path, operations in schema["paths"].items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        ]
        document = (
            "# V1.5 管理后端接口清单\n\n"
            "由 scripts/export-v15-openapi.py 从实际挂载路由生成。"
            "本仓库冻结契约供管理前端和BFF分别更新其客户端。本任务不写其他仓库。\n\n"
            "内部用户接口仅接受受信BFF HMAC正文。学生不能直接访问。"
            "收藏确认凭据须由BFF在最终保存的同一SQL事务内锁定并复验。"
            "HTTP预检查不能代替该步骤。\n\n"
            "| 方法 | 路径 |\n| --- | --- |\n"
        )
        document += "".join(f"| {method} | `{path}` |\n" for method, path in sorted(rows))
        (root / "v15-routes.md").write_text(document, encoding="utf-8")
        print(f"Frozen {len(rows)} mounted operations; no network connection opened")
    finally:
        asyncio.run(runtime.close())


if __name__ == "__main__":
    main()
