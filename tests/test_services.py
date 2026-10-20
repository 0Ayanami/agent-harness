"""Behavior tests for composed services and their original agent mechanisms."""

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_harness import AgentRuntime, HarnessConfig
from agent_harness.core.messages import repair_interrupted_turn
from agent_harness.core.models import RecoveryState


def response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool(name, arguments, tool_id="tool-1"):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


class FakeClient:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **arguments):
        self.calls.append(deepcopy(arguments))
        if arguments["max_tokens"] == 1000:
            return response(text("[]"))
        if not self.replies:
            raise AssertionError("Unexpected model call")
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def runtime(tmp_path):
    instance = AgentRuntime(HarnessConfig(tmp_path, "test-model"), FakeClient())
    yield instance
    instance.close()


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for worker")


def test_main_engine_delivers_tool_results(runtime):
    runtime.gateway.client.replies = [
        response(
            tool("write_file", {"path": "hello.txt", "content": "你好"}),
            stop_reason="tool_use",
        ),
        response(tool("read_file", {"path": "hello.txt"}, "tool-2"), stop_reason="tool_use"),
        response(text("done")),
    ]
    history = [{"role": "user", "content": "write and read"}]
    assert runtime.run(history, "write and read") == "done"
    assert history[4]["content"][0]["content"] == "你好"
    assert history[4]["content"][0]["tool_use_id"] == "tool-2"


def test_permission_denial_remains_a_tool_result(runtime):
    runtime.gateway.client.replies = [
        response(
            tool("write_file", {"path": "../escape", "content": "bad"}),
            stop_reason="tool_use",
        ),
        response(text("blocked")),
    ]
    history = [{"role": "user", "content": "write"}]
    runtime.run(history, "write")
    assert "Permission denied" in history[2]["content"][0]["content"]
    assert not (runtime.config.workdir.parent / "escape").exists()


def test_subagent_reuses_engine_with_isolated_history(runtime):
    runtime.workspace.run_write("source.txt", "child input")
    runtime.gateway.client.replies = [
        response(tool("read_file", {"path": "source.txt"}), stop_reason="tool_use"),
        response(text("child summary")),
    ]
    assert runtime.subagents.engine is runtime.engine
    assert runtime.subagents.run("inspect source.txt") == "child summary"
    assert runtime.gateway.client.calls[1]["messages"][2]["content"][0]["content"] == "child input"
    assert "task" not in {
        definition["name"] for definition in runtime.gateway.client.calls[0]["tools"]
    }


def test_memory_recall_injected_into_system_prompt(runtime):
    runtime.memory.write_memory_file("Language", "user", "Chinese responses", "Respond in Chinese")
    runtime.gateway.client.replies = [response(text("[0]"))]
    prompt = runtime.system_prompt([{"role": "user", "content": "Which language?"}])
    assert "Respond in Chinese" in prompt
    assert "Memory catalog" in prompt


def test_memory_recall_falls_back_when_model_unavailable(runtime):
    runtime.memory.write_memory_file("Language", "user", "Chinese language", "Respond in Chinese")
    runtime.gateway.client.replies = [RuntimeError("offline")]
    assert "Respond in Chinese" in runtime.memory.load_memories(
        [{"role": "user", "content": "Chinese language"}]
    )
    with pytest.raises(ValueError):
        runtime.memory.memory_path("../escape.md")


def test_task_dependencies_and_durable_state(runtime):
    before, after = (
        runtime.tasks.create_task("before"),
        runtime.tasks.create_task("after"),
    )
    runtime.tasks.update_task(after.id, [before.id])
    assert not runtime.tasks.can_start(after.id)
    runtime.tasks.claim_task(before.id)
    runtime.tasks.complete_task(before.id)
    assert runtime.tasks.can_start(after.id)
    reopened = AgentRuntime(runtime.config, FakeClient(), state_dir=runtime.state_dir)
    assert reopened.tasks.load_task(before.id).status == "completed"
    assert reopened.tasks.load_task(after.id).blockedBy == [before.id]
    reopened.close()


def test_task_store_serializes_parallel_writers(runtime):
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = list(pool.map(lambda i: runtime.tasks.create_task(f"task {i}"), range(12)))
    assert len({task.id for task in tasks}) == 12
    assert len(runtime.tasks.list_tasks()) == 12


def test_teammate_completion_and_shutdown(runtime):
    assigned = runtime.tasks.create_task("review")
    runtime.team.IDLE_SCAN_INTERVAL = 0.02
    runtime.gateway.client.replies = [
        response(tool("complete_task", {"task_id": assigned.id}), stop_reason="tool_use"),
        response(text("review complete")),
    ]
    assert "spawned" in runtime.team.spawn_teammate_thread(
        "reviewer", "review", "review", assigned.id
    )
    wait_until(lambda: runtime.coordination.active_teammates.get("reviewer") == "idle")
    assert runtime.tasks.load_task(assigned.id).status == "completed"
    assert any(
        "review complete" in message["content"] for message in runtime.team.consume_lead_inbox()
    )
    runtime.team.run_request_shutdown("reviewer")
    wait_until(lambda: "reviewer" not in runtime.coordination.active_teammates)
    runtime.team.consume_lead_inbox()
    assert any(
        request.type == "shutdown" and request.status == "approved"
        for request in runtime.coordination.pending_requests.values()
    )


