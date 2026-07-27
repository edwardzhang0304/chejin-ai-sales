from __future__ import annotations

import argparse
import json
from pathlib import Path

from omniauto_tree import load_source_provenance, tree_manifest, verify_same_tree


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--packaged", type=Path)
    args = parser.parse_args()
    result = (
        verify_same_tree(args.source, args.packaged)
        if args.packaged
        else {
            "source": tree_manifest(args.source),
            "provenance": load_source_provenance(args.source),
        }
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
