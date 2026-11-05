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
from ..services.library_rag import LIBRARY_TOOL_NAMES, LibraryRAG, library_options
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
        retrieval=None,
        personalized=True,
        temporary=False,
    ):
        self.config = config
        self.state_dir = state_dir or config.data_dir / "cli"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._event_sink = emit
        self.shutdown = threading.Event()
        self.cancelled = threading.Event()
        self.rag = LibraryRAG(
            retrieval, self.emit, self.cancelled, context_limit=config.context_limit
        )
        self.coordination = CoordinationState()
        background_state = BackgroundState()
        self.gateway = ModelGateway(config, client)
        self.set_memory_options(personalized=personalized, temporary=temporary)
        self.memory = MemoryStore(config.workdir, client, config.model, lock=memory_lock)
        self.workspace = WorkspaceTools(config.workdir, memory_allowed=self._memory_allowed)
        self.tasks = TaskService(config, self.state_dir, self.coordination, background_state)
        self.context = ContextService(config, self.state_dir, self.memory, self.gateway)
        self.mcp = MCPService()
        self.mcp.mcp_tool_policies = {}
        self.hooks = HookRegistry()
        self.permissions = PermissionPolicy(
            config.workdir, approve or (lambda name, args: False), self.mcp
        )
        self.hooks.register("PreToolUse", self.permissions.check)
        self.dispatcher = ToolDispatcher(self.hooks, self.emit, self.cancelled)
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
            config, self.gateway, self.dispatcher, self.context, self.emit, self.cancelled
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
        if retrieval is not None:
            self.rag.register(self.registry)
        self.subagents.registry = self.registry
        self._unacknowledged_jobs = []
        self._rounds_without_todo = 0
        self.hooks.register("PostToolUse", self._track_todo_updates)

    def set_memory_options(self, *, personalized, temporary):
        self._memory_options = personalized, temporary

    def _memory_allowed(self, *, write=False):
        personalized, temporary = self._memory_options
        return personalized and (not write or not temporary)

    def emit(self, kind, payload):
        # Library drafts and unchecked citation markers stay inside this model turn.
        # The finished answer is emitted after revalidating its sources.
        if self.rag.active and kind == "assistant" and payload.get("scope") != "subagent":
            return
        self._event_sink(kind, payload)

    def _tool_pool(self):
        definitions, handlers = self.registry.snapshot()
        if self.rag.active:
            definitions = [item for item in definitions if item["name"] in LIBRARY_TOOL_NAMES]
        else:
            definitions = [item for item in definitions if item["name"] not in LIBRARY_TOOL_NAMES]
        names = {item["name"] for item in definitions}
        return definitions, {name: handler for name, handler in handlers.items() if name in names}

    def _track_todo_updates(self, block, output):
        self._rounds_without_todo = (
            0 if block.name == "todo_write" else self._rounds_without_todo + 1
        )

    def acknowledge_scheduled(self):
        self.scheduler.acknowledge_cron_jobs(self._unacknowledged_jobs)
        self._unacknowledged_jobs.clear()

    def system_prompt(self, history):
        if self.rag.active:
            return self.rag.system_prompt()
        context = {
            "connected_mcp": list(self.mcp.mcp_clients),
            "active_teammates": list(self.coordination.active_teammates),
        }
        if self._memory_allowed():
            context["memory_catalog"] = self.memory.read_memory_index()
            context["memories"] = self.memory.load_memories(history)
        return self.context.assemble_system_prompt(context)

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

    def _finish_library_answer(self, history, result, checkpoint, *, require_citation=False):
        payload = self.rag.answer_payload(result, require_citation=require_citation)
        history.append(
            {"role": "assistant", "content": [{"type": "text", "text": payload["text"]}]}
        )
        if checkpoint:
            checkpoint()
        self._event_sink(
            "assistant",
            {
                "text": payload["text"],
                "scope": "agent",
                "run_id": payload["run_id"],
            },
        )
        self.emit("library_answer", payload)
        return payload["text"]

    def _run_library(self, history, request, options, checkpoint):
        if not self.rag.begin(request, options):
            return self._finish_library_answer(history, self.rag.unavailable_answer(), checkpoint)
        # A new scope must not inherit tool output or raw document text from a
        # previous question. The user/verified answer and trace events persist;
        # model/tool turns themselves are private to the current question.
        current = [{"role": "user", "content": request}]
        result = self.engine.run(
            current,
            request,
            tool_pool=self._tool_pool,
            system_prompt=self.system_prompt,
            max_iterations=8,
            allow_background=False,
        )
        self.rag._check_cancelled()
        if not result.strip() or current[-1].get("role") != "assistant":
            return self._finish_library_answer(
                history,
                "本轮检索已达到执行预算。请缩小问题范围后重试；暂未生成可验证的资料回答。",
                checkpoint,
            )
        return self._finish_library_answer(history, result, checkpoint, require_citation=True)

    def run(self, history, request, checkpoint=None, *, library=None, memory_source=None):
        self.hooks.trigger("UserPromptSubmit", request)
        options = library_options(library)
        rag_turn = options is not None
        try:
            if rag_turn:
                result = self._run_library(history, request, options, checkpoint)
                self.hooks.trigger("Stop", history)
                return result
            result = self.engine.run(
                history,
                request,
                tool_pool=self._tool_pool,
                system_prompt=self.system_prompt,
                before_turn=self.inject_events,
                after_model=self.acknowledge_scheduled,
                checkpoint=checkpoint,
            )
            self.hooks.trigger("Stop", history)
            eligible_messages = history if memory_source is None else memory_source
            if (
                eligible_messages
                and self._memory_allowed(write=True)
                and not self.cancelled.is_set()
                and self.memory.extract_memories(eligible_messages)
            ):
                self.memory.consolidate_memories()
            return result
        except InterruptedError:
            self.scheduler.restore_cron_jobs(self._unacknowledged_jobs)
            self._unacknowledged_jobs.clear()
            raise
        except Exception:
            self.scheduler.restore_cron_jobs(self._unacknowledged_jobs)
            self._unacknowledged_jobs.clear()
            if rag_turn:
                raise RuntimeError("资料库问答执行失败，请检查模型和检索服务后重试。") from None
            raise
        finally:
            if rag_turn:
                self.rag.end()
                self._rounds_without_todo = 0
            self.tasks.release_completed_assignment("agent")

    def has_events(self):
        return bool(
            self.scheduler.cron_queue
            or self.team.BUS.peek("lead")
            or self.background.has_pending_background()
        )

    def has_live_workers(self):
        background = self.background.background_state
        with background.lock:
            if any(task["status"] == "running" for task in background.tasks.values()):
                return True
        with self.coordination.team_lock:
            return bool(self.coordination.active_teammates)

    def summary(self):
        return {
            "tasks": [asdict(task) for task in self.tasks.list_tasks()],
            "todos": self.tasks.CURRENT_TODOS,
            "teammates": dict(self.coordination.active_teammates),
            "crons": [asdict(job) for job in self.scheduler.scheduled_jobs.values()],
            "memory_catalog": self.memory.read_memory_index() if self._memory_allowed() else "",
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
