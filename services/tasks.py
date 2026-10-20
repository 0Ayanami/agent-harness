import ast
import json
from pathlib import Path
from ..core.models import Task
from ..infrastructure.tasks import TaskRepository
from ..infrastructure.git import GitWorktreeAdapter


class TaskService:
    """Dependency, ownership and worktree policies over task and Git adapters."""

    def create_task(self, subject: str, description: str = "") -> Task:
        return self.repository.create_task(subject, description)

    def load_task(self, task_id: str) -> Task:
        return self.repository.load_task(task_id)

    def list_tasks(self) -> list[Task]:
        return self.repository.list_tasks()

    def __init__(self, config, state_dir, coordination, background_state):
        self.config = config
        self.coordination = coordination
        self.background_state = background_state
        self.repository = TaskRepository(config, state_dir / "tasks")
        self.git = GitWorktreeAdapter(config)
        self.task_lock = self.repository.lock
        self.CURRENT_TODOS = []
        self.teammate_assignments = {}
        self.assignment_versions = {}

    def agent_cwd(self):
        try:
            return (self.assignment_cwd("agent"), None)
        except (FileNotFoundError, ValueError) as error:
            return (None, f"Error: Invalid task assignment: {error}")

    def advance_assignment_version(self, owner: str):
        """Invalidate old approvals without clearing an explicit plan requirement."""
        with self.task_lock:
            self.assignment_versions[owner] = self.assignment_versions.get(owner, 0) + 1
            gates = self.coordination.plan_gates
            request_ids = self.coordination.plan_request_ids
            team = self.coordination.team_lock
            if team is not None:
                team.acquire()
            try:
                if isinstance(gates, dict) and owner in gates and (gates[owner] != "not_required"):
                    gates[owner] = "required"
                if isinstance(request_ids, dict):
                    request_ids.pop(owner, None)
            finally:
                if team is not None:
                    team.release()

    def _task_depends_on(self, task_id: str, target_id: str) -> bool:
        """Return whether task_id transitively depends on target_id."""
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.repository.load_task(current).blockedBy)
        return False

    def update_task(self, task_id: str, addBlockedBy: list[str]) -> Task:
        """Add dependency edges after create_task has returned real task IDs."""
        if not isinstance(addBlockedBy, list):
            raise ValueError("addBlockedBy must be a list of task IDs")
        with self.repository.transaction():
            task = self.repository.load_task(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    f"Task {task_id} dependencies can only be updated while pending and unowned"
                )
            dependencies = list(dict.fromkeys(addBlockedBy))
            for dependency in dependencies:
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself")
                if not self.repository.path_for(dependency).is_file():
                    raise ValueError(f"Dependency not found: {dependency}")
                if dependency not in task.blockedBy and self._task_depends_on(dependency, task_id):
                    raise ValueError(f"Dependency cycle detected: {task_id} -> {dependency}")
            task.blockedBy.extend(
                (dependency for dependency in dependencies if dependency not in task.blockedBy)
            )
            self.repository.save_task(task)
            return task

    def can_start(self, task_id: str) -> bool:
        task = self.repository.load_task(task_id)
        for dep_id in task.blockedBy:
            try:
                dep_path = self.repository.path_for(dep_id)
            except ValueError:
                return False
            if not dep_path.exists():
                return False
            if self.repository.load_task(dep_id).status != "completed":
                return False
        return True

    def _owner_in_progress(self, owner: str) -> Task | None:
        return next(
            (
                task
                for task in self.repository.list_tasks()
                if task.status == "in_progress" and task.owner == owner
            ),
            None,
        )

    def _incomplete_dependencies(self, task: Task) -> list[str]:
        incomplete = []
        for dep_id in task.blockedBy:
            try:
                dep_path = self.repository.path_for(dep_id)
            except ValueError:
                incomplete.append(dep_id)
                continue
            if not dep_path.exists() or self.repository.load_task(dep_id).status != "completed":
                incomplete.append(dep_id)
        return incomplete

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        """Atomically claim one task and bind the owner's filesystem cwd."""
        with self.repository.transaction():
            task = self.repository.load_task(task_id)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"
            if task.owner:
                return f"Task {task_id} is already owned by {task.owner}"
            assignment = self.teammate_assignments.get(owner)
            if assignment:
                return f"Owner {owner} must finish the current work turn for {assignment['task_id']} before claiming another task"
            current = self._owner_in_progress(owner)
            if current:
                return f"Owner {owner} must complete {current.id} before claiming another task"
            if not self.can_start(task_id):
                return f"Blocked by: {self._incomplete_dependencies(task)}"
            cwd, error = self.task_worktree_cwd(task)
            if error:
                return f"Cannot claim {task_id}: {error}"
            task.owner = owner
            task.status = "in_progress"
            self.repository.save_task(task)
            self.teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
            self.advance_assignment_version(owner)
        print(f"  \x1b[36m[claim] {task.subject} -> in_progress (owner: {owner})\x1b[0m")
        return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str, owner: str = "agent") -> str:
        """Complete an assignment only when the caller owns it."""
        with self.repository.transaction():
            task = self.repository.load_task(task_id)
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}; cannot complete"
            gate = self.coordination.plan_gates.get(owner, "not_required")
            if gate in {"required", "pending", "rejected"}:
                return f"Task {task_id} cannot complete while plan status is {gate}"
            assignment = self.teammate_assignments.get(owner)
            if not assignment or assignment.get("task_id") != task.id:
                cwd, error = self.task_worktree_cwd(task)
                if error:
                    return f"Task {task_id} cannot complete: {error}"
                self.teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
            task.status = "completed"
            self.repository.save_task(task)
            unblocked = [
                t.subject
                for t in self.repository.list_tasks()
                if t.status == "pending" and t.blockedBy and self.can_start(t.id)
            ]
        print(f"  \x1b[32m[complete] {task.subject}\x1b[0m")
        msg = f"Completed {task.id} ({task.subject})"
        if unblocked:
            msg += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"  \x1b[33m[unblocked] {', '.join(unblocked)}\x1b[0m")
        return msg

    def task_worktree_cwd(self, task: Task) -> tuple[Path, str | None]:
        """Resolve a task cwd, failing closed for broken worktree bindings."""
        if not task.worktree:
            return (self.config.workdir, None)
        path, error = self.git.registered(task.worktree)
        return (path or self.config.workdir, error)

    def assignment_cwd(self, owner: str) -> Path:
        with self.task_lock:
            assignment = self.teammate_assignments.get(owner)
            task = self._owner_in_progress(owner)
            if task and (not assignment or assignment.get("task_id") != task.id):
                cwd, error = self.task_worktree_cwd(task)
                if error:
                    raise ValueError(error)
                assignment = {"task_id": task.id, "cwd": cwd}
                self.teammate_assignments[owner] = assignment
            elif not assignment:
                return self.config.workdir
            task = self.repository.load_task(str(assignment["task_id"]))
            if task.status not in {"in_progress", "completed"} or task.owner != owner:
                raise ValueError(f"Assignment for {owner} is no longer active")
            cwd, error = self.task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            if cwd.resolve() != Path(assignment["cwd"]).resolve():
                raise ValueError(f"Assignment cwd changed for task {task.id}")
            return cwd

    def release_completed_assignment(self, owner: str) -> bool:
        """Release a completed cwd lease only at a model turn boundary."""
        with self.task_lock:
            assignment = self.teammate_assignments.get(owner)
            if not assignment:
                return False
            task = self.repository.load_task(str(assignment["task_id"]))
            if task.status != "completed" or task.owner != owner:
                return False
            self.teammate_assignments.pop(owner, None)
            self.advance_assignment_version(owner)
            if owner in self.coordination.plan_gates:
                self.coordination.plan_gates[owner] = "not_required"
            return True

    def release_teammate_assignment(self, owner: str):
        """Return abandoned teammate work to the task board on thread exit."""
        with self.task_lock:
            try:
                task = self._owner_in_progress(owner)
                if task:
                    task.status = "pending"
                    task.owner = None
                    self.repository.save_task(task)
            finally:
                self.teammate_assignments.pop(owner, None)
                self.advance_assignment_version(owner)
                if owner in self.coordination.plan_gates:
                    self.coordination.plan_gates[owner] = "not_required"

    def create_worktree(self, name: str, task_id: str) -> str:
        """Create and bind a dedicated worktree after all inputs validate."""
        error = self.git.validate_name(name)
        if error:
            return f"Error: {error}"
        try:
            path = self.git.path_for(name)
            task_path = self.repository.path_for(task_id)
        except ValueError as exc:
            return f"Error: {exc}"
        branch = self.git.branch_for(name)
        with self.task_lock:
            if not task_path.exists():
                return f"Error: Task {task_id} not found"
            task = self.repository.load_task(task_id)
            if task.status != "pending" or task.owner is not None:
                return f"Error: Task {task_id} must be pending and unowned"
            if task.worktree:
                return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
            if any((t.worktree == name for t in self.repository.list_tasks() if t.id != task_id)):
                return f"Error: Worktree '{name}' is already bound to another task"
            if path.exists():
                return f"Error: Worktree path already exists: {path}"
            ok, root = self.git.run_checked(["rev-parse", "--show-toplevel"])
            if not ok or Path(root).resolve() != self.config.workdir.resolve():
                return "Error: Working directory must be the root of a Git repository"
            ok, branch_check = self.git.run_checked(["check-ref-format", "--branch", branch])
            if not ok:
                return f"Error: Invalid worktree branch '{branch}': {branch_check}"
            exists, _ = self.git.run_checked(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
            )
            if exists:
                return f"Error: Branch '{branch}' already exists"
            entries, registry_error = self.git.list_registered()
            if registry_error:
                return f"Error: {registry_error}"
            if path in entries:
                return f"Error: Worktree path is already registered: {path}"
            self.git.directory.mkdir(parents=True, exist_ok=True)
            ok, result = self.git.run_checked(["worktree", "add", "-b", branch, str(path), "HEAD"])
            if not ok:
                entries, registry_error = self.git.list_registered()
                branch_exists, _ = self.git.run_checked(
                    ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
                )
                artifacts = []
                if path.exists():
                    artifacts.append(f"checkout path '{path}'")
                if registry_error is None and path in entries:
                    artifacts.append("registered Git worktree")
                if branch_exists:
                    artifacts.append(f"branch '{branch}'")
                if artifacts:
                    return f"Partial operation: git worktree add reported an error after leaving {', '.join(artifacts)}. Task {task_id} remains unbound and no Git data was deleted. Run `git worktree list`, inspect '{path}' and '{branch}', then keep or remove those artifacts manually after preserving any work. Git error: {result}"
                return f"Git error: {result}"
            try:
                task.worktree = name
                self.repository.save_task(task)
            except Exception as exc:
                return f"Partial success: Worktree '{name}' was created at {path} on branch '{branch}', but task binding failed: {exc}. Git data was retained for manual recovery."
        print(f"  \x1b[33m[worktree] created: {name} at {path}\x1b[0m")
        return f"Worktree '{name}' created at {path} for task {task_id}"

    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        """Remove a registered checkout while always retaining its branch."""
        error = self.git.validate_name(name)
        if error:
            return f"Error: {error}"
        with self.task_lock:
            path, error = self.git.registered(name)
            if error:
                return f"Error: {error}"
            bound = [task for task in self.repository.list_tasks() if task.worktree == name]
            if not bound:
                return f"Error: Worktree '{name}' is not bound to a task"
            active = [task for task in bound if task.status != "completed"]
            if active:
                return f"Error: Worktree '{name}' is bound to active task {active[0].id}; complete it before removal"
            leased = [
                owner
                for owner, assignment in self.teammate_assignments.items()
                if Path(assignment["cwd"]).resolve() == path.resolve()
            ]
            if leased:
                return f"Error: Worktree '{name}' is still in use by {', '.join(sorted(leased))}; wait for the turn to end"
            with self.background_state.lock:
                running = [
                    task
                    for task in self.background_state.tasks.values()
                    if task.get("status") == "running"
                    and task.get("cwd")
                    and (Path(task["cwd"]).resolve() == path.resolve())
                ]
            if running:
                return f"Error: Worktree '{name}' has a running background command; wait for it to finish"
            ok, status = self.git.run_checked(["status", "--porcelain", "--ignored"], cwd=path)
            if not ok:
                return f"Error: Cannot verify worktree '{name}' status: {status}"
            if status != "(no output)" and (not discard_changes):
                changed = len([line for line in status.splitlines() if line.strip()])
                return f"Error: Worktree '{name}' has {changed} uncommitted change(s); preserve or discard them manually"
            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            args.append(str(path))
            ok, result = self.git.run_checked(args)
            if not ok:
                return f"Git error: {result}"
            try:
                for task in bound:
                    task.worktree = None
                    self.repository.save_task(task)
            except Exception as exc:
                return f"Partial success: Worktree '{name}' was removed and branch '{self.git.branch_for(name)}' retained, but task unbinding failed: {exc}. Manual recovery is required."
        print(f"  \x1b[33m[worktree] removed: {name}; branch retained\x1b[0m")
        return f"Worktree '{name}' removed; branch '{self.git.branch_for(name)}' retained"

    def _normalize_todos(self, todos):
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError):
                    return (None, "Error: todos must be a list or JSON array string")
        if not isinstance(todos, list):
            return (None, "Error: todos must be a list")
        for i, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return (None, f"Error: todos[{i}] must be an object")
            if "content" not in todo or "status" not in todo:
                return (None, f"Error: todos[{i}] missing 'content' or 'status'")
            if todo["status"] not in ("pending", "in_progress", "completed"):
                return (None, f"Error: todos[{i}] has invalid status '{todo['status']}'")
        return (todos, None)

    def run_todo_write(self, todos: list) -> str:
        todos, error = self._normalize_todos(todos)
        if error:
            return error
        self.CURRENT_TODOS = todos
        print(f"  \x1b[33m[todo] updated {len(self.CURRENT_TODOS)} item(s)\x1b[0m")
        return f"Updated {len(self.CURRENT_TODOS)} todos"

    def run_create_task(self, subject: str, description: str = "") -> str:
        task = self.repository.create_task(subject, description)
        print(f"  \x1b[34m[create] {task.subject}\x1b[0m")
        return f"Created {task.id}: {task.subject}"

    def run_update_task(self, task_id: str, addBlockedBy: list[str]) -> str:
        try:
            task = self.update_task(task_id, addBlockedBy)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"
        dependencies = ", ".join(task.blockedBy) or "(none)"
        print(f"  \x1b[34m[update] {task.subject} blockedBy: {dependencies}\x1b[0m")
        return f"Updated {task.id} blockedBy: {dependencies}"

    def run_list_tasks(self) -> str:
        tasks = self.repository.list_tasks()
        if not tasks:
            return "No tasks."
        return "\n".join(
            (
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks
            )
        )

    def run_get_task(self, task_id: str) -> str:
        try:
            return self.repository.get_task_json(task_id)
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def run_claim_task(self, task_id: str) -> str:
        try:
            return self.claim_task(task_id, owner="agent")
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def run_complete_task(self, task_id: str) -> str:
        try:
            return self.complete_task(task_id, owner="agent")
        except ValueError as exc:
            return f"Error: {exc}"
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
