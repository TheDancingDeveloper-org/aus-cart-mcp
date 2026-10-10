"""`python -m aus_cart_mcp.watch serve` runs the service; the other commands are for the operator.

serve                       run the HTTP + MCP server, scheduler and Telegram buttons
auscart status              aus-cart-mcp: retailer, session and today's usage
track QUERY|ID|URL          track a product (a query lists candidates; --pick N tracks one)
track --from-cart           track every cart line not yet tracked
untrack ID                  stop tracking (history is kept)
tracked                     list tracked items with price, baseline and discount
stats ID                    an item's trend block
candidates                  suggested items from cart history
refresh [--force]           poll prices now, within the budget (--force bypasses the cap; logged)
add ID [--quantity N]       add a known product to the cart
telegram check|test         alert bot readiness (no message) / one test message
hash-password               print a value for AUS_CARTWATCH_UI_PASSWORD_HASH (reads the password from stdin)
db migrate|stats|backup|compact   database maintenance
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys

from aus_cart_mcp.watch import analytics, budget, config, detect, tracking
from aus_cart_mcp.watch.store import Store


def _store() -> Store:
    store = Store(config.db_path())
    store.migrate()
    return store


def _money(value) -> str:
    return f"${value:.2f}" if isinstance(value, int | float) else "-"


async def _auscart_status(store: Store) -> int:
    retailer = config.retailer()
    async with budget.client(store, retailer, purpose="cart") as client:
        connection = await client.connection_status(retailer=retailer)
        usage = await client.usage_summary(days=1)
    separate = config.aus_cart_prices_key() != config.aus_cart_key()
    print(f"server      {client.url}")
    print(f"retailer    {connection.retailer}")
    state = "connected" if connection.connected else f"not connected ({connection.error or 'no session'})"
    print(f"session     {state}{f' as {connection.first_name}' if connection.first_name else ''}")
    if connection.paused:
        print(f"paused      {connection.paused}")
    print(f"usage today {usage.calls} calls, {usage.upstream_requests} upstream requests (cart tenant)")
    via = "a separate anonymous tenant" if separate else "the cart tenant (set AUS_CARTWATCH_AUS_CART_PRICES_KEY)"
    print(f"prices via  {via}")
    return 0


async def _track(store: Store, args: argparse.Namespace) -> int:
    from aus_cart_mcp.watch.service import Service

    service = Service(store, notifiers=[])
    if args.from_cart:
        results = await service.track_from_cart()
        for result in results:
            print(result.message)
        print(f"{sum(r.created for r in results)} new tracked items from the cart")
        return 0
    if not args.reference:
        print("give a search, a product id or a product URL (or --from-cart)", file=sys.stderr)
        return 2
    options = {"label": args.label, "target_price": args.target, "threshold": args.threshold}
    result = await service.track(" ".join(args.reference), **options)
    if result.candidates:
        if args.pick is not None:
            if not 1 <= args.pick <= len(result.candidates):
                print(f"--pick must be between 1 and {len(result.candidates)}", file=sys.stderr)
                return 2
            tracker = tracking.Tracker(store, None, service.retailer)  # type: ignore[arg-type]
            print(tracker.track_product(result.candidates[args.pick - 1], **options).message)
            return 0
        for n, product in enumerate(result.candidates, 1):
            special = " (on special)" if product.on_special else ""
            print(
                f"{n:>2}. {product.product_id:>8}  {_money(product.price):>8}  {product.name} {product.size}{special}"
            )
        print("track one with --pick N, or by its id")
        return 0
    print(result.message)
    return 0 if result.item else 1


def _tracked(store: Store) -> int:
    for item in tracking.list_tracked(store, config.retailer()):
        s = item["stats"]
        flags = " HEAVY" if s.get("heavy") else (" sale" if s.get("on_sale") else "")
        off = f" -{round((s.get('discount') or 0) * 100)}%" if s.get("discount") else ""
        print(
            f"{item['product_id']:>8}  {_money(s.get('price')):>8}  usual {_money(s.get('baseline')):>8}{off}{flags}  "
            f"{item.get('label') or item['name']}"
        )
    return 0


def _stats(store: Store, product_id: str) -> int:
    retailer = config.retailer()
    if store.get_product(retailer, product_id) is None:
        print(f"{product_id} has never been observed", file=sys.stderr)
        return 1
    print(json.dumps(analytics.refresh_item_stats(store, retailer, product_id), indent=2))
    return 0


async def _refresh(store: Store, force: bool) -> int:
    from aus_cart_mcp.watch.service import Service

    result = await Service(store, notifiers=[]).scheduler.refresh(force=force, note="manual (cli)")
    print(f"{result.run_id}: {result.outcome} {result.note} (observed {result.observed})")
    return 0 if result.outcome in ("ok", "partial", "skipped") else 1


async def _add(store: Store, product_id: str, quantity: float) -> int:
    from aus_cart_mcp.watch.service import Service

    outcome = await Service(store, notifiers=[]).add_to_cart(product_id, quantity, reason="cli")
    print(outcome.message)
    return 0 if outcome.ok else 1


async def _telegram(action: str) -> int:
    from aus_cart_mcp.watch.alerts import Message
    from aus_cart_mcp.watch.alerts.telegram import TelegramError, TelegramNotifier

    if not config.telegram_token():
        print(
            "AUS_CARTWATCH_TELEGRAM_TOKEN is not set (see docs/OPERATIONS.md § Open operator actions)", file=sys.stderr
        )
        return 2
    if not config.telegram_chat_ids():
        print("AUS_CARTWATCH_TELEGRAM_CHAT_IDS is not set", file=sys.stderr)
        return 2
    bot = TelegramNotifier(config.telegram_token(), config.telegram_chat_ids())
    try:
        if action == "check":
            lines = await bot.check()
            print("\n".join(lines))
            return 0 if all("NOT reachable" not in line for line in lines) else 1
        await bot.send(Message("operator", "aus_cart_mcp.watch: test message, alerts will arrive here.", []))
        print("sent")
        return 0
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="aus_cart_mcp.watch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the HTTP + MCP server")
    auscart = sub.add_parser("auscart", help="talk to aus-cart-mcp")
    auscart.add_subparsers(dest="action", required=True).add_parser("status", help="retailer, session and usage")
    track = sub.add_parser("track", help="track a product")
    track.add_argument("reference", nargs="*")
    track.add_argument("--from-cart", action="store_true")
    track.add_argument("--pick", type=int)
    track.add_argument("--label")
    track.add_argument("--target", type=float)
    track.add_argument("--threshold", type=float)
    sub.add_parser("untrack", help="stop tracking").add_argument("product_id")
    sub.add_parser("tracked", help="list tracked items")
    sub.add_parser("stats", help="an item's trend block").add_argument("product_id")
    sub.add_parser("candidates", help="suggested items from cart history")
    refresh = sub.add_parser("refresh", help="poll prices now")
    refresh.add_argument("--now", action="store_true", help="accepted for clarity; refresh always runs now")
    refresh.add_argument("--force", action="store_true", help="bypass the daily cap (operator only; logged)")
    add = sub.add_parser("add", help="add a known product to the cart")
    add.add_argument("product_id")
    add.add_argument("--quantity", type=float, default=1)
    sub.add_parser("hash-password", help="hash a web UI password")
    telegram = sub.add_parser("telegram", help="Telegram alert bot")
    tsub = telegram.add_subparsers(dest="action", required=True)
    tsub.add_parser("check", help="bot identity and chat reachability, without sending a message")
    tsub.add_parser("test", help="send one test message to the allowed chats")
    db = sub.add_parser("db", help="database maintenance")
    dbsub = db.add_subparsers(dest="action", required=True)
    dbsub.add_parser("migrate", help="apply pending migrations")
    dbsub.add_parser("stats", help="row counts and file size")
    backup = dbsub.add_parser("backup", help="online backup into a directory, keeping the newest N")
    backup.add_argument("directory", nargs="?", default=None)
    backup.add_argument("--keep", type=int, default=None)
    compact = dbsub.add_parser("compact", help="apply retention and reclaim space")
    compact.add_argument("--observation-days", type=int, default=730)
    compact.add_argument("--snapshot-days", type=int, default=90)
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from aus_cart_mcp.watch.server import create_app

        uvicorn.run(create_app(), host=config.host(), port=config.port(), log_level="info")
        return 0
    if args.cmd == "hash-password":
        from aus_cart_mcp.watch.web.auth import hash_password

        password = getpass.getpass("password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        if len(password) < 8:
            print("use at least 8 characters", file=sys.stderr)
            return 2
        print(hash_password(password))
        return 0
    if args.cmd == "db":
        store = Store(config.db_path())
        if args.action == "migrate":
            applied = store.migrate()
            print("\n".join(applied) if applied else "up to date", f"(schema version {store.schema_version()})")
        elif args.action == "backup":
            store.migrate()
            directory = args.directory or config.backup_dir()
            if not directory:
                print("give a directory or set AUS_CARTWATCH_BACKUP_DIR", file=sys.stderr)
                return 2
            print(store.backup(directory, keep=args.keep or config.backup_keep()))
        elif args.action == "stats":
            store.migrate()
            print(json.dumps(store.stats(), indent=2))
        else:
            store.migrate()
            deleted = store.compact(observation_days=args.observation_days, snapshot_days=args.snapshot_days)
            print(json.dumps({"deleted": deleted, **store.stats()}, indent=2))
        return 0
    if args.cmd == "telegram":
        return asyncio.run(_telegram(args.action))
    store = _store()
    if args.cmd == "auscart":
        return asyncio.run(_auscart_status(store))
    if args.cmd == "track":
        return asyncio.run(_track(store, args))
    if args.cmd == "untrack":
        found = tracking.untrack(store, config.retailer(), args.product_id)
        print("untracked" if found else "not tracked")
        return 0 if found else 1
    if args.cmd == "tracked":
        return _tracked(store)
    if args.cmd == "stats":
        return _stats(store, args.product_id)
    if args.cmd == "candidates":
        for c in detect.candidates(store, config.retailer()):
            print(f"{c['product_id']:>8}  {c['confidence']:.0%}  in {c['seen']}/{c['episodes']} shops  {c['name']}")
        return 0
    if args.cmd == "refresh":
        return asyncio.run(_refresh(store, args.force))
    return asyncio.run(_add(store, args.product_id, args.quantity))


if __name__ == "__main__":
    sys.exit(main())
