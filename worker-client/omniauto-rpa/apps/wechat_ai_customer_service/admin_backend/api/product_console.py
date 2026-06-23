"""Merchant-friendly product workbench APIs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from apps.wechat_ai_customer_service.knowledge_paths import runtime_app_root

from ..services.product_console_service import ProductConsoleService


router = APIRouter(prefix="/api/product-console", tags=["product-console"])


def service() -> ProductConsoleService:
    return ProductConsoleService()


@router.get("/catalog")
def catalog(include_archived: bool = Query(False)) -> dict[str, Any]:
    return service().catalog(include_archived=include_archived)


@router.get("/products/{product_id}")
def product_detail(product_id: str) -> dict[str, Any]:
    try:
        return service().detail(product_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"product not found: {product_id}") from exc


@router.post("/products/{product_id}/inventory")
def adjust_inventory(product_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return service().adjust_inventory(
            product_id,
            operation=str((payload or {}).get("operation") or ""),
            quantity=(payload or {}).get("quantity"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"product not found: {product_id}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/command")
def product_command(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return service().command(
            str((payload or {}).get("message") or ""),
            use_llm=bool((payload or {}).get("use_llm", True)),
            dry_run=bool((payload or {}).get("dry_run", False)),
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/upload-draft")
async def upload_product_draft(use_llm: bool = Form(True), file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        content = await file.read()
        return service().upload_product_draft(
            filename=file.filename or "product_upload.txt",
            content=content,
            use_llm=use_llm,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm-intake")
def product_llm_intake(payload: dict[str, Any]) -> dict[str, Any]:
    from ..services.knowledge_generator import KnowledgeGenerator

    try:
        text = str((payload or {}).get("text") or "").strip()
        incoming_session_id = str((payload or {}).get("session_id") or "").strip()
        use_llm = bool((payload or {}).get("use_llm", True))
        if not text:
            raise ValueError("text is required")

        if incoming_session_id.startswith("gen_"):
            gen_result = KnowledgeGenerator().continue_session(incoming_session_id, text, use_llm=use_llm)
            gen_session = gen_result.get("session") if isinstance(gen_result, dict) else {}
            compat = new_assistant_session(use_llm=use_llm)
            compat.update(
                {
                    "mode": "generator",
                    "generator_session_id": str(gen_session.get("session_id") or incoming_session_id),
                    "status": "ready" if str(gen_session.get("status") or "") == "ready" else "collecting",
                    "question": str(gen_session.get("question") or ""),
                    "missing_fields": gen_session.get("missing_fields") or [],
                    "warnings": gen_session.get("warnings") or [],
                    "draft_item": gen_session.get("draft_item") or {},
                }
            )
            save_assistant_session(compat)
            return {
                "ok": True,
                "session": {
                    "session_id": str(compat.get("session_id") or ""),
                    "mode": "generator",
                    "status": str(compat.get("status") or ""),
                },
                "status": str(compat.get("status") or ""),
                "question": str(compat.get("question") or ""),
                "missing_fields": compat.get("missing_fields") or [],
                "draft_item": compat.get("draft_item") or {},
                "warnings": compat.get("warnings") or [],
                "direct_apply_allowed": str(compat.get("status") or "") == "ready",
                "assistant_preview": {},
            }

        session = load_assistant_session(incoming_session_id) if incoming_session_id else new_assistant_session(use_llm=use_llm)
        session["updated_at"] = now_iso()
        session["use_llm"] = bool(use_llm)
        history = session.get("history") if isinstance(session.get("history"), list) else []
        history.append({"role": "user", "content": text, "created_at": now_iso()})
        session["history"] = history[-30:]

        mode = str(session.get("mode") or "")
        if mode == "generator":
            gen = KnowledgeGenerator()
            generator_session_id = str(session.get("generator_session_id") or "")
            if not generator_session_id:
                gen_result = gen.create_session(text, preferred_category_id="products", use_llm=use_llm)
            else:
                gen_result = gen.continue_session(generator_session_id, text, use_llm=use_llm)
            gen_session = gen_result.get("session") if isinstance(gen_result, dict) else {}
            session.update(
                {
                    "mode": "generator",
                    "generator_session_id": str(gen_session.get("session_id") or session.get("generator_session_id") or ""),
                    "status": "ready" if str(gen_session.get("status") or "") == "ready" else "collecting",
                    "question": str(gen_session.get("question") or ""),
                    "missing_fields": gen_session.get("missing_fields") or [],
                    "warnings": gen_session.get("warnings") or [],
                    "draft_item": gen_session.get("draft_item") or {},
                    "command_plan": {},
                    "aggregated_text": str(session.get("aggregated_text") or ""),
                }
            )
        else:
            previous_aggregated = str(session.get("aggregated_text") or "").strip()
            aggregated = f"{previous_aggregated}\n{text}".strip() if previous_aggregated else text
            session["aggregated_text"] = aggregated
            command_service = service()
            try:
                plan = command_service.command(aggregated, use_llm=use_llm, dry_run=True)
            except ValueError as exc:
                question = str(exc) or "信息还不完整，请继续补充。"
                if previous_aggregated:
                    try:
                        latest_plan = command_service.command(text, use_llm=use_llm, dry_run=True)
                    except ValueError as latest_exc:
                        latest_question = str(latest_exc or "").strip()
                        if latest_question and is_generic_command_error(question):
                            question = latest_question
                    else:
                        session["aggregated_text"] = text
                        apply_command_plan_to_session(session, latest_plan, source_text=text, use_llm=use_llm)
                        latest_plan = None
                        question = ""
                if question:
                    session.update(
                        {
                            "mode": "command",
                            "generator_session_id": "",
                            "status": "collecting",
                            "question": question,
                            "missing_fields": parse_missing_fields_from_error(question),
                            "warnings": [],
                            "draft_item": {},
                            "command_plan": {},
                        }
                    )
            else:
                apply_command_plan_to_session(session, plan, source_text=aggregated, use_llm=use_llm)

        save_assistant_session(session)
        direct_apply_allowed = str(session.get("status") or "") == "ready" and str(session.get("mode") or "") in {"command", "generator"}
        return {
            "ok": True,
            "session": {
                "session_id": str(session.get("session_id") or ""),
                "mode": str(session.get("mode") or ""),
                "status": str(session.get("status") or ""),
            },
            "status": str(session.get("status") or ""),
            "question": str(session.get("question") or ""),
            "missing_fields": session.get("missing_fields") or [],
            "draft_item": session.get("draft_item") or {},
            "warnings": session.get("warnings") or [],
            "direct_apply_allowed": direct_apply_allowed,
            "assistant_preview": session.get("command_plan") or {},
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"session not found: {incoming_session_id}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm-intake/{session_id}/apply")
def apply_product_llm_intake(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from ..services.knowledge_generator import KnowledgeGenerator

    try:
        if session_id.startswith("gen_"):
            gen = KnowledgeGenerator()
            gen_session = gen.require_session(session_id)
            if str(gen_session.get("status") or "") != "ready":
                raise ValueError("Session is not ready. Please complete all required fields first.")
            if str(gen_session.get("category_id") or "") != "products":
                raise ValueError("Session category is not products.")
            saved = gen.confirm_session(session_id)
            if not bool(saved.get("ok")):
                raise ValueError(str(saved.get("message") or "failed to save product session"))
            return {
                "ok": True,
                "action": "product_llm_intake_applied",
                "item": saved.get("item") if isinstance(saved, dict) else {},
                "message": "商品已通过 AI 对话录入并入库。",
            }

        session = load_assistant_session(session_id)
        if str(session.get("status") or "") != "ready":
            raise ValueError("Session is not ready. Please complete all required fields first.")
        mode = str(session.get("mode") or "")
        if mode == "command":
            command_text = str(session.get("aggregated_text") or "").strip()
            if not command_text:
                raise ValueError("No command text in session.")
            result = service().command(command_text, use_llm=bool(session.get("use_llm", True)), dry_run=False)
            session.update({"status": "applied", "applied_at": now_iso(), "last_result": result})
            save_assistant_session(session)
            return {
                "ok": True,
                "action": "product_command_applied",
                "item": result.get("item") if isinstance(result, dict) else {},
                "result": result,
                "message": "操作已确认并执行。",
            }
        if mode != "generator":
            raise ValueError("unknown assistant session mode")

        generator_session_id = str(session.get("generator_session_id") or "")
        if not generator_session_id:
            raise ValueError("missing generator session id")
        gen = KnowledgeGenerator()
        gen_session = gen.require_session(generator_session_id)
        if str(gen_session.get("status") or "") != "ready":
            raise ValueError("Session is not ready. Please complete all required fields first.")
        if str(gen_session.get("category_id") or "") != "products":
            raise ValueError("Session category is not products.")
        saved = gen.confirm_session(generator_session_id)
        if not bool(saved.get("ok")):
            raise ValueError(str(saved.get("message") or "failed to save product session"))
        session.update({"status": "applied", "applied_at": now_iso(), "last_result": saved})
        save_assistant_session(session)
        return {
            "ok": True,
            "action": "product_llm_intake_applied",
            "item": saved.get("item") if isinstance(saved, dict) else {},
            "message": "商品已通过 AI 对话录入并入库。",
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


ASSISTANT_SESSION_ROOT = runtime_app_root() / "admin" / "product_console_assistant_sessions"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_assistant_session(*, use_llm: bool) -> dict[str, Any]:
    session_id = "pca_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    return {
        "session_id": session_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "collecting",
        "mode": "command",
        "use_llm": bool(use_llm),
        "history": [],
        "aggregated_text": "",
        "question": "",
        "missing_fields": [],
        "warnings": [],
        "draft_item": {},
        "command_plan": {},
        "generator_session_id": "",
        "last_result": {},
    }


def assistant_session_path(session_id: str) -> Path:
    return ASSISTANT_SESSION_ROOT / f"{session_id}.json"


def save_assistant_session(session: dict[str, Any]) -> None:
    ASSISTANT_SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    assistant_session_path(str(session.get("session_id") or "")).write_text(
        json.dumps(session, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_assistant_session(session_id: str) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    if not sid:
        raise FileNotFoundError("empty session id")
    path = assistant_session_path(sid)
    if not path.exists():
        raise FileNotFoundError(sid)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FileNotFoundError(sid)
    return payload


def parse_missing_fields_from_error(message: str) -> list[str]:
    text = str(message or "").strip()
    if not text:
        return []
    anchor = "请补充："
    if anchor in text:
        trailing = text.split(anchor, 1)[1].strip().strip("。")
        parts = [item.strip() for item in trailing.split("、") if item.strip()]
        return parts[:12]
    if "识别到要修改的商品" in text:
        return ["商品名称、SKU 或别名"]
    return []


def is_generic_command_error(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return True
    markers = [
        "没有明确的操作指令",
        "我还没听清你的需求",
        "我还没完全识别你的修改意图",
        "信息还不完整",
    ]
    return any(marker in text for marker in markers)


def apply_command_plan_to_session(session: dict[str, Any], plan: dict[str, Any], *, source_text: str, use_llm: bool) -> None:
    if str(plan.get("action") or "") == "draft_product":
        gen = KnowledgeGenerator()
        gen_result = gen.create_session(source_text, preferred_category_id="products", use_llm=use_llm)
        gen_session = gen_result.get("session") if isinstance(gen_result, dict) else {}
        session.update(
            {
                "mode": "generator",
                "generator_session_id": str(gen_session.get("session_id") or ""),
                "status": "ready" if str(gen_session.get("status") or "") == "ready" else "collecting",
                "question": str(gen_session.get("question") or ""),
                "missing_fields": gen_session.get("missing_fields") or [],
                "warnings": gen_session.get("warnings") or [],
                "draft_item": gen_session.get("draft_item") or {},
                "command_plan": {},
            }
        )
        return
    session.update(
        {
            "mode": "command",
            "generator_session_id": "",
            "status": "ready",
            "question": str(plan.get("summary") or "已识别操作，请确认执行。"),
            "missing_fields": [],
            "warnings": [],
            "draft_item": {
                "data": {
                    "name": str(plan.get("target_product_name") or ""),
                    "summary": str(plan.get("summary") or ""),
                }
            },
            "command_plan": plan,
        }
    )
