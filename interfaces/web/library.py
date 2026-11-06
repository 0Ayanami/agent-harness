"""HTTP routes for saved files and their searchable text and vector indexes."""

from typing import Literal

import anyio
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool

from ...services.library import LibraryError


class FilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    favorite: StrictBool | None = None


class LibrarySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] | None = Field(default=None, max_length=100)
    favorites_only: StrictBool = False
    limit: int = Field(default=10, ge=1, le=50)
    mode: Literal["keyword", "vector", "hybrid"] = "keyword"


class LibraryFileResponse(FileResponse):
    """Release a version lease after normal sends, invalid ranges and disconnects."""

    def __init__(self, *args, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.release)


def library_router(service, require_local_client, *, retrieval=None):
    router = APIRouter(prefix="/library", tags=["library"])

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LibraryError as error:
            raise HTTPException(error.status_code, str(error)) from error

    def ingestion_service():
        if service.ingestion is None:
            raise HTTPException(503, "Document indexing is not configured")
        return service.ingestion

    @router.post("/search", dependencies=[Depends(require_local_client)])
    def search_library(body: LibrarySearch):
        arguments = body.model_dump()
        mode = arguments.pop("mode")
        if retrieval is not None:
            return invoke(retrieval.search, **arguments, mode=mode)
        if mode != "keyword":
            raise HTTPException(503, "Semantic retrieval is not configured")
        return invoke(ingestion_service().search, **arguments)

    @router.get("/retrieval")
    def retrieval_status():
        if retrieval is None:
            return {"configured": False}
        return invoke(retrieval.status)

    @router.post(
        "/files/{file_id}/vectorize",
        status_code=202,
        dependencies=[Depends(require_local_client)],
    )
    def vectorize_file(file_id: str):
        if retrieval is None:
            raise HTTPException(503, "Semantic retrieval is not configured")
        return invoke(retrieval.enqueue, file_id, force=True)

    @router.get("/jobs/{job_id}")
    def ingestion_job(job_id: str):
        return invoke(ingestion_service().get_job, job_id)

    @router.post(
        "/files/{file_id}/reindex", status_code=202, dependencies=[Depends(require_local_client)]
    )
    def reindex_file(file_id: str):
        return invoke(ingestion_service().enqueue, file_id, force=True)

    @router.get("/files/{file_id}/chunks/{chunk_id}")
    def read_indexed_chunk(file_id: str, chunk_id: str):
        return invoke(ingestion_service().read_chunk, file_id, chunk_id)

    @router.get("/files")
    def list_files(
        q: str = Query(default="", max_length=255),
        favorite: bool | None = None,
        extension: str | None = None,
        sort: Literal["updated_desc", "name_asc", "size_desc"] = "updated_desc",
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return invoke(
            service.list,
            q=q.strip(),
            favorite=favorite,
            extension=extension,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    @router.post("/files", status_code=201, dependencies=[Depends(require_local_client)])
    def upload_file(file: UploadFile = File(...)):
        try:
            return invoke(service.upload, file.filename or "", file.file)
        finally:
            file.file.close()

    @router.get("/files/{file_id}")
    def file_detail(file_id: str):
        return invoke(service.get, file_id)

    @router.patch("/files/{file_id}", dependencies=[Depends(require_local_client)])
    def patch_file(file_id: str, body: FilePatch):
        if any(value is None for value in body.model_dump(exclude_unset=True).values()):
            raise HTTPException(422, "Name and favorite cannot be null")
        return invoke(service.patch, file_id, name=body.name, favorite=body.favorite)

    @router.put("/files/{file_id}/content", dependencies=[Depends(require_local_client)])
    def replace_file(file_id: str, file: UploadFile = File(...)):
        try:
            return invoke(service.replace, file_id, file.filename or "", file.file)
        finally:
            file.file.close()

    @router.delete(
        "/files/{file_id}", status_code=204, dependencies=[Depends(require_local_client)]
    )
    def delete_file(file_id: str):
        invoke(service.delete, file_id)
        return Response(status_code=204)

    @router.get("/files/{file_id}/preview")
    def preview_file(file_id: str):
        return invoke(service.preview, file_id)

    @router.get("/files/{file_id}/content")
    def file_content(
        file_id: str,
        download: bool = True,
        version_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
    ):
        record, path, key = invoke(service.acquire_content, file_id, version_id=version_id)
        try:
            return LibraryFileResponse(
                path,
                media_type=record["mime_type"],
                filename=record["name"],
                content_disposition_type="attachment" if download else "inline",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                release=lambda: service.release_content(key),
            )
        except BaseException:
            service.release_content(key)
            raise

    return router
