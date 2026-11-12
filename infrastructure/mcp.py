import re


class MCPClient:
    """Small in-process stand-in for MCP tools/list and tools/call."""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        names = [tool.get("name") for tool in tool_defs]
        if any((not isinstance(name, str) or not name for name in names)):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


class MCPService:
    DOCKER_IMAGES = {
        "arxiv": "mcp/arxiv-mcp-server",
        "wikipedia": "mcp/wikipedia-mcp",
    }
    SERVER_ALIASES = {"arxiv-mcp-server": "arxiv", "wikipedia-mcp": "wikipedia"}
    WIKIPEDIA_READ_TOOLS = {
        "extract_key_facts",
        "get_article",
        "get_coordinates",
        "get_links",
        "get_related_topics",
        "get_sections",
        "get_summary",
        "search_wikipedia",
        "summarize_article_for_query",
        "summarize_article_section",
        "test_wikipedia_connectivity",
    }

    def __init__(self, *, arxiv_storage=None, import_pdf=None):
        self.arxiv_storage = arxiv_storage
        self.import_pdf = import_pdf
        self._init_mcp()

    def _init_mcp(self):
        self.mcp_clients: dict[str, MCPClient] = {}
        self._DISALLOWED_CHARS = re.compile("[^a-zA-Z0-9_-]")
        self.MCP_HOST_POLICY = {
            ("docs", "search"): "allow",
            ("docs", "get_version"): "allow",
            ("deploy", "status"): "allow",
            ("deploy", "trigger"): "confirm",
        }
        self.MCP_HOST_POLICY.update(
            {("arxiv", name): "allow" for name in ("search_papers", "list_papers", "read_paper")}
        )
        self.MCP_HOST_POLICY.update(
            {
                ("wikipedia", prefix + name): "allow"
                for prefix in ("", "wikipedia_")
                for name in self.WIKIPEDIA_READ_TOOLS
            }
        )
        self.MOCK_SERVERS = {
            "docs": self._mock_server_docs,
            "deploy": self._mock_server_deploy,
        }

    def normalize_mcp_name(self, name: str) -> str:
        """Replace characters outside the model tool-name alphabet."""
        normalized = self._DISALLOWED_CHARS.sub("_", name)
        if not normalized:
            raise ValueError("MCP names cannot normalize to an empty string")
        return normalized

    def _mock_server_docs(self) -> MCPClient:
        client = MCPClient("docs")
        client.register(
            tool_defs=[
                {
                    "name": "search",
                    "description": "Search the documentation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "get_version",
                    "description": "Get the documentation API version.",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                    "annotations": {"readOnlyHint": True},
                },
            ],
            handlers={
                "search": lambda query: f"[docs] Found 3 results for '{query}'",
                "get_version": lambda: "[docs] API v2.1.0",
            },
        )
        return client

    def _mock_server_deploy(self) -> MCPClient:
        client = MCPClient("deploy")
        client.register(
            tool_defs=[
                {
                    "name": "trigger",
                    "description": "Trigger a deployment.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                    "annotations": {"destructiveHint": True},
                },
                {
                    "name": "status",
                    "description": "Check deployment status.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                        "required": ["service"],
                    },
                    "annotations": {"readOnlyHint": True},
                },
            ],
            handlers={
                "trigger": lambda service: f"[deploy] Triggered: {service}",
                "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
            },
        )
        return client

    def connect_mcp(self, name: str) -> str:
        name = self.SERVER_ALIASES.get(name, name)
        if name in self.mcp_clients:
            return f"MCP server '{name}' already connected"
        factory = self.MOCK_SERVERS.get(name)
        if name in self.DOCKER_IMAGES:
            factory = lambda: self._connect_docker(name)
        if not factory:
            available = ", ".join([*self.DOCKER_IMAGES, *self.MOCK_SERVERS])
            return f"Unknown server '{name}'. Available: {available}"
        try:
            mcp_client = factory()
        except Exception as error:
            return (
                f"MCP error: could not connect to '{name}' ({type(error).__name__}). "
                f"Check Docker Desktop and the local image {self.DOCKER_IMAGES.get(name, name)}."
            )
        self.mcp_clients[name] = mcp_client
        tool_names = [tool["name"] for tool in mcp_client.tools]
        print(f"  \x1b[31m[mcp] connected: {name} -> {tool_names}\x1b[0m")
        return f"Connected to MCP server '{name}'. Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}"

    def _connect_docker(self, name):
        from .library import LibraryBlobStore
        from .mcp_stdio import DockerMCPClient

        storage = self.arxiv_storage if name == "arxiv" else None
        if storage is not None:
            LibraryBlobStore._assert_plain(storage)
            storage.mkdir(parents=True, exist_ok=True)
        return DockerMCPClient(self.DOCKER_IMAGES[name], storage=storage)

    def _handler(self, server, client, tool):
        if server == "arxiv" and tool == "import_pdf" and self.import_pdf:
            return self.import_pdf

        def call(**arguments):
            return client.call_tool(tool, arguments)

        return call

    def assemble_tool_pool(self, builtin_tools, builtin_handlers) -> tuple[list[dict], dict]:
        """Merge builtin tools + all MCP tools into one pool."""
        tools = list(builtin_tools)
        handlers = dict(builtin_handlers)
        policies: dict[str, str] = {}
        origins = {tool["name"]: f"built-in tool {tool['name']!r}" for tool in tools}
        for server_name, mcp_client in self.mcp_clients.items():
            safe_server = self.normalize_mcp_name(server_name)
            tool_defs = mcp_client.tools
            if server_name == "arxiv" and self.import_pdf:
                tool_defs = [
                    *tool_defs,
                    {
                        "name": "import_pdf",
                        "description": "Download the original PDF from arXiv by paper ID and add it "
                        "to the local file library for preview, download and indexing. Use this to "
                        "save a PDF. The server's download_paper tool converts papers to Markdown.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"paper_id": {"type": "string"}},
                            "required": ["paper_id"],
                            "additionalProperties": False,
                        },
                    },
                ]
            for tool_def in tool_defs:
                raw_name = tool_def["name"]
                safe_tool = self.normalize_mcp_name(raw_name)
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                if len(prefixed) > 64:
                    raise ValueError(f"MCP tool name is longer than 64 characters: {prefixed}")
                origin = f"MCP tool {server_name!r}/{raw_name!r}"
                if prefixed in origins:
                    raise ValueError(
                        f"MCP tool name collision after normalization: {prefixed!r} maps both {origins[prefixed]} and {origin}"
                    )
                schema = tool_def.get("inputSchema", {})
                if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                    raise ValueError(f"Invalid input schema for {origin}")
                origins[prefixed] = origin
                tools.append(
                    {
                        "name": prefixed,
                        "description": tool_def.get("description", ""),
                        "input_schema": schema,
                    }
                )
                handlers[prefixed] = self._handler(server_name, mcp_client, raw_name)
                policies[prefixed] = self.MCP_HOST_POLICY.get((server_name, raw_name), "confirm")
        self.mcp_tool_policies = policies
        return (tools, handlers)
