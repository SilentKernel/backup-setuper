from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backup_setuper.remote import TargetSSH


def _make_target() -> TargetSSH:
    """A TargetSSH whose Fabric Connection is fully mocked — no real SSH."""
    t = TargetSSH.__new__(TargetSSH)
    t.host = "example"
    t.sudo = False
    t.conn = MagicMock()
    return t


def _result(stdout: str = "", ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, ok=ok)


def test_ensure_known_host_skips_keyscan_when_already_trusted():
    t = _make_target()
    # ssh-keygen -F finds the entry on the first call — no mkdir/keyscan needed.
    t.conn.run.side_effect = [_result("[h]:23 ssh-ed25519 KEYBASE", ok=True)]

    added = t.ensure_known_host("h", 23)

    assert added is False
    calls = [c.args[0] for c in t.conn.run.call_args_list]
    assert any("ssh-keygen -F" in c for c in calls)
    assert not any("ssh-keyscan" in c for c in calls)
    # Fast path: never touches /root/.ssh.
    assert not any("mkdir" in c for c in calls)


def test_ensure_known_host_appends_when_unknown():
    t = _make_target()
    scan_output = "|1|HASH|HASH ssh-ed25519 AAAAFAKE\n|1|HASH|HASH ssh-rsa AAAAFAKE\n"
    t.conn.run.side_effect = [
        _result("", ok=False),                    # ssh-keygen -F → not found
        _result(""),                              # mkdir/touch
        _result(scan_output, ok=True),            # ssh-keyscan
        _result("", ok=True),                     # cat >> known_hosts
    ]

    added = t.ensure_known_host("h", 23)

    assert added is True
    calls = t.conn.run.call_args_list
    cmds = [c.args[0] for c in calls]
    assert any("ssh-keyscan -p 23" in c for c in cmds)
    # Last call is the append; its in_stream carries the scan output verbatim.
    append_call = calls[-1]
    assert "cat >> /root/.ssh/known_hosts" in append_call.args[0]
    assert append_call.kwargs["in_stream"].getvalue() == scan_output


def test_ensure_known_host_raises_when_keyscan_empty():
    t = _make_target()
    t.conn.run.side_effect = [
        _result("", ok=False),        # ssh-keygen -F → not found
        _result(""),                  # mkdir/touch
        _result("", ok=True),         # ssh-keyscan → empty stdout
    ]

    with pytest.raises(RuntimeError, match="ssh-keyscan returned nothing"):
        t.ensure_known_host("dead.host", 23)
