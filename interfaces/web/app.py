"""Thin HTTP adapter. All execution is delegated to application services."""

import json
import time
from contextlib import ExitStack, asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ...application.sessions import SessionConflict, SessionService
from ...services.ingestion import IngestionService
from ...services.library import LibraryService
from ...services.retrieval import RetrievalService
from .library import library_router
from .skills import skills_router


class LibraryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    document_ids: list[str] | None = Field(default=None, max_length=100)
    favorites_only: StrictBool = False
    mode: Literal["hybrid", "keyword", "vector"] = "hybrid"


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=30000)
    library: LibraryOptions | None = None


class ApprovalRequest(BaseModel):
    allow: bool


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    personalized: StrictBool = True
    temporary: StrictBool = False


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: StrictBool | None = None
    personalized: StrictBool | None = None
    temporary: StrictBool | None = None


class ScheduledTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron_expr: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=30000)
    recurring: StrictBool = True
    persistent: StrictBool = True


def require_local_client(request: Request, x_agent_client: str = Header(default="")):
    if x_agent_client != "web":
        raise HTTPException(403, "X-Agent-Client header is required")
    origin = request.headers.get("origin")
    allowed = {str(request.base_url).rstrip("/"), *request.app.state.frontend_origins}
    if origin and origin not in allowed:
        raise HTTPException(403, "Cross-origin mutations are not allowed")


def create_app(config, client_factory, *, frontend_origins=(), frontend_directory=None):
    library = LibraryService(config.data_dir / "library")
    ingestion = IngestionService(
        library, cache_dir=config.workdir / ".artifacts" / "cache" / "jieba"
    )
    retrieval = RetrievalService(config, library, ingestion)
    application = SessionService(
        config, client_factory, retrieval=retrieval, library=library, resume_schedules=True
    )

    @asynccontextmanager
    async def lifespan(app):
        services = (library, ingestion, retrieval, application)
        with ExitStack() as shutdown:
            for service in services:
                shutdown.callback(service.close)
            for service in services:
                service.start()
            yield

    app = FastAPI(title="Agent Harness", lifespan=lifespan)
    app.state.application = application
    app.state.library = library
    app.state.ingestion = ingestion
    app.state.retrieval = retrieval
    app.state.frontend_origins = frozenset(frontend_origins)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    def session_operation(operation, session_id, *args, **kwargs):
        try:
            return operation(session_id, *args, **kwargs)
        except KeyError:
            raise HTTPException(404, "Session not found") from None
        except SessionConflict as error:
            raise HTTPException(409, str(error)) from error

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
            "redis": application.repository.cache_status(),
        }

    @router.get("/sessions")
    def list_sessions(archived: bool = False):
        return application.repository.list(archived=archived)

    @router.post("/sessions", status_code=201, dependencies=[Depends(require_local_client)])
    def create_session(body: SessionCreate | None = None):
        return application.create(**(body.model_dump() if body else {}))

    @router.get("/sessions/{session_id}")
    def session_snapshot(session_id: str):
        return session_operation(application.snapshot, session_id)

    @router.patch("/sessions/{session_id}", dependencies=[Depends(require_local_client)])
    def update_session(session_id: str, body: SessionPatch):
        changes = body.model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise HTTPException(
                422, "Provide at least one conversation setting with a boolean value"
            )
        return session_operation(application.update, session_id, **changes)

    @router.delete(
        "/sessions/{session_id}", status_code=204, dependencies=[Depends(require_local_client)]
    )
    def delete_session(session_id: str):
        session_operation(application.delete, session_id)
        return Response(status_code=204)

    @router.get("/scheduled-tasks")
    def list_scheduled_tasks():
        return application.scheduled_tasks()

    @router.get("/sessions/{session_id}/scheduled-tasks")
    def list_session_scheduled_tasks(session_id: str):
        return session_operation(application.scheduled_tasks, session_id)

    @router.post(
        "/sessions/{session_id}/scheduled-tasks",
        status_code=201,
        dependencies=[Depends(require_local_client)],
    )
    def create_scheduled_task(session_id: str, body: ScheduledTaskCreate):
        try:
            return session_operation(
                application.create_scheduled_task, session_id, **body.model_dump()
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except OSError:
            raise HTTPException(503, "Could not save scheduled task") from None

    @router.delete(
        "/sessions/{session_id}/scheduled-tasks/{job_id}",
        status_code=204,
        dependencies=[Depends(require_local_client)],
    )
    def delete_scheduled_task(session_id: str, job_id: str):
        try:
            deleted = session_operation(application.delete_scheduled_task, session_id, job_id)
        except OSError:
            raise HTTPException(503, "Could not remove scheduled task") from None
        if not deleted:
            raise HTTPException(404, "Scheduled task not found")
        return Response(status_code=204)

    @router.post(
        "/sessions/{session_id}/messages",
        status_code=202,
        dependencies=[Depends(require_local_client)],
    )
    def send_message(session_id: str, body: MessageRequest):
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "Message must not be empty")
        try:
            session_operation(
                application.submit,
                session_id,
                message,
                library=body.library.model_dump() if body.library is not None else None,
            )
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"status": "accepted"}

    @router.post(
        "/sessions/{session_id}/cancel",
        dependencies=[Depends(require_local_client)],
    )
    def cancel_run(session_id: str):
        session_operation(application.cancel, session_id)
        return {"status": "cancelling"}

    @router.post(
        "/sessions/{session_id}/approvals/{approval_id}",
        dependencies=[Depends(require_local_client)],
    )
    def resolve_approval(session_id: str, approval_id: str, body: ApprovalRequest):
        session = session_operation(application.get, session_id)
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
        cursor = max(after, last_event_id)
        notification_version = application.repository.notification_version(session_id)
        page = await run_in_threadpool(
            application.repository.event_page, session_id, cursor, snapshot=once
        )
        if not page["exists"]:
            raise HTTPException(404, "Session not found")

        async def stream():
            current_page, current_cursor = page, cursor
            observed_version = notification_version
            upper_bound = page["through"]
            deadline = time.monotonic() + 15
            while not application.shutdown.is_set():
                batch = current_page["events"]
                for event in batch:
                    current_cursor = event["id"]
                    yield f"id: {current_cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if once:
                    if not batch or current_cursor >= upper_bound:
                        return
                    current_page = await run_in_threadpool(
                        application.repository.event_page,
                        session_id, current_cursor, through=upper_bound,
                    )
                    continue
                if (
                    time.monotonic() > deadline
                    or await request.is_disconnected()
                    or not current_page["exists"]
                ):
                    return
                yield ": keepalive\n\n"
                # Pub/Sub is only a wakeup hint. Sampling before the SQLite
                # read closes the notify/read/wait race; a bounded timeout
                # still replays durable rows after dropped notifications.
                if len(batch) < 200:
                    await application.repository.wait_for_events(
                        session_id, observed_version,
                        timeout=min(2.0, max(0.0, deadline - time.monotonic())),
                    )
                observed_version = application.repository.notification_version(session_id)
                # SQLite may wait on a writer or filesystem I/O. One worker call
                # reads the whole page without blocking unrelated ASGI requests.
                current_page = await run_in_threadpool(
                    application.repository.event_page,
                    session_id, current_cursor, through=upper_bound,
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    router.include_router(library_router(library, require_local_client, retrieval=retrieval))
    router.include_router(skills_router(config.workdir))
    app.include_router(router, prefix="/api")
    app.include_router(router, prefix="/api/agent", include_in_schema=False)
    if frontend_directory is not None:
        app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")
    return app
