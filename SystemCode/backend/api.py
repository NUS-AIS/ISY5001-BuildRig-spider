import asyncio
import json
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from backend.models import SessionCreate, MessageCreate, RunCreate, MemoryPatch, HybridRequest, ValidationRequest
from backend.requirements_parser import parse_requirements
from backend.validation import validate_option


def create_router(store, corpus, engine):
    router = APIRouter(prefix="/api/v1", tags=["recommendation"])

    def owned_session(session_id: str, owner_id: str):
        item = store.session(session_id)
        if not item or item.get("owner_id") != owner_id:
            raise HTTPException(404, "Session not found")
        return item

    def require_internal(token: str | None):
        configured = engine.settings.internal_api_token
        if configured and token != configured:
            raise HTTPException(403, "Invalid internal service token")

    def owned_run(run_id: str, owner_id: str):
        if store.run_owner(run_id) != owner_id:
            raise HTTPException(404, "Run not found")
        return store.run(run_id)

    def owned_memory(memory_id: str, owner_id: str):
        memory = store.memory(memory_id)
        if not memory:
            raise HTTPException(404, "Memory not found")
        owned_session(memory["session_id"], owner_id)
        return memory

    @router.post("/sessions", status_code=201)
    def create_session(body: SessionCreate, x_user_id: str = Header("dev-user")):
        return store.create_session(body.model_dump(), x_user_id)

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str, x_user_id: str = Header("dev-user")):
        item = owned_session(session_id, x_user_id)
        item["memories"] = store.list_memories(session_id)
        return item

    @router.post("/sessions/{session_id}/messages")
    def add_message(session_id: str, body: MessageCreate, x_user_id: str = Header("dev-user")):
        session = owned_session(session_id, x_user_id)
        if session["requirements_version"] != body.expected_requirements_version:
            raise HTTPException(409, detail={"code": "REQUIREMENTS_VERSION_CONFLICT", "current_version": session["requirements_version"]})
        mid = store.add_message(session_id, body.client_message_id, body.text)
        requirements, questions = parse_requirements(body.text, session["requirements"], engine.model, corpus)
        status = "needs_clarification" if questions else "ready"
        try:
            version = store.update_requirements(session_id, body.expected_requirements_version, requirements, status)
        except ValueError as exc:
            raise HTTPException(409, detail={"code": "REQUIREMENTS_VERSION_CONFLICT", "current_version": exc.args[0]})
        assistant_message, generation_source = engine.intake_reply(body.text, requirements, questions)
        reply_options = questions[0].get("options", []) if questions else []
        return {"session_id": session_id, "message_id": mid, "requirements_version": version,
                "status": status, "requirements": requirements, "questions": questions,
                "can_generate": not questions, "assistant_message": assistant_message,
                "generation_source": generation_source, "reply_options": reply_options}

    @router.post("/sessions/{session_id}/runs", status_code=202)
    def create_run(session_id: str, body: RunCreate, background: BackgroundTasks,
                   idempotency_key: str | None = Header(None), x_user_id: str = Header("dev-user")):
        session = owned_session(session_id, x_user_id)
        req = store.requirements(session_id, body.requirements_version)
        if not req:
            raise HTTPException(409, detail={"code": "REQUIREMENTS_VERSION_NOT_FOUND"})
        _, questions = parse_requirements("", req)
        if questions:
            raise HTTPException(409, detail={"code": "REQUIREMENTS_INCOMPLETE", "questions": questions})
        run = store.create_run(session_id, body.requirements_version, corpus.snapshot_id, body.orchestration_mode, idempotency_key)
        if run["status"] == "queued":
            background.add_task(engine.execute, run["id"], body.maximum_options)
        return {**run, "status_url": f"/api/v1/runs/{run['id']}", "events_url": f"/api/v1/runs/{run['id']}/events",
                "result_url": f"/api/v1/runs/{run['id']}/result", "idempotency_key": idempotency_key}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, x_user_id: str = Header("dev-user")):
        run = owned_run(run_id, x_user_id)
        return run

    @router.get("/runs/{run_id}/result")
    def get_result(run_id: str, x_user_id: str = Header("dev-user")):
        run = owned_run(run_id, x_user_id)
        if run["status"] not in ("completed", "failed", "cancelled"):
            raise HTTPException(409, detail={"code": "RESULT_NOT_READY", "status": run["status"]})
        return run["result"] if run["status"] == "completed" else {"status": run["status"], "error": run["error"]}

    @router.get("/runs/{run_id}/execution")
    def inspect_execution(run_id: str, x_user_id: str = Header("dev-user")):
        run = owned_run(run_id, x_user_id)
        return {"run": run, "operation_state": store.operation_state(run_id),
                "tool_calls": store.tool_calls(run_id), "usage_ledger": store.usage_rows(run_id)}

    @router.get("/runs/{run_id}/events")
    async def run_events(run_id: str, after: int = Query(0, ge=0), x_user_id: str = Header("dev-user")):
        owned_run(run_id, x_user_id)
        async def stream():
            cursor = after
            while True:
                events = store.events(run_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                current = store.run(run_id)
                if current["status"] in ("completed", "failed", "cancelled") and not events:
                    break
                await asyncio.sleep(.25)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @router.post("/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str, x_user_id: str = Header("dev-user")):
        run = owned_run(run_id, x_user_id)
        if run["status"] in ("completed", "failed", "cancelled"):
            return run
        store.set_run(run_id, "cancelled", "cancelled")
        store.event(run_id, "cancelled", {"stage": "cancelled"})
        return store.run(run_id)

    @router.get("/sessions/{session_id}/memories")
    def memories(session_id: str, x_user_id: str = Header("dev-user")):
        owned_session(session_id, x_user_id)
        return {"items": store.list_memories(session_id)}

    @router.post("/sessions/{session_id}/memories", status_code=201)
    def add_memory(session_id: str, kind: str, value: str, confirmed: bool = False,
                   x_user_id: str = Header("dev-user")):
        session = owned_session(session_id, x_user_id)
        if not session["long_term_memory"]:
            raise HTTPException(409, detail={"code": "LONG_TERM_MEMORY_DISABLED"})
        return store.add_memory(session_id, kind, value, confirmed, None)

    @router.patch("/memories/{memory_id}")
    def patch_memory(memory_id: str, body: MemoryPatch, x_user_id: str = Header("dev-user")):
        owned_memory(memory_id, x_user_id)
        try:
            return store.patch_memory(memory_id, body.expected_version, body.value, body.confirmed)
        except KeyError:
            raise HTTPException(404, "Memory not found")
        except ValueError as exc:
            raise HTTPException(409, detail={"code": "MEMORY_VERSION_CONFLICT", "current_version": exc.args[0]})

    @router.delete("/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str, x_user_id: str = Header("dev-user")):
        owned_memory(memory_id, x_user_id)
        if not store.delete_memory(memory_id):
            raise HTTPException(404, "Memory not found")

    @router.post("/internal/retrieval/hybrid")
    def hybrid(body: HybridRequest, x_internal_token: str | None = Header(None)):
        require_internal(x_internal_token)
        return corpus.search(body.query, body.categories, body.product_ids, body.top_k)

    @router.get("/internal/candidates/{category}")
    def internal_candidates(category: str, maximum_minor: int | None = None,
                            limit: int = Query(25, ge=1, le=100),
                            x_internal_token: str | None = Header(None)):
        require_internal(x_internal_token)
        return {"snapshot_id": corpus.snapshot_id,
                "items": corpus.candidates(category, maximum_minor, limit)}

    @router.post("/internal/options/validate")
    def validate(body: ValidationRequest, x_internal_token: str | None = Header(None)):
        require_internal(x_internal_token)
        return validate_option(body.option, body.requirements)

    @router.get("/health")
    def health():
        production = engine.settings.retrieval_backend == "neo4j_milvus"
        store_status = corpus.status() if hasattr(corpus, "status") else None
        published = bool(store_status) and store_status.get("manifest_status") == "published"
        detail = None
        if not production:
            detail = "Local contract fallback is active; Neo4j and Milvus are not serving requests."
        elif not published:
            detail = "The pinned snapshot is not published in Neo4j/Milvus; run python -m backend.index_data."
        return {"status": "ok" if production and published else "degraded",
                "snapshot_id": corpus.snapshot_id,
                "retrieval_backend": engine.settings.retrieval_backend,
                "knowledge_base": store_status,
                "recent_degradations": getattr(corpus, "events", [])[-5:],
                "detail": detail,
                "models": {
                    "chat_provider": engine.settings.model_provider,
                    "chat_model": engine.settings.model_name,
                    "embedding_provider": engine.settings.embedding_provider,
                    "embedding_model": engine.settings.embedding_model,
                    "embedding_dimensions": engine.settings.embedding_dimensions,
                },
                "pi_runtime_configured": bool(engine.settings.pi_runtime_url)}

    return router
