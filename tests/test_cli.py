import pytest

from agents_app_connector import __version__
from agents_app_connector.cli import EXIT_BAD_SETTINGS, SUBCOMMANDS, MigrateSettings, main


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("migrate", "admin", "app", "purge"):
        assert name in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agents-app-connector {__version__}"


def test_no_subcommand_exits_2(capsys):
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().out


def test_constants():
    assert EXIT_BAD_SETTINGS == 78
    assert set(SUBCOMMANDS) == {"migrate", "admin", "app", "purge"}


@pytest.mark.parametrize("command", ["migrate", "admin", "app", "purge"])
def test_bad_settings_exit_78(monkeypatch, capsys, command):
    import os

    for key in list(os.environ):
        if key.startswith("APPCONN_"):
            monkeypatch.delenv(key)
    assert main([command]) == EXIT_BAD_SETTINGS
    assert "APPCONN_DATABASE_URL" in capsys.readouterr().err


def test_migrate_settings_needs_only_database_url(monkeypatch):
    monkeypatch.setenv("APPCONN_DATABASE_URL", "postgresql://u:p@db/x")
    assert MigrateSettings().database_url.get_secret_value() == "postgresql://u:p@db/x"
