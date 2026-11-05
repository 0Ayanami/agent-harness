"""Persistent text ingestion and real keyword retrieval; no vector or model substitutes."""

import io
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.services.library import LibraryService
from test_library import FILES, HEADERS, pdf_bytes, upload
from test_services import FakeClient

SEARCH = "/api/library/search"


def wait_index(browser, document_id, expected="ready", *, version_id=None, timeout=15):
    """Wait on the persisted public state, with a useful failure description."""
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = browser.get(f"{FILES}/{document_id}")
        assert response.status_code == 200, response.text
        latest = response.json().get("ingestion")
        if latest and latest["status"] == expected:
            if version_id is None or latest["version_id"] == version_id:
                return latest
        time.sleep(0.03)
    raise AssertionError(f"Expected ingestion {expected!r}; final public state was {latest!r}")


def search(browser, query, **filters):
    response = browser.post(SEARCH, headers=HEADERS, json={"query": query, **filters})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "keyword"
    assert result["warning"] is None
    assert isinstance(result["hits"], list)
    return result["hits"]


@pytest.fixture
def indexed_library(tmp_path):
    config = HarnessConfig(tmp_path, "ingestion-test-model")
    model = FakeClient()
    app = create_app(config, lambda: model)
    with TestClient(app) as browser:
        yield browser, app, config
    assert model.calls == [], "Document ingestion and keyword search must not invoke a model"


def test_markdown_is_automatically_indexed_with_real_source_location(indexed_library):
    browser, _, _ = indexed_library
    body = (
        "# 运维手册\n\n"
        "## 冷启动恢复\n\n"
        "蓝鲸协议要求先恢复检查点，再按 NEBULA_731 顺序重新启动服务。\n\n"
        "完成后核对健康检查，不要跳过来源记录。\n"
    )
    document = upload(browser, "说明.md", body.encode())
    status = wait_index(browser, document["id"], version_id=document["version_id"])
    assert document["status"] == "stored"
    assert status["job_id"] and status["chunk_count"] >= 1
    assert status["error"] is None
    job = browser.get(f"/api/library/jobs/{status['job_id']}")
    assert job.status_code == 200, job.text
    assert job.json()["document_id"] == document["id"]
    assert job.json()["version_id"] == document["version_id"]
    assert job.json()["status"] == "ready"

    hit = search(browser, "冷启动")[0]
    assert hit["document_id"] == document["id"]
    assert hit["version_id"] == document["version_id"]
    assert hit["name"] == "说明.md"
    assert "NEBULA_731" in hit["text"]
    assert any(title in hit["location"]["heading"] for title in ("运维手册", "冷启动恢复"))
    assert hit["location"]["start_line"] <= 5 <= hit["location"]["end_line"]
    assert math.isfinite(hit["score"])
    for query in ("蓝鲸", "恢复", "鲸", "NEBULA_731"):
        assert document["id"] in {item["document_id"] for item in search(browser, query)}

    original = browser.get(f"{FILES}/{document['id']}/chunks/{hit['chunk_id']}")
    assert original.status_code == 200, original.text
    for field in ("chunk_id", "document_id", "version_id", "name", "text", "location"):
        assert original.json()[field] == hit[field]


def test_pdf_and_utf16_text_are_extracted_and_not_just_filename_matches(indexed_library):
    browser, _, _ = indexed_library
    pdf = upload(browser, "架构.pdf", pdf_bytes("QUASAR protocol restores checkpoints"))
    note = upload(browser, "编码.txt", "银杏协议将检查点保存在事务日志中。".encode("utf-16"))
    for document in (pdf, note):
        wait_index(browser, document["id"])
    pdf_hit = next(item for item in search(browser, "QUASAR") if item["document_id"] == pdf["id"])
    assert "restores checkpoints" in pdf_hit["text"]
    assert pdf_hit["location"]["page"] == 1
    assert note["id"] in {item["document_id"] for item in search(browser, "银杏")}
    assert browser.get(FILES, params={"q": "QUASAR"}).json()["items"] == []


def test_long_document_has_searchable_tail_and_multiple_chunks(indexed_library):
    browser, _, _ = indexed_library
    paragraphs = [
        f"第{number}段。" + "持久任务需要记录原始资料与处理进度。" * 18 for number in range(20)
    ]
    marker = "最终段落的 ORBITAL_END_793 必须能被完整检索。"
    body = "# 分块检验\n\n" + "\n\n".join([*paragraphs, marker])
    document = upload(browser, "较长的正文.md", body.encode())
    status = wait_index(browser, document["id"])
    assert status["chunk_count"] > 1
    hit = next(item for item in search(browser, "ORBITAL_END_793") if marker in item["text"])
    assert hit["document_id"] == document["id"]
    assert hit["location"]["end_line"] >= 40


