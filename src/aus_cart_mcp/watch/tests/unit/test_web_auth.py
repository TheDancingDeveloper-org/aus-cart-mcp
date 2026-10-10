from aus_cart_mcp.watch.web import auth, chart, sparkline


def test_password_hash_roundtrip():
    stored = auth.hash_password("correct horse")
    assert stored.startswith("scrypt:") and "$" not in stored
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("x", "bcrypt$a$b")
    assert not auth.verify_password("x", "garbage")
    assert auth.verify_password("correct horse", stored.replace(":", "$"))  # the first format still verifies


def test_session_tokens():
    token = auth.issue("s", now=1000)
    assert auth.valid("s", token, now=1001)
    assert not auth.valid("s", token, now=1000 + auth.SESSION_SECONDS + 1)
    assert not auth.valid("other", token, now=1001)
    assert not auth.valid("s", token + "x", now=1001)
    assert not auth.valid("s", None) and not auth.valid("", token)
    assert not auth.valid("s", "a.b.c")


def test_svg_helpers():
    assert sparkline([1.0]) == "" and chart([]) == ""
    assert "<polyline" in sparkline([1.0, None, 2.0, 1.5])
    rows = [{"price": 2.0, "on_special": 0}, {"price": 1.0, "on_special": 1}, {"price": None}]
    svg = chart(rows)
    assert 'class="special"' in svg and "$2.00" in svg and "$1.00" in svg
