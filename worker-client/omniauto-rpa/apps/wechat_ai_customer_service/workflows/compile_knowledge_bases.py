"""Compile classified knowledge bases into compatibility JSON files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_compiler import KnowledgeCompiler  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Output directory. Defaults to data/compiled/structured_compat.")
    parser.add_argument("--dry-run", action="store_true", help="Build the report without writing files.")
    args = parser.parse_args()

    compiler = KnowledgeCompiler(output_root=args.output)
    if args.dry_run:
        compiled = compiler.compile()
        result = {
            "ok": True,
            "dry_run": True,
            "counts": compiled["metadata"]["counts"],
            "output_root": str(compiler.output_root),
        }
    else:
        result = compiler.compile_to_disk()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
