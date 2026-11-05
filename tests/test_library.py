"""Library HTTP behavior with real files and SQLite, without online model calls."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from test_services import FakeClient

HEADERS = {"X-Agent-Client": "web"}
FILES = "/api/library/files"


def pdf_bytes(label="Library PDF preview"):
    """Build a valid single-page PDF, including its cross-reference table."""
    commands = f"BT /F1 18 Tf 36 100 Td ({label}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 160] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(commands)} >>\nstream\n".encode() + commands + b"endstream",
    ]
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, value in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


PDF = pdf_bytes()


@pytest.fixture
def library(tmp_path):
    config = HarnessConfig(tmp_path, "library-test-model")
    model = FakeClient()
    with TestClient(create_app(config, lambda: model)) as browser:
        yield browser, config
    assert model.calls == [], "File management must not call the online model"


def upload(browser, name="知识库笔记.txt", content=None, *, prefix=FILES):
    if content is None:
        content = "第一阶段只保存资料，尚未建立向量索引。".encode()
    result = browser.post(prefix, headers=HEADERS, files={"file": (name, content)})
    assert result.status_code == 201, result.text
    return result.json()


def original_files(config):
    root = config.data_dir / "library" / "originals"
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def test_upload_preview_download_and_metadata_survive_restart(tmp_path):
    config = HarnessConfig(tmp_path, "library-test-model")
    model = FakeClient()
    content = "# 检索设计\n\n保留原文位置，才能展示真实引用。\n".encode()
    with TestClient(create_app(config, lambda: model)) as browser:
        record = upload(browser, "检索设计.md", content)
        identifier = record["id"]
        assert record["name"] == record["original_name"] == "检索设计.md"
        assert record["extension"] == "md"
        assert record["size_bytes"] == len(content)
        assert record["sha256"] == hashlib.sha256(content).hexdigest()
        assert record["status"] == "stored"
        assert record["version_id"]
        assert record["created_at"] and record["updated_at"]
        assert record["favorite"] is False
        preview = browser.get(f"{FILES}/{identifier}/preview")
        assert preview.status_code == 200
        assert preview.json()["text"] == content.decode()
        assert preview.json()["truncated"] is False
        assert preview.json()["version_id"] == record["version_id"]
        download = browser.get(f"{FILES}/{identifier}/content")
        assert download.status_code == 200
        assert download.content == content
        assert "attachment" in download.headers["content-disposition"]
        saved = browser.patch(
            f"{FILES}/{identifier}",
            headers=HEADERS,
            json={"name": "来源引用.md", "favorite": True},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version_id"] == record["version_id"]
        assert [path.read_bytes() for path in original_files(config)] == [content]

    with TestClient(create_app(config, lambda: model)) as browser:
        restored = browser.get(f"/api/agent/library/files/{identifier}")
        assert restored.status_code == 200
        assert restored.json()["name"] == "来源引用.md"
        assert restored.json()["favorite"] is True
        assert browser.get(f"{FILES}/{identifier}/content").content == content
        listing = browser.get(FILES, params={"q": "引用", "favorite": True}).json()
        assert [item["id"] for item in listing["items"]] == [identifier]
        assert listing["counts"] == {"all": 1, "favorites": 1}
    assert model.calls == []


def test_filters_sorting_and_pagination_use_persisted_records(library):
    browser, _ = library
    note = upload(browser, "甲知识笔记.md", b"# Markdown notes")
    upload(browser, "乙运维清单.txt", "重启后重新打开资料库。".encode())
    upload(browser, "丙系统说明.pdf", PDF)
    browser.patch(f"{FILES}/{note['id']}", headers=HEADERS, json={"favorite": True})

    listing = browser.get(FILES, params={"sort": "name_asc"}).json()
    assert listing["total"] == 3
    assert [item["name"] for item in listing["items"]] == sorted(
        item["name"] for item in listing["items"]
    )
    page = browser.get(FILES, params={"sort": "name_asc", "limit": 1, "offset": 1}).json()
    assert page["total"] == 3
    assert page["limit"] == 1 and page["offset"] == 1
    # Ingestion progresses between requests; pagination compares stable identities.
    assert [item["id"] for item in page["items"]] == [item["id"] for item in listing["items"][1:2]]
    favorites = browser.get(FILES, params={"favorite": True, "extension": "md"}).json()
    assert [item["id"] for item in favorites["items"]] == [note["id"]]
    assert favorites["counts"] == {"all": 3, "favorites": 1}
    matches = browser.get(FILES, params={"q": "运维"}).json()
    assert [item["name"] for item in matches["items"]] == ["乙运维清单.txt"]
    sizes = browser.get(FILES, params={"sort": "size_desc"}).json()["items"]
    assert [item["size_bytes"] for item in sizes] == sorted(
        (item["size_bytes"] for item in sizes), reverse=True
    )
    assert browser.get(FILES, params={"q": "不存在的资料"}).json()["items"] == []


def test_replace_preserves_identity_and_failed_replace_preserves_contents(library):
    browser, _ = library
    record = upload(browser, "运行手册.txt", b"original contents")
    endpoint = f"{FILES}/{record['id']}"
    browser.patch(endpoint, headers=HEADERS, json={"favorite": True})
    updated_content = "替换后保留资料 ID 和收藏，更新内容版本。".encode()
    result = browser.put(
        f"{endpoint}/content",
        headers=HEADERS,
        files={"file": ("运行手册.txt", updated_content)},
    )
    assert result.status_code == 200, result.text
    updated = result.json()
    assert updated["id"] == record["id"]
    assert updated["favorite"] is True
    assert updated["version_id"] != record["version_id"]
    assert updated["sha256"] == hashlib.sha256(updated_content).hexdigest()
    assert browser.get(f"{endpoint}/content").content == updated_content
    assert browser.get(f"{endpoint}/preview").json()["text"] == updated_content.decode()

    invalid = browser.put(
        f"{endpoint}/content",
        headers=HEADERS,
        files={"file": ("program.exe", b"unsupported replacement")},
    )
    assert invalid.status_code == 415
    assert browser.get(endpoint).json()["version_id"] == updated["version_id"]
    assert browser.get(f"{endpoint}/content").content == updated_content


def test_duplicate_and_delete_leave_no_active_file_or_original(library):
    browser, config = library
    content = b"a uniquely identifiable uploaded file"
    record = upload(browser, "资料.txt", content)
    duplicate = browser.post(FILES, headers=HEADERS, files={"file": ("另一名字.txt", content)})
    assert duplicate.status_code == 409
    assert browser.get(FILES).json()["total"] == 1
    assert len(original_files(config)) == 1
    endpoint = f"{FILES}/{record['id']}"
    assert browser.delete(endpoint, headers=HEADERS).status_code == 204
    for suffix in ("", "/preview", "/content"):
        assert browser.get(endpoint + suffix).status_code == 404
    assert browser.delete(endpoint, headers=HEADERS).status_code == 404
    assert browser.get(FILES).json()["counts"] == {"all": 0, "favorites": 0}
    assert original_files(config) == []
    replacement = upload(browser, "重新上传.txt", content)
    assert replacement["id"] != record["id"]


def test_concurrent_duplicate_uploads_publish_one_file(library):
    browser, config = library

    def send(index):
        return browser.post(
            FILES,
            headers=HEADERS,
            files={"file": (f"并发资料{index}.txt", b"same concurrent document")},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, range(2)))
    assert sorted(result.status_code for result in results) == [201, 409]
    assert browser.get(FILES).json()["total"] == 1
    assert len(original_files(config)) == 1


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("空文件.txt", b"", 422),
        ("程序.exe", b"executable-like content", 415),
        ("错误.pdf", b"this is not a PDF", 415),
        ("../escape.txt", b"must not escape upload storage", 422),
    ],
)
def test_rejected_uploads_do_not_create_documents(library, name, content, expected):
    browser, config = library
    result = browser.post(FILES, headers=HEADERS, files={"file": (name, content)})
    assert result.status_code == expected, result.text
    assert browser.get(FILES).json()["total"] == 0
    assert original_files(config) == []
    assert not (config.workdir / "escape.txt").exists()


def test_upload_size_limit_leaves_no_partial_original(library):
    browser, config = library
    result = browser.post(
        FILES,
        headers=HEADERS,
        files={"file": ("too-large.txt", b"x" * (25 * 1024 * 1024 + 1))},
    )
    assert result.status_code == 413, result.text
    assert browser.get(FILES).json()["total"] == 0
    assert original_files(config) == []


def test_rename_rejects_paths_and_extension_changes(library):
    browser, _ = library
    record = upload(browser)
    endpoint = f"{FILES}/{record['id']}"
    for body in ({}, {"name": "../outside.txt"}, {"name": "pretend.pdf"}, {"name": ""}):
        result = browser.patch(endpoint, headers=HEADERS, json=body)
        assert result.status_code == 422, result.text
    assert browser.get(endpoint).json()["name"] == record["name"]


def test_all_mutations_require_local_web_client(library):
    browser, _ = library
    record = upload(browser)
    endpoint = f"{FILES}/{record['id']}"
    for headers in ({}, {**HEADERS, "Origin": "https://unrelated.invalid"}):
        assert (
            browser.post(FILES, headers=headers, files={"file": ("资料.txt", b"new file")})
        ).status_code == 403
        assert browser.patch(endpoint, headers=headers, json={"favorite": True}).status_code == 403
        assert (
            browser.put(
                f"{endpoint}/content", headers=headers, files={"file": ("资料.txt", b"replace")}
            ).status_code
            == 403
        )
        assert browser.delete(endpoint, headers=headers).status_code == 403
    assert browser.get(endpoint).json()["favorite"] is False
    assert browser.get(FILES).json()["total"] == 1


def test_pdf_serves_original_inline_and_as_download(library):
    browser, _ = library
    record = upload(browser, "系统设计.pdf", PDF)
    for download, disposition in ((False, "inline"), (True, "attachment")):
        result = browser.get(f"{FILES}/{record['id']}/content", params={"download": download})
        assert result.status_code == 200
        assert result.content == PDF
        assert result.headers["content-type"].startswith("application/pdf")
        assert disposition in result.headers["content-disposition"]
    partial = browser.get(
        f"{FILES}/{record['id']}/content",
        headers={"Range": "bytes=0-19"},
        params={"download": False},
    )
    assert partial.status_code == 206
    assert partial.content == PDF[:20]
    assert partial.headers["content-range"] == f"bytes 0-19/{len(PDF)}"
    assert record["status"] == "stored"
    assert browser.delete(f"{FILES}/{record['id']}", headers=HEADERS).status_code == 204
    assert browser.get(f"{FILES}/{record['id']}/content").status_code == 404


def test_versioned_pdf_preview_rejects_a_replaced_original(library):
    browser, config = library
    record = upload(browser, "版本预览.pdf", PDF)
    file_url = f"{FILES}/{record['id']}"
    content_url = f"{file_url}/content"
    version = {"download": False, "version_id": record["version_id"]}
    assert browser.get(content_url, params=version).content == PDF

    updated = pdf_bytes("Updated preview contents")
    replacement = browser.put(content_url, headers=HEADERS, files={"file": ("新版.pdf", updated)})
    assert replacement.status_code == 200
    stale = browser.get(content_url, params=version, headers={"Range": "bytes=0-19"})
    assert stale.status_code == 409

    version["version_id"] = replacement.json()["version_id"]
    assert browser.get(content_url, params=version).content == updated
    assert browser.get(content_url).content == updated
    assert browser.delete(file_url, headers=HEADERS).status_code == 204
    assert original_files(config) == []


def test_text_preview_honors_bom_and_truncation_without_changing_original(library):
    browser, _ = library
    value = "中文 BOM 文本与原文件分别处理。"
    content = value.encode("utf-16")
    record = upload(browser, "编码.txt", content)
    endpoint = f"{FILES}/{record['id']}"
    assert browser.get(f"{endpoint}/preview").json()["text"] == value
    assert browser.get(f"{endpoint}/content").content == content

    large = "长文本内容。" * 40000
    record = upload(browser, "长文本.txt", large.encode())
    preview = browser.get(f"{FILES}/{record['id']}/preview").json()
    assert preview["truncated"] is True
    assert 0 < len(preview["text"]) <= 200000
    assert large.startswith(preview["text"])


def test_invalid_pdf_range_does_not_keep_replaced_or_deleted_original_alive(library):
    browser, config = library
    record = upload(browser, "预览请求.pdf", PDF)
    endpoint = f"{FILES}/{record['id']}"
    invalid = browser.get(
        f"{endpoint}/content",
        params={"download": False},
        headers={"Range": f"bytes={len(PDF) + 100}-"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(PDF)}"

    updated = pdf_bytes("Replacement PDF document")
    result = browser.put(
        f"{endpoint}/content", headers=HEADERS, files={"file": ("新原件.pdf", updated)}
    )
    assert result.status_code == 200, result.text
    assert result.json()["version_id"] != record["version_id"]
    assert [path.read_bytes() for path in original_files(config)] == [updated]
    assert browser.delete(endpoint, headers=HEADERS).status_code == 204
    assert original_files(config) == []


def test_replace_database_failure_preserves_published_version_and_removes_new_blob(
    library, monkeypatch
):
    browser, config = library
    content = b"Keep this original if publishing replacement metadata fails."
    record = upload(browser, "可靠更新.txt", content)
    endpoint = f"{FILES}/{record['id']}"
    before = browser.patch(endpoint, headers=HEADERS, json={"favorite": True}).json()

    def fail_replace(*args, **kwargs):
        raise sqlite3.OperationalError("simulated replacement transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(browser.app.state.library.repository, "replace", fail_replace)
        with pytest.raises(sqlite3.OperationalError, match="replacement transaction failure"):
            browser.put(
                f"{endpoint}/content",
                headers=HEADERS,
                files={"file": ("新内容.txt", b"Unpublished replacement must be removed.")},
            )

    after = browser.get(endpoint).json()
    assert after["version_id"] == before["version_id"]
    assert after["sha256"] == before["sha256"]
    assert after["favorite"] is True
    assert after["updated_at"] == before["updated_at"]
    assert browser.get(f"{endpoint}/content").content == content
    assert [path.read_bytes() for path in original_files(config)] == [content]
    assert not list((config.data_dir / "library" / ".staging").iterdir())


def test_failed_original_cleanup_is_hidden_immediately_and_retried_after_restart(
    tmp_path, monkeypatch
):
    config = HarnessConfig(tmp_path, "library-test-model")
    model = FakeClient()
    app = create_app(config, lambda: model)

    def deny_remove(key):
        raise PermissionError("simulated original still open in another application")

    with monkeypatch.context() as patch:
        with TestClient(app) as browser:
            record = upload(
                browser, "等待清理.txt", b"Delete visibility must not depend on unlink."
            )
            endpoint = f"{FILES}/{record['id']}"
            saved = original_files(config)
            assert len(saved) == 1
            patch.setattr(app.state.library.blobs, "remove", deny_remove)
            assert browser.delete(endpoint, headers=HEADERS).status_code == 204
            assert browser.get(FILES).json()["total"] == 0
            for suffix in ("", "/preview", "/content"):
                assert browser.get(endpoint + suffix).status_code == 404
            assert saved[0].is_file()
        # Cleanup still fails at shutdown, so recovery must use the persisted job.
        assert saved[0].is_file()

    with TestClient(create_app(config, lambda: model)) as browser:
        assert browser.get(FILES).json()["total"] == 0
        assert browser.get(endpoint).status_code == 404
        assert original_files(config) == []
    assert model.calls == []
