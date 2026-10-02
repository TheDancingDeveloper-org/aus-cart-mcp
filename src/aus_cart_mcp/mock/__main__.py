"""Serve a mock retailer locally: `python -m aus_cart_mcp.mock [--port 8090] [--retailer woolworths]`."""

from __future__ import annotations

import argparse

import uvicorn

from aus_cart_mcp.mock import MOCKS


def main() -> None:
    parser = argparse.ArgumentParser(prog="aus_cart_mcp.mock")
    parser.add_argument("--retailer", default="woolworths", choices=sorted(MOCKS))
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    print(f"mock {args.retailer} on http://127.0.0.1:{args.port}")
    print(f"point the server at it with AUS_CART_MCP_{args.retailer.upper()}_BASE_URL=http://127.0.0.1:{args.port}")
    uvicorn.run(MOCKS[args.retailer].create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
