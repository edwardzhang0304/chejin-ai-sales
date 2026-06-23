"""Move real-chat samples out of formal chats and into RAG/style learning layers."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.admin_backend.services.knowledge_compiler import KnowledgeCompiler
from apps.wechat_ai_customer_service.knowledge_paths import default_admin_knowledge_base_root, tenant_context, tenant_root
from apps.wechat_ai_customer_service.workflows.knowledge_runtime import KnowledgeRuntime
from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore, write_json_with_retry
from apps.wechat_ai_customer_service.workflows.rag_layer import RagService
from apps.wechat_ai_customer_service.workflows.real_chat_learning import (
    formal_chat_item_is_real_chat,
    formal_chat_item_to_experience,
    formal_chat_item_to_style_example,
    merge_records_by_id,
    read_json,
    write_jsonl,
)


DEFAULT_TENANT_ID = "chejin"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate formal real-chat items to RAG/style learning layers.")
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = migrate_real_chat_formal_to_rag_first(tenant_id=str(args.tenant_id or DEFAULT_TENANT_ID), dry_run=bool(args.dry_run))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def migrate_real_chat_formal_to_rag_first(*, tenant_id: str, dry_run: bool = False) -> dict[str, Any]:
    tenant_id = tenant_id.strip() or DEFAULT_TENANT_ID
    migration_id = "real_chat_rag_first_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    with tenant_context(tenant_id):
        tenant_dir = tenant_root(tenant_id)
        formal_items_dir = default_admin_knowledge_base_root(tenant_id) / "chats" / "items"
        formal_items_dir_resolved = formal_items_dir.resolve()
        if not formal_items_dir.exists():
            return {"ok": False, "message": f"formal chats items dir not found: {formal_items_dir}"}

        matched: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(formal_items_dir.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and formal_chat_item_is_real_chat(item, path=path):
                matched.append((path, item))

        experiences = [
            record
            for path, item in matched
            if (
                record := formal_chat_item_to_experience(
                    item,
                    tenant_id=tenant_id,
                    migration_id=migration_id,
                    source_file=str(path),
                )
            )
        ]
        style_examples = [
            record
            for path, item in matched
            if (
                record := formal_chat_item_to_style_example(
                    item,
                    tenant_id=tenant_id,
                    migration_id=migration_id,
                    source_file=str(path),
                )
            )
        ]

        backup_dir = tenant_dir / "migration_backups" / "real_chat_formal_to_rag" / migration_id
        backup_items_dir = backup_dir / "items"
        manifest = {
            "ok": True,
            "tenant_id": tenant_id,
            "migration_id": migration_id,
            "dry_run": dry_run,
            "formal_items_dir": str(formal_items_dir),
            "matched_formal_items": len(matched),
            "rag_experience_planned": len(experiences),
            "style_examples_planned": len(style_examples),
            "matched_files": [str(path) for path, _item in matched],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if dry_run:
            manifest["backup_dir"] = str(backup_dir)
            manifest["would_remove_from_formal"] = len(matched)
            return manifest

        backup_items_dir.mkdir(parents=True, exist_ok=True)
        for path, _item in matched:
            _assert_child(path.resolve(), formal_items_dir_resolved)
            shutil.copy2(path, backup_items_dir / path.name)

        (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        store = RagExperienceStore(tenant_id=tenant_id)
        existing_experiences = read_json(store.path, [])
        if not isinstance(existing_experiences, list):
            existing_experiences = []
        merged_experiences = merge_records_by_id(existing_experiences, experiences, "experience_id")
        write_json_with_retry(store.path, merged_experiences)

        style_path = tenant_dir / "style_memory" / "examples.jsonl"
        existing_style = read_jsonl_rows(style_path)
        merged_style = merge_records_by_id(existing_style, style_examples, "id")
        write_jsonl(style_path, merged_style)

        removed: list[str] = []
        for path, _item in matched:
            resolved = path.resolve()
            _assert_child(resolved, formal_items_dir_resolved)
            path.unlink()
            removed.append(str(path))

        compile_result = KnowledgeCompiler(runtime=KnowledgeRuntime(tenant_id=tenant_id)).compile_to_disk()
        rag_index = RagService(tenant_id=tenant_id).rebuild_index()

        report = {
            **manifest,
            "backup_dir": str(backup_dir),
            "rag_experience_written": len(experiences),
            "rag_experience_total": len(merged_experiences),
            "style_examples_written": len(style_examples),
            "style_examples_total": len(merged_style),
            "removed_from_formal": len(removed),
            "removed_files": removed,
            "rag_experience_path": str(store.path),
            "style_path": str(style_path),
            "compile": compile_result,
            "rag_index": rag_index,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        report_path = backup_dir / "migration_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _assert_child(path: Path, parent: Path) -> None:
    if parent not in [path, *path.parents]:
        raise RuntimeError(f"refusing to mutate path outside expected directory: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
