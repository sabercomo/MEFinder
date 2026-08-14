"""Run the packaged MCP sidecar through a real STDIO client session."""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


EXPECTED_TOOLS = ["list_documents", "locate_quote", "read_document_window"]


async def smoke(executable: Path, runtime_root: Path | None) -> None:
    server_args = []
    if runtime_root is not None:
        server_args = ["--runtime-root", str(runtime_root)]
    parameters = StdioServerParameters(
        command=str(executable),
        args=server_args,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            if initialized.server_info.name != "mefinder":
                raise RuntimeError(
                    f"Unexpected MCP server name: {initialized.server_info.name}"
                )
            listed = await session.list_tools()
            tool_names = [tool.name for tool in listed.tools]
            if tool_names != EXPECTED_TOOLS:
                raise RuntimeError(f"Unexpected MCP tools: {tool_names}")
            documents = await session.call_tool("list_documents", {})
            if documents.is_error:
                raise RuntimeError(f"Packaged MCP list_documents failed: {documents}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("runtime_root", type=Path, nargs="?")
    args = parser.parse_args()
    runtime_root = args.runtime_root.resolve() if args.runtime_root is not None else None
    anyio.run(smoke, args.executable.resolve(), runtime_root)


if __name__ == "__main__":
    main()
