import pytest
"""Unit tests for the small parsing/decision helpers in remote.py.

The Fabric I/O is mocked elsewhere (test_bootstrap.py). Here we just verify
the pure logic: pubkey parsing and authorized_keys filtering decisions, plus
the sudo-routing on TargetSSH.
"""
import os
from unittest.mock import MagicMock, patch

from backup_setuper.remote import (
    HetznerBox,
    HetznerBoxRef,
    TargetSSH,
    _auth_hint,
    _key_body,
    ensure_agent_socket,
)


def test_key_body_parses_standard_lines():
    body = "AAAAC3NzaC1lZDI1NTE5AAAAIK1234567890abc"
    assert _key_body(f"ssh-ed25519 {body} user@host") == body
    assert _key_body(f"ssh-rsa {body}") == body
    assert _key_body("# comment line") is None
    assert _key_body("") is None
    assert _key_body("garbage") is None


def _box_with_authorized_keys(content: str) -> HetznerBox:
    box = HetznerBox.__new__(HetznerBox)
    box.ref = HetznerBoxRef(user="u1", host="h", port=23)
    box.conn = MagicMock()
    box.conn.run.return_value.stdout = content
    box.conn.run.return_value.ok = True
    return box


def test_install_key_skips_when_already_present():
    body = "AAAAC3NzaC1lZDI1NTE5AAAAIK1234567890abc"
    pubkey = f"ssh-ed25519 {body} silentbox"
    box = _box_with_authorized_keys(f"ssh-ed25519 {body} mac-existing\n")
    assert box.install_key(pubkey) == "already-present"
    # install-ssh-key NOT called
    calls = [c for c in box.conn.run.call_args_list if c.args and "install-ssh-key" in c.args[0]]
    assert not calls


def test_install_key_runs_when_absent():
    pubkey = "ssh-ed25519 NEWBODYabc silentbox"
    box = _box_with_authorized_keys("ssh-ed25519 OLDBODYxyz mac\n")
    assert box.install_key(pubkey) == "installed"
    calls = [c for c in box.conn.run.call_args_list if c.args and "install-ssh-key" in c.args[0]]
    assert len(calls) == 1


def test_revoke_key_removes_matching_line_keeps_others():
    body_target = "TARGETbody"
    body_mac = "MACbody"
    pubkey = f"ssh-ed25519 {body_target} silentbox"
    box = _box_with_authorized_keys(
        f"ssh-ed25519 {body_mac} mac\n"
        f"ssh-ed25519 {body_target} silentbox\n"
    )
    assert box.revoke_key(pubkey) == "revoked"
    # The put() call should contain only the Mac key.
    put_calls = [c for c in box.conn.put.call_args_list]
    assert len(put_calls) == 1
    written = put_calls[0].args[0].getvalue().decode("utf-8")
    assert body_mac in written
    assert body_target not in written


def test_revoke_key_refuses_when_it_would_empty_the_file():
    body_target = "ONLYKEYbody"
    pubkey = f"ssh-ed25519 {body_target} silentbox"
    box = _box_with_authorized_keys(f"ssh-ed25519 {body_target} silentbox\n")
    assert box.revoke_key(pubkey) == "refused"
    # NEVER writes when it would lock you out.
    assert not box.conn.put.called


def test_revoke_key_not_present():
    pubkey = "ssh-ed25519 MISSINGbody silentbox"
    box = _box_with_authorized_keys("ssh-ed25519 OTHERbody mac\n")
    assert box.revoke_key(pubkey) == "not-present"
    assert not box.conn.put.called


# ---------- TargetSSH sudo routing ----------

def _make_target(sudo: bool, sudo_password: str | None = None) -> TargetSSH:
    with patch("backup_setuper.remote.Connection") as MockConn:
        instance = MockConn.return_value
        # Default both .run and .sudo to a result object with ok=True and stdout="".
        for attr in ("run", "sudo"):
            getattr(instance, attr).return_value.ok = True
            getattr(instance, attr).return_value.stdout = ""
        t = TargetSSH(host="x", user="ludo", sudo=sudo, sudo_password=sudo_password)
    return t


def test_target_uses_sudo_method_when_enabled():
    t = _make_target(sudo=True, sudo_password="secret")
    t.ensure_dir("/root/x")
    # Goes through sudo, not run
    assert t.conn.sudo.called
    assert not t.conn.run.called


def test_target_uses_run_method_when_sudo_disabled():
    t = _make_target(sudo=False)
    t.ensure_dir("/root/x")
    assert t.conn.run.called
    assert not t.conn.sudo.called


