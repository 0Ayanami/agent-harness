from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from agent_harness import HarnessConfig
from agent_harness.interfaces.web.app import create_app
from agent_harness.infrastructure.mcp import MCPService
from agent_harness.services.permissions import PermissionPolicy
from agent_harness.services import arxiv
from test_library import FILES, PDF, upload
from test_services import FakeClient, wait_until


def test_wikipedia_and_arxiv_use_host_tool_policy(tmp_path, monkeypatch):
    service = MCPService()
    available = {
        "wikipedia": ["search_wikipedia", "wikipedia_get_summary", "new_export"],
        "arxiv": ["search_papers", "download_paper"],
    }
    monkeypatch.setattr(
        service,
        "_connect_docker",
        lambda name: SimpleNamespace(
            tools=[
                {"name": tool, "annotations": {"readOnlyHint": True}} for tool in available[name]
            ]
        ),
    )
    assert "Connected" in service.connect_mcp("wikipedia-mcp")
    assert "Connected" in service.connect_mcp("arxiv")
    definitions, _ = service.assemble_tool_pool([], {})
    prompts = []
    permissions = PermissionPolicy(tmp_path, lambda name, _: prompts.append(name) or False, service)
    for definition in definitions:
        result = permissions.check(SimpleNamespace(name=definition["name"], input={}))
        assert bool(result) == definition["name"].endswith(("new_export", "download_paper"))
    assert set(prompts) == {"mcp__wikipedia__new_export", "mcp__arxiv__download_paper"}


def test_arxiv_tool_imports_into_the_web_library(tmp_path, monkeypatch):
    class DiscoveredServer:
        tools = []

    config = HarnessConfig(tmp_path, "test-model")
    app = create_app(config, FakeClient)

    def arxiv_response(request):
        assert str(request.url) == "https://arxiv.org/pdf/hep-th/9901001v2"
        return httpx.Response(200, content=PDF)

    with (
        httpx.Client(transport=httpx.MockTransport(arxiv_response)) as upstream,
        TestClient(app) as web,
    ):
        monkeypatch.setattr(arxiv.httpx, "stream", upstream.stream)
        sessions = app.state.application
        live = sessions.get(sessions.create()["id"])
        runtime = live.runtime
        monkeypatch.setattr(runtime.mcp, "_connect_docker", lambda name: DiscoveredServer())
        assert "Connected" in runtime.mcp.connect_mcp("arxiv-mcp-server")
        _, handlers = runtime.registry.snapshot()
        call = {
            "id": "mcp-test",
            "name": "mcp__arxiv__import_pdf",
            "input": {"paper_id": "hep-th/9901001v2"},
        }
        runtime.permissions.approve = lambda *_: False
        assert runtime.dispatcher.execute(call, handlers) == "Permission denied by user"
        runtime.permissions.approve = lambda *_: True
        assert "Imported arxiv-hep-th-9901001v2.pdf" in runtime.dispatcher.execute(call, handlers)

        imported = web.get(FILES).json()["items"][0]
        assert imported["name"] == "arxiv-hep-th-9901001v2.pdf"
        assert web.get(f"{FILES}/{imported['id']}/content").content == PDF
        assert (
            config.data_dir
            / "library"
            / "originals"
            / imported["id"]
            / imported["version_id"]
            / "source.pdf"
        ).read_bytes() == PDF
        wait_until(lambda: app.state.ingestion.status(imported["id"])["status"] == "ready")
        assert "already exists" in runtime.dispatcher.execute(call, handlers)
        assert len(web.get(FILES).json()["items"]) == 1
        assert upload(web)["extension"] == "txt"
