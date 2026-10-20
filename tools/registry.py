"""Tool contracts and handlers have one registration boundary."""


class ToolRegistry:
    def __init__(self, mcp):
        self.mcp = mcp
        self.definitions = []
        self.handlers = {}

    def register(self, definition: dict, handler):
        name = definition["name"]
        if name in self.handlers:
            raise ValueError(f"Duplicate tool: {name}")
        self.definitions.append(definition)
        self.handlers[name] = handler

    def snapshot(self):
        return self.mcp.assemble_tool_pool(self.definitions, self.handlers)