def test_write_file_sudo_uses_tmp_then_install():
    t = _make_target(sudo=True, sudo_password="secret")
    t.write_file("/root/backup-scripts/foo.sh", "echo hi\n", mode="755")
    # SFTP put landed in /tmp/bs-*
    put_calls = t.conn.put.call_args_list
    assert len(put_calls) == 1
    tmp_path = put_calls[0].kwargs["remote"]
    assert tmp_path.startswith("/tmp/bs-")
    # sudo was called with install + cleanup
    sudo_cmds = [c.args[0] for c in t.conn.sudo.call_args_list]
    assert any("install -m 755 -o root -g root" in c for c in sudo_cmds)
    assert any("rm -f" in c and "/tmp/bs-" in c for c in sudo_cmds)


def test_sudo_wraps_chained_commands_in_bash_c():
    """Regression: chained `&&` commands must ride ONE sudo invocation.

    Without the wrap, `sudo -S -p '…' mkdir … && chmod …` runs `chmod` as
    the SSH user (not root), breaking `ensure_dir`/`ensure_known_host` when
    `sudo: true` is set on a non-root SSH user.
    """
    t = _make_target(sudo=True, sudo_password="secret")
    t.ensure_dir("/root/x", mode="700")
    sudo_cmds = [c.args[0] for c in t.conn.sudo.call_args_list]
    assert len(sudo_cmds) == 1
    cmd = sudo_cmds[0]
    # Wrapped in bash -c so the remote shell parses && INSIDE sudo's command.
    assert cmd.startswith("bash -c ")
    # The full chain (both halves) is inside the single sudo'd payload.
    assert "mkdir -p /root/x" in cmd
    assert "chmod 700 /root/x" in cmd


def _last_check_cmd(t: TargetSSH, sudo: bool) -> str:
    method = t.conn.sudo if sudo else t.conn.run
    cmds = [c.args[0] for c in method.call_args_list if c.args and "restic" in c.args[0] and "check" in c.args[0]]
    return cmds[-1]


def test_restic_check_structural_no_read_data():
    t = _make_target(sudo=False)
    assert t.restic_check("rclone:r:p", "PASS") is True
    cmd = _last_check_cmd(t, sudo=False)
    assert "restic -r rclone:r:p check" in cmd
    assert "--read-data" not in cmd


def test_restic_check_read_data_flag():
    t = _make_target(sudo=False)
    t.restic_check("rclone:r:p", "PASS", read_data=True)
    cmd = _last_check_cmd(t, sudo=False)
    assert cmd.endswith("check --read-data")


def test_restic_check_read_data_subset_overrides_read_data():
    t = _make_target(sudo=False)
    t.restic_check("rclone:r:p", "PASS", read_data=True, read_data_subset="10%")
    cmd = _last_check_cmd(t, sudo=False)
    # subset wins; the bare --read-data flag is not also appended
    assert cmd.endswith("check --read-data-subset 10%")
    assert "check --read-data-subset" in cmd


def test_restic_check_routes_through_sudo():
    t = _make_target(sudo=True, sudo_password="secret")
    t.restic_check("rclone:r:p", "PASS")
    assert t.conn.sudo.called
    cmd = _last_check_cmd(t, sudo=True)
    assert cmd.startswith("bash -c ")
    assert "restic -r" in cmd and "check" in cmd


def test_rclone_wipe_purges_on_a_single_connection():
    """FTP servers with a per-account connection cap answer "530 bad login" to the
    extra logins rclone's default 8 checkers open, leaving the purge half done."""
    t = _make_target(sudo=False)
    assert t.rclone_wipe("online-ftp", "silentbox25") is True
    cmd = t.conn.run.call_args[0][0]
    assert cmd.startswith("rclone purge --checkers 1 ")
    assert cmd.endswith(" online-ftp:silentbox25")
    assert "--stats 30s" in cmd


@pytest.mark.parametrize("sudo", [False, True])
def test_target_never_forwards_local_stdin(sudo):
    """invoke 2.2.1's stdin reader crashes on Python 3.14 when a key is pressed
    mid-command; nothing we run needs the local keyboard."""
    t = TargetSSH(host="x", user="ludo", sudo=sudo, sudo_password="p" if sudo else None)
    assert t.conn.config.run.in_stream is False


