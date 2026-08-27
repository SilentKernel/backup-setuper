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


# ---------- wipe ----------

@pytest.fixture
def wipe_target(fake_target):
    fake_target.rclone_probe_repo.return_value = ("repo", "")
    return fake_target


def _real_machine(example_machine_path, example_secrets):
    return load_machine(example_machine_path, load_secrets(example_secrets))


def test_wipe_purges_then_reinits(monkeypatch, wipe_target, example_machine_path, example_secrets):
    m = _real_machine(example_machine_path, example_secrets)
    _patch_target(monkeypatch, wipe_target)

    bs.wipe(m, dest_name="ftp", assume_yes=True)

    ftp = next(d for d in m.destinations if d.name == "ftp")
    wipe_target.rclone_wipe.assert_called_once_with(ftp.rclone_remote, ftp.repo_path)
    wipe_target.restic_init.assert_called_once_with(ftp.repository_url, m.restic.password)

    # Ordering: the repo must be gone before we re-init it.
    order = [c[0] for c in wipe_target.mock_calls if c[0] in ("rclone_wipe", "restic_init")]
    assert order == ["rclone_wipe", "restic_init"]


def test_wipe_no_reinit_skips_restic_init(monkeypatch, wipe_target, example_machine_path, example_secrets):
    m = _real_machine(example_machine_path, example_secrets)
    _patch_target(monkeypatch, wipe_target)

    bs.wipe(m, dest_name="ftp", assume_yes=True, reinit=False)

    wipe_target.rclone_wipe.assert_called_once()
    wipe_target.restic_init.assert_not_called()


def test_wipe_refuses_hetzner_sftp(monkeypatch, wipe_target, example_machine_path, example_secrets):
    """rm -rf over SSH against a shared Storage Box is not something this tool does."""
    m = _real_machine(example_machine_path, example_secrets)
    _patch_target(monkeypatch, wipe_target)

    with pytest.raises(click.ClickException, match="rclone destinations only"):
        bs.wipe(m, dest_name="hetzner-hel", assume_yes=True)

    wipe_target.rclone_wipe.assert_not_called()


def test_wipe_rejects_unknown_destination(monkeypatch, wipe_target, example_machine_path, example_secrets):
    m = _real_machine(example_machine_path, example_secrets)
    _patch_target(monkeypatch, wipe_target)

    with pytest.raises(click.ClickException, match="unknown destination"):
        bs.wipe(m, dest_name="nope", assume_yes=True)

    wipe_target.rclone_wipe.assert_not_called()


def test_wipe_aborts_when_path_is_not_a_restic_repo(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    m = _real_machine(example_machine_path, example_secrets)
    wipe_target.rclone_probe_repo.return_value = ("absent", "")
    _patch_target(monkeypatch, wipe_target)

    with pytest.raises(click.ClickException, match="holds no restic"):
        bs.wipe(m, dest_name="ftp", assume_yes=True)

    wipe_target.rclone_wipe.assert_not_called()


def test_wipe_force_overrides_the_repo_guard(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    m = _real_machine(example_machine_path, example_secrets)
    wipe_target.rclone_probe_repo.return_value = ("absent", "")
    _patch_target(monkeypatch, wipe_target)

    bs.wipe(m, dest_name="ftp", assume_yes=True, force=True)

    wipe_target.rclone_wipe.assert_called_once()


def test_wipe_refuses_repo_path_that_would_hit_the_remote_root(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    m = _real_machine(example_machine_path, example_secrets)
    ftp = next(d for d in m.destinations if d.name == "ftp")
    ftp.repo_path = "/"
    _patch_target(monkeypatch, wipe_target)

    with pytest.raises(click.ClickException, match="remote's root"):
        bs.wipe(m, dest_name="ftp", assume_yes=True)

    wipe_target.rclone_wipe.assert_not_called()


def test_wipe_requires_typing_the_destination_name(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    m = _real_machine(example_machine_path, example_secrets)
    _patch_target(monkeypatch, wipe_target)
    monkeypatch.setattr(bs.click, "prompt", lambda *a, **k: "wrong")

    with pytest.raises(click.ClickException, match="confirmation did not match"):
        bs.wipe(m, dest_name="ftp")

    wipe_target.rclone_wipe.assert_not_called()


def test_wipe_proceeds_when_the_name_is_typed_correctly(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    m = _real_machine(example_machine_path, example_secrets)
    _patch_target(monkeypatch, wipe_target)
    monkeypatch.setattr(bs.click, "prompt", lambda *a, **k: "ftp")

    bs.wipe(m, dest_name="ftp")

    wipe_target.rclone_wipe.assert_called_once()


def test_wipe_refuses_when_the_backend_is_unreachable(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    """A locked/down backend must NOT be reported as "no repository here".

    Real case: Online.net locked the dedibackup space after the file quota was
    exceeded, so `rclone lsf` failed at login. Reporting that as an empty path
    sends the operator hunting for a missing repo instead of a blocked account.
    """
    m = _real_machine(example_machine_path, example_secrets)
    wipe_target.rclone_probe_repo.return_value = (
        "unreachable", "500 service unavailable")
    _patch_target(monkeypatch, wipe_target)

    with pytest.raises(click.ClickException, match="cannot reach") as exc:
        bs.wipe(m, dest_name="ftp", assume_yes=True)

    assert "500 service unavailable" in str(exc.value)
    wipe_target.rclone_wipe.assert_not_called()


def test_wipe_unreachable_is_not_forceable(
    monkeypatch, wipe_target, example_machine_path, example_secrets
):
    """--force overrides "empty path", never "backend down"."""
    m = _real_machine(example_machine_path, example_secrets)
    wipe_target.rclone_probe_repo.return_value = ("unreachable", "500 service unavailable")
    _patch_target(monkeypatch, wipe_target)

    with pytest.raises(click.ClickException, match="--force will not help"):
        bs.wipe(m, dest_name="ftp", assume_yes=True, force=True)

    wipe_target.rclone_wipe.assert_not_called()


def test_bootstrap_survives_one_failing_restic_init(
    monkeypatch, fake_target, example_machine_path, example_secrets
):
    """One unreachable destination must not abort the whole run with a traceback.

    Real case: the ftp repo probe failed, restic init then died, and bootstrap
    exploded *after* every script had already been deployed.
    """
    m = _real_machine(example_machine_path, example_secrets)
    fake_target.restic_repo_exists.return_value = False

    def _init(repo_url, _pw):
        if "online-ftp" in repo_url:
            raise RuntimeError("exit status 1")

    fake_target.restic_init.side_effect = _init
    _patch_target(monkeypatch, fake_target)
    _patch_hetzner_box(monkeypatch)

    with pytest.raises(click.ClickException, match="restic init failed for: ftp"):
        bs.bootstrap(m, dry_run=False, sudo_password=None)

    # Every destination was still attempted — no early abort.
    attempted = [c.args[0] for c in fake_target.restic_init.call_args_list]
    assert len(attempted) == len(m.destinations)
