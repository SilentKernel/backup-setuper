from pathlib import Path

import pytest
import yaml

from backup_setuper.config import ConfigError, load_machine, load_secrets


def test_load_example_machine(example_machine_path, example_secrets):
    secrets = load_secrets(example_secrets)
    m = load_machine(example_machine_path, secrets)
    assert m.name == "silentbox"
    assert m.target.host == "silentbox.example.com"
    assert m.restic.password == "TEST-RESTIC-PASS"
    names = [d.name for d in m.destinations]
    assert names == ["silentds", "ftp", "hetzner-hel", "hetzner-fsn"]
    # urls
    by_name = {d.name: d for d in m.destinations}
    assert by_name["silentds"].repository_url == "rclone:silentds-webdav:silentbox"
    assert by_name["ftp"].repository_url == "rclone:online-ftp:silentbox"
    assert by_name["hetzner-hel"].repository_url == "sftp://u123456@u123456.your-storagebox.de:23//home/silentbox"
    # hetzner subset
    assert {d.name for d in m.hetzner_destinations} == {"hetzner-hel", "hetzner-fsn"}


def test_missing_secret_ref(tmp_path, example_machine_path):
    p = tmp_path / "secrets.yaml"
    p.write_text("silentbox-restic-pass: foo\n")  # other refs missing
    with pytest.raises(ConfigError, match="silentds-hc"):
        load_machine(example_machine_path, load_secrets(p))


def test_unknown_kind(tmp_path, example_secrets):
    bad = {
        "machine": "x",
        "target": {"host": "h"},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/x"],
        "destinations": [
            {"name": "d", "kind": "magic-cloud", "healthcheck_ref": "silentds-hc",
             "schedule": {"hour": 1, "prune_minute": 0}},
        ],
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ConfigError, match="unknown kind"):
        load_machine(p, load_secrets(example_secrets))


def test_hour_collision(tmp_path, example_secrets):
    cfg = {
        "machine": "x",
        "target": {"host": "h"},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/x"],
        "destinations": [
            {"name": "a", "kind": "hetzner-sftp", "healthcheck_ref": "hetzner-hel-hc",
             "schedule": {"hour": 2, "prune_minute": 0},
             "sftp": {"user": "u", "host": "h", "repo_path": "/p"}},
            {"name": "b", "kind": "hetzner-sftp", "healthcheck_ref": "hetzner-fsn-hc",
             "schedule": {"hour": 2, "prune_minute": 15},
             "sftp": {"user": "u", "host": "h", "repo_path": "/p"}},
        ],
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="colliding backup hours"):
        load_machine(p, load_secrets(example_secrets))


def test_target_port_and_sudo_defaults(example_machine_path, example_secrets):
    m = load_machine(example_machine_path, load_secrets(example_secrets))
    # Defaults — silentbox.example.yaml does not set port or sudo.
    assert m.target.port == 22
    assert m.target.sudo is False