def test_compatibility_characters_are_searchable_without_rewriting_source(indexed_library):
    browser, _, _ = indexed_library
    # PDF text commonly contains Kangxi radicals and full-width Latin characters.
    body = "上下⽂压缩与⼯具调⽤由ＡｇｅｎｔＲｕｎｔｉｍｅ协调，版本２０２６。"
    document = upload(browser, "规范化检索.txt", body.encode())
    wait_index(browser, document["id"])
    for query in ("上下文压缩", "工具调用", "agentruntime", "2026"):
        hits = search(browser, query)
        hit = next(item for item in hits if item["document_id"] == document["id"])
        assert body in hit["text"], "Search normalization must preserve the displayed original"


def test_scope_favorites_rename_and_query_syntax_use_current_metadata(indexed_library):
    browser, _, _ = indexed_library
    first = upload(browser, "甲说明.txt", "苍穹协议使用检查点恢复，保留崩溃前的数据。".encode())
    second = upload(browser, "乙说明.txt", "苍穹协议另有故障审计机制，避免数据重复。".encode())
    for document in (first, second):
        wait_index(browser, document["id"])
    assert {hit["document_id"] for hit in search(browser, "苍穹")} == {first["id"], second["id"]}
    assert search(browser, "苍穹", favorites_only=True) == []
    patched = browser.patch(
        f"{FILES}/{second['id']}",
        headers=HEADERS,
        json={"name": "收藏后的审计说明.txt", "favorite": True},
    )
    assert patched.status_code == 200, patched.text
    favorite_hits = search(browser, "苍穹", favorites_only=True)
    assert {hit["document_id"] for hit in favorite_hits} == {second["id"]}
    assert {hit["name"] for hit in favorite_hits} == {"收藏后的审计说明.txt"}
    assert {hit["document_id"] for hit in search(browser, "苍穹", document_ids=[first["id"]])} == {
        first["id"]
    }
    assert search(browser, "苍穹", document_ids=[first["id"]], favorites_only=True) == []
    assert search(browser, "苍穹", document_ids=[]) == []

    foreign_chunk = favorite_hits[0]
    assert (
        browser.get(f"{FILES}/{first['id']}/chunks/{foreign_chunk['chunk_id']}").status_code == 404
    )
    for query in ('"', "*", "NEAR(苍穹, 2)", "苍穹 OR (恢复", "' OR 1=1 --"):
        # User text is data, not raw FTS/SQL syntax. It must never cause a query error.
        search(browser, query)
    assert browser.get(FILES).json()["total"] == 2


def test_replace_and_delete_immediately_remove_old_versions_from_search(indexed_library):
    browser, _, _ = indexed_library
    document = upload(browser, "版本.txt", b"OLD_ZEPHYR_428 describes the retired procedure.")
    wait_index(browser, document["id"])
    old_hit = search(browser, "OLD_ZEPHYR_428")[0]
    replacement = browser.put(
        f"{FILES}/{document['id']}/content",
        headers=HEADERS,
        files={"file": ("新版本.txt", b"NEW_ORBIT_902 describes the active procedure.")},
    )
    assert replacement.status_code == 200, replacement.text
    updated = replacement.json()
    assert search(browser, "OLD_ZEPHYR_428") == []
    assert browser.get(f"{FILES}/{document['id']}/chunks/{old_hit['chunk_id']}").status_code == 404
    wait_index(browser, document["id"], version_id=updated["version_id"])
    new_hit = search(browser, "NEW_ORBIT_902")[0]
    assert new_hit["version_id"] == updated["version_id"]
    assert browser.delete(f"{FILES}/{document['id']}", headers=HEADERS).status_code == 204
    assert search(browser, "NEW_ORBIT_902") == []
    assert browser.get(f"{FILES}/{document['id']}/chunks/{new_hit['chunk_id']}").status_code == 404


def blank_or_encrypted_pdf(*, encrypted=False):
    from pypdf import PdfWriter

    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=160)
    if encrypted:
        writer.encrypt("test-document-password")
    writer.write(output)
    return output.getvalue()


