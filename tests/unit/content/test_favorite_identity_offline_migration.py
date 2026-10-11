"""Offline SQL must not bypass the required ownership and collision checks."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_favorite_identity_migration_refuses_before_ddl(direction, capsys):
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", "mysql+pymysql://offline:offline@localhost/offline")
    with pytest.raises(RuntimeError, match=r"requires.*online"):
        if direction == "upgrade":
            command.upgrade(config, "0018:0019", sql=True)
        else:
            command.downgrade(config, "0019:0018", sql=True)
    assert "ALTER TABLE favorite_snapshot" not in capsys.readouterr().out
