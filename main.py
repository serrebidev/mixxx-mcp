#!/usr/bin/env python3
"""
mixxx-mcp entry point

Usage:
  python main.py             # stdio transport (Claude Desktop, claude-code)
  python main.py --http      # streamable HTTP on :8080
  python main.py --port 9090 # custom port
"""

import argparse
import sys
import os

# Allow running as: python main.py from the project root
sys.path.insert(0, os.path.dirname(__file__))

from src.server import mcp, startup


def main():
    parser = argparse.ArgumentParser(description="mixxx-mcp — MCP server for Mixxx DJ Software")
    parser.add_argument("--http", action="store_true", help="Use streamable HTTP transport")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080)")
    parser.add_argument("--osc-port", type=int, default=57121, help="OSC listen port (default: 57121)")
    args = parser.parse_args()

    # Boot bridges
    startup()

    if args.http:
        # Banners go to stderr: on stdio transport stdout carries JSON-RPC only,
        # and anything else printed there corrupts the protocol stream.
        print(f"[mixxx-mcp] HTTP transport on {args.host}:{args.port}",
              file=sys.stderr, flush=True)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        print("[mixxx-mcp] stdio transport", file=sys.stderr, flush=True)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
