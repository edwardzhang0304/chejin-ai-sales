"""Exercise delivery file selection without building an archive or executable."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from client_delivery_policy import is_secret_file_path
from omniauto_tree import include_file


def source_packager():
    spec = importlib.util.spec_from_file_location(
        "secret_exclusion_source", ROOT / "scripts/build-source-package.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", [
    ".env", ".env.production", ".env.local", ".ENV.PRODUCTION",
    "tenant.env", "tenant.env.backup", "tenant.local.env",
    "vision-runtime.json", "private.pem", "signing.key", "signing.p12", "signing.pfx",
])
def test_all_delivery_selectors_reject_secret_files(name):
    relative = f"apps/wechat_ai_customer_service/{name}"
    assert is_secret_file_path(relative)
    assert is_secret_file_path(relative.replace("/", "\\"))
    source = source_packager()
    assert source._is_excluded(ROOT / "omniauto-rpa" / relative)
    assert source._forbidden_entries([f"worker-client/omniauto-rpa/{relative}"])
    assert not include_file(ROOT / "omniauto-rpa", ROOT / "omniauto-rpa" / relative)


@pytest.mark.parametrize("name", [
    "llm_config.py", "env_config.py", "config.json",
    "release-signing-public-keys.json", "chejin_worker_client/web_assets/index.html",
])
def test_runtime_code_and_public_signing_keys_remain_allowed(name):
    assert not is_secret_file_path(name)


def test_formal_pyinstaller_selector_rejects_environment_overlay(tmp_path):
    # Execute only the file selector; importing the spec would start PyInstaller.
    tree = ast.parse((ROOT / "packaging/chejin-worker-client.spec").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "include_omniauto_file")
    import client_delivery_policy as policy
    namespace = {
        "OMNIAUTO_RPA_SOURCE": tmp_path,
        "OMNIAUTO_CLIENT_EXCLUDES": (),
        "EXCLUDED_OMNIAUTO_PARTS": set(),
        "ALLOWED_OMNIAUTO_DATA_PREFIXES": (),
        "is_client_runtime_junk_path": policy.is_client_runtime_junk_path,
        "is_client_forbidden_path": policy.is_client_forbidden_path,
        "is_secret_file_path": policy.is_secret_file_path,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "selector", "exec"), namespace)
    select = namespace["include_omniauto_file"]
    assert not select(tmp_path / ".env.production")
    assert not select(tmp_path / "vision-runtime.json")
    assert select(tmp_path / "llm_config.py")
    # File-system boundary only: Windows symlink creation can require privilege.
    with patch.object(Path, "is_symlink", return_value=True):
        assert not select(tmp_path / "linked-config.json")
