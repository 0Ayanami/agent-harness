import threading


class BackgroundTasks:
    def __init__(self, workspace, tasks, background_state, hooks):
        self.workspace = workspace
        self.tasks = tasks
        self.background_state = background_state
        self.hooks = hooks
        self._bg_counter = 0

    def should_run_background(self, tool_name: str, tool_input: dict) -> bool:
        return tool_name == "bash" and tool_input.get("run_in_background") is True

    def start_background_task(self, block, handlers: dict) -> str:
        command = block.input.get("command", block.name)
        cwd, cwd_error = self.tasks.agent_cwd()

        def worker():
            try:
                if block.name != "bash":
                    raise ValueError("only bash can run in the background")
                if cwd_error:
                    raise ValueError(cwd_error.removeprefix("Error: "))
                output, exit_code = self.workspace._run_bash_process(
                    str(block.input["command"]), cwd
                )
                result = self.workspace._format_bash_result(output, exit_code)
                status = "completed" if exit_code == 0 else "failed"
            except Exception as exc:
                result = f"Error: {type(exc).__name__}: {exc}"
                status = "failed"
            try:
                self.hooks.trigger("PostToolUse", block, result)
            except Exception as exc:
                result = f"Error: PostToolUse hook failed: {type(exc).__name__}: {exc}\n{result}"
                status = "failed"
            with self.background_state.lock:
                task = self.background_state.tasks.get(bg_id)
                if task is None:
                    return
                task["status"] = status
                self.background_state.results[bg_id] = str(result)

        with self.background_state.lock:
            self._bg_counter += 1
            bg_id = f"bg_{self._bg_counter:04d}"
            self.background_state.tasks[bg_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
                "cwd": str(cwd) if cwd else None,
            }
        thread = threading.Thread(target=worker, daemon=True)
        try:
            thread.start()
        except Exception:
            with self.background_state.lock:
                self.background_state.tasks.pop(bg_id, None)
                self.background_state.results.pop(bg_id, None)
            raise
        print(f"  \x1b[33m[background] {bg_id}: {str(command)[:60]}\x1b[0m")
        return bg_id

    def collect_background_results(self) -> list[str]:
        with self.background_state.lock:
            ready = [
                bg_id
                for bg_id, task in self.background_state.tasks.items()
                if task["status"] in {"completed", "failed"}
            ]
            completed = [
                (
                    bg_id,
                    self.background_state.tasks.pop(bg_id),
                    self.background_state.results.pop(bg_id, ""),
                )
                for bg_id in ready
            ]
        notifications = []
        for bg_id, task, output in completed:
            summary = output[:200] if len(output) > 200 else output
            notifications.append(
                f"<task_notification>\n  <task_id>{bg_id}</task_id>\n  <status>{task['status']}</status>\n  <command>{task['command']}</command>\n  <summary>{summary}</summary>\n</task_notification>"
            )
        return notifications

    def has_pending_background(self) -> bool:
        """Return whether terminal background work is waiting for delivery."""
        with self.background_state.lock:
            return any(
                (
                    task["status"] in {"completed", "failed"}
                    for task in self.background_state.tasks.values()
                )
            )
