"""Composition root: construct services once and wire their explicit dependencies."""

import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import HarnessConfig

from ..core.engine import AgentEngine
from ..core.events import discard_event
from ..core.hooks import HookRegistry
from ..core.state import BackgroundState, CoordinationState
from ..infrastructure.mcp import MCPService
from ..infrastructure.model import ModelGateway
from ..services.background import BackgroundTasks
from ..services.context import ContextService
from ..services.memory import MemoryStore
from ..services.permissions import PermissionPolicy
from ..services.scheduler import Scheduler
from ..services.subagents import SubagentService
from ..services.tasks import TaskService
from ..services.team import TeamService
from ..tools.builtins import register_builtins
from ..tools.dispatcher import ToolDispatcher
from ..tools.registry import ToolRegistry
from ..tools.workspace import WorkspaceTools


class AgentRuntime:
    """Per-session services. Engine execution has no dependency on this facade."""

    def __init__(
        self,
        config: HarnessConfig,
        client: Any,
        *,
        state_dir: Path | None = None,
        approve=None,
        emit=discard_event,
        memory_lock=None,
    ):
        self.config = config
        self.state_dir = state_dir or config.data_dir / "cli"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.shutdown = threading.Event()
        self.cancelled = threading.Event()
        self.coordination = CoordinationState()
        background_state = BackgroundState()
        self.gateway = ModelGateway(config, client)
        self.workspace = WorkspaceTools(config.workdir)
        self.memory = MemoryStore(config.workdir, client, config.model, lock=memory_lock)
        self.tasks = TaskService(config, self.state_dir, self.coordination, background_state)
        self.context = ContextService(config, self.state_dir, self.memory, self.gateway)
        self.mcp = MCPService()
        self.mcp.mcp_tool_policies = {}
        self.hooks = HookRegistry()
        self.permissions = PermissionPolicy(
            config.workdir, approve or (lambda name, args: False), self.mcp
        )
        self.hooks.register("PreToolUse", self.permissions.check)
        self.dispatcher = ToolDispatcher(self.hooks, emit, self.cancelled)
        self.background = BackgroundTasks(self.workspace, self.tasks, background_state, self.hooks)
        self.dispatcher.background = self.background
        self.scheduler = Scheduler(self.state_dir, self.shutdown)
        self.team = TeamService(
            config,
            self.state_dir,
            self.tasks,
            self.workspace,
            self.gateway,
            self.dispatcher,
            self.coordination,
            self.shutdown,
        )
        self.engine = AgentEngine(
            config, self.gateway, self.dispatcher, self.context, emit, self.cancelled
        )
        self.subagents = SubagentService(config.workdir, self.engine, self.workspace)
        self.registry = ToolRegistry(self.mcp)
        register_builtins(
            self.registry,
            workspace=self.workspace,
            tasks=self.tasks,
            context=self.context,
            subagents=self.subagents,
            scheduler=self.scheduler,
            team=self.team,
            mcp=self.mcp,
        )
        self.subagents.registry = self.registry
        self._unacknowledged_jobs = []
        self._rounds_without_todo = 0
        self.hooks.register("PostToolUse", self._track_todo_updates)

    def _track_todo_updates(self, block, output):
        self._rounds_without_todo = (
            0 if block.name == "todo_write" else self._rounds_without_todo + 1
        )

    def acknowledge_scheduled(self):
        self.scheduler.acknowledge_cron_jobs(self._unacknowledged_jobs)
        self._unacknowledged_jobs.clear()

    def system_prompt(self, history):
        return self.context.assemble_system_prompt(
            {
                "memory_catalog": self.memory.read_memory_index(),
                "memories": self.memory.load_memories(history),
                "connected_mcp": list(self.mcp.mcp_clients),
                "active_teammates": list(self.coordination.active_teammates),
            }
        )

    def inject_events(self, history):
        if self._rounds_without_todo >= 3:
            history.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
            self._rounds_without_todo = 0
        jobs = self.scheduler.consume_cron_queue()
        self._unacknowledged_jobs.extend(jobs)
        for job in jobs:
            history.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            self.emit("scheduled", {"prompt": job.prompt, "id": job.id})
        inbox = self.team.consume_lead_inbox()
        if inbox:
            history.append({"role": "user", "content": self.team.format_team_events(inbox)})
            self.emit("team_events", {"messages": inbox})
        notes = self.background.collect_background_results()
        if notes:
            history.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": note} for note in notes],
                }
            )
            self.emit("background_completed", {"notifications": notes})
        return "\n".join(f"Run scheduled task: {job.prompt}" for job in jobs)

    def run(self, history, request, checkpoint=None):
        self.hooks.trigger("UserPromptSubmit", request)
        try:
            result = self.engine.run(
                history,
                request,
                tool_pool=self.registry.snapshot,
                system_prompt=self.system_prompt,
                before_turn=self.inject_events,
                after_model=self.acknowledge_scheduled,
                checkpoint=checkpoint,
            )
            self.hooks.trigger("Stop", history)
            if not self.cancelled.is_set() and self.memory.extract_memories(history):
                self.memory.consolidate_memories()
            return result
        except Exception:
            self.scheduler.restore_cron_jobs(self._unacknowledged_jobs)
            self._unacknowledged_jobs.clear()
            raise
        finally:
            self.tasks.release_completed_assignment("agent")

    def has_events(self):
        return bool(
            self.scheduler.cron_queue
            or self.team.BUS.peek("lead")
            or self.background.has_pending_background()
        )

    def summary(self):
        return {
            "tasks": [asdict(task) for task in self.tasks.list_tasks()],
            "todos": self.tasks.CURRENT_TODOS,
            "teammates": dict(self.coordination.active_teammates),
            "crons": [asdict(job) for job in self.scheduler.scheduled_jobs.values()],
            "memory_catalog": self.memory.read_memory_index(),
        }

    def cancel(self):
        self.cancelled.set()
        self.workspace._stop_all_shell_processes()

    def close(self):
        self.shutdown.set()
        self.cancel()
        thread = getattr(self.scheduler, "thread", None)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        close_client = getattr(self.gateway.client, "close", None)
        if close_client:
            close_client()
