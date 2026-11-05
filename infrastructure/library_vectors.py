"""Serialized access to one persistent local Qdrant client per application/profile."""

import threading
from uuid import NAMESPACE_URL, uuid5


class VectorIndexError(RuntimeError):
    pass


class QdrantLibraryIndex:
    COLLECTION = "library_chunks"

    def __init__(self, path, dimensions):
        from qdrant_client import QdrantClient, models

        self.models = models
        self.lock = threading.RLock()
        self.client = None
        self.path = path
        self.created = False
        try:
            self.client = QdrantClient(path=str(path), force_disable_check_same_thread=True)
            if not self.client.collection_exists(self.COLLECTION):
                self.created = True
                self.client.create_collection(
                    self.COLLECTION,
                    vectors_config=models.VectorParams(
                        size=dimensions, distance=models.Distance.COSINE
                    ),
                )
            else:
                parameters = self.client.get_collection(self.COLLECTION).config.params.vectors
                if getattr(parameters, "size", None) != dimensions:
                    raise VectorIndexError(
                        "Vector index dimensions do not match this embedding profile"
                    )
        except Exception:
            if self.client is not None:
                self.client.close()
            raise VectorIndexError(
                "Local vector index could not be opened; use one backend per profile"
            ) from None

    def upsert(self, job_id, chunks, vectors):
        if len(chunks) != len(vectors):
            raise VectorIndexError("Vector count does not match the document chunks")
        models = self.models
        points = [
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"library-vector:{job_id}:{chunk['id']}")),
                vector=vector,
                payload={
                    "job_id": job_id,
                    "document_id": chunk["document_id"],
                    "version_id": chunk["version_id"],
                    "chunk_id": chunk["id"],
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        try:
            with self.lock:
                for first in range(0, len(points), 128):
                    self.client.upsert(
                        self.COLLECTION, points=points[first : first + 128], wait=True
                    )
        except Exception:
            raise VectorIndexError("Document vectors could not be saved") from None

    def query(self, vector, job_ids, *, limit):
        if not job_ids:
            return []
        try:
            with self.lock:
                result = self.client.query_points(
                    self.COLLECTION,
                    query=vector,
                    query_filter=self.models.Filter(
                        must=[
                            self.models.FieldCondition(
                                key="job_id", match=self.models.MatchAny(any=job_ids)
                            )
                        ]
                    ),
                    limit=limit,
                    with_payload=True,
                )
            return [{"payload": point.payload, "score": point.score} for point in result.points]
        except Exception:
            raise VectorIndexError("Semantic vector lookup failed") from None

    def delete_job(self, job_id):
        try:
            with self.lock:
                self.client.delete(
                    self.COLLECTION,
                    points_selector=self.models.FilterSelector(
                        filter=self.models.Filter(
                            must=[
                                self.models.FieldCondition(
                                    key="job_id", match=self.models.MatchValue(value=job_id)
                                )
                            ]
                        )
                    ),
                    wait=True,
                )
        except Exception:
            raise VectorIndexError("Retired document vectors could not be removed") from None

    def count_job(self, job_id):
        try:
            with self.lock:
                return self.client.count(
                    self.COLLECTION,
                    count_filter=self.models.Filter(
                        must=[
                            self.models.FieldCondition(
                                key="job_id", match=self.models.MatchValue(value=job_id)
                            )
                        ]
                    ),
                    exact=True,
                ).count
        except Exception:
            raise VectorIndexError("Stored document vectors could not be verified") from None

    def close(self):
        with self.lock:
            if self.client is not None:
                self.client.close()
                self.client = None
