"""Shared MCP tool test helpers; the behavior tests live in the test_mcp_tool_* modules."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from wizolt.config import (
    Config,
)
from wizolt.mcp import MCPToolInfo
from wizolt.session import Session, bootstrap_features


def _index_session(servers):
    """Build a session with the given {server: [(tool_name, n_schema_fields), ...]}."""
    s = Session(cwd="/tmp", config=Config.from_dict({"mcp": {name: {"url": f"https://{name}/mcp", "auto_connect": True} for name in servers}}))
    bootstrap_features(s)
    for name, tools in servers.items():
        s.mcp.tools[name] = [
            MCPToolInfo(
                name=tool_name,
                description="A tool.",
                input_schema={
                    "type": "object",
                    "properties": {f"p{i}": {"type": "string", "description": "d" * 40} for i in range(nfields)},
                    "required": [f"p{i}" for i in range(min(2, nfields))],
                },
                annotations={},
            )
            for tool_name, nfields in tools
        ]
    s.mcp.discovery_status = "ready"
    return s