def test_worktree_tools_follow_task_assignment(runtime):
    root = runtime.config.workdir
    for arguments in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Harness Tests"],
        ["git", "config", "user.email", "harness@example.invalid"],
    ):
        subprocess.run(arguments, cwd=root, capture_output=True, check=True)
    runtime.workspace.run_write("tracked.txt", "initial")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, capture_output=True, check=True)
    assigned = runtime.tasks.create_task("isolated change")
    assert "Error" not in runtime.tasks.create_worktree("isolated", assigned.id)
    assert "Claimed" in runtime.tasks.claim_task(assigned.id)
    _, handlers = runtime.registry.snapshot()
    assert "Wrote" in handlers["write_file"](path="isolated.txt", content="inside worktree")
    assert not (root / "isolated.txt").exists()
    assert (runtime.tasks.assignment_cwd("agent") / "isolated.txt").read_text(
        encoding="utf-8"
    ) == "inside worktree"


def test_background_notification_uses_workspace_service(runtime):
    block = tool("bash", {"command": "echo background-ok", "run_in_background": True})
    task_id = runtime.background.start_background_task(block, {})
    wait_until(runtime.background.has_pending_background)
    notes = runtime.background.collect_background_results()
    assert len(notes) == 1 and task_id in notes[0] and "background-ok" in notes[0]
    assert "<status>completed</status>" in notes[0]


def test_mcp_discovery_and_policy(runtime):
    runtime.mcp.connect_mcp("docs")
    tools, handlers = runtime.registry.snapshot()
    assert "mcp__docs__search" in {definition["name"] for definition in tools}
    assert "RAG" in handlers["mcp__docs__search"](query="RAG")
    assert runtime.mcp.mcp_tool_policies["mcp__docs__search"] == "allow"


def test_cron_pending_delivery_survives_reopen(runtime):
    job = runtime.scheduler.schedule_job("* * * * *", "remember", recurring=False)
    runtime.scheduler._enqueue_due_job(job)
    reopened = AgentRuntime(runtime.config, FakeClient(), state_dir=runtime.state_dir)
    reopened.scheduler.load_durable_jobs()
    delivered = reopened.scheduler.consume_cron_queue()
    assert [item.id for item in delivered] == [job.id]
    reopened.scheduler.acknowledge_cron_jobs(delivered)
    assert json.loads(reopened.scheduler.DURABLE_PATH.read_text(encoding="utf-8")) == []
    reopened.close()


def test_context_preparation_keeps_tool_pairs(runtime):
    history = [{"role": "user", "content": "inspect"}]
    for i in range(30):
        history.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "read_file",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}],
                },
            ]
        )
    runtime.context.prepare_context(history, "inspect")
    seen = set()
    for message in history:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block["type"] == "tool_use":
                seen.add(block["id"])
            if block["type"] == "tool_result":
                assert block["tool_use_id"] in seen


def test_restart_repairs_orphaned_tool_calls():
    history = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "orphan", "name": "bash", "input": {}}],
        }
    ]
    repair_interrupted_turn(history)
    assert history[-1]["content"][0]["tool_use_id"] == "orphan"
    repair_interrupted_turn(history)
    assert len(history) == 2


def test_retry_uses_fallback_model(runtime, monkeypatch):
    from dataclasses import replace

    runtime.gateway.config = replace(runtime.config, fallback_model="fallback-model")
    monkeypatch.setattr("agent_harness.infrastructure.model.time.sleep", lambda _: None)
    state = RecoveryState("test-model")
    calls = []

    def request():
        calls.append(state.current_model)
        if len(calls) < 3:
            raise RuntimeError("529 overloaded")
        return "recovered"

    assert runtime.gateway.with_retry(request, state) == "recovered"
    assert calls == ["test-model", "test-model", "fallback-model"]


def test_close_stops_scheduler(runtime):
    runtime.scheduler.start_runtime_services()
    runtime.close()
    assert not runtime.scheduler.thread.is_alive()


def test_scheduled_delivery_acknowledged_when_model_accepts_prompt(runtime):
    job = runtime.scheduler.schedule_job("* * * * *", "scheduled request", recurring=False)
    runtime.scheduler._enqueue_due_job(job)
    runtime.gateway.client.replies = [
        response(tool("read_file", {"path": "missing.txt"}), stop_reason="tool_use"),
        RuntimeError("later provider failure"),
    ]
    with pytest.raises(RuntimeError, match="later provider failure"):
        runtime.run([{"role": "user", "content": "initial"}], "initial")
    assert job.id not in runtime.scheduler.scheduled_jobs
    assert runtime.scheduler.cron_queue == []
