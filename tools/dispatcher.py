"""Shared tool execution pipeline for main agent, subagents and HTTP events."""

from types import SimpleNamespace


class ToolDispatcher:
    def __init__(self, hooks, emit, stop_event):
        self.hooks = hooks
        self.emit = emit
        self.stop_event = stop_event
        self.background = None

    @staticmethod
    def call_handler(handler, args: dict, name: str) -> str:
        if handler is None:
            return f"Unknown tool: {name}"
        try:
            return str(handler(**args))
        except Exception as error:
            return f"Error: {type(error).__name__}: {error}"

    def execute(self, call: dict, handlers: dict, *, allow_background=True) -> str:
        block = SimpleNamespace(id=call["id"], name=call["name"], input=call.get("input", {}))
        self.emit("tool_started", {"id": block.id, "name": block.name, "input": block.input})
        if self.stop_event.is_set():
            output = "Execution cancelled by user"
        else:
            denied = self.hooks.trigger("PreToolUse", block)
            if denied:
                output = str(denied)
            elif self.stop_event.is_set():
                output = "Execution cancelled by user"
            elif (
                allow_background
                and self.background
                and self.background.should_run_background(block.name, block.input)
            ):
                task_id = self.background.start_background_task(block, handlers)
                output = f"[Background task {task_id} started] Result will arrive as a task_notification."
            else:
                output = self.call_handler(handlers.get(block.name), block.input, block.name)
                self.hooks.trigger("PostToolUse", block, output)
        self.emit("tool_finished", {"id": block.id, "name": block.name, "output": output})
        return output
