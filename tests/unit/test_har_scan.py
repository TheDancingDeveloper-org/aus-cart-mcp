import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import har_session_scan  # noqa: E402


def jwt(iat: int, exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"iat": iat, "exp": exp}).encode()).decode().rstrip("=")
    return f"x.{body}.sig"


def test_scan_reports_renewals_without_values(tmp_path, capsys):
    secret = jwt(1_790_990_000, 1_790_993_600)
    cookie_header = f"wow-auth-token={secret}; Path=/\nbm_sv=zzz; Path=/"
    har = {"log": {"entries": [
        {"startedDateTime": "2026-10-03T05:00:00.000Z", "request": {"method": "GET", "url": "https://www.woolworths.com.au/api/ui/v2/bootstrap"},
         "response": {"status": 200, "headers": [], "cookies": []}},
        {"startedDateTime": "2026-10-03T05:55:00.000Z", "request": {"method": "POST", "url": "https://www.woolworths.com.au/api/v3/ui/authentication/token?x=1"},
         "response": {"status": 200, "headers": [{"name": "set-cookie", "value": cookie_header}], "cookies": []}},
    ]}}  # fmt: skip
    path = tmp_path / "x.har"
    path.write_text(json.dumps(har))
    assert har_session_scan.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "POST   200 www.woolworths.com.au/api/v3/ui/authentication/token" in out
    assert "exp" in out and "bm_sv" in out
    assert secret not in out and "zzz" not in out and "x=1" not in out
