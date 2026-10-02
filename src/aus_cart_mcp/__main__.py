"""`python -m aus_cart_mcp serve` runs the server; the other commands manage customers.

serve                       run the MCP server (AUS_CART_MCP_DB, AUS_CART_MCP_SECRET, PORT)
tenant create <name>        add a customer and print its API key (shown once)
tenant rotate <name>        issue a new API key for a customer
tenant disable|enable <name>
tenant list
"""

from __future__ import annotations

import argparse
import sys

from aus_cart_mcp import config
from aus_cart_mcp.store import Store


def _store() -> Store:
    return Store(config.db_path(), config.secret())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aus_cart_mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    tenant = sub.add_parser("tenant")
    tsub = tenant.add_subparsers(dest="action", required=True)
    for action in ("create", "rotate", "disable", "enable"):
        tsub.add_parser(action).add_argument("name")
    tsub.add_parser("list")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from aus_cart_mcp.server import create_app

        uvicorn.run(create_app(), host="0.0.0.0", port=config.port(), log_level="info")
        return 0

    store = _store()
    if args.action == "create":
        _, key = store.create_tenant_sync(args.name)
        print(key)
    elif args.action == "rotate":
        print(store.rotate_key_sync(args.name))
    elif args.action in ("disable", "enable"):
        store.set_disabled_sync(args.name, args.action == "disable")
        print(f"{args.name}: {args.action}d")
    else:
        for row in store.list_tenants_sync():
            print(f"{row['id']}\t{row['name']}\t{row['created_at']}\t{'disabled' if row['disabled_at'] else 'active'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
