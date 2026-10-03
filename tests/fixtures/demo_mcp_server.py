"""A tiny MCP server used by the tests. Run with no args for stdio, or `http PORT` for Streamable HTTP."""

import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("demo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@server.tool()
def delete_note(name: str) -> str:
    """Delete a note by name."""
    return f"deleted {name}"


@server.tool()
def hidden_tool() -> str:
    """A tool the config hides."""
    return "should not be visible"


@server.tool()
def explode() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run()
