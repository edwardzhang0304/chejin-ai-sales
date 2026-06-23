"""Build a scoped evidence pack for one WeChat customer-service message."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from adapters.knowledge_loader import build_evidence_pack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True, help="Customer message text.")
    parser.add_argument("--manifest", type=Path, help="Optional manifest path.")
    parser.add_argument("--context-json", help="Optional conversation context JSON.")
    args = parser.parse_args()

    context = json.loads(args.context_json) if args.context_json else {}
    pack = build_evidence_pack(args.text, manifest_path=args.manifest, context=context)
    print(json.dumps(pack, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