def test_target_port_and_sudo_overrides(tmp_path, example_secrets):
    cfg = {
        "machine": "silentbox",
        "target": {"host": "silentbox-2025", "user": "ludo", "port": 2244, "sudo": True},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/home/ludo"],
        "destinations": [
            {"name": "h", "kind": "hetzner-sftp", "healthcheck_ref": "hetzner-hel-hc",
             "schedule": {"hour": 2, "prune_minute": 0},
             "sftp": {"user": "u", "host": "h", "repo_path": "/p"}},
        ],
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    m = load_machine(p, load_secrets(example_secrets))
    assert m.target.host == "silentbox-2025"
    assert m.target.user == "ludo"
    assert m.target.port == 2244
    assert m.target.sudo is True


def test_load_kumabox_example(tmp_path):
    """The shipped machines/kumabox.example.yaml parses cleanly with the example secret refs."""
    secrets_data = {
        "kumabox-restic-pass": "TEST-RESTIC-PASS",
        "kumabox-silentds-kuma": "https://kuma.example.com/api/push/tok1",
        "kumabox-online-ftp-kuma": "https://kuma.example.com/api/push/tok2",
        "kumabox-online-ftp-pass": "TEST-FTP-PASS",
        "kumabox-hetzner-kuma": "https://kuma.example.com/api/push/tok3",
        "silentds-webdav-pass": "TEST-WEBDAV-PASS",
    }
    sp = tmp_path / "secrets.yaml"
    sp.write_text(yaml.safe_dump(secrets_data))
    example = Path(__file__).resolve().parent.parent / "machines" / "kumabox.example.yaml"
    m = load_machine(example, load_secrets(sp))
    assert m.name == "kumabox"
    kinds = {d.monitor.kind for d in m.destinations}
    assert kinds == {"kuma"}
    silentds = next(d for d in m.destinations if d.name == "silentds")
    assert silentds.monitor.success_url == "https://kuma.example.com/api/push/tok1?status=up&msg=OK&ping="
    assert silentds.monitor.has_start is False


def test_load_kuma_machine(kuma_machine_path, kuma_secrets):
    m = load_machine(kuma_machine_path, load_secrets(kuma_secrets))
    by_name = {d.name: d for d in m.destinations}
    silentds = by_name["silentds"]
    assert silentds.monitor.kind == "kuma"
    assert silentds.monitor.base == "https://kuma.example.com/api/push/abcDEF123"
    assert silentds.monitor.has_start is False
    assert silentds.monitor.start_url == ""
    assert silentds.monitor.success_url == "https://kuma.example.com/api/push/abcDEF123?status=up&msg=OK&ping="
    assert silentds.monitor.fail_url == "https://kuma.example.com/api/push/abcDEF123?status=down&msg=fail&ping="


def test_healthchecks_legacy_monitor_shape(example_machine_path, example_secrets):
    m = load_machine(example_machine_path, load_secrets(example_secrets))
    d = next(x for x in m.destinations if x.name == "silentds")
    assert d.monitor.kind == "healthchecks"
    assert d.monitor.has_start is True
    assert d.monitor.start_url == "https://hc-ping.com/11111111-1111-1111-1111-111111111111/start"
    assert d.monitor.success_url == "https://hc-ping.com/11111111-1111-1111-1111-111111111111"
    assert d.monitor.fail_url == "https://hc-ping.com/11111111-1111-1111-1111-111111111111/fail"


def test_monitor_and_legacy_both_rejected(tmp_path, example_secrets):
    cfg = {
        "machine": "x",
        "target": {"host": "h"},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/x"],
        "destinations": [
            {"name": "d", "kind": "hetzner-sftp",
             "healthcheck_ref": "silentds-hc",
             "monitor": {"kind": "healthchecks", "url_ref": "silentds-hc"},
             "schedule": {"hour": 1, "prune_minute": 0},
             "sftp": {"user": "u", "host": "h", "repo_path": "/p"}},
        ],
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="either 'healthcheck_ref' or 'monitor'"):
        load_machine(p, load_secrets(example_secrets))


def test_unknown_monitor_kind(tmp_path, example_secrets):
    cfg = {
        "machine": "x",
        "target": {"host": "h"},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/x"],
        "destinations": [
            {"name": "d", "kind": "hetzner-sftp",
             "monitor": {"kind": "pingdom", "url_ref": "silentds-hc"},
             "schedule": {"hour": 1, "prune_minute": 0},
             "sftp": {"user": "u", "host": "h", "repo_path": "/p"}},
        ],
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="unknown monitor kind"):
        load_machine(p, load_secrets(example_secrets))


def test_missing_monitor_block(tmp_path, example_secrets):
    cfg = {
        "machine": "x",
        "target": {"host": "h"},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/x"],
        "destinations": [
            {"name": "d", "kind": "hetzner-sftp",
             "schedule": {"hour": 1, "prune_minute": 0},
             "sftp": {"user": "u", "host": "h", "repo_path": "/p"}},
        ],
    }
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="missing 'monitor'"):
        load_machine(p, load_secrets(example_secrets))


def test_quote_safe_rejects_single_quote(tmp_path, example_machine_path):
    p = tmp_path / "secrets.yaml"
    p.write_text("silentbox-restic-pass: \"bad'value\"\nsilentds-hc: x\nsilentds-webdav-pass: x\nonline-ftp-hc: x\nonline-ftp-pass: x\nhetzner-hel-hc: x\nhetzner-fsn-hc: x\n")
    with pytest.raises(ConfigError, match="single quote"):
        load_machine(example_machine_path, load_secrets(p))


