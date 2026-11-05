"""Tests for the `python -m app` entry point and the CLI-host mismatch warning."""

from __future__ import annotations

import logging
import sys

from app import __main__, config, main as app_main


def _fake_uvicorn_run(monkeypatch, calls: dict) -> None:
    def fake_run(app: str, **kwargs):
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(__main__.uvicorn, "run", fake_run)


def _default_settings():
    """Settings with every external source stripped: pure defaults."""
    return config.Settings(
        _env_file=None, host="127.0.0.1", port=8000
    )


def _patch_default_settings(monkeypatch):
    monkeypatch.delenv("FUBALA_HOST", raising=False)
    monkeypatch.delenv("FUBALA_PORT", raising=False)
    monkeypatch.setattr(app_main, "get_settings", _default_settings)


def test_main_binds_settings_host_and_port(monkeypatch):
    """FUBALA_HOST/FUBALA_PORT (Settings) decide the bind, nothing else."""
    calls: dict = {}
    _fake_uvicorn_run(monkeypatch, calls)
    monkeypatch.setenv("FUBALA_HOST", "0.0.0.0")
    monkeypatch.setenv("FUBALA_PORT", "8123")

    assert __main__.main() == 0
    assert calls["app"] == "app.main:app"
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 8123


def test_main_defaults_to_loopback_and_8000(monkeypatch):
    """No FUBALA_HOST/FUBALA_PORT anywhere (env or .env) -> 127.0.0.1:8000."""
    calls: dict = {}
    _fake_uvicorn_run(monkeypatch, calls)
    monkeypatch.delenv("FUBALA_HOST", raising=False)
    monkeypatch.delenv("FUBALA_PORT", raising=False)
    # the repo's backend/.env must not leak into the default expectations
    monkeypatch.setattr(
        __main__, "get_settings", lambda: config.Settings(_env_file=None)
    )

    assert __main__.main() == 0
    assert calls["app"] == "app.main:app"
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8000


def test_cli_host_mismatch_warns_exactly_once(monkeypatch, caplog):
    """A --host that disagrees with Settings.host yields ONE warning naming
    both values and stating the self-update bind; repeated calls stay silent."""
    monkeypatch.setattr(app_main, "_HOST_MISMATCH_WARNED", False)
    monkeypatch.setattr(
        sys, "argv", ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
    )
    _patch_default_settings(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        app_main._warn_cli_host_mismatch()
        app_main._warn_cli_host_mismatch()

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "0.0.0.0" in message  # the CLI value
    assert "127.0.0.1" in message  # the Settings value the self-update uses
    # the self-update restart is stated to use the Settings value
    assert "127.0.0.1:8000" in message


def test_cli_host_equals_form_mismatch_warns(monkeypatch, caplog):
    """--host=X spelling is parsed the same way."""
    monkeypatch.setattr(app_main, "_HOST_MISMATCH_WARNED", False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "--host=10.0.0.5", "app.main:app"])
    _patch_default_settings(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        app_main._warn_cli_host_mismatch()

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "10.0.0.5" in records[0].getMessage()
    assert "127.0.0.1" in records[0].getMessage()


def test_cli_host_match_is_silent(monkeypatch, caplog):
    """--host equal to Settings.host -> no warning."""
    monkeypatch.setattr(app_main, "_HOST_MISMATCH_WARNED", False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.main:app", "--host", "127.0.0.1"])
    _patch_default_settings(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        app_main._warn_cli_host_mismatch()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_cli_host_is_silent(monkeypatch, caplog):
    """pytest's own argv has no --host: the helper must not fire there."""
    monkeypatch.setattr(app_main, "_HOST_MISMATCH_WARNED", False)
    monkeypatch.setattr(sys, "argv", ["pytest", "-q"])
    _patch_default_settings(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        app_main._warn_cli_host_mismatch()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
