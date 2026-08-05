from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(filename: str):
    path = VERSIONS_DIR / filename
    spec = spec_from_file_location(f"migration_{path.stem}", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("filename", "error_code"),
    [
        (
            "20260804_0019_omniauto_product_knowledge.py",
            "IRREVERSIBLE_MIGRATION_20260804_0019",
        ),
        (
            "20260805_0020_admin_cookie_sessions.py",
            "IRREVERSIBLE_MIGRATION_20260805_0020",
        ),
        (
            "20260806_0021_reply_action_vehicle_facts.py",
            "DOWNGRADE_CHAIN_BLOCKED_BY_DATA_SAFETY",
        ),
        (
            "20260807_0022_vehicle_file_cleanup_outbox.py",
            "IRREVERSIBLE_MIGRATION_20260807_0022",
        ),
    ],
)
def test_data_bearing_migrations_refuse_automatic_downgrade(
    monkeypatch,
    filename,
    error_code,
):
    migration = _load_migration(filename)
    destructive_calls = []

    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda *args, **kwargs: destructive_calls.append(("execute", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda *args, **kwargs: destructive_calls.append(("drop_table", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *args, **kwargs: destructive_calls.append(("drop_index", args, kwargs)),
    )

    with pytest.raises(RuntimeError, match=error_code) as exc:
        migration.downgrade()

    assert "backup" in str(exc.value).lower()
    assert "forward migration" in str(exc.value).lower()
    assert destructive_calls == []
