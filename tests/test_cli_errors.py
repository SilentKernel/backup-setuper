from click.testing import CliRunner
from invoke.exceptions import UnexpectedExit
from unittest.mock import MagicMock

from backup_setuper import cli
from backup_setuper import bootstrap as bs


def _run_with(monkeypatch, exc, example_machine_path, example_secrets):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(bs, "check", boom)
    return CliRunner().invoke(
        cli.main, ["check", str(example_machine_path), "--secrets", str(example_secrets)]
    )


def test_unexpected_error_is_one_line(monkeypatch, example_machine_path, example_secrets):
    r = _run_with(monkeypatch, ValueError("bad\nthing happened"), example_machine_path, example_secrets)
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert "Error: thing happened (ValueError)" in r.output


def test_remote_failure_is_one_line(monkeypatch, example_machine_path, example_secrets):
    res = MagicMock(exited=3, stderr="noise\nFailed to purge: directory not found\n", command="rclone purge x")
    r = _run_with(monkeypatch, UnexpectedExit(res), example_machine_path, example_secrets)
    assert "Traceback" not in r.output
    assert "Error: remote command failed (exit 3): Failed to purge: directory not found" in r.output
