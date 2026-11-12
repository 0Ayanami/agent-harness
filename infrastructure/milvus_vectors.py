"""Milvus dense-vector storage; all authoritative text and scope stay in SQLite."""

import json
import math
import threading
from uuid import NAMESPACE_URL, uuid5

from .library_vectors import VectorIndexError


def vector_identifier(job_id, chunk_id):
    return str(uuid5(NAMESPACE_URL, f"library-vector:{job_id}:{chunk_id}"))


class MilvusLibraryIndex:
    def __init__(self, uri, dimensions, collection, *, token=None, client=None):
        self.client = None
        self.lock = threading.RLock()
        self.COLLECTION = collection
        self.dimensions = dimensions
        self.created = False
        try:
            from pymilvus import DataType, MilvusClient

            self.client = client or MilvusClient(uri=uri, token=token or "", timeout=10)
            self.created = not self.client.has_collection(collection_name=collection)
            if self.created:
                schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
                schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
                for field in ("job_id", "document_id", "version_id", "section_id", "chunk_id"):
                    schema.add_field(field_name=field, datatype=DataType.VARCHAR, max_length=128)
                schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=dimensions)
                indexes = MilvusClient.prepare_index_params()
                indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
                self.client.create_collection(
                    collection_name=collection, schema=schema, index_params=indexes,
                    consistency_level="Strong",
                )
            else:
                fields = self.client.describe_collection(collection_name=collection)["fields"]
                vector = next(field for field in fields if field["name"] == "vector")
                if int(vector["params"]["dim"]) != dimensions:
                    raise ValueError("Mismatched vector dimensions")
                names = {field["name"] for field in fields}
                if not {"id", "job_id", "document_id", "version_id", "section_id", "chunk_id"} <= names:
                    raise ValueError("Mismatched vector schema")
                self.client.load_collection(collection_name=collection)
        except ImportError:
            raise VectorIndexError("Install the milvus optional dependency to use Milvus") from None
        except Exception:
            if self.client is not None and client is None:
                self.client.close()
            raise VectorIndexError("Milvus could not be opened; check server and collection configuration") from None

    def upsert(self, job_id, chunks, vectors):
        if len(chunks) != len(vectors):
            raise VectorIndexError("Vector count does not match the document chunks")
        rows = []
        for chunk, vector in zip(chunks, vectors):
            if len(vector) != self.dimensions or any(
                type(value) not in (int, float) or not math.isfinite(value) for value in vector
            ):
                raise VectorIndexError("Invalid vector dimensions or values")
            rows.append({
                "id": vector_identifier(job_id, chunk["id"]), "job_id": job_id,
                "document_id": chunk["document_id"], "version_id": chunk["version_id"],
                "section_id": chunk.get("section_id") or "", "chunk_id": chunk["id"],
                "vector": vector,
            })
        try:
            with self.lock:
                for first in range(0, len(rows), 128):
                    self.client.upsert(collection_name=self.COLLECTION, data=rows[first:first + 128])
        except Exception:
            raise VectorIndexError("Document vectors could not be saved to Milvus") from None

    def query(self, vector, job_ids, *, limit):
        if not job_ids:
            return []
        try:
            with self.lock:
                result = self.client.search(
                    collection_name=self.COLLECTION, data=[vector], anns_field="vector",
                    filter="job_id in " + json.dumps(list(job_ids)), limit=limit,
                    search_params={"metric_type": "COSINE"}, consistency_level="Strong",
                    output_fields=["job_id", "document_id", "version_id", "section_id", "chunk_id"],
                )
            return [{"payload": point["entity"], "score": point["distance"]} for point in result[0]]
        except Exception:
            raise VectorIndexError("Milvus vector lookup failed") from None

    def delete_job(self, job_id):
        try:
            with self.lock:
                self.client.delete(collection_name=self.COLLECTION, filter="job_id == " + json.dumps(job_id))
        except Exception:
            raise VectorIndexError("Retired document vectors could not be removed from Milvus") from None

    def count_job(self, job_id):
        try:
            with self.lock:
                rows = self.client.query(
                    collection_name=self.COLLECTION, filter="job_id == " + json.dumps(job_id),
                    output_fields=["count(*)"], consistency_level="Strong",
                )
            return int(rows[0]["count(*)"])
        except Exception:
            raise VectorIndexError("Milvus vectors could not be verified") from None

    def close(self):
        with self.lock:
            if self.client is not None:
                self.client.close()
                self.client = None
