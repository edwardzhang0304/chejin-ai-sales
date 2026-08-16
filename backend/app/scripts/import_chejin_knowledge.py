"""Command-line entry point for the reviewed Chejin knowledge seed."""

from __future__ import annotations

import argparse
import json

from app.core.database import SessionLocal
from app.services.chejin_knowledge_seed import (
    SEED_ID,
    KnowledgeSeedConflictError,
    import_chejin_knowledge,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import the reviewed Chejin formal-knowledge seed")
    parser.add_argument("--seed", required=True, choices=[SEED_ID])
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--activate", action="store_true")
    operation.add_argument("--rollback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-version-upgrade",
        action="store_true",
        help="allow replacement only when the installed seed_version is older",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    operation = "activate" if args.activate else "rollback" if args.rollback else "import"
    try:
        with SessionLocal.begin() as db:
            result = import_chejin_knowledge(
                db,
                operation=operation,
                dry_run=args.dry_run,
                allow_version_upgrade=args.allow_version_upgrade,
            )
    except KnowledgeSeedConflictError as exc:
        print(
            json.dumps(
                {
                    "seed_id": SEED_ID,
                    "operation": operation,
                    "dry_run": args.dry_run,
                    "conflicts": len(exc.item_ids),
                    "conflict_item_ids": exc.item_ids,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
