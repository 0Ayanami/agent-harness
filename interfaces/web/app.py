"""Thin HTTP adapter. All execution is delegated to application services."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ...application.sessions import SessionService


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=30000)


class ApprovalRequest(BaseModel):
    allow: bool


def require_local_client(request: Request, x_agent_client: str = Header(default="")):
    if x_agent_client != "web":
        raise HTTPException(403, "X-Agent-Client header is required")
    origin = request.headers.get("origin")
    allowed = {str(request.base_url).rstrip("/"), *request.app.state.frontend_origins}
    if origin and origin not in allowed:
        raise HTTPException(403, "Cross-origin mutations are not allowed")


def create_app(config, client_factory, *, frontend_origins=(), frontend_directory=None):
    application = SessionService(config, client_factory)

    @asynccontextmanager
    async def lifespan(app):
        application.start()
        try:
            yield
        finally:
            application.close()

    app = FastAPI(title="Agent Harness", lifespan=lifespan)
    app.state.application = application
    app.state.frontend_origins = frozenset(frontend_origins)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    def find_session(session_id):
        try:
            return application.get(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found") from None

    if frontend_directory is None:

        @app.get("/")
        def index():
            return {
                "name": "Agent Harness API",
                "docs": "/docs",
                "frontend": next(iter(frontend_origins), None),
            }

    router = APIRouter()

    @router.get("/health")
    def health():
        return {
            "status": "ok",
            "model": config.model,
            "workdir": str(config.workdir),
            "interface": "web",
        }

    @router.get("/sessions")
    def list_sessions():
        return application.repository.list()

    @router.post("/sessions", status_code=201, dependencies=[Depends(require_local_client)])
    def create_session():
        return application.create()

    @router.get("/sessions/{session_id}")
    def session_snapshot(session_id: str):
        find_session(session_id)
        return application.snapshot(session_id)

    @router.post(
        "/sessions/{session_id}/messages",
        status_code=202,
        dependencies=[Depends(require_local_client)],
    )
    def send_message(session_id: str, body: MessageRequest):
        find_session(session_id)
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "Message must not be empty")
        try:
            application.submit(session_id, message)
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "accepted"}

    @router.post(
        "/sessions/{session_id}/cancel",
        dependencies=[Depends(require_local_client)],
    )
    def cancel_run(session_id: str):
        find_session(session_id)
        application.cancel(session_id)
        return {"status": "cancelling"}

    @router.post(
        "/sessions/{session_id}/approvals/{approval_id}",
        dependencies=[Depends(require_local_client)],
    )
    def resolve_approval(session_id: str, approval_id: str, body: ApprovalRequest):
        session = find_session(session_id)
        try:
            session.approvals.answer(approval_id, body.allow)
        except KeyError:
            raise HTTPException(404, "Approval not found or already resolved") from None
        return {"status": "resolved"}

    @router.get("/sessions/{session_id}/events")
    async def events(
        session_id: str,
        request: Request,
        after: int = 0,
        once: bool = False,
        last_event_id: int = Header(default=0, alias="Last-Event-ID"),
    ):
        find_session(session_id)

        async def stream():
            cursor = max(after, last_event_id)
            upper_bound = application.repository.cursor(session_id) if once else None
            deadline = time.monotonic() + 15
            while not application.shutdown.is_set():
                batch = application.repository.events(session_id, cursor, through=upper_bound)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if once:
                    if not batch or cursor >= upper_bound:
                        return
                    continue
                if time.monotonic() > deadline or await request.is_disconnected():
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.3)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    app.include_router(router, prefix="/api")
    app.include_router(router, prefix="/api/agent", include_in_schema=False)
    if frontend_directory is not None:
        app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")
    return app
