"""Local RAG auxiliary layer for WeChat customer-service knowledge.

The first implementation is intentionally offline-safe: it stores source/chunk
metadata and uses deterministic lexical retrieval. Embedding/vector backends can
be layered behind the same service later without changing the safety contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.knowledge_paths import (  # noqa: E402
    active_tenant_id,
    tenant_rag_cache_root,
    tenant_rag_chunks_root,
    tenant_rag_index_root,
    tenant_rag_sources_root,
)
from apps.wechat_ai_customer_service.platform_understanding_rules import (  # noqa: E402
    platform_understanding_cache_token,
    rag_terms,
    semantic_equivalents as visible_semantic_equivalents,
)
from apps.wechat_ai_customer_service.admin_backend.services.knowledge_contamination_guard import (  # noqa: E402
    rag_chunk_exclusion_reason,
    rag_chunk_is_retrievable,
)
from apps.wechat_ai_customer_service.storage import get_postgres_store, load_storage_config  # noqa: E402
try:  # noqa: E402
    from apps.wechat_ai_customer_service.workflows.evidence_authority import can_authorize_reply_content
except Exception:  # pragma: no cover - supports isolated script imports
    can_authorize_reply_content = None  # type: ignore[assignment]


SUPPORTED_SUFFIXES = {".txt", ".md", ".json", ".csv"}
DEFAULT_SOURCE_TYPES = {
    "upload",
    "chat_log",
    "product_doc",
    "policy_doc",
    "erp_export",
    "manual",
    "wechat_raw_message",
    "cleaned_real_chat_pack",
    "real_chat",
}
RETRIEVAL_MODE = "hybrid_lexical_semantic"
VECTOR_DIMENSIONS = 96
SOURCE_TYPE_BOOSTS = {
    "product_doc": 0.045,
    "manual": 0.04,
    "policy_doc": 0.03,
    "erp_export": 0.02,
    "rag_experience": 0.02,
    "cleaned_real_chat_pack": 0.015,
    "real_chat": 0.012,
    "wechat_raw_message": -0.01,
    "chat_log": -0.02,
}
CATEGORY_BOOSTS = {
    "product_explanations": 0.04,
    "product_faq": 0.035,
    "product_rules": 0.025,
    "products": 0.025,
    "policies": 0.015,
    "rag_experience": 0.01,
}
DEFAULT_SEMANTIC_EQUIVALENTS = {
    "电源": ["供电", "供电方式"],
    "供电": ["电源", "供电方式"],
    "民宿客房": ["酒店公寓", "客房"],
    "酒店公寓": ["民宿客房", "客房"],
}

_SEMANTIC_EQUIVALENTS_MAP_CACHE: dict[str, Any] = {
    "key": None,
    "value": None,
}
WINDOWS_TRANSIENT_WRITE_ERRNOS = {13, 22}
WINDOWS_TRANSIENT_WRITE_WINERRORS = {5, 32, 33}
WINDOWS_WRITE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.25)


def is_transient_windows_write_error(exc: OSError) -> bool:
    return (
        os.name == "nt"
        and (
            int(getattr(exc, "errno", 0) or 0) in WINDOWS_TRANSIENT_WRITE_ERRNOS
            or int(getattr(exc, "winerror", 0) or 0) in WINDOWS_TRANSIENT_WRITE_WINERRORS
        )
    )


def write_json_file(path: Path, payload: Any) -> None:
    """Write JSON via a temp file so Windows never sees a half-written index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    last_error: OSError | None = None
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for index, delay in enumerate(WINDOWS_WRITE_RETRY_DELAYS):
            try:
                os.replace(tmp_path, path)
                return
            except OSError as exc:
                last_error = exc
                if not is_transient_windows_write_error(exc):
                    raise
                if index < len(WINDOWS_WRITE_RETRY_DELAYS) - 1:
                    time.sleep(delay)
        if last_error is not None:
            raise last_error
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def remove_path_with_retry(path: Path) -> None:
    last_error: OSError | None = None
    for index, delay in enumerate(WINDOWS_WRITE_RETRY_DELAYS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if not is_transient_windows_write_error(exc):
                raise
            if index < len(WINDOWS_WRITE_RETRY_DELAYS) - 1:
                time.sleep(delay)
    if last_error is not None:
        raise last_error


class RagService:
    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        sources_root: Path | None = None,
        chunks_root: Path | None = None,
        index_root: Path | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self.tenant_id = active_tenant_id(tenant_id)
        self.sources_root = sources_root or tenant_rag_sources_root(self.tenant_id)
        self.chunks_root = chunks_root or tenant_rag_chunks_root(self.tenant_id)
        self.index_root = index_root or tenant_rag_index_root(self.tenant_id)
        self.cache_root = cache_root or tenant_rag_cache_root(self.tenant_id)

    @property
    def sources_path(self) -> Path:
        return self.sources_root / "sources.json"

    @property
    def index_path(self) -> Path:
        return self.index_root / "index.json"

    def ensure_dirs(self) -> None:
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            db.initialize_schema()
            if not config.mirror_files:
                return
        for root in (self.sources_root, self.chunks_root, self.index_root, self.cache_root):
            root.mkdir(parents=True, exist_ok=True)
        for name in ("uploads", "chat_logs", "product_docs", "policy_docs", "erp_exports"):
            (self.sources_root / name).mkdir(parents=True, exist_ok=True)

    def ingest_file(
        self,
        path: Path,
        *,
        source_type: str = "upload",
        category: str = "",
        product_id: str = "",
        layer: str = "tenant",
        rebuild_index: bool = True,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        path = Path(path)
        text = read_source_text(path)
        if not text.strip():
            return {"ok": False, "message": "source has no readable text", "path": str(path)}
        source_type = normalize_source_type(source_type)
        content_hash = stable_digest(text, 32)
        source_id = "source_" + stable_digest(f"{self.tenant_id}:{path}:{source_type}:{category}:{product_id}:{content_hash}", 16)
        now_text = now()
        source_record = {
            "source_id": source_id,
            "tenant_id": self.tenant_id,
            "layer": layer or "tenant",
            "source_type": source_type,
            "category": category or infer_category_from_path(path),
            "product_id": product_id or "",
            "source_path": str(path),
            "content_hash": content_hash,
            "status": "active",
            "created_at": now_text,
            "updated_at": now_text,
        }
        chunks = build_chunks(
            text,
            source=source_record,
        )
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            db.upsert_rag_source(source_record)
            db.replace_rag_chunks(source_id, chunks)
            result = {
                "ok": True,
                "source": source_record,
                "source_id": source_id,
                "chunk_count": len(chunks),
                "chunks_path": str(self.chunks_root / f"{source_id}.json"),
            }
            if rebuild_index:
                result["index"] = self.rebuild_index()
            if not config.mirror_files:
                return result
        self.write_source(source_record)
        chunks_path = self.chunks_root / f"{source_id}.json"
        write_json_file(chunks_path, {"source": source_record, "chunks": chunks})
        result = {
            "ok": True,
            "source": source_record,
            "source_id": source_id,
            "chunk_count": len(chunks),
            "chunks_path": str(chunks_path),
        }
        if rebuild_index:
            result["index"] = self.update_index_for_chunks(source_id, chunks)
        return result

    def write_source(self, source_record: dict[str, Any]) -> None:
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            db.upsert_rag_source(source_record)
            if not config.mirror_files:
                return
        records = self.list_sources()
        records = [item for item in records if item.get("source_id") != source_record.get("source_id")]
        records.append(source_record)
        records.sort(key=lambda item: (str(item.get("source_type") or ""), str(item.get("source_id") or "")))
        write_json_file(self.sources_path, records)

    def list_sources(self) -> list[dict[str, Any]]:
        db = postgres_store(self.tenant_id)
        if db:
            records = db.list_rag_sources(self.tenant_id)
            if records:
                return records
        if not self.sources_path.exists():
            return []
        return json.loads(self.sources_path.read_text(encoding="utf-8"))

    def delete_source_by_path(self, source_path: Path) -> dict[str, Any]:
        target = str(Path(source_path))
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            deleted_sources = db.delete_rag_source_by_path(self.tenant_id, target)
            self.rebuild_index()
            if not config.mirror_files:
                return {"ok": True, "deleted_sources": deleted_sources, "deleted_chunks": 0}
        records = self.list_sources()
        matched = [item for item in records if str(item.get("source_path") or "") == target]
        if not matched:
            return {"ok": True, "deleted_sources": 0, "deleted_chunks": 0}
        remaining = [item for item in records if str(item.get("source_path") or "") != target]
        self.ensure_dirs()
        write_json_file(self.sources_path, remaining)
        deleted_chunks = 0
        for item in matched:
            chunks_path = self.chunks_root / f"{item.get('source_id')}.json"
            if chunks_path.exists():
                remove_path_with_retry(chunks_path)
                deleted_chunks += 1
        self.rebuild_index()
        return {"ok": True, "deleted_sources": len(matched), "deleted_chunks": deleted_chunks}

    def iter_chunks(self, *, include_experience_pool: bool = False) -> list[dict[str, Any]]:
        db = postgres_store(self.tenant_id)
        if db:
            chunks = [chunk for chunk in db.list_rag_chunks(self.tenant_id) if rag_chunk_is_retrievable(chunk)]
            if include_experience_pool:
                chunks.extend(self.iter_experience_chunks())
            if chunks:
                return chunks
        chunks: list[dict[str, Any]] = []
        if not self.chunks_root.exists():
            if include_experience_pool:
                chunks.extend(self.iter_experience_chunks())
            return chunks
        for path in sorted(self.chunks_root.glob("source_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for chunk in payload.get("chunks", []) or []:
                if rag_chunk_is_retrievable(chunk):
                    chunks.append(chunk)
        if include_experience_pool:
            chunks.extend(self.iter_experience_chunks())
        return chunks

    def iter_experience_chunks(self) -> list[dict[str, Any]]:
        try:
            from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore
        except Exception:
            return []
        store = RagExperienceStore(tenant_id=self.tenant_id, root=self.sources_root.parent / "rag_experience")
        chunks: list[dict[str, Any]] = []
        for item in store.list_retrievable(limit=1000):
            text = build_experience_chunk_text(item)
            if not text.strip():
                continue
            experience_id = str(item.get("experience_id") or "")
            hit = item.get("rag_hit", {}) or {}
            quality = item.get("quality", {}) if isinstance(item.get("quality"), dict) else {}
            chunks.append(
                {
                    "chunk_id": "chunk_" + stable_digest(f"rag_experience:{experience_id}:{text}", 16),
                    "source_id": experience_id,
                    "tenant_id": self.tenant_id,
                    "layer": "rag_experience",
                    "source_type": "rag_experience",
                    "category": "rag_experience",
                    "product_id": hit.get("product_id") or "",
                    "source_path": str(store.path),
                    "chunk_index": 0,
                    "text": text,
                    "char_count": len(text),
                    "status": "active",
                    "quality": quality,
                    "created_at": item.get("created_at") or now(),
                }
            )
        return chunks

    def iter_experience_reference_chunks(self) -> list[dict[str, Any]]:
        try:
            from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore
        except Exception:
            return []
        store = RagExperienceStore(tenant_id=self.tenant_id, root=self.sources_root.parent / "rag_experience")
        chunks: list[dict[str, Any]] = []
        for item in store.list_reference_candidates(limit=1000):
            text = build_experience_chunk_text(item)
            if not text.strip():
                continue
            experience_id = str(item.get("experience_id") or "")
            hit = item.get("rag_hit", {}) or {}
            quality = item.get("quality", {}) if isinstance(item.get("quality"), dict) else {}
            chunks.append(
                {
                    "chunk_id": "chunk_" + stable_digest(f"rag_experience_reference:{experience_id}:{text}", 16),
                    "source_id": experience_id,
                    "tenant_id": self.tenant_id,
                    "layer": "rag_experience",
                    "source_type": "rag_experience",
                    "category": "rag_experience",
                    "product_id": hit.get("product_id") or "",
                    "source_path": str(store.path),
                    "chunk_index": 0,
                    "text": text,
                    "char_count": len(text),
                    "status": "active",
                    "quality": quality,
                    "reference_only": True,
                    "created_at": item.get("created_at") or now(),
                }
            )
        return chunks

    def rebuild_index(self) -> dict[str, Any]:
        self.ensure_dirs()
        chunks = [
            chunk
            for chunk in self.iter_chunks(include_experience_pool=False)
            if runtime_rag_entry_allowed(chunk)
        ]
        high_risk_terms = rag_terms("high_risk_terms")
        semantic_equivalents_map()
        entries = [build_index_entry(chunk, high_risk_terms=high_risk_terms) for chunk in chunks]
        payload = {
            "schema_version": 1,
            "tenant_id": self.tenant_id,
            "built_at": now(),
            "entry_count": len(entries),
            "entries": entries,
        }
        db = postgres_store(self.tenant_id)
        config = load_storage_config()
        if db:
            db.replace_rag_index(self.tenant_id, entries)
            if not config.mirror_files:
                return {"ok": True, "index_path": f"postgres://{db.schema}.rag_index_entries", "entry_count": len(entries)}
        write_json_file(self.index_path, payload)
        return {"ok": True, "index_path": str(self.index_path), "entry_count": len(entries)}

    def update_index_for_chunks(self, source_id: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        """Update file-backed RAG index for one source without scanning all chunks."""
        self.ensure_dirs()
        db = postgres_store(self.tenant_id)
        if db or not self.index_path.exists():
            return self.rebuild_index()
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.rebuild_index()
        entries = [
            item
            for item in payload.get("entries", []) or []
            if str(item.get("source_id") or "") != str(source_id)
        ]
        high_risk_terms = rag_terms("high_risk_terms")
        semantic_equivalents_map()
        entries.extend(
            build_index_entry(chunk, high_risk_terms=high_risk_terms)
            for chunk in chunks
            if rag_chunk_is_retrievable(chunk) and runtime_rag_entry_allowed(chunk)
        )
        next_payload = {
            "schema_version": 1,
            "tenant_id": self.tenant_id,
            "built_at": now(),
            "entry_count": len(entries),
            "entries": entries,
        }
        write_json_file(self.index_path, next_payload)
        return {"ok": True, "index_path": str(self.index_path), "entry_count": len(entries), "mode": "incremental_source_update"}

    def load_index(self) -> dict[str, Any]:
        db = postgres_store(self.tenant_id)
        if db:
            entries = db.list_rag_index(self.tenant_id)
            if not entries:
                self.rebuild_index()
                entries = db.list_rag_index(self.tenant_id)
            return {"schema_version": 1, "tenant_id": self.tenant_id, "entries": entries, "built_at": "postgres"}
        if not self.index_path.exists():
            self.rebuild_index()
        elif self.index_is_stale():
            try:
                # Rebuild through the current service instance so custom roots
                # (test/eval sandboxes) stay consistent with their own index path.
                self.rebuild_index()
            except Exception:
                try:
                    from apps.wechat_ai_customer_service.workflows.rag_experience_store import rebuild_rag_index_safely

                    rebuild_rag_index_safely(self.tenant_id, trigger="rag_index_stale_on_load", force_sync=True)
                except Exception:
                    pass
        if not self.index_path.exists():
            return {"schema_version": 1, "tenant_id": self.tenant_id, "entries": []}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def index_is_stale(self) -> bool:
        if postgres_store(self.tenant_id):
            return False
        if not self.index_path.exists():
            return True
        index_mtime = self.index_path.stat().st_mtime
        candidates = list(self.chunks_root.glob("source_*.json")) if self.chunks_root.exists() else []
        try:
            from apps.wechat_ai_customer_service.workflows.rag_experience_store import RagExperienceStore

            experience_path = RagExperienceStore(tenant_id=self.tenant_id).path
            if experience_path.exists():
                candidates.append(experience_path)
        except Exception:
            pass
        return any(path.exists() and path.stat().st_mtime > index_mtime for path in candidates)

    def search(
        self,
        query: str,
        *,
        product_id: str = "",
        category: str = "",
        source_type: str = "",
        limit: int = 6,
    ) -> dict[str, Any]:
        query_text = str(query or "").strip()
        if not query_text:
            return {"ok": True, "query": query_text, "hits": [], "confidence": 0.0}
        index = self.load_index()
        query_profile = build_query_profile(query_text)
        hits: list[dict[str, Any]] = []
        for entry in index.get("entries", []) or []:
            if not runtime_rag_entry_allowed(entry):
                continue
            if product_id and str(entry.get("product_id") or "") not in {"", product_id}:
                continue
            if category and str(entry.get("category") or "") != category:
                continue
            if source_type and str(entry.get("source_type") or "") != source_type:
                continue
            scoring = score_entry(query_text, query_profile, entry, product_id=product_id)
            score = float(scoring.get("final", 0.0))
            if score <= 0:
                continue
            hit = {
                "chunk_id": entry.get("chunk_id"),
                "source_id": entry.get("source_id"),
                "score": round(score, 4),
                "retrieval_mode": RETRIEVAL_MODE,
                "scoring": scoring,
                "text": entry.get("text"),
                "source_path": entry.get("source_path"),
                "layer": entry.get("layer"),
                "source_type": entry.get("source_type"),
                "category": entry.get("category"),
                "product_id": entry.get("product_id"),
                "risk_terms": entry.get("risk_terms", []),
            }
            hits.append(hit)
        hits.sort(key=lambda item: item["score"], reverse=True)
        hits = hits[: max(1, min(int(limit or 6), 20))]
        confidence = hits[0]["score"] if hits else 0.0
        return {
            "ok": True,
            "query": query_text,
            "query_profile": compact_query_profile(query_profile),
            "tenant_id": self.tenant_id,
            "hits": hits,
            "confidence": round(float(confidence), 4),
            "confidence_band": confidence_band(float(confidence)),
            "retrieval_mode": RETRIEVAL_MODE,
            "rag_can_authorize": False,
            "structured_priority": True,
        }

    def search_experience_references(self, query: str, *, limit: int = 3) -> dict[str, Any]:
        query_text = str(query or "").strip()
        if not query_text:
            return {"ok": True, "query": query_text, "hits": [], "confidence": 0.0, "reference_only": True}
        query_profile = build_query_profile(query_text)
        high_risk_terms = rag_terms("high_risk_terms")
        hits: list[dict[str, Any]] = []
        for chunk in self.iter_experience_reference_chunks():
            entry = build_index_entry(chunk, high_risk_terms=high_risk_terms)
            scoring = score_entry(query_text, query_profile, entry, product_id="")
            score = float(scoring.get("final", 0.0))
            if score <= 0:
                continue
            hits.append(
                {
                    "chunk_id": entry.get("chunk_id"),
                    "source_id": entry.get("source_id"),
                    "score": round(score, 4),
                    "retrieval_mode": RETRIEVAL_MODE,
                    "scoring": scoring,
                    "text": entry.get("text"),
                    "source_path": entry.get("source_path"),
                    "layer": entry.get("layer"),
                    "source_type": entry.get("source_type"),
                    "category": entry.get("category"),
                    "product_id": entry.get("product_id"),
                    "risk_terms": entry.get("risk_terms", []),
                    "reference_only": True,
                    "rag_can_authorize": False,
                }
            )
        hits.sort(key=lambda item: item["score"], reverse=True)
        hits = hits[: max(1, min(int(limit or 3), 10))]
        confidence = hits[0]["score"] if hits else 0.0
        return {
            "ok": True,
            "query": query_text,
            "query_profile": compact_query_profile(query_profile),
            "tenant_id": self.tenant_id,
            "hits": hits,
            "confidence": round(float(confidence), 4),
            "confidence_band": confidence_band(float(confidence)),
            "retrieval_mode": RETRIEVAL_MODE,
            "rag_can_authorize": False,
            "structured_priority": True,
            "reference_only": True,
        }

    def evidence(
        self,
        query: str,
        *,
        context: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        context = context or {}
        result = self.search(
            query,
            product_id=str(context.get("last_product_id") or context.get("product_id") or ""),
            limit=limit,
        )
        reference_result = self.search_experience_references(query, limit=min(3, max(1, int(limit or 5))))
        hits = list(result.get("hits", []) or [])
        hits.extend(reference_result.get("hits", []) or [])
        confidence = max(float(result.get("confidence") or 0.0), float(reference_result.get("confidence") or 0.0))
        return {
            "enabled": True,
            "query": result.get("query"),
            "tenant_id": self.tenant_id,
            "hits": hits[: max(1, min(int(limit or 5), 20)) + min(3, len(reference_result.get("hits", []) or []))],
            "confidence": round(confidence, 4),
            "reference_hit_count": len(reference_result.get("hits", []) or []),
            "rag_can_authorize": False,
            "structured_priority": True,
        }

    def status(self) -> dict[str, Any]:
        db = postgres_store(self.tenant_id)
        if db:
            sources = db.list_rag_sources(self.tenant_id)
            chunks = db.list_rag_chunks(self.tenant_id)
            index_entries = db.list_rag_index(self.tenant_id)
            return {
                "ok": True,
                "tenant_id": self.tenant_id,
                "backend": "postgres",
                "schema": db.schema,
                "sources_root": str(self.sources_root),
                "chunks_root": str(self.chunks_root),
                "index_root": str(self.index_root),
                "cache_root": str(self.cache_root),
                "source_count": len(sources),
                "chunk_count": len(chunks),
                "index_entry_count": len(index_entries),
                "index_exists": bool(index_entries),
                "index_path": f"postgres://{db.schema}.rag_index_entries",
                "updated_at": "postgres",
            }
        sources = self.list_sources()
        chunks = self.iter_chunks(include_experience_pool=False)
        index = {"entries": [], "built_at": ""}
        if self.index_path.exists():
            try:
                index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                index = {"entries": [], "built_at": ""}
        return {
            "ok": True,
            "tenant_id": self.tenant_id,
            "sources_root": str(self.sources_root),
            "chunks_root": str(self.chunks_root),
            "index_root": str(self.index_root),
            "cache_root": str(self.cache_root),
            "source_count": len(sources),
            "chunk_count": len(chunks),
            "index_entry_count": len(index.get("entries", []) or []),
            "index_exists": self.index_path.exists(),
            "index_path": str(self.index_path),
            "updated_at": str(index.get("built_at") or ""),
            "index_stale": self.index_is_stale() if self.index_path.exists() else True,
        }


def read_source_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def build_chunks(text: str, *, source: dict[str, Any], max_chars: int = 900, overlap: int = 120) -> list[dict[str, Any]]:
    normalized = normalize_text_block(text)
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    if not paragraphs:
        paragraphs = [normalized]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = (current + "\n\n" + paragraph).strip()
            continue
        if current:
            chunks.append(current)
        current = paragraph
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max(0, max_chars - overlap) :]
    if current:
        chunks.append(current)

    result = []
    for index, chunk_text in enumerate(chunks):
        chunk_id = "chunk_" + stable_digest(f"{source.get('source_id')}:{index}:{chunk_text}", 16)
        result.append(
            {
                "chunk_id": chunk_id,
                "source_id": source.get("source_id"),
                "tenant_id": source.get("tenant_id"),
                "layer": source.get("layer"),
                "source_type": source.get("source_type"),
                "category": source.get("category"),
                "product_id": source.get("product_id"),
                "source_path": source.get("source_path"),
                "chunk_index": index,
                "text": chunk_text,
                "char_count": len(chunk_text),
                "status": "active",
                "created_at": source.get("created_at") or now(),
            }
        )
    return result


def build_experience_chunk_text(item: dict[str, Any]) -> str:
    hit = item.get("rag_hit", {}) or {}
    parts = [
        f"AI经验池概括：{item.get('summary') or ''}",
        f"客户问法：{item.get('question') or ''}",
        f"历史回复要点：{item.get('reply_text') or ''}",
    ]
    hit_text = str(hit.get("text") or "").strip()
    if hit_text:
        parts.append(f"当时命中的资料：{hit_text}")
    product_id = str(hit.get("product_id") or "").strip()
    if product_id:
        parts.append(f"关联商品：{product_id}")
    return normalize_text_block("\n".join(parts))


def runtime_rag_entry_allowed(entry: dict[str, Any]) -> bool:
    """Guard old indexes so AI经验池/chats cannot leak into runtime content evidence."""

    source_type = str(entry.get("source_type") or "").strip()
    category = str(entry.get("category") or "").strip()
    layer = str(entry.get("layer") or "").strip()
    if source_type == "rag_experience" or category == "rag_experience" or layer == "rag_experience":
        return False
    if source_type in {
        "cleaned_real_chat_pack",
        "real_chat",
        "real_chat_style",
        "wechat_raw_message",
        "raw_wechat_private",
        "raw_wechat_group",
        "raw_wechat_file_transfer",
        "chat_log",
        "upload",
    }:
        return False
    if can_authorize_reply_content is not None:
        return bool(can_authorize_reply_content(entry, category_id=category, source_type=source_type))
    return True


def build_index_entry(chunk: dict[str, Any], *, high_risk_terms: set[str] | None = None) -> dict[str, Any]:
    text = str(chunk.get("text") or "")
    terms = sorted(tokenize(text))
    semantic_terms = sorted(expand_semantic_terms(text, terms))
    vector = build_sparse_vector([*terms, *semantic_terms])
    risk_terms = high_risk_terms if high_risk_terms is not None else rag_terms("high_risk_terms")
    return {
        **chunk,
        "retrieval_exclusion_reason": rag_chunk_exclusion_reason(chunk),
        "terms": terms,
        "semantic_terms": semantic_terms,
        "vector": vector,
        "vector_dimensions": VECTOR_DIMENSIONS,
        "term_count": len(terms),
        "semantic_term_count": len(semantic_terms),
        "risk_terms": sorted(term for term in risk_terms if term in text),
    }


def normalize_text_block(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def tokenize(text: str) -> set[str]:
    normalized = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_.-]{2,}", normalized, flags=re.IGNORECASE))
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", normalized)
    for run in cjk_runs:
        tokens.add(run)
        for size in (2, 3, 4):
            if len(run) >= size:
                for index in range(0, len(run) - size + 1):
                    tokens.add(run[index : index + size])
    return {token for token in tokens if token.strip()}


def build_query_profile(query: str) -> dict[str, Any]:
    query_text = normalize_search_text(query)
    terms = tokenize(query_text)
    expanded_terms = expand_semantic_terms(query_text, terms)
    variants = sorted(build_query_variants(query_text, expanded_terms))
    variant_terms = set(terms)
    for variant in variants:
        variant_terms.update(tokenize(variant))
    expanded_terms.update(variant_terms)
    return {
        "text": query_text,
        "terms": terms,
        "expanded_terms": expanded_terms,
        "variants": variants,
        "vector": build_sparse_vector(expanded_terms),
    }


def build_query_variants(query: str, expanded_terms: set[str]) -> set[str]:
    variants = {query}
    for term in sorted(expanded_terms, key=len, reverse=True):
        if term and term in query:
            for equivalent in semantic_equivalents(term):
                variants.add(query.replace(term, equivalent))
    return {variant for variant in variants if variant.strip()}


def expand_semantic_terms(text: str, terms: set[str] | list[str] | tuple[str, ...] | None = None) -> set[str]:
    base = set(terms or tokenize(text))
    normalized = normalize_search_text(text)
    expanded = set(base)
    for term in list(base):
        expanded.update(semantic_equivalents(term))
    configured_equivalents = semantic_equivalents_map()
    for term, equivalents in configured_equivalents.items():
        if term in normalized:
            expanded.add(term)
            expanded.update(equivalents)
        elif any(equivalent in normalized for equivalent in equivalents):
            expanded.add(term)
            expanded.update(equivalents)
    return {normalize_search_text(item) for item in expanded if normalize_search_text(item)}


def semantic_equivalents(term: str) -> set[str]:
    normalized = normalize_search_text(term)
    configured_equivalents = semantic_equivalents_map()
    equivalents = set(configured_equivalents.get(normalized, ()))
    for key, values in configured_equivalents.items():
        if normalized in values:
            equivalents.add(key)
            equivalents.update(values)
    equivalents.discard(normalized)
    return {normalize_search_text(item) for item in equivalents if normalize_search_text(item)}


def semantic_equivalents_map() -> dict[str, set[str]]:
    key = platform_understanding_cache_token()
    if _SEMANTIC_EQUIVALENTS_MAP_CACHE.get("key") == key and isinstance(_SEMANTIC_EQUIVALENTS_MAP_CACHE.get("value"), dict):
        return _SEMANTIC_EQUIVALENTS_MAP_CACHE["value"]
    merged: dict[str, set[str]] = {}
    for raw_map in (visible_semantic_equivalents(), DEFAULT_SEMANTIC_EQUIVALENTS):
        for raw_key, raw_values in raw_map.items():
            key = normalize_search_text(raw_key)
            if not key:
                continue
            bucket = merged.setdefault(key, set())
            for raw_value in raw_values:
                value = normalize_search_text(raw_value)
                if value and value != key:
                    bucket.add(value)
    for key, values in list(merged.items()):
        for value in values:
            merged.setdefault(value, set()).add(key)
    _SEMANTIC_EQUIVALENTS_MAP_CACHE["key"] = platform_understanding_cache_token()
    _SEMANTIC_EQUIVALENTS_MAP_CACHE["value"] = merged
    return merged


def score_entry(query: str, query_profile: dict[str, Any] | set[str], entry: dict[str, Any], *, product_id: str = "") -> dict[str, float]:
    text = str(entry.get("text") or "").lower()
    if isinstance(query_profile, set):
        query_profile = {"text": normalize_search_text(query), "terms": query_profile, "expanded_terms": set(query_profile), "variants": [query]}
    query_text = str(query_profile.get("text") or normalize_search_text(query))
    query_terms = set(query_profile.get("terms", set()) or set())
    expanded_query_terms = set(query_profile.get("expanded_terms", set()) or query_terms)
    entry_terms = set(entry.get("terms", []) or [])
    if not entry_terms:
        entry_terms = tokenize(text)
    entry_semantic_terms = set(entry.get("semantic_terms", []) or [])
    if not entry_semantic_terms:
        entry_semantic_terms = expand_semantic_terms(text, entry_terms)
    query_vector = query_profile.get("vector") if isinstance(query_profile.get("vector"), dict) else build_sparse_vector(expanded_query_terms)
    entry_vector = entry.get("vector") if isinstance(entry.get("vector"), dict) else build_sparse_vector(entry_terms | entry_semantic_terms)
    vector_similarity = cosine_similarity(query_vector, entry_vector)
    overlap = query_terms & entry_terms
    semantic_overlap = expanded_query_terms & (entry_terms | entry_semantic_terms)
    variants = [str(item).lower() for item in query_profile.get("variants", []) or [] if str(item).strip()]
    phrase_match = query_text and query_text in text
    variant_match = any(variant and variant in text for variant in variants)
    if not overlap and not semantic_overlap and not phrase_match and not variant_match and vector_similarity < 0.1:
        return empty_scoring()
    coverage = len(overlap) / max(1, len(query_terms))
    density = len(overlap) / math.sqrt(max(1, len(entry_terms)))
    semantic_coverage = len(semantic_overlap) / max(1, len(expanded_query_terms))
    semantic_density = len(semantic_overlap) / math.sqrt(max(1, len(entry_semantic_terms | entry_terms)))
    phrase_bonus = 0.16 if phrase_match else 0.08 if variant_match else 0.0
    product_bonus = 0.15 if product_id and str(entry.get("product_id") or "") == product_id else 0.0
    boost = SOURCE_TYPE_BOOSTS.get(str(entry.get("source_type") or ""), 0.0) + CATEGORY_BOOSTS.get(str(entry.get("category") or ""), 0.0)
    risk_penalty = 0.08 if entry.get("risk_terms") else 0.0
    lexical = coverage * 0.46 + min(0.16, density)
    semantic = semantic_coverage * 0.2 + min(0.12, semantic_density)
    vector_component = min(0.14, vector_similarity * 0.18)
    final = lexical + semantic + vector_component + phrase_bonus + product_bonus + boost - risk_penalty
    return {
        "lexical": round(max(0.0, lexical), 4),
        "semantic": round(max(0.0, semantic), 4),
        "vector": round(max(0.0, vector_component), 4),
        "vector_similarity": round(max(0.0, vector_similarity), 4),
        "phrase": round(max(0.0, phrase_bonus), 4),
        "product": round(max(0.0, product_bonus), 4),
        "boost": round(boost, 4),
        "risk_penalty": round(risk_penalty, 4),
        "final": round(min(0.99, max(0.0, final)), 4),
    }


def empty_scoring() -> dict[str, float]:
    return {
        "lexical": 0.0,
        "semantic": 0.0,
        "vector": 0.0,
        "vector_similarity": 0.0,
        "phrase": 0.0,
        "product": 0.0,
        "boost": 0.0,
        "risk_penalty": 0.0,
        "final": 0.0,
    }


def compact_query_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "terms": sorted(profile.get("terms", set()) or [])[:40],
        "expanded_terms": sorted(profile.get("expanded_terms", set()) or [])[:60],
        "variants": sorted(profile.get("variants", []) or [])[:12],
        "vector_dimensions": VECTOR_DIMENSIONS,
    }


def confidence_band(score: float) -> str:
    if score >= 0.62:
        return "high"
    if score >= 0.28:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def normalize_search_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def infer_category_from_path(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    for category in ("products", "chats", "policies", "erp_exports"):
        if category in parts:
            return category
    return ""


def normalize_source_type(value: str) -> str:
    text = str(value or "upload").strip()
    return text if text in DEFAULT_SOURCE_TYPES else "upload"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stable_digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def build_sparse_vector(terms: set[str] | list[str] | tuple[str, ...]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for term in terms:
        normalized = normalize_search_text(str(term))
        if not normalized:
            continue
        bucket = int(hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8], 16) % VECTOR_DIMENSIONS
        key = str(bucket)
        weights[key] = weights.get(key, 0.0) + 1.0
    norm = math.sqrt(sum(value * value for value in weights.values()))
    if norm <= 0:
        return {}
    return {key: round(value / norm, 6) for key, value in sorted(weights.items(), key=lambda item: int(item[0]))}


def cosine_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    if not left or not right:
        return 0.0
    total = 0.0
    for key, value in left.items():
        try:
            total += float(value) * float(right.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return max(0.0, min(1.0, total))


def compact_hits(hits: list[dict[str, Any]], *, limit: int = 3, text_limit: int = 260) -> list[dict[str, Any]]:
    compacted = []
    for hit in hits[:limit]:
        compacted.append(
            {
                "chunk_id": hit.get("chunk_id"),
                "source_id": hit.get("source_id"),
                "score": hit.get("score"),
                "category": hit.get("category"),
                "product_id": hit.get("product_id"),
                "retrieval_mode": hit.get("retrieval_mode"),
                "scoring": hit.get("scoring", {}),
                "text": str(hit.get("text") or "")[:text_limit],
            }
        )
    return compacted


def postgres_store(tenant_id: str):
    config = load_storage_config()
    if not config.use_postgres or not config.postgres_configured:
        return None
    store = get_postgres_store(tenant_id=tenant_id, config=config)
    return store if store.available() else None
