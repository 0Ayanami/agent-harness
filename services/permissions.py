"""Tool authorization independent of stdin, HTTP or the model implementation."""

from typing import Callable


ApprovalHandler = Callable[[str, dict], bool]


class PermissionPolicy:
    DENY_LIST = ("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=")

    def __init__(self, workdir, approve: ApprovalHandler, mcp):
        self.workdir = workdir.resolve()
        self.approve = approve
        self.mcp = mcp

    def check(self, block):
        name, arguments = block.name, block.input
        if name == "bash":
            command = arguments.get("command")
            if not isinstance(command, str):
                return "Permission denied: shell command must be a string"
            for pattern in self.DENY_LIST:
                if pattern in command:
                    return f"Permission denied: '{pattern}' is on the deny list"
            if not self.approve(name, arguments):
                return "Permission denied by user"
        if name in ("read_file", "write_file", "edit_file"):
            path = arguments.get("path")
            if not isinstance(path, str) or not (self.workdir / path).resolve().is_relative_to(
                self.workdir
            ):
                return "Permission denied: path is outside the workspace"
        if name.startswith("mcp__") and self.mcp.mcp_tool_policies.get(name, "confirm") != "allow":
            if not self.approve(name, arguments):
                return "Permission denied by user"
        return None
