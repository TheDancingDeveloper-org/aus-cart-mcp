import json

from aus_cartwatch.__main__ import main


def test_db_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUS_CARTWATCH_DB", str(tmp_path / "cli.db"))
    assert main(["db", "migrate"]) == 0
    assert "0001_init.sql" in capsys.readouterr().out
    assert main(["db", "migrate"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert main(["db", "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"]["observations"] == 0
    assert main(["db", "backup"]) == 2
    assert main(["db", "backup", str(tmp_path / "b"), "--keep", "3"]) == 0
    assert "aus-cartwatch-" in capsys.readouterr().out
    assert main(["db", "compact"]) == 0
    assert "deleted" in json.loads(capsys.readouterr().out)
