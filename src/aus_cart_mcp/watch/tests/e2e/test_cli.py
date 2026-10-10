"""The operator CLI against the real aus-cart-mcp."""

import asyncio
import io

import pytest

from aus_cart_mcp.watch import __main__ as cli

MILK = "888140"


@pytest.fixture()
def env(aus_cart, tmp_path, monkeypatch):
    monkeypatch.setenv("AUS_CARTWATCH_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_URL", aus_cart.url)
    monkeypatch.setenv("AUS_CARTWATCH_AUS_CART_KEY", aus_cart.key)
    return aus_cart


async def run(*argv):
    # The CLI calls asyncio.run itself; run it in a thread so the test's loop keeps serving the stack.
    return await asyncio.to_thread(cli.main, list(argv))


async def test_track_tracked_stats_refresh_add_untrack(env, capsys):
    assert await run("track", "full", "cream", "milk") == 0
    out = capsys.readouterr().out
    assert MILK in out and "--pick" in out
    assert await run("track", "full", "cream", "milk", "--pick", "99") == 2
    assert await run("track", "full", "cream", "milk", "--pick", "1", "--target", "4.5") == 0
    assert "now tracking" in capsys.readouterr().out
    assert await run("track", MILK, "--label", "Milk") == 0
    assert "updated" in capsys.readouterr().out
    assert await run("track", "99999999") == 1
    assert await run("track") == 2
    assert await run("tracked") == 0
    assert "Milk" in capsys.readouterr().out
    assert await run("refresh", "--now") == 0
    assert ": ok" in capsys.readouterr().out
    assert await run("stats", MILK) == 0
    assert '"price": 4.95' in capsys.readouterr().out
    assert await run("stats", "nope") == 1
    assert await run("candidates") == 0
    assert await run("add", MILK) == 1  # no retailer session yet
    assert "myaiagent app" in capsys.readouterr().out
    await env.connect_session()
    assert await run("add", MILK, "--quantity", "2") == 0
    assert await run("track", "--from-cart") == 0
    assert "0 new tracked items" in capsys.readouterr().out
    assert await run("untrack", MILK) == 0
    assert await run("untrack", MILK) == 1


def test_hash_password(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("long enough password\n"))
    assert cli.main(["hash-password"]) == 0
    assert capsys.readouterr().out.startswith("scrypt:")
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["hash-password"]) == 2
