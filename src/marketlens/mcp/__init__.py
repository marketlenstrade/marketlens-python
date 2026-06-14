"""marketlens MCP server: agentic access to the SDK over stdio.

Run with the ``marketlens-mcp`` console script (or ``python -m marketlens.mcp``)
after ``pip install 'marketlens[mcp]'``. Authenticates with ``MARKETLENS_API_KEY``.
"""

from __future__ import annotations


def main() -> None:
    from marketlens.mcp.server import main as _main

    _main()


def build_server():
    """Build and return the FastMCP server (lazy import of the optional dep)."""
    from marketlens.mcp.server import build_server as _build

    return _build()


__all__ = ["main", "build_server"]
