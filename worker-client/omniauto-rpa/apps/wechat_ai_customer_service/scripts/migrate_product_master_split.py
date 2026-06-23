"""Copy legacy product category files into the product-master root.

This script is idempotent. It never deletes legacy files because those files are
rollback evidence and compatibility fallback during the product-master split.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    DEFAULT_TENANT_ID,
    TENANTS_ROOT,
    active_tenant_id,
)
from apps.wechat_ai_customer_service.product_master import ProductMasterStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy knowledge_bases/products into product_master.")
    parser.add_argument("--tenant-id", default="", help="Tenant to migrate. Defaults to active tenant.")
    parser.add_argument("--all-tenants", action="store_true", help="Migrate every tenant directory plus default.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing product_master items.")
    args = parser.parse_args()

    tenant_ids = discover_tenants() if args.all_tenants else [active_tenant_id(args.tenant_id or None)]
    results = []
    for tenant_id in tenant_ids:
        store = ProductMasterStore(tenant_id=tenant_id)
        result = store.migrate_from_legacy(overwrite=bool(args.overwrite))
        result["tenant_id"] = tenant_id
        result["product_master_root"] = str(store.root)
        results.append(result)
    print(json.dumps({"ok": all(item.get("ok") for item in results), "results": results}, ensure_ascii=True, indent=2))
    return 0 if all(item.get("ok") for item in results) else 1


def discover_tenants() -> list[str]:
    ids = {DEFAULT_TENANT_ID}
    if TENANTS_ROOT.exists():
        for path in sorted(item for item in TENANTS_ROOT.iterdir() if item.is_dir()):
            ids.add(active_tenant_id(path.name))
    return sorted(ids)


if __name__ == "__main__":
    raise SystemExit(main())
