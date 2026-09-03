from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, SessionLocal, engine
from app.core.request_context import ActorContext
from app.errors import AppError
from app.main import app
from app.models.audit import OperationLog
from app.models.c3 import Conversation, MessageBatch
from app.models.knowledge_management import (
    CurrentKnowledgeRelease,
    KnowledgeRelease,
    ManagedKnowledgeItem,
)
from app.services.c3_service import create_control_message_batch
from app.services import knowledge_management_service
from app.schemas.knowledge_management import KnowledgePublishPreviewRequest
from app.services.knowledge_management_service import (
    build_retrieval_index,
    confirm_preview,
    create_draft,
    create_preview,
    dashboard_summary,
    release_snapshot_for_batch,
)


client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _preview(payload: dict) -> dict:
    response = client.post("/api/knowledge/releases/preview", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_draft(title: str, content: str) -> dict:
    response = client.post("/api/knowledge/items", json={"title": title, "content": content})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _update_draft(item: dict, *, title: str | None = None, content: str | None = None) -> dict:
    response = client.put(
        f"/api/knowledge/items/{item['id']}/draft",
        json={
            "title": title if title is not None else item["title"],
            "content": content if content is not None else item["content"],
            "expected_updated_at": item["updated_at"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _confirm(preview: dict) -> dict:
    response = client.post(
        "/api/knowledge/releases",
        json={
            "preview_id": preview["preview_id"],
            "content_digest": preview["content_digest"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_initial_release_is_empty_and_seed_import_requires_explicit_draft_action():
    summary = client.get("/api/knowledge/summary")
    assert summary.status_code == 200, summary.text
    summary_data = summary.json()["data"]
    assert summary_data["published_count"] == 0
    assert summary_data["draft_count"] == 0
    assert summary_data["current_release"]["is_current"] is True

    listing = client.get("/api/knowledge/items", params={"status": "published", "page_size": 100})
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]["items"]
    assert items == []

    imported = client.post("/api/knowledge/seeds/import-drafts")
    assert imported.status_code == 200, imported.text
    imported_data = imported.json()["data"]
    assert imported_data["imported_count"] >= 5
    assert all(item["status"] == "draft" for item in imported_data["items"])
    assert client.get("/api/knowledge/summary").json()["data"]["published_count"] == 0
    assert client.get("/api/knowledge/summary").json()["data"]["draft_count"] == imported_data["imported_count"]


def test_bootstrap_and_confirm_obey_foreign_key_insert_order():
    """Exercise the PostgreSQL FK boundary even in the default SQLite suite."""

    constrained_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={
            "schema_translate_map": {"wechat_ai_customer_service": None}
        },
    )

    @event.listens_for(constrained_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=constrained_engine)
    actor = ActorContext(
        operator_id=UUID("00000000-0000-0000-0000-000000000001"),
        operator_name="Ops Tester",
        role="authenticated",
        ip_address="127.0.0.1",
        user_agent="knowledge-fk-test",
        request_id="knowledge-fk-test",
    )
    try:
        with Session(constrained_engine) as db:
            bootstrap = dashboard_summary(db)["current_release"]
            db.commit()

            draft = create_draft(
                db,
                title="外键顺序验证知识",
                content="发布版本、知识条目、修订记录和当前版本指针必须在同一事务中按依赖顺序写入。",
                actor=actor,
            )
            db.commit()
            preview = create_preview(
                db,
                KnowledgePublishPreviewRequest(
                    operation="create",
                    item_id=draft["id"],
                ),
                actor,
            )
            db.commit()
            published = confirm_preview(
                db,
                preview_id=preview["preview_id"],
                content_digest=preview["content_digest"],
                actor=actor,
            )
            db.commit()

            assert published["release"]["id"] != bootstrap["id"]
            assert db.get(CurrentKnowledgeRelease, "chejin").release_id == published["release"]["id"]
    finally:
        Base.metadata.drop_all(bind=constrained_engine)
        constrained_engine.dispose()


def test_bootstrap_freezes_preexisting_batches_before_first_operator_publish():
    conversation_id = "conv-before-knowledge-release"
    with SessionLocal() as db:
        db.add(Conversation(conversation_id=conversation_id))
        db.add(
            MessageBatch(
                id="batch-before-knowledge-release",
                conversation_id=conversation_id,
                trigger_type="customer_message",
                trigger_key="pre-deployment-batch",
                knowledge_release_id=None,
            )
        )
        db.commit()

    bootstrap = client.get("/api/knowledge/summary").json()["data"][
        "current_release"
    ]
    with SessionLocal() as db:
        batch = db.get(MessageBatch, "batch-before-knowledge-release")
        assert batch is not None
        assert batch.knowledge_release_id == bootstrap["id"]


def test_preview_is_non_effective_confirm_is_atomic_and_invalid_preview_cannot_publish():
    initial = client.get("/api/knowledge/summary").json()["data"]
    draft = _create_draft(
        "到店试驾安排说明",
        "客户希望试驾时，先确认意向车型和可到店时间；具体车辆与时间由销售确认。",
    )
    preview = _preview(
        {
            "operation": "create",
            "item_id": draft["id"],
        }
    )
    assert preview["can_publish"] is True
    assert client.get("/api/knowledge/summary").json()["data"]["current_release"]["id"] == initial["current_release"]["id"]
    before_publish = client.get("/api/knowledge/items", params={"keyword": "到店试驾安排说明"}).json()["data"]
    assert before_publish["total"] == 1
    assert before_publish["items"][0]["status"] == "draft"
    assert initial["published_count"] == 0

    published = _confirm(preview)
    assert published["release"]["id"] != initial["current_release"]["id"]
    assert published["item"]["title"] == "到店试驾安排说明"
    assert client.get("/api/knowledge/items", params={"keyword": "到店试驾安排说明"}).json()["data"]["total"] == 1

    invalid_draft = _create_draft("错误承诺", "我们保证最低价并且百分百有车。")
    invalid = _preview(
        {
            "operation": "create",
            "item_id": invalid_draft["id"],
        }
    )
    assert invalid["can_publish"] is False
    before_invalid_confirm = client.get("/api/knowledge/summary").json()["data"]["current_release"]["id"]
    denied = client.post(
        "/api/knowledge/releases",
        json={"preview_id": invalid["preview_id"], "content_digest": invalid["content_digest"]},
    )
    assert denied.status_code == 409, denied.text
    assert client.get("/api/knowledge/summary").json()["data"]["current_release"]["id"] == before_invalid_confirm
    with SessionLocal() as db:
        failed = db.query(OperationLog).filter(OperationLog.event_type == "knowledge_operation_failed").one()
        assert failed.extra_metadata["error_code"] == "KNOWLEDGE_PREVIEW_VALIDATION_FAILED"


def test_edit_archive_and_rollback_create_immutable_release_history():
    initial_release = client.get("/api/knowledge/summary").json()["data"]["current_release"]
    draft = _create_draft("试驾规则", "客户提出试驾时先确认车型和时间。")
    created = _confirm(_preview({"operation": "create", "item_id": draft["id"]}))
    item = created["item"]
    published_release = created["release"]

    updated_draft = _update_draft(item, content=f"{item['content']}\n如客户信息不足，先追问必要事实。")
    update_preview = _preview(
        {
            "operation": "update",
            "item_id": item["id"],
            "expected_updated_at": updated_draft["updated_at"],
        }
    )
    updated = _confirm(update_preview)
    assert updated["release"]["action"] == "update"

    staged_response = client.post(f"/api/knowledge/items/{item['id']}/archive")
    assert staged_response.status_code == 200, staged_response.text
    staged = staged_response.json()["data"]
    archive_preview = _preview(
        {
            "operation": "archive",
            "item_id": item["id"],
            "expected_updated_at": staged["updated_at"],
        }
    )
    archived = _confirm(archive_preview)
    assert archived["item"]["status"] == "archived"

    rollback_preview_response = client.post(
        "/api/knowledge/releases/rollback/preview",
        json={"target_release_id": published_release["id"]},
    )
    assert rollback_preview_response.status_code == 200, rollback_preview_response.text
    rolled_back = _confirm(rollback_preview_response.json()["data"])
    assert rolled_back["release"]["action"] == "rollback"
    assert rolled_back["release"]["id"] not in {
        initial_release["id"],
        published_release["id"],
        updated["release"]["id"],
        archived["release"]["id"],
    }
    assert rolled_back["release"]["snapshot_sha256"] == published_release["snapshot_sha256"]

    releases = client.get("/api/knowledge/releases", params={"page_size": 100}).json()["data"]["items"]
    assert len(releases) == 5
    assert sum(1 for release in releases if release["is_current"]) == 1
    with SessionLocal() as db:
        assert db.query(OperationLog).filter(OperationLog.module == "knowledge").count() >= 6


def test_message_batch_freezes_release_and_new_batch_gets_new_release_only():
    current = client.get("/api/knowledge/summary").json()["data"]["current_release"]
    with SessionLocal() as db:
        db.add_all([Conversation(conversation_id="conversation-old"), Conversation(conversation_id="conversation-new")])
        db.commit()
        first = create_control_message_batch(
            db,
            conversation_id="conversation-old",
            trigger_type="customer_message",
            trigger_key="generation-1",
        )["batch"]
        db.commit()
    assert first["knowledge_release_id"] == current["id"]

    draft = _create_draft("新批次知识", "这条知识只能进入发布后新创建的消息批次。")
    published = _confirm(_preview({"operation": "create", "item_id": draft["id"]}))
    with SessionLocal() as db:
        old_batch = db.get(MessageBatch, first["id"])
        assert old_batch is not None
        assert old_batch.knowledge_release_id == current["id"]
        old_snapshot = release_snapshot_for_batch(db, old_batch.knowledge_release_id)
        assert all(item["title"] != "新批次知识" for item in old_snapshot["items"])

        second = create_control_message_batch(
            db,
            conversation_id="conversation-new",
            trigger_type="customer_message",
            trigger_key="generation-2",
        )["batch"]
        db.commit()
        assert second["knowledge_release_id"] == published["release"]["id"]
        new_snapshot = release_snapshot_for_batch(db, second["knowledge_release_id"])
        assert any(item["title"] == "新批次知识" for item in new_snapshot["items"])

    pending_content = "这是尚未发布的新草稿，不得进入 Brain。"
    _update_draft(published["item"], content=pending_content)
    with SessionLocal() as db:
        db.add(Conversation(conversation_id="conversation-draft-hidden"))
        db.commit()
        while_draft = create_control_message_batch(
            db,
            conversation_id="conversation-draft-hidden",
            trigger_type="customer_message",
            trigger_key="generation-draft-hidden",
        )["batch"]
        db.commit()
        assert while_draft["knowledge_release_id"] == published["release"]["id"]
        draft_hidden_snapshot = release_snapshot_for_batch(
            db,
            while_draft["knowledge_release_id"],
        )
        assert all(item["content"] != pending_content for item in draft_hidden_snapshot["items"])
        assert any(item["content"] == "这条知识只能进入发布后新创建的消息批次。" for item in draft_hidden_snapshot["items"])


def test_index_build_failure_rolls_back_release_pointer_and_keeps_draft(monkeypatch):
    initial = client.get("/api/knowledge/summary").json()["data"]["current_release"]
    draft = _create_draft("索引失败测试", "发布只能在不可变检索索引完整构建后切换当前版本。")
    preview = _preview({"operation": "create", "item_id": draft["id"]})

    def fail_index(_snapshot):
        raise AppError("KNOWLEDGE_INDEX_BUILD_FAILED", "知识检索索引构建失败", 500)

    monkeypatch.setattr(knowledge_management_service, "build_retrieval_index", fail_index)
    response = client.post(
        "/api/knowledge/releases",
        json={"preview_id": preview["preview_id"], "content_digest": preview["content_digest"]},
    )
    assert response.status_code == 500, response.text

    with SessionLocal() as db:
        pointer = db.get(CurrentKnowledgeRelease, "chejin")
        item = db.get(ManagedKnowledgeItem, draft["id"])
        preview_row = db.get(
            knowledge_management_service.KnowledgePublishPreview,
            preview["preview_id"],
        )
        assert pointer.release_id == initial["id"]
        assert db.query(KnowledgeRelease).count() == 1
        assert item.status == "draft"
        assert item.draft_revision_id == draft["draft_revision_id"]
        assert preview_row.consumed is False


def test_omniauto_uses_exact_batch_snapshot_and_rejects_digest_mismatch():
    omniauto_root = Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
    workflow_root = omniauto_root / "apps" / "wechat_ai_customer_service" / "workflows"
    for path in (omniauto_root, workflow_root, workflow_root.parent, workflow_root.parent / "adapters"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.wechat_ai_customer_service.workflows.reply_evidence_builder import (  # noqa: PLC0415
        ChejinKnowledgeProjectionError,
        apply_chejin_knowledge_release,
    )

    items = [
        {
            "item_id": "knowledge-1",
            "revision_id": "revision-1",
            "title": "正式规则",
            "content": "只使用消息批次冻结的正式知识。",
            "content_sha256": "ignored-by-release-projection",
        }
    ]
    digest = sha256(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    retrieval_index = build_retrieval_index(items)
    target_state = {
        "chejin_knowledge_required": True,
        "chejin_knowledge_release": {
            "release_id": "release-1",
            "version": "KR-TEST-01",
            "snapshot_sha256": digest,
            "retrieval_index": retrieval_index,
            "retrieval_index_sha256": sha256(
                json.dumps(retrieval_index, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "items": items,
        },
    }
    evidence_pack = {
        "knowledge": {
            "selected_items": [{"id": "stale-runtime"}],
            "evidence": {
                "faq": [{"id": "stale-runtime", "answer": "不得进入 Brain"}],
                "policies": {"stale-runtime": {"answer": "不得进入 Brain"}},
                "product_scoped": [{"id": "vehicle-published-1", "answer": "车辆公开说明"}],
            },
            "formal_knowledge": {"faq": [{"id": "stale-runtime", "answer": "不得进入 Brain"}]},
        }
    }
    evidence_pack["rag"] = {"hits": [{"text": "旧RAG毒数据"}]}
    apply_chejin_knowledge_release(evidence_pack, target_state, query_text="请按正式规则处理")
    faq = evidence_pack["knowledge"]["formal_knowledge"]["faq"]
    assert [item["id"] for item in faq] == ["knowledge-1"]
    assert evidence_pack["knowledge"]["selected_items"] == []
    assert evidence_pack["knowledge"]["formal_knowledge"]["policies"] == {}
    assert evidence_pack["knowledge"]["formal_knowledge"]["product_scoped"] == []
    assert evidence_pack["knowledge"]["evidence"]["product_scoped"] == []
    assert evidence_pack["knowledge_release"]["release_id"] == "release-1"
    assert evidence_pack["rag"]["hits"] == []

    target_state["chejin_knowledge_release"]["snapshot_sha256"] = "0" * 64
    try:
        apply_chejin_knowledge_release(evidence_pack, target_state, query_text="请按正式规则处理")
    except ChejinKnowledgeProjectionError as exc:
        assert "digest_mismatch" in str(exc)
    else:  # pragma: no cover - fail-closed contract
        raise AssertionError("digest mismatch must stop before Provider")
