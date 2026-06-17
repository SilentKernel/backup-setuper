from contextlib import contextmanager
from unittest.mock import MagicMock

import click
import pytest

from backup_setuper import bootstrap as bs
from backup_setuper.config import load_machine, load_secrets


@pytest.fixture
def fake_target():
    t = MagicMock()
    t.has.return_value = True
    t.ensure_ssh_key.return_value = "ssh-ed25519 AAAAFAKEKEYBODY backup-setuper-test"
    t.restic_repo_exists.return_value = True  # skip restic init
    t.list_files.return_value = []
    t.rclone_obscure.return_value = "obscured"
    return t


def _patch_target(monkeypatch, target):
    @contextmanager
    def _open(_machine, _pwd):
        yield target
    monkeypatch.setattr(bs, "_open_target", _open)


def _patch_hetzner_box(monkeypatch):
    box = MagicMock()
    box.install_key.return_value = "already-present"

    @contextmanager
    def _ctor(_ref):
        yield box
    monkeypatch.setattr(bs, "HetznerBox", _ctor)
    return box


def test_bootstrap_does_not_create_firewall_scripts_dir(
    monkeypatch, fake_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    _patch_target(monkeypatch, fake_target)
    _patch_hetzner_box(monkeypatch)

    bs.bootstrap(machine, dry_run=False, sudo_password=None)

    # All positional paths passed to ensure_dir across the run.
    dirs_created = [c.args[0] for c in fake_target.ensure_dir.call_args_list]
    assert "/root/backup-scripts" in dirs_created
    assert "/root/backup-logs" in dirs_created
    assert "/root/.config/rclone" in dirs_created
    # The actual fix: firewall-scripts must NOT be created.
    assert "/root/firewall-scripts" not in dirs_created


def test_bootstrap_pretrusts_hetzner_hosts_before_restic_init(
    monkeypatch, fake_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    fake_target.ensure_known_host.return_value = True
    _patch_target(monkeypatch, fake_target)
    _patch_hetzner_box(monkeypatch)

    bs.bootstrap(machine, dry_run=False, sudo_password=None)

    # One ensure_known_host call per hetzner destination, with the right (host, port).
    scanned = [c.args for c in fake_target.ensure_known_host.call_args_list]
    expected = {(d.sftp.host, d.sftp.port) for d in machine.hetzner_destinations}
    assert set(scanned) == expected

    # Ordering: every ensure_known_host call must precede any restic_repo_exists call.
    method_order = [c[0] for c in fake_target.mock_calls if c[0] in ("ensure_known_host", "restic_repo_exists")]
    last_scan = max(i for i, m in enumerate(method_order) if m == "ensure_known_host")
    first_probe = min(i for i, m in enumerate(method_order) if m == "restic_repo_exists")
    assert last_scan < first_probe, f"ensure_known_host must run before restic_repo_exists: {method_order}"


def test_init_repos_pretrusts_hetzner_hosts(
    monkeypatch, fake_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    fake_target.ensure_known_host.return_value = False
    _patch_target(monkeypatch, fake_target)

    bs.init_repos(machine, sudo_password=None)

    scanned = {c.args for c in fake_target.ensure_known_host.call_args_list}
    expected = {(d.sftp.host, d.sftp.port) for d in machine.hetzner_destinations}
    assert scanned == expected


# ---------- check ----------

def _in_sync_script(password: str) -> str:
    return f"#!/usr/bin/env bash\nexport RESTIC_PASSWORD='{password}'\nexport RESTIC_REPOSITORY='x'\n"


@pytest.fixture
def check_target(fake_target):
    """fake_target tuned for `check`: healthy repos + in-sync deployed scripts."""
    fake_target.restic_check.return_value = True
    fake_target.read_file.return_value = _in_sync_script("TEST-RESTIC-PASS")
    fake_target.ensure_known_host.return_value = False
    return fake_target


def test_check_runs_once_per_destination(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    _patch_target(monkeypatch, check_target)

    bs.check(machine, sudo_password=None)

    assert check_target.restic_check.call_count == len(machine.destinations)


def test_check_single_destination(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    _patch_target(monkeypatch, check_target)

    bs.check(machine, dest_name="ftp", sudo_password=None)

    assert check_target.restic_check.call_count == 1
    repo_url = check_target.restic_check.call_args.args[0]
    assert repo_url == "rclone:online-ftp:silentbox"


def test_check_unknown_destination_raises(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    _patch_target(monkeypatch, check_target)

    with pytest.raises(click.ClickException) as e:
        bs.check(machine, dest_name="nope", sudo_password=None)
    assert "unknown destination" in str(e.value)


def test_check_pretrusts_hetzner_before_check(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    _patch_target(monkeypatch, check_target)

    bs.check(machine, sudo_password=None)

    order = [c[0] for c in check_target.mock_calls if c[0] in ("ensure_known_host", "restic_check")]
    last_scan = max(i for i, m in enumerate(order) if m == "ensure_known_host")
    first_check = min(i for i, m in enumerate(order) if m == "restic_check")
    assert last_scan < first_check


def test_check_failing_repo_raises(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    check_target.restic_check.return_value = False
    _patch_target(monkeypatch, check_target)

    with pytest.raises(click.ClickException):
        bs.check(machine, dest_name="ftp", sudo_password=None)


def test_check_forwards_read_data_flags(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    _patch_target(monkeypatch, check_target)

    bs.check(machine, dest_name="ftp", read_data=True, read_data_subset="10%", sudo_password=None)

    kwargs = check_target.restic_check.call_args.kwargs
    assert kwargs["read_data"] is True
    assert kwargs["read_data_subset"] == "10%"


def test_check_password_drift_raises(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    # restic check passes, but the deployed script has a stale password.
    check_target.read_file.return_value = _in_sync_script("STALE-PASS")
    _patch_target(monkeypatch, check_target)

    with pytest.raises(click.ClickException):
        bs.check(machine, dest_name="ftp", sudo_password=None)


def test_check_missing_script_raises(
    monkeypatch, check_target, example_machine_path, example_secrets
):
    machine = load_machine(example_machine_path, load_secrets(example_secrets))
    check_target.read_file.return_value = None  # script not deployed
    _patch_target(monkeypatch, check_target)

    with pytest.raises(click.ClickException):
        bs.check(machine, dest_name="ftp", sudo_password=None)


def test_script_password_extraction():
    assert bs._script_password(_in_sync_script("SECRET")) == "SECRET"
    assert bs._script_password("no password line here\n") is None
    assert bs._script_password("export RESTIC_PASSWORD=noquotes\n") is None
