from __future__ import annotations

import json

from app.services.feishu_adapter import get_feishu_adapter
from app.services.feishu_service import backfill_sales_open_ids


def main() -> int:
    adapter = get_feishu_adapter()
    readiness = adapter.configuration_status()
    if not readiness["ready"]:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_code": "FEISHU_APP_CONFIG_MISSING",
                },
                ensure_ascii=False,
            )
        )
        return 2

    result = backfill_sales_open_ids(adapter=adapter)
    print(json.dumps({"status": "completed", **result}, ensure_ascii=False))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
