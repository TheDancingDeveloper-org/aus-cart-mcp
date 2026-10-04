"""Find how a retailer's site renews its login token, from a HAR file (Vogt WI-800).

    python scripts/har_session_scan.py woolworths.har [--cookie wow-auth-token]

Capture: sign in to woolworths.com.au in a desktop browser, open DevTools → Network
("Preserve log" on), leave the tab open past the 60-minute mark (click around now and then),
then "Save all as HAR". The HAR holds your cookies: keep it private, never commit it.

This prints only times, methods, paths, statuses, cookie *names* and token issue/expiry times.
It never prints cookie values, tokens or bodies.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import sys
from urllib.parse import urlsplit

AUTH_COOKIES = ("wow-auth-token", "prodwow-auth-token")


def jwt_times(token: str) -> str:
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return "not a JWT"
    fmt = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%H:%M:%SZ")  # noqa: E731
    return f"iat {fmt(claims['iat']) if 'iat' in claims else '?'} exp {fmt(claims['exp']) if 'exp' in claims else '?'}"


def set_cookies(entry: dict) -> dict[str, str]:
    out = {}
    for header in entry["response"].get("headers", []):
        if header["name"].lower() == "set-cookie":
            for line in header["value"].split("\n"):
                name, _, rest = line.partition("=")
                out[name.strip()] = rest.split(";")[0]
    for cookie in entry["response"].get("cookies", []):
        out.setdefault(cookie["name"], cookie.get("value", ""))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("har")
    parser.add_argument("--cookie", action="append", help="auth cookie name(s) to follow")
    args = parser.parse_args(argv)
    watched = tuple(args.cookie or AUTH_COOKIES)
    entries = json.load(open(args.har, encoding="utf-8"))["log"]["entries"]
    entries.sort(key=lambda e: e["startedDateTime"])
    renewals, seen_hosts = [], set()
    for entry in entries:
        url = urlsplit(entry["request"]["url"])
        seen_hosts.add(url.netloc)
        cookies = set_cookies(entry)
        hits = [n for n in cookies if n in watched]
        if not hits:
            continue
        when = entry["startedDateTime"][11:19]
        times = "; ".join(f"{n}: {jwt_times(cookies[n])}" for n in hits)
        names = ", ".join(sorted(n for n in cookies if n not in watched))
        renewals.append(
            f"{when} {entry['request']['method']:<6} {entry['response']['status']} {url.netloc}{url.path}\n"
            f"         sets {times}\n         also sets: {names or '-'}"
        )
    print(f"{len(entries)} requests across {len(seen_hosts)} hosts; {len(renewals)} set {', '.join(watched)}:\n")
    print("\n".join(renewals) if renewals else "none: extend the capture past the token's expiry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
