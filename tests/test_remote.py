"""Unit tests for the small parsing/decision helpers in remote.py.

The Fabric I/O is mocked elsewhere (test_bootstrap.py). Here we just verify
the pure logic: pubkey parsing and authorized_keys filtering decisions, plus
the sudo-routing on TargetSSH.
"""
from unittest.mock import MagicMock, patch

from backup_setuper.remote import HetznerBox, HetznerBoxRef, TargetSSH, _key_body


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


def test_write_file_no_sudo_direct_put():
    t = _make_target(sudo=False)
    t.write_file("/root/foo", "x")
    # Direct put to final path, plain chmod via run
    assert t.conn.put.call_args.kwargs["remote"] == "/root/foo"
    run_cmds = [c.args[0] for c in t.conn.run.call_args_list]
    assert any("chmod 644 /root/foo" in c for c in run_cmds)
    assert not t.conn.sudo.called
