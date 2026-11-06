"""The skill browser exposes read-only, workspace-local manifests."""

import os
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.interfaces.web.skills import MAX_SKILL_BYTES


def client_for(workdir):
    return TestClient(create_app(HarnessConfig(workdir, "skill-test-model"), lambda: None))


def write_manifest(workdir, directory, markdown):
    manifest = workdir / "skills" / directory / "SKILL.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(markdown, encoding="utf-8")
    return manifest


def test_browse_grouped_and_flat_manifests_with_stable_ids(tmp_path):
    flat = write_manifest(tmp_path, "plain", "# Plain\n")
    grouped_manifest = write_manifest(
        tmp_path,
        "开发/测试",
        "---\nname: 示例\ndescription: >\n  A useful\n  test.\n---\n# 指南\n",
    )
    write_manifest(tmp_path, ".hidden", "hidden skill")
    write_manifest(tmp_path, "plain/references/example", "auxiliary file")
    (flat.parent / "notes.md").write_text("not a skill manifest", encoding="utf-8")
    with client_for(tmp_path) as browser:
        first = browser.get("/api/skills")
        assert first.status_code == 200
        assert browser.get("/api/agent/skills").json() == first.json()
        entries = first.json()["skills"]
        assert len(entries) == 2
        grouped = next(entry for entry in entries if entry["category"] is not None)
        assert grouped == {
            "id": sha256("开发/测试".encode()).hexdigest(),
            "name": "示例",
            "category": "开发",
            "description": "A useful test.",
        }
        detail = browser.get(f"/api/skills/{grouped['id']}")
        assert detail.status_code == 200
        assert detail.json()["markdown"] == grouped_manifest.read_bytes().decode("utf-8")
        plain = next(entry for entry in entries if entry["category"] is None)
        assert plain["name"] == "plain"
        assert "markdown" not in plain
        flat.write_text("# Updated\n", encoding="utf-8")
        assert browser.get(f"/api/skills/{plain['id']}").json()[
            "markdown"
        ] == flat.read_bytes().decode("utf-8")
        assert browser.get(f"/api/skills/{plain['id']}").json()["id"] == plain["id"]
        assert browser.post(f"/api/skills/{plain['id']}").status_code == 405
        assert browser.get("/api/skills/notes.md").status_code == 404


def test_empty_or_unreadable_catalog_remains_local(tmp_path):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    write_manifest(tmp_path, "elsewhere", "outside the selected workspace")
    write_manifest(isolated, "oversized", "x" * (MAX_SKILL_BYTES + 1))
    invalid = write_manifest(isolated, "invalid", "")
    invalid.write_bytes(b"\xff\xfe\xff")
    with client_for(isolated) as browser:
        assert browser.get("/api/skills").json() == {"skills": []}
        assert browser.get("/api/skills/" + sha256(b"oversized").hexdigest()).status_code == 404
        assert browser.get("/api/skills/" + sha256(b"elsewhere").hexdigest()).status_code == 404


def test_manifest_cannot_expose_a_hardlinked_file(tmp_path):
    outside = tmp_path / "credentials.txt"
    outside.write_text("secret", encoding="utf-8")
    manifest = tmp_path / "skills" / "linked" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    try:
        os.link(outside, manifest)
    except OSError as error:
        pytest.skip(f"filesystem does not allow hardlinks: {error}")
    with client_for(tmp_path) as browser:
        assert browser.get("/api/skills").json() == {"skills": []}
        assert browser.get("/api/skills/" + sha256(b"linked").hexdigest()).status_code == 404


def test_manifest_and_skill_directory_symlinks_are_excluded(tmp_path):
    outside = tmp_path / "other" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("secret", encoding="utf-8")
    folder = tmp_path / "skills" / "linked-file"
    folder.mkdir(parents=True)
    try:
        (folder / "SKILL.md").symlink_to(outside)
        (folder.parent / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"filesystem does not allow symbolic links: {error}")
    with client_for(tmp_path) as browser:
        assert browser.get("/api/skills").json() == {"skills": []}
        for slug in (b"linked-file", b"linked-dir"):
            assert browser.get("/api/skills/" + sha256(slug).hexdigest()).status_code == 404