def test_image_only_and_encrypted_pdf_report_actionable_terminal_states(indexed_library):
    browser, _, _ = indexed_library
    blank = upload(browser, "扫描件.pdf", blank_or_encrypted_pdf())
    status = wait_index(browser, blank["id"], "needs_ocr")
    assert status["chunk_count"] == 0
    assert status["error"] and "ocr" in status["error"].lower()
    locked = upload(browser, "加密资料.pdf", blank_or_encrypted_pdf(encrypted=True))
    status = wait_index(browser, locked["id"], "failed")
    assert status["chunk_count"] == 0
    assert status["error"]
    assert search(browser, "扫描件") == []


def test_failed_parser_is_visible_and_manual_reindex_retries_it(indexed_library, monkeypatch):
    browser, app, _ = indexed_library

    def fail_parse(*args, **kwargs):
        raise RuntimeError("simulated extraction failure for retry")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.ingestion.parser, "parse", fail_parse)
        document = upload(browser, "可重试.txt", "乌桕协议要求失败后能够重新执行入库。".encode())
        failed = wait_index(browser, document["id"], "failed")
        assert failed["error"], "A failed ingestion must explain its state to the user"
        assert search(browser, "乌桕") == []
    response = browser.post(f"{FILES}/{document['id']}/reindex", headers=HEADERS)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["document_id"] == document["id"]
    assert job["version_id"] == document["version_id"]
    assert job["id"]
    restored = wait_index(browser, document["id"])
    assert restored["error"] is None
    assert document["id"] in {hit["document_id"] for hit in search(browser, "乌桕")}
    assert browser.post(f"{FILES}/{document['id']}/reindex").status_code == 403


def test_preexisting_saved_files_are_indexed_and_survive_application_restart(tmp_path):
    config = HarnessConfig(tmp_path, "ingestion-test-model")
    model = FakeClient()
    legacy = LibraryService(config.data_dir / "library")
    document = legacy.upload("旧资料.txt", io.BytesIO("星槎协议来自此前已经保存的原件。".encode()))
    legacy.close()
    with TestClient(create_app(config, lambda: model)) as browser:
        wait_index(browser, document["id"])
        first_hit = search(browser, "星槎")[0]
    with TestClient(create_app(config, lambda: model)) as browser:
        assert browser.get(f"{FILES}/{document['id']}").json()["ingestion"]["status"] == "ready"
        restored = search(browser, "星槎")[0]
        assert restored["document_id"] == first_hit["document_id"]
        assert restored["chunk_id"] == first_hit["chunk_id"]
        assert restored["version_id"] == first_hit["version_id"]
    assert model.calls == []


def test_processing_job_is_recovered_after_restart_without_running_original_worker(tmp_path):
    config = HarnessConfig(tmp_path, "ingestion-test-model")
    model = FakeClient()
    stopped_app = create_app(config, lambda: model)
    document = stopped_app.state.library.upload(
        "中断任务.txt", io.BytesIO("木棉协议必须在服务中断后恢复入库。".encode())
    )
    claimed = stopped_app.state.ingestion.index.claim()
    assert claimed and claimed["document_id"] == document["id"]
    assert stopped_app.state.ingestion.get_job(claimed["id"])["status"] == "processing"
    # No lifespan was entered, so the original worker has never been started.
    with TestClient(create_app(config, lambda: model)) as browser:
        wait_index(browser, document["id"])
        assert document["id"] in {hit["document_id"] for hit in search(browser, "木棉")}
    assert model.calls == []


def test_deleting_during_parse_cannot_publish_document_again(indexed_library, monkeypatch):
    browser, app, _ = indexed_library
    entered, release = threading.Event(), threading.Event()
    parse = app.state.ingestion.parser.parse

    def slow_parse(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10), "Test must release the in-flight parser"
        return parse(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(app.state.ingestion.parser, "parse", slow_parse)
        document = upload(
            browser, "处理中删除.txt", b"DELETED_ECHO_581 must never become searchable."
        )
        try:
            assert entered.wait(timeout=10), "Worker did not claim the persisted job"
            state = browser.get(f"{FILES}/{document['id']}").json()["ingestion"]
            with ThreadPoolExecutor(max_workers=1) as executor:
                deletion = executor.submit(
                    browser.delete, f"{FILES}/{document['id']}", headers=HEADERS
                )
                try:
                    assert deletion.result(timeout=3).status_code == 204
                finally:
                    release.set()
        finally:
            release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = browser.get(f"/api/library/jobs/{state['job_id']}")
            if response.status_code == 404 or response.json()["status"] != "processing":
                break
            time.sleep(0.03)
        else:
            raise AssertionError("Cancelled/deleted ingestion did not leave processing state")
    assert search(browser, "DELETED_ECHO_581") == []
    assert browser.get(f"{FILES}/{document['id']}").status_code == 404
