import random
import re
import threading
from pathlib import Path
from ..core.models import Task, ProtocolState
from ..infrastructure.mailbox import MessageBus


class TeamService:
    def __init__(
        self,
        config,
        state_dir,
        tasks,
        workspace,
        gateway,
        dispatcher,
        coordination,
        stop_event,
    ):
        self.config = config
        self.state_dir = state_dir
        self.tasks = tasks
        self.workspace = workspace
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.coordination = coordination
        self.stop_event = stop_event
        self._init_teams()

    def _init_teams(self):
        self.VALID_AGENT_NAME = re.compile("^[A-Za-z0-9_-]{1,64}$")
        self.RESERVED_TEAMMATE_NAMES = {"lead", "agent"}
        self.BUS = MessageBus(self.state_dir / "mailboxes")
        self.IDLE_SCAN_INTERVAL = 2.0

    def is_valid_agent_name(self, name: str) -> bool:
        return bool(self.VALID_AGENT_NAME.fullmatch(name))

    def new_request_id(self) -> str:
        while True:
            request_id = f"req_{random.randint(0, 999999):06d}"
            if request_id not in self.coordination.pending_requests:
                return request_id

    def match_response(
        self,
        response_type: str,
        request_id: str,
        approve: bool,
        from_agent: str,
        to_agent: str,
    ) -> bool:
        with self.coordination.team_lock:
            state = self.coordination.pending_requests.get(request_id)
            if not state:
                print(f"  \x1b[31m[protocol] unknown request_id: {request_id}\x1b[0m")
                return False
            expected = {
                "shutdown": "shutdown_response",
                "plan_approval": "plan_approval_response",
            }[state.type]
            if response_type != expected:
                print(f"  \x1b[31m[protocol] expected {expected}, got {response_type}\x1b[0m")
                return False
            if from_agent != state.target or to_agent != state.sender:
                print(f"  \x1b[31m[protocol] {request_id} responder mismatch\x1b[0m")
                return False
            if state.status != "pending":
                return False
            state.status = "approved" if approve else "rejected"
        icon = "approved" if approve else "rejected"
        color = "32" if approve else "31"
        print(
            f"  \x1b[{color}m[protocol] {state.type} {icon} ({request_id}: {state.status})\x1b[0m"
        )
        return True

    def consume_lead_inbox(self, route_protocol=True) -> list[dict]:
        msgs = self.BUS.read_inbox("lead")
        if route_protocol:
            for msg in msgs:
                meta = msg.get("metadata", {})
                req_id = meta.get("request_id", "")
                msg_type = msg.get("type", "")
                if req_id and msg_type.endswith("_response"):
                    self.match_response(
                        msg_type,
                        req_id,
                        meta.get("approve", False),
                        msg.get("from", ""),
                        msg.get("to", ""),
                    )
        return msgs

    def format_team_events(self, msgs: list[dict]) -> str:
        lines = []
        for msg in msgs:
            request_id = msg.get("metadata", {}).get("request_id")
            suffix = f" request_id={request_id}" if request_id else ""
            lines.append(f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}")
        return "[Team events]\n" + "\n".join(lines)

    def scan_unclaimed_tasks(self) -> list[Task]:
        """Return ready tasks whose optional worktree binding is usable."""
        with self.tasks.task_lock:
            ready = []
            for task in self.tasks.list_tasks():
                if (
                    task.status != "pending"
                    or task.owner is not None
                    or (not self.tasks.can_start(task.id))
                ):
                    continue
                _, error = self.tasks.task_worktree_cwd(task)
                if not error:
                    ready.append(task)
            return ready

    def claim_next_task(self, name: str) -> Task | None:
        """Claim the first still-available task, never a second assignment."""
        with self.tasks.task_lock:
            if self.tasks.teammate_assignments.get(name) or self.tasks._owner_in_progress(name):
                return None
        for task in self.scan_unclaimed_tasks():
            result = self.tasks.claim_task(task.id, owner=name)
            if result.startswith("Claimed "):
                return self.tasks.load_task(task.id)
        return None

    def _last_assistant_text(self, content) -> str:
        for block in content:
            if getattr(block, "type", None) == "text":
                return block.text.strip()
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", "")).strip()
        return ""

    def current_work_identity(self, owner: str) -> tuple[int, str | None]:
        with self.tasks.task_lock:
            assignment = self.tasks.teammate_assignments.get(owner)
            task_id = str(assignment["task_id"]) if assignment else None
            return (self.tasks.assignment_versions.get(owner, 0), task_id)

    def _run_teammate_tool(self, name: str, block, handlers: dict) -> str:
        gate = self.coordination.plan_gates.get(name, "not_required")
        if block.name in {"bash", "write_file", "edit_file"} and gate not in {
            "not_required",
            "approved",
        }:
            return f"Blocked: plan status is {gate}."
        return self.dispatcher.execute(
            {"id": block.id, "name": block.name, "input": block.input},
            handlers,
            allow_background=False,
        )

    def apply_plan_response(self, name: str, msg: dict) -> tuple[bool, str]:
        """Apply only the Lead response for this teammate's current plan."""
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id", "")
        work_version, task_id = self.current_work_identity(name)
        with self.coordination.team_lock:
            state = self.coordination.pending_requests.get(request_id)
            expected_id = self.coordination.plan_request_ids.get(name)
            valid = (
                msg.get("from") == "lead"
                and msg.get("to") == name
                and (request_id == expected_id)
                and (state is not None)
                and (state.type == "plan_approval")
                and (state.sender == name)
                and (state.target == "lead")
                and (state.work_version == work_version)
                and (state.task_id == task_id)
                and (state.status in {"approved", "rejected"})
                and (metadata.get("approve", False) == (state.status == "approved"))
            )
            if not valid:
                return (False, "[Ignored plan response: request mismatch]")
            self.coordination.plan_gates[name] = state.status
            self.coordination.active_teammates[name] = "working"
            self.coordination.plan_request_ids.pop(name, None)
            outcome = state.status
        return (True, f"[Plan {outcome}] {msg['content']}")

    def apply_shutdown_request(self, name: str, msg: dict) -> tuple[bool, str]:
        """Accept only a pending shutdown request sent by Lead to this teammate."""
        request_id = msg.get("metadata", {}).get("request_id", "")
        with self.coordination.team_lock:
            state = self.coordination.pending_requests.get(request_id)
            valid = (
                msg.get("from") == "lead"
                and msg.get("to") == name
                and (state is not None)
                and (state.type == "shutdown")
                and (state.sender == "lead")
                and (state.target == name)
                and (state.status == "pending")
                and (self.coordination.active_teammates.get(name) != "stopping")
            )
            if not valid:
                return (False, "[Ignored shutdown request: request mismatch]")
            self.coordination.active_teammates[name] = "stopping"
        return (True, request_id)

    def _teammate_send_message(self, from_name: str, to: str, content: str) -> str:
        with self.coordination.team_lock:
            if to != "lead" and to not in self.coordination.active_teammates:
                return f"Agent '{to}' is not active"
        self.BUS.send(from_name, to, content)
        return f"Sent to {to}"

    def spawn_teammate_thread(
        self,
        name: str,
        role: str,
        prompt: str,
        task_id: str | None = None,
        require_plan: bool = False,
    ) -> str:
        if not self.is_valid_agent_name(name):
            return "Invalid teammate name: use 1-64 letters, digits, underscores, or dashes"
        if name.lower() in self.RESERVED_TEAMMATE_NAMES:
            return f"Invalid teammate name: '{name}' is reserved by the runtime"
        with self.coordination.team_lock:
            if any(
                (
                    existing.casefold() == name.casefold()
                    for existing in self.coordination.active_teammates
                )
            ):
                return f"Teammate '{name}' already exists"
            self.coordination.active_teammates[name] = "working"
            self.coordination.plan_gates[name] = "required" if require_plan else "not_required"
            self.tasks.assignment_versions[name] = 0
        if task_id:
            try:
                claimed = self.tasks.claim_task(task_id, owner=name)
            except (FileNotFoundError, ValueError) as exc:
                claimed = f"Error: {exc}"
            if not claimed.startswith("Claimed "):
                with self.coordination.team_lock:
                    self.coordination.active_teammates.pop(name, None)
                    self.coordination.plan_gates.pop(name, None)
                    self.tasks.assignment_versions.pop(name, None)
                return f"Cannot spawn teammate '{name}': {claimed}"
        system = f"You are '{name}', a {role}. Use tools to complete tasks. You can list and claim tasks from the board. If the initial message contains [Assigned task], it is already claimed; do not call claim_task for it again. The runtime runs every filesystem tool in the claimed task's working directory. When asked for a plan, submit it before bash, write_file, or edit_file and wait for approval. The runtime delivers your final text to Lead. Use send_message only for intermediate coordination, and address the coordinator as 'lead'."

        def handle_inbox_message(name: str, msg: dict, messages: list):
            msg_type = msg.get("type", "message")
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            if msg_type == "shutdown_request":
                accepted, notice = self.apply_shutdown_request(name, msg)
                if not accepted:
                    messages.append({"role": "user", "content": notice})
                    return False
                req_id = notice
                self.BUS.send(
                    name,
                    "lead",
                    "Shutting down gracefully.",
                    "shutdown_response",
                    {"request_id": req_id, "approve": True},
                )
                print(f"  \x1b[35m[protocol] {name} approved shutdown ({req_id})\x1b[0m")
                return True
            if msg_type == "plan_approval_response":
                _, notice = self.apply_plan_response(name, msg)
                messages.append({"role": "user", "content": notice})
            elif msg_type == "plan_request":
                messages.append({"role": "user", "content": f"[Plan required] {msg['content']}"})
            elif msg_type == "message":
                messages.append(
                    {
                        "role": "user",
                        "content": f"[Message from {msg['from']}] {msg['content']}",
                    }
                )
            return False

        def run_loop():

            def current_cwd() -> tuple[Path | None, str | None]:
                if name not in self.tasks.teammate_assignments:
                    return (None, "Error: Claim a Task before using workspace tools.")
                try:
                    return (self.tasks.assignment_cwd(name), None)
                except (FileNotFoundError, ValueError) as exc:
                    return (None, f"Error: Invalid task assignment: {exc}")

            def _run_bash(command: str) -> str:
                cwd, error = current_cwd()
                return error or self.workspace.run_bash(command, cwd=cwd)

            def _run_read(path: str, limit: int | None = None, offset: int = 0) -> str:
                cwd, error = current_cwd()
                return error or self.workspace.run_read(path, limit=limit, offset=offset, cwd=cwd)

            def _run_write(path: str, content: str) -> str:
                cwd, error = current_cwd()
                return error or self.workspace.run_write(path, content, cwd=cwd)

            def _run_edit(path: str, old_text: str, new_text: str) -> str:
                cwd, error = current_cwd()
                return error or self.workspace.run_edit(path, old_text, new_text, cwd=cwd)

            def _run_glob(pattern: str) -> str:
                cwd, error = current_cwd()
                return error or self.workspace.run_glob(pattern, cwd=cwd)

            def _run_list_tasks():
                tasks = self.tasks.list_tasks()
                if not tasks:
                    return "No tasks."
                return "\n".join(
                    (
                        f"  {t.id}: {t.subject} [{t.status}]"
                        + (f" (wt:{t.worktree})" if t.worktree else "")
                        for t in tasks
                    )
                )

            def _run_claim_task(task_id: str):
                try:
                    return self.tasks.claim_task(task_id, owner=name)
                except ValueError as exc:
                    return f"Error: {exc}"
                except FileNotFoundError:
                    return f"Error: Task {task_id} not found"

            def _run_complete_task(task_id: str):
                try:
                    return self.tasks.complete_task(task_id, owner=name)
                except ValueError as exc:
                    return f"Error: {exc}"
                except FileNotFoundError:
                    return f"Error: Task {task_id} not found"

            initial_prompt = prompt
            if task_id:
                task = self.tasks.load_task(task_id)
                initial_prompt += f"\n\n[Assigned task {task.id}] {task.subject}\n{task.description}\nWork directory: {self.tasks.assignment_cwd(name)}"
            if require_plan:
                initial_prompt += "\n\n[Plan required] Submit a plan and wait for Lead approval before bash, write_file, or edit_file."
            messages = [{"role": "user", "content": initial_prompt}]
            sub_tools = [
                {
                    "name": "bash",
                    "description": "Run a shell command.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
                {
                    "name": "read_file",
                    "description": "Read file.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "limit": {"type": "integer"},
                            "offset": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                },
                {
                    "name": "write_file",
                    "description": "Write file.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
                {
                    "name": "edit_file",
                    "description": "Replace text in a file.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["path", "old_text", "new_text"],
                    },
                },
                {
                    "name": "glob",
                    "description": "Find files by glob pattern; ** matches recursively.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"pattern": {"type": "string"}},
                        "required": ["pattern"],
                    },
                },
                {
                    "name": "send_message",
                    "description": "Send an intermediate message to 'lead' or an active teammate.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["to", "content"],
                    },
                },
                {
                    "name": "submit_plan",
                    "description": "Submit a plan for Lead approval.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"plan": {"type": "string"}},
                        "required": ["plan"],
                    },
                },
                {
                    "name": "list_tasks",
                    "description": "List all tasks on the board.",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
                {
                    "name": "claim_task",
                    "description": "Claim a pending task.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                },
                {
                    "name": "complete_task",
                    "description": "Mark an in-progress task as completed.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"task_id": {"type": "string"}},
                        "required": ["task_id"],
                    },
                },
            ]
            sub_handlers = {
                "bash": _run_bash,
                "read_file": _run_read,
                "write_file": _run_write,
                "edit_file": _run_edit,
                "glob": _run_glob,
                "send_message": lambda to, content: self._teammate_send_message(name, to, content),
                "submit_plan": lambda plan: self._teammate_submit_plan(name, plan),
                "list_tasks": _run_list_tasks,
                "claim_task": _run_claim_task,
                "complete_task": _run_complete_task,
            }
            should_stop = False
            while not should_stop and (not self.stop_event.is_set()):
                for msg in self.BUS.read_inbox(name):
                    if handle_inbox_message(name, msg, messages):
                        should_stop = True
                        break
                if should_stop:
                    break
                with self.coordination.team_lock:
                    self.coordination.active_teammates[name] = "working"
                try:
                    response = self.gateway.client.messages.create(
                        model=self.config.model,
                        system=system,
                        messages=messages,
                        tools=sub_tools,
                        max_tokens=8000,
                    )
                except Exception as exc:
                    self.BUS.send(name, "lead", f"{type(exc).__name__}: {exc}", "error")
                    break
                messages.append({"role": "assistant", "content": response.content})
                tool_calls = [block for block in response.content if block.type == "tool_use"]
                if tool_calls:
                    results = []
                    for block in tool_calls:
                        output = self._run_teammate_tool(name, block, sub_handlers)
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(output),
                            }
                        )
                    messages.append({"role": "user", "content": results})
                    continue
                summary = self._last_assistant_text(response.content)
                gate = self.coordination.plan_gates.get(name, "not_required")
                if gate != "pending" and summary:
                    self.BUS.send(name, "lead", summary, "result")
                if gate == "pending":
                    with self.coordination.team_lock:
                        self.coordination.active_teammates[name] = "waiting_approval"
                else:
                    self.tasks.release_completed_assignment(name)
                    with self.coordination.team_lock:
                        self.coordination.active_teammates[name] = "idle"
                    self.BUS.send(name, "lead", "Waiting for more work.", "idle_notification")
                while not self.stop_event.is_set():
                    inbox = self.BUS.wait_for_messages(name, self.IDLE_SCAN_INTERVAL)
                    if inbox:
                        for msg in inbox:
                            if handle_inbox_message(name, msg, messages):
                                should_stop = True
                                break
                        if should_stop or messages[-1]["role"] == "user":
                            break
                        continue
                    task = self.claim_next_task(name)
                    if not task:
                        continue
                    try:
                        workdir = str(self.tasks.assignment_cwd(name))
                    except (FileNotFoundError, ValueError) as exc:
                        workdir = f"unavailable ({exc})"
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[Auto-claimed task {task.id}] {task.subject}\n{task.description}\nWork directory: {workdir}",
                        }
                    )
                    print(f"  \x1b[32m[idle] {name} claimed {task.id}: {task.subject}\x1b[0m")
                    break

        def run():
            try:
                run_loop()
            except Exception as exc:
                try:
                    self.BUS.send(name, "lead", f"{type(exc).__name__}: {exc}", "error")
                except Exception:
                    pass
            finally:
                try:
                    self.tasks.release_teammate_assignment(name)
                except Exception as exc:
                    try:
                        self.BUS.send(
                            name,
                            "lead",
                            f"Assignment cleanup failed: {type(exc).__name__}: {exc}",
                            "error",
                        )
                    except Exception:
                        pass
                with self.coordination.team_lock:
                    self.coordination.active_teammates.pop(name, None)
                    self.coordination.plan_gates.pop(name, None)
                    self.coordination.plan_request_ids.pop(name, None)
                print(f"  \x1b[32m[teammate] {name} finished\x1b[0m")

        threading.Thread(target=run, daemon=True).start()
        print(f"  \x1b[36m[teammate] {name} spawned as {role}\x1b[0m")
        assigned = f" for {task_id}" if task_id else " without an initial Task"
        return f"Teammate '{name}' spawned as {role}{assigned}. End this turn; the runtime will deliver its events."

    def _teammate_submit_plan(self, from_name: str, plan: str) -> str:
        with self.tasks.task_lock:
            assignment = self.tasks.teammate_assignments.get(from_name)
            task_id = str(assignment["task_id"]) if assignment else None
            work_version = self.tasks.assignment_versions.get(from_name, 0)
            with self.coordination.team_lock:
                if self.coordination.plan_gates.get(from_name) == "pending":
                    return "A plan is already waiting for review."
                req_id = self.new_request_id()
                self.coordination.pending_requests[req_id] = ProtocolState(
                    request_id=req_id,
                    type="plan_approval",
                    sender=from_name,
                    target="lead",
                    status="pending",
                    payload=plan,
                    work_version=work_version,
                    task_id=task_id,
                )
                self.coordination.plan_gates[from_name] = "pending"
                self.coordination.plan_request_ids[from_name] = req_id
                self.coordination.active_teammates[from_name] = "waiting_approval"
        self.BUS.send(from_name, "lead", plan, "plan_approval_request", {"request_id": req_id})
        return f"Plan submitted ({req_id}). Wait for Lead's decision."

    def run_request_shutdown(self, teammate: str) -> str:
        if teammate not in self.coordination.active_teammates:
            return f"Teammate '{teammate}' is not active"
        with self.coordination.team_lock:
            req_id = self.new_request_id()
            self.coordination.pending_requests[req_id] = ProtocolState(
                request_id=req_id,
                type="shutdown",
                sender="lead",
                target=teammate,
                status="pending",
                payload="",
            )
        self.BUS.send(
            "lead",
            teammate,
            "Finish the current step and shut down.",
            "shutdown_request",
            {"request_id": req_id},
        )
        print(f"  \x1b[35m[protocol] shutdown_request -> {teammate} ({req_id})\x1b[0m")
        return f"Shutdown requested from {teammate} ({req_id})"

    def run_request_plan(self, teammate: str, task: str) -> str:
        if teammate not in self.coordination.active_teammates:
            return f"Teammate '{teammate}' is not active"
        with self.coordination.team_lock:
            self.coordination.plan_gates[teammate] = "required"
        self.BUS.send("lead", teammate, task, "plan_request")
        return f"Plan requested from {teammate}"

    def run_review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        state = self.coordination.pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        work_version, task_id = self.current_work_identity(state.sender)
        with self.coordination.team_lock:
            state = self.coordination.pending_requests.get(request_id)
            if not state:
                return f"Request {request_id} not found"
            if state.type != "plan_approval":
                return f"Request {request_id} is not a plan"
            if state.status != "pending":
                return f"Request {request_id} already {state.status}"
            if state.work_version != work_version or state.task_id != task_id:
                return f"Request {request_id} belongs to an earlier assignment"
            if self.coordination.plan_request_ids.get(state.sender) != request_id:
                return f"Request {request_id} is not the current plan"
            state.status = "approved" if approve else "rejected"
        content = feedback or (
            "Plan approved." if approve else "Revise the plan and submit it again."
        )
        self.BUS.send(
            "lead",
            state.sender,
            content,
            "plan_approval_response",
            {"request_id": request_id, "approve": approve},
        )
        icon = "approved" if approve else "rejected"
        print(f"  \x1b[32m[protocol] plan {icon} ({request_id})\x1b[0m")
        return f"Plan {state.status} ({request_id})"

    def run_spawn_teammate(
        self,
        name: str,
        role: str,
        prompt: str,
        task_id: str | None = None,
        require_plan: bool = False,
    ) -> str:
        return self.spawn_teammate_thread(name, role, prompt, task_id, require_plan)

    def run_list_teammates(self) -> str:
        with self.coordination.team_lock:
            if not self.coordination.active_teammates:
                return "No active teammates."
            return "\n".join(
                (
                    f"{name}: {status}"
                    for name, status in sorted(self.coordination.active_teammates.items())
                )
            )

    def run_send_message(self, to: str, content: str) -> str:
        if to not in self.coordination.active_teammates:
            return f"Teammate '{to}' is not active"
        self.BUS.send("lead", to, content)
        return f"Sent to {to}"
