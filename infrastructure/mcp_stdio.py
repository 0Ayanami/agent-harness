"""Docker-hosted MCP tools over the standard stdio transport."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DockerMCPClient:
    def __init__(self, image, *, storage=None):
        args = ["run", "-i", "--rm", "--init", "--pull=never"]
        self.arxiv_conversion = storage is not None
        if storage is not None:
            # The Docker Catalog image resolves its storage to /app/papers.
            args += ["-v", f"{storage}:/app/papers"]
        self.parameters = StdioServerParameters(
            command="docker",
            args=[*args, image],
            env={
                key: os.environ[key]
                for key in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_CONFIG")
                if key in os.environ
            },
        )
        self.tools = asyncio.run(self._discover())

    @asynccontextmanager
    async def _session(self, timeout):
        async with asyncio.timeout(timeout):
            async with stdio_client(self.parameters) as (reader, writer):
                async with ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=timeout)
                ) as session:
                    await session.initialize()
                    yield session

    async def _discover(self):
        tools, seen_cursors = [], set()
        async with self._session(30) as session:
            page = await session.list_tools()
            while True:
                tools.extend(tool.model_dump(exclude_none=True) for tool in page.tools)
                if not page.nextCursor:
                    return tools
                if page.nextCursor in seen_cursors:
                    raise ValueError("MCP server repeated a tools/list cursor")
                seen_cursors.add(page.nextCursor)
                page = await session.list_tools(cursor=page.nextCursor)

    async def _call(self, tool, arguments):
        async with self._session(120) as session:
            result = await session.call_tool(tool, arguments)
            if (
                not self.arxiv_conversion
                or tool != "download_paper"
                or arguments.get("check_status")
            ):
                return result
            status = self._conversion_status(result)
            if status not in ("downloading", "converting"):
                return result
            # Conversion status is kept in this server process until Markdown is written.
            while status in ("downloading", "converting"):
                await asyncio.sleep(1)
                result = await session.call_tool(tool, {**arguments, "check_status": True})
                status = self._conversion_status(result)
            if status == "success":
                return await session.call_tool(tool, arguments)
            return result

    @staticmethod
    def _conversion_status(result):
        if result.isError:
            return None
        payload = result.structuredContent
        if payload is None and result.content and result.content[0].type == "text":
            try:
                payload = json.loads(result.content[0].text)
            except ValueError:
                return None
        return payload.get("status") if isinstance(payload, dict) else None

    def call_tool(self, tool, arguments):
        try:
            result = asyncio.run(self._call(tool, arguments))
        except TimeoutError:
            return f"MCP error: '{tool}' did not respond before the tool timeout"
        parts = [item.text for item in result.content if item.type == "text"]
        if not parts and result.structuredContent is not None:
            parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
        if not parts:
            parts.append("MCP server returned no text or structured result")
        return ("MCP error: " if result.isError else "") + "\n".join(parts)