def test_write_file_no_sudo_direct_put():
    t = _make_target(sudo=False)
    t.write_file("/root/foo", "x")
    # Direct put to final path, plain chmod via run
    assert t.conn.put.call_args.kwargs["remote"] == "/root/foo"
    run_cmds = [c.args[0] for c in t.conn.run.call_args_list]
    assert any("chmod 644 /root/foo" in c for c in run_cmds)
    assert not t.conn.sudo.called


# ---------- IdentityAgent -> SSH_AUTH_SOCK ----------
#
# paramiko honours only $SSH_AUTH_SOCK and ignores IdentityAgent, so external
# agents (1Password, Secretive, gpg-agent) fail with "No authentication methods
# available" even though plain `ssh` works. ensure_agent_socket bridges that.

def _ssh_config(tmp_path, body: str):
    p = tmp_path / "config"
    p.write_text(body)
    return p


def test_agent_socket_set_from_identity_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    sock = tmp_path / "agent.sock"
    sock.touch()
    cfg = _ssh_config(tmp_path, f"Host silentbox\n\tIdentityAgent {sock}\n")

    assert ensure_agent_socket("silentbox", cfg) == str(sock)
    assert os.environ["SSH_AUTH_SOCK"] == str(sock)


def test_agent_socket_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    sock = tmp_path / ".1password" / "agent.sock"
    sock.parent.mkdir()
    sock.touch()
    cfg = _ssh_config(tmp_path, "Host *\n\tIdentityAgent ~/.1password/agent.sock\n")

    assert ensure_agent_socket("anything", cfg) == str(sock)


def test_agent_socket_respects_existing_env(tmp_path, monkeypatch):
    """An explicitly-set SSH_AUTH_SOCK always wins — we never override the user."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/keyring/ssh")
    sock = tmp_path / "agent.sock"
    sock.touch()
    cfg = _ssh_config(tmp_path, f"Host *\n\tIdentityAgent {sock}\n")

    assert ensure_agent_socket("h", cfg) is None
    assert os.environ["SSH_AUTH_SOCK"] == "/run/user/1000/keyring/ssh"


def test_agent_socket_ignores_missing_socket(tmp_path, monkeypatch):
    """A stale IdentityAgent path must not set SSH_AUTH_SOCK to a dead socket."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    cfg = _ssh_config(tmp_path, f"Host *\n\tIdentityAgent {tmp_path / 'gone.sock'}\n")

    assert ensure_agent_socket("h", cfg) is None
    assert "SSH_AUTH_SOCK" not in os.environ


def test_agent_socket_no_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ensure_agent_socket("h", tmp_path / "nope") is None
    assert "SSH_AUTH_SOCK" not in os.environ


def test_agent_socket_without_identity_agent_directive(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    cfg = _ssh_config(tmp_path, "Host silentbox\n\tPort 2244\n")
    assert ensure_agent_socket("silentbox", cfg) is None


def test_agent_socket_survives_malformed_config(tmp_path, monkeypatch):
    """A broken ~/.ssh/config must not abort the run."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    cfg = _ssh_config(tmp_path, "this is not\x00 valid ssh config {{{\n")
    assert ensure_agent_socket("h", cfg) is None


def test_auth_hint_mentions_the_agent_when_none_is_reachable(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    hint = _auth_hint("silentbox", Exception("No authentication methods available"))
    assert "SSH_AUTH_SOCK" in hint
    assert "agent.sock" in hint


def test_auth_hint_mentions_approval_when_an_agent_is_set(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/home/u/.1password/agent.sock")
    hint = _auth_hint("silentbox", Exception("Authentication timeout."))
    assert "/home/u/.1password/agent.sock" in hint
    assert "approve" in hint.lower()


def test_enter_rejects_wrong_sudo_password_cleanly():
    from invoke.exceptions import AuthFailure
    t = _make_target(sudo=True, sudo_password="typo")
    t.conn.sudo.side_effect = AuthFailure(result=None, prompt="[sudo] ")
    with pytest.raises(RuntimeError, match="sudo password rejected"):
        t.__enter__()
    assert t.conn.close.called


def test_rclone_wipe_treats_missing_path_as_done():
    """An interrupted purge can finish server-side; re-running must not fail."""
    t = _make_target(sudo=False)
    t.conn.run.return_value.ok = False
    t.conn.run.return_value.exited = 3
    t.conn.run.return_value.stderr = "Failed to list: directory not found"
    assert t.rclone_wipe("online-ftp", "silentbox25") is False
    cmds = [c[0][0] for c in t.conn.run.call_args_list]
    assert not any("purge" in c for c in cmds)
