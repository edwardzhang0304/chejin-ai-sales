from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CLIENT_PATHS = [
    APP_ROOT / "adapters",
    APP_ROOT / "admin_backend",
    APP_ROOT / "auth",
    APP_ROOT / "exports",
    APP_ROOT / "storage",
    APP_ROOT / "sync",
    APP_ROOT / "workflows",
    APP_ROOT / "knowledge_paths.py",
    APP_ROOT / "llm_config.py",
]
SERVER_MODULE_PREFIX = "apps.wechat_ai_customer_service.vps_admin"
CLIENT_MANIFEST_PATH = APP_ROOT / "deploy" / "client_source_manifest.json"
SERVER_MANIFEST_PATH = APP_ROOT / "deploy" / "server_private_manifest.json"


def main() -> int:
    checks = [
        check_client_source_does_not_import_server,
        check_client_entrypoints_do_not_load_server_modules,
        check_delivery_manifests_exclude_private_state,
    ]
    results = []
    for check in checks:
        try:
            check()
            results.append({"name": check.__name__, "ok": True})
        except Exception as exc:
            results.append({"name": check.__name__, "ok": False, "error": repr(exc)})
            break
    failures = [item for item in results if not item.get("ok")]
    print(json.dumps({"ok": not failures, "count": len(results), "failures": failures, "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def check_client_source_does_not_import_server() -> None:
    violations: list[dict[str, Any]] = []
    for path in iter_client_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module.startswith(SERVER_MODULE_PREFIX):
                        violations.append({"path": str(path), "line": node.lineno, "module": module})
                continue
            if module.startswith(SERVER_MODULE_PREFIX):
                violations.append({"path": str(path), "line": node.lineno, "module": module})
    assert_true(not violations, f"client-deliverable code must not import server modules: {violations}")


def check_client_entrypoints_do_not_load_server_modules() -> None:
    import apps.wechat_ai_customer_service.admin_backend.app  # noqa: F401
    import apps.wechat_ai_customer_service.sync.vps_sync  # noqa: F401
    import apps.wechat_ai_customer_service.workflows.listen_and_reply  # noqa: F401

    loaded = sorted(name for name in sys.modules if name.startswith(SERVER_MODULE_PREFIX))
    assert_true(not loaded, f"client entrypoints loaded server modules: {loaded}")


def check_delivery_manifests_exclude_private_state() -> None:
    client_manifest = json.loads(CLIENT_MANIFEST_PATH.read_text(encoding="utf-8"))
    server_manifest = json.loads(SERVER_MANIFEST_PATH.read_text(encoding="utf-8"))
    client_includes = [str(item) for item in client_manifest.get("include_paths", [])]
    client_excludes = {str(item) for item in client_manifest.get("exclude_paths", [])}
    required_excludes = {
        "apps/wechat_ai_customer_service/vps_admin/",
        "apps/wechat_ai_customer_service/data/shared_knowledge/",
        "apps/wechat_ai_customer_service/data/versions/",
        "apps/wechat_ai_customer_service/data/raw_inbox/",
        "apps/wechat_ai_customer_service/data/review_candidates/",
        "runtime/apps/wechat_ai_customer_service/vps_admin/",
    }
    assert_true(required_excludes.issubset(client_excludes), f"client manifest is missing excludes: {sorted(required_excludes - client_excludes)}")
    assert_true(not any("vps_admin" in path for path in client_includes), f"client manifest includes server path: {client_includes}")
    assert_true(
        "apps/wechat_ai_customer_service/vps_admin/" in set(server_manifest.get("include_paths", [])),
        "server manifest must own vps_admin source",
    )


def iter_client_python_files() -> list[Path]:
    files: list[Path] = []
    for root in CLIENT_PATHS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