# ---------- pack_size_mib / per-destination retention ----------

def _one_dest_machine(tmp_path, restic_extra=None, **dest_extra):
    """A minimal one-destination machine, with extra keys spliced into the destination."""
    secrets = tmp_path / "s.yaml"
    secrets.write_text(yaml.safe_dump({"pw": "PASS", "hc": "1111"}))
    dest = {
        "name": "ftp",
        "kind": "rclone-ftp",
        "healthcheck_ref": "hc",
        "rclone_remote": "online-ftp",
        "repo_path": "box",
        "schedule": {"hour": 3, "prune_minute": 15},
        "ftp": {"host": "h", "user": "u", "password_ref": "pw"},
    }
    dest.update(dest_extra)
    restic = {"password_ref": "pw"}
    restic.update(restic_extra or {})
    cfg = tmp_path / "m.yaml"
    cfg.write_text(yaml.safe_dump({
        "machine": "m",
        "target": {"host": "h"},
        "restic": restic,
        "sources": ["/home/x"],
        "destinations": [dest],
    }))
    return load_machine(cfg, load_secrets(secrets))


def test_pack_size_mib_defaults_to_none(tmp_path):
    assert _one_dest_machine(tmp_path).destinations[0].pack_size_mib is None


def test_pack_size_mib_parses(tmp_path):
    m = _one_dest_machine(tmp_path, pack_size_mib=128)
    assert m.destinations[0].pack_size_mib == 128


@pytest.mark.parametrize("bad", [0, -1, 129, 1024])
def test_pack_size_mib_out_of_range_rejected(tmp_path, bad):
    """128 MiB is restic's hard MaxPackSize; anything outside 1..128 is a config error."""
    with pytest.raises(ConfigError, match="pack_size_mib"):
        _one_dest_machine(tmp_path, pack_size_mib=bad)


def test_pack_size_mib_non_integer_rejected(tmp_path):
    with pytest.raises(ConfigError, match="must be an integer"):
        _one_dest_machine(tmp_path, pack_size_mib="big")


def test_destination_retention_defaults_to_none(tmp_path):
    assert _one_dest_machine(tmp_path).destinations[0].retention is None


def test_destination_retention_parses(tmp_path):
    m = _one_dest_machine(tmp_path, retention="--keep-daily 7 --keep-weekly 4")
    assert m.destinations[0].retention == "--keep-daily 7 --keep-weekly 4"


@pytest.mark.parametrize("bad", [
    "--keep-daily 7; id",
    "--keep-daily $(id)",
    "--keep-daily 7 && id",
    "--keep-daily 7 | tee out",
    "--keep-daily `id`",
    "--keep-daily 7 > out",
])
def test_destination_retention_rejects_shell_metacharacters(tmp_path, bad):
    """retention is interpolated UNQUOTED into the generated bash — keep it to plain words."""
    with pytest.raises(ConfigError, match="not allowed"):
        _one_dest_machine(tmp_path, retention=bad)


def test_machine_global_retention_also_validated(tmp_path):
    """The pre-existing machine-global field goes through the same guard."""
    with pytest.raises(ConfigError, match="restic.retention"):
        _one_dest_machine(tmp_path, restic_extra={"retention": "--keep-daily 30; id"})


# ---------- schedule.prune_daily ----------

def test_prune_daily_defaults_to_false(tmp_path):
    assert _one_dest_machine(tmp_path).destinations[0].schedule.prune_daily is False


@pytest.mark.parametrize("value", [True, False])
def test_prune_daily_parses(tmp_path, value):
    m = _one_dest_machine(tmp_path, schedule={"hour": 3, "prune_minute": 15, "prune_daily": value})
    assert m.destinations[0].schedule.prune_daily is value


@pytest.mark.parametrize("bad", ["yes please", "true", 3, 1, None])
def test_prune_daily_rejects_non_bool(tmp_path, bad):
    with pytest.raises(ConfigError, match="prune_daily"):
        _one_dest_machine(tmp_path, schedule={"hour": 3, "prune_minute": 15, "prune_daily": bad})
