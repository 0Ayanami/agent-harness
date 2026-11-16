"""Native-text hierarchy, legacy migration and current-version tree invariants."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.infrastructure.document_parser import DocumentParser
from agent_harness.interfaces.web.app import create_app
from agent_harness.services.ingestion import IngestionService
from agent_harness.services.library import LibraryError, LibraryService
from test_library import pdf_bytes
from test_services import FakeClient, wait_until


def flatten(sections):
    for section in sections:
        yield section
        yield from flatten(section["children"])


def test_markdown_sections_form_real_tree_and_ignore_fenced_code(tmp_path):
    path = tmp_path / "outline.md"
    body = (
        "Document preamble.\n# First\nFirst body.\n## Child\nChild body.\n"
        "```python\n# This is code\n## Still code\n```\n"
        "### Grandchild\nLeaf body.\n# Second\nSecond body.\n"
        "~~~text\n# Also code\n~~~\n"
    )
    path.write_text(body, encoding="utf-8")
    parser = DocumentParser()
    document = parser.parse_document(path, "md")
    sections = document["sections"]
    assert [section["title"] for section in sections] == [None, "First", "Child", "Grandchild", "Second"]
    by_title = {section["title"]: section for section in sections}
    assert by_title["Child"]["parent_key"] == by_title["First"]["key"]
    assert by_title["Grandchild"]["parent_key"] == by_title["Child"]["key"]
    assert by_title["Second"]["parent_key"] == "root"
    assert by_title["Grandchild"]["location"]["heading_path"] == ["First", "Child", "Grandchild"]
    assert "## Still code" in by_title["Child"]["text"]
    assert [section["ordinal"] for section in sections] == list(range(5))
    keys = {section["key"] for section in sections}
    assert all(chunk["section_key"] in keys for chunk in document["chunks"])
    assert [chunk["text"] for chunk in document["chunks"] if chunk["content_type"] == "code"] == [
        "```python\n# This is code\n## Still code\n```", "~~~text\n# Also code\n~~~",
    ]
    compatible = parser.parse(path, "md")
    assert isinstance(compatible, list) and compatible == document["chunks"]
    assert compatible.sections == sections


def test_markdown_chunks_keep_structure_and_exact_source_lines(tmp_path):
    path = tmp_path / "structures.md"
    body = (
        "# Reference\n" + "Context line.\n" * 120
        + "\nName | Value\n:--- | ---:\nSaturn \\| rings | 95\n"
        + "\n~~~~python\n    print('literal')\n~~~\n# still code\n~~~~\n"
        + "\n## Next\noutside prose\n"
    )
    path.write_text(body, encoding="utf-8")
    parsed = DocumentParser().parse_document(path, "md")
    original_lines = body.splitlines()
    table, code = [chunk for chunk in parsed["chunks"] if chunk["content_type"] != "text"]
    assert table["text"] == "Name | Value\n:--- | ---:\nSaturn \\| rings | 95"
    assert code["text"] == "~~~~python\n    print('literal')\n~~~\n# still code\n~~~~"
    for chunk in (table, code):
        location = chunk["location"]
        assert chunk["text"] == "\n".join(original_lines[location["start_line"] - 1:location["end_line"]])
        assert location["heading_path"] == ["Reference"]
    assert parsed["chunks"][-1]["location"]["heading_path"] == ["Reference", "Next"]


def test_oversize_markdown_blocks_prefer_whole_lines(tmp_path):
    path = tmp_path / "large.md"
    rows = [f"Key{i} | " + "value " * 14 for i in range(33)]
    code = ["    " + "x" * 74 for _ in range(22)] + ["y" * 1300]
    path.write_text("Name | Value\n--- | ---\n" + "\n".join(rows)
                    + "\n\n```text\n" + "\n".join(code) + "\n```\n", encoding="utf-8")
    parts = DocumentParser().parse(path, "md")
    assert all(len(part["text"]) <= 1200 for part in parts)
    tables = [part for part in parts if part["content_type"] == "table"]
    codes = [part for part in parts if part["content_type"] == "code"]
    assert len(tables) > 1 and len(codes) > 1
    assert all(sum(row in part["text"].splitlines() for part in tables) == 1 for row in rows)
    assert sum(part["text"].count("y") for part in codes) == 1300
    assert len({part["location"]["block_start_line"] for part in codes}) == 1


def test_plain_text_root_and_pdf_page_sections_keep_source_locations(tmp_path):
    text = tmp_path / "text.txt"
    text.write_text("Native paragraph.\n" * 200, encoding="utf-8")
    parser = DocumentParser()
    document = parser.parse_document(text, "txt")
    assert len(document["sections"]) == 1
    assert document["sections"][0]["kind"] == "document"
    assert len(document["chunks"]) > 1
    assert max(len(chunk["text"]) for chunk in document["chunks"]) <= 1200
    assert all(chunk["section_key"] == "root" for chunk in document["chunks"])
    pdf = tmp_path / "native.pdf"
    pdf.write_bytes(pdf_bytes("ORBITAL native PDF text"))
    parsed_pdf = parser.parse_document(pdf, "pdf")
    root, page = parsed_pdf["sections"]
    assert root["kind"] == "document" and page["kind"] == "page"
    assert page["parent_key"] == root["key"] and page["location"]["page"] == 1
    assert parsed_pdf["chunks"][0]["section_key"] == page["key"]
    assert parsed_pdf["chunks"][0]["metadata"]["extraction_method"] == "pypdf_native_text"


def test_persistent_tree_ids_survive_reopen_and_old_sections_retire_on_replace(tmp_path):
    root = tmp_path / "library"
    library = LibraryService(root)
    ingestion = IngestionService(library, cache_dir=tmp_path / "cache")
    ingestion.start()
    try:
        document = library.upload("notes.md", io.BytesIO(b"# Parent\nbody\n## Child\nleaf"))
        wait_until(lambda: ingestion.status(document["id"])["status"] == "ready")
        first = ingestion.structure(document["id"])
        nodes = list(flatten(first["sections"]))
        assert first["section_count"] == 3 and first["chunk_count"] == 2
        assert {node["title"] for node in nodes} == {None, "Parent", "Child"}
        assert all(chunk["section_id"] == node["id"] for node in nodes for chunk in node["chunks"])
        section_ids = {node["id"] for node in nodes}
    finally:
        ingestion.close()
        library.close()
    library = LibraryService(root)
    ingestion = IngestionService(library, cache_dir=tmp_path / "cache")
    ingestion.start()
    try:
        reopened = ingestion.structure(document["id"])
        assert {node["id"] for node in flatten(reopened["sections"])} == section_ids
        updated = library.replace(document["id"], "notes.md", io.BytesIO(b"# New\nupdated text"))
        wait_until(lambda: ingestion.status(document["id"])["status"] == "ready")
        current = ingestion.structure(document["id"])
        assert current["version_id"] == updated["version_id"] != first["version_id"]
        assert not section_ids.intersection(node["id"] for node in flatten(current["sections"]))
        library.delete(document["id"])
        with pytest.raises(LibraryError) as missing:
            ingestion.structure(document["id"])
        assert missing.value.status_code == 404
        with library.repository.connection() as database:
            assert database.execute("SELECT COUNT(*) FROM library_sections").fetchone()[0] == 0
    finally:
        ingestion.close()
        library.close()


def test_legacy_flat_chunks_are_preserved_then_automatically_reparsed(tmp_path):
    library = LibraryService(tmp_path / "library")
    body = b"# Parent\nLegacy body.\n## Child\nLegacy leaf."
    document = library.upload("legacy.md", io.BytesIO(body))
    # Build precisely the pre-hierarchy schema instead of testing only an empty DB.
    with library.repository.connection() as database:
        database.executescript("""
            CREATE TABLE library_chunks (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,text TEXT NOT NULL,search_text TEXT NOT NULL,location TEXT NOT NULL);
            CREATE TABLE library_ingestion_jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL,version_id TEXT NOT NULL,status TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        """)
        database.execute("INSERT INTO library_chunks VALUES (?,?,?,?,?,?,?)", (
            "legacy-chunk", document["id"], document["version_id"], 0,
            body.decode(), body.decode(), json.dumps({"start_line": 1}),
        ))
        database.execute(
            "INSERT INTO library_ingestion_jobs(id,document_id,version_id,status,chunk_count,created_at,updated_at) "
            "VALUES ('legacy-job',?,?,'ready',1,'old','old')", (document["id"], document["version_id"]),
        )
    ingestion = IngestionService(library, cache_dir=tmp_path / "cache")
    try:
        before = ingestion.structure(document["id"])
        assert before["chunk_count"] == 1 and before["section_count"] == 1
        assert before["sections"][0]["metadata"]["needs_reparse"]
        assert before["sections"][0]["chunks"][0]["chunk_id"] == "legacy-chunk"
        ingestion.start()
        wait_until(lambda: ingestion.status(document["id"])["status"] == "ready")
        after = ingestion.structure(document["id"])
        assert after["section_count"] == 3 and after["chunk_count"] == 2
        assert after["version_id"] == document["version_id"]
        assert library.get(document["id"])["sha256"] == document["sha256"]
        assert not any(node["metadata"].get("needs_reparse") for node in flatten(after["sections"]))
    finally:
        ingestion.close()
        library.close()


def test_superseded_ingestion_cannot_publish_sections(tmp_path):
    library = LibraryService(tmp_path / "library")
    ingestion = IngestionService(library, cache_dir=tmp_path / "cache")
    try:
        document = library.upload("old.md", io.BytesIO(b"# Old\nretired body"))
        job = ingestion.index.claim()
        assert job
        path = tmp_path / "parsed.md"
        path.write_text("# Old\nretired body", encoding="utf-8")
        parsed = ingestion.parser.parse(path, "md")
        library.replace(document["id"], "new.md", io.BytesIO(b"# New\nactive body"))
        assert not ingestion.index.publish(job, parsed)
        current = ingestion.structure(document["id"])
        assert current["sections"] == [] and current["chunk_count"] == 0
    finally:
        ingestion.close()
        library.close()


def test_structure_http_exposes_current_layers_without_embedding_and_delete_is_404(tmp_path):
    app = create_app(HarnessConfig(tmp_path, "offline-hierarchy-test"), FakeClient)
    headers = {"X-Agent-Client": "web"}
    with TestClient(app) as browser:
        response = browser.post(
            "/api/agent/library/files", headers=headers,
            files={"file": ("outline.md", b"# Parent\nbody\n\nName | Value\n--- | ---\nSaturn | 95\n\n## Child\nleaf\n```py\n    run_probe()\n```\n")},
        )
        assert response.status_code == 201
        document = response.json()
        wait_until(lambda: app.state.ingestion.status(document["id"])["status"] == "ready")
        response = browser.get(f"/api/agent/library/files/{document['id']}/structure")
        assert response.status_code == 200
        result = response.json()
        assert result["document_id"] == document["id"] and result["version_id"] == document["version_id"]
        assert result["section_count"] == 3 and result["chunk_count"] == 4
        assert result["vector_count"] == 0
        chunks = [chunk for section in flatten(result["sections"]) for chunk in section["chunks"]]
        assert [chunk["content_type"] for chunk in chunks] == ["text", "table", "text", "code"]
        assert all(chunk["section_id"] and chunk["vectors"] == [] for chunk in chunks)
        for query, kind, excerpt in (("Saturn", "table", "Saturn | 95"), ("run_probe", "code", "    run_probe()")):
            hits = browser.post("/api/agent/library/search", headers=headers,
                                json={"query": query, "mode": "keyword"}).json()["hits"]
            assert hits[0]["content_type"] == kind
            assert excerpt in hits[0]["text"]
        assert browser.get(f"/api/library/files/{document['id']}/structure").json() == result
        assert browser.delete(f"/api/library/files/{document['id']}", headers=headers).status_code == 204
        assert browser.get(f"/api/agent/library/files/{document['id']}/structure").status_code == 404
