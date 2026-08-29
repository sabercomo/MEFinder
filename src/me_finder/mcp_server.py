"""STDIO MCP adapter for the read-only literature-verification service."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Mapping, Sequence

import anyio
from jsonschema import Draft202012Validator, ValidationError
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from .application import LiteratureVerificationService
from .structured_reader import SourceNotFound, UnsupportedSourceType


LOGGER = logging.getLogger(__name__)
CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "contracts"
    / "v0.4.4-mcp-v1-tools.json"
)
ERROR_MESSAGES = {
    "invalid_input": "输入参数不符合工具契约。",
    "index_not_found": "当前 MEFinder 数据目录中没有文献索引。",
    "index_unavailable": "MEFinder 文献索引暂时不可用。",
    "source_not_found": "未找到指定文献。",
    "unsupported_source_type": "该文献类型不支持结构化阅读。",
    "internal_error": "MEFinder MCP 内部错误。",
}


def _load_contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


CONTRACT = _load_contract()
TOOL_CONTRACTS = {
    str(item["name"]): item
    for item in CONTRACT["tools"]
    if isinstance(item, Mapping)
}
ERROR_RETRYABILITY = {
    str(item["code"]): bool(item["retryable"])
    for item in CONTRACT["errors"]
    if isinstance(item, Mapping)
}
INPUT_VALIDATORS = {
    name: Draft202012Validator(tool["inputSchema"])
    for name, tool in TOOL_CONTRACTS.items()
}


def _definition_references(value: object) -> set[str]:
    references: set[str] = set()
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            references.add(reference.removeprefix("#/$defs/"))
        for nested in value.values():
            references.update(_definition_references(nested))
    elif isinstance(value, list):
        for nested in value:
            references.update(_definition_references(nested))
    return references


def _advertised_output_schema(tool: Mapping[str, object]) -> dict[str, object]:
    schema = copy.deepcopy(tool["outputSchema"])
    definitions = CONTRACT["$defs"]
    required = _definition_references(schema)
    pending = set(required)
    while pending:
        name = pending.pop()
        nested = _definition_references(definitions[name])
        pending.update(nested - required)
        required.update(nested)
    schema["$defs"] = {
        name: copy.deepcopy(definition)
        for name, definition in definitions.items()
        if name in required
    }
    return schema


def _tool_definitions() -> list[Tool]:
    return [
        Tool(
            name=name,
            title=str(tool["title"]),
            description=str(tool["description"]),
            inputSchema=copy.deepcopy(tool["inputSchema"]),
            outputSchema=_advertised_output_schema(tool),
            annotations=ToolAnnotations.model_validate(tool["annotations"]),
        )
        for name, tool in TOOL_CONTRACTS.items()
    ]


TOOLS = _tool_definitions()


def _success_text(tool_name: str, result: Mapping[str, object]) -> str:
    if tool_name == "list_documents":
        return f"找到 {result['total']} 篇文献。"
    if tool_name == "locate_quote":
        return f"找到 {result['total']} 个原句候选。"
    if tool_name == "verify_quotes":
        return (
            f"核对了 {result['total']} 条引文："
            f"{result['verified_count']} 条逐字命中，"
            f"{result['approximate_count']} 条疑似错引，"
            f"{result['not_found_count']} 条未找到。"
        )
    if tool_name == "diff_quote":
        return {
            "identical": "引文与原句完全一致。",
            "different": "引文与原句存在差异，详见 diff。",
            "not_found": "未在索引中找到可比对的原句。",
        }[str(result["status"])]
    return f"读取了 {len(result['items'])} 个文献位置。"


def _error_result(code: str) -> CallToolResult:
    payload = {
        "schema_version": "1",
        "error": {
            "code": code,
            "message": ERROR_MESSAGES[code],
            "retryable": ERROR_RETRYABILITY[code],
        },
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structuredContent=payload,
        isError=True,
    )


def _execute_tool(
    service: LiteratureVerificationService,
    tool_name: str,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    if tool_name == "list_documents":
        return service.list_documents(
            query=arguments.get("query"),
            source_type=arguments.get("source_type", "all"),
            limit=arguments.get("limit", 20),
        )
    if tool_name == "locate_quote":
        return service.locate_quote(
            arguments["quote"],
            mode=arguments.get("mode", "auto"),
            source_file_id=arguments.get("source_file_id"),
            source_type=arguments.get("source_type", "all"),
            limit=arguments.get("limit", 5),
        )
    if tool_name == "verify_quotes":
        return service.verify_quotes(
            arguments["quotes"],
            mode=arguments.get("mode", "auto"),
            source_file_id=arguments.get("source_file_id"),
            source_type=arguments.get("source_type", "all"),
            matches_per_quote=arguments.get("matches_per_quote", 1),
        )
    if tool_name == "diff_quote":
        return service.diff_quote(
            arguments["quote"],
            source_file_id=arguments.get("source_file_id"),
            source_type=arguments.get("source_type", "all"),
            mode=arguments.get("mode", "fuzzy"),
        )
    return service.read_document_window(
        arguments["source_file_id"],
        start=arguments.get("start", 0),
        count=arguments.get("count", 10),
    )


def _call_tool(
    service: LiteratureVerificationService,
    tool_name: str,
    arguments: Mapping[str, object],
) -> CallToolResult:
    validator = INPUT_VALIDATORS.get(tool_name)
    if validator is None:
        return _error_result("invalid_input")
    try:
        validator.validate(arguments)
        result = _execute_tool(service, tool_name, arguments)
    except ValidationError:
        return _error_result("invalid_input")
    except UnsupportedSourceType:
        return _error_result("unsupported_source_type")
    except SourceNotFound:
        return _error_result("source_not_found")
    except FileNotFoundError:
        return _error_result("index_not_found")
    except ValueError:
        return _error_result("invalid_input")
    except (sqlite3.Error, OSError):
        return _error_result("index_unavailable")
    except Exception:
        LOGGER.exception("Unhandled MCP tool error: %s", tool_name)
        return _error_result("internal_error")
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=_success_text(tool_name, result),
            )
        ],
        structuredContent=result,
        isError=False,
    )


def create_server(
    service: LiteratureVerificationService | None = None,
) -> Server[dict[str, object]]:
    verification_service = service or LiteratureVerificationService()

    async def list_tools(
        _context: object,
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)

    async def call_tool(
        _context: object,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        return _call_tool(
            verification_service,
            params.name,
            params.arguments or {},
        )

    server_contract = CONTRACT["server"]
    return Server(
        str(server_contract["name"]),
        version=str(CONTRACT["release"]),
        instructions=str(server_contract["instructions"]),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _serve_stdio(server: Server[dict[str, object]]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        with redirect_stdout(sys.stderr):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )


def run_stdio_server(server: Server[dict[str, object]]) -> None:
    anyio.run(_serve_stdio, server)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the MEFinder MCP server.")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="Override the MEFinder runtime data root for source-mode testing.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    service = (
        LiteratureVerificationService(lambda: args.runtime_root)
        if args.runtime_root is not None
        else LiteratureVerificationService()
    )
    run_stdio_server(create_server(service))


if __name__ == "__main__":
    main()
