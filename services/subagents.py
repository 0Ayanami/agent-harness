"""Focused agents reuse the engine with a bounded tool set and fresh history."""

from ..core.messages import extract_text


class SubagentService:
    ALLOWED_TOOLS = {"bash", "read_file", "write_file", "edit_file", "glob"}

    def __init__(self, workdir, engine, workspace):
        self.workdir = workdir
        self.engine = engine
        self.workspace = workspace
        self.registry = None

    def run(self, description: str) -> str:
        definitions, _ = self.registry.snapshot()
        schemas = [tool for tool in definitions if tool["name"] in self.ALLOWED_TOOLS]
        handlers = {
            "bash": self.workspace.run_bash,
            "read_file": self.workspace.run_read,
            "write_file": self.workspace.run_write,
            "edit_file": self.workspace.run_edit,
            "glob": self.workspace.run_glob,
        }
        history = [{"role": "user", "content": description}]
        self.engine.emit("subagent_started", {"description": description})
        system = f"You are a coding subagent at {self.workdir}. Complete the task and return a concise summary. Do not spawn more agents."
        self.engine.run(
            history,
            description,
            tool_pool=lambda: (schemas, handlers),
            system_prompt=lambda _: system,
            max_iterations=30,
            allow_background=False,
            scope="subagent",
        )
        summary = next(
            (
                extract_text(message["content"])
                for message in reversed(history)
                if message["role"] == "assistant" and extract_text(message["content"])
            ),
            "Subagent finished without a text summary.",
        )
        self.engine.emit("subagent_finished", {"summary": summary})
        return summary
