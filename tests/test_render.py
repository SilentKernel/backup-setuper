from pathlib import Path

from backup_setuper.config import load_machine, load_secrets
from backup_setuper.render import (
    build_rclone_remotes,
    render_all,
    render_backup_script,
    render_cron,
    render_prune_script,
    render_rclone_conf,
)


def _machine(example_machine_path, example_secrets):
    return load_machine(example_machine_path, load_secrets(example_secrets))


def test_backup_script_silentds(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    silentds = next(d for d in m.destinations if d.name == "silentds")
    out = render_backup_script(m, silentds)
    assert out.startswith("#!/usr/bin/env bash\n")
    assert "https://hc-ping.com/11111111-1111-1111-1111-111111111111" in out
    assert "export RESTIC_PASSWORD='TEST-RESTIC-PASS'" in out
    assert "export RESTIC_REPOSITORY='rclone:silentds-webdav:silentbox'" in out
    assert "--exclude='/home/ludo/.*'" in out
    assert "--exclude='/home/ludo/internal-api.example.com/data'" in out
    assert "--tag silentbox" in out
    assert "/home/ludo /root/backup-scripts /root/firewall-scripts" in out
    assert "${HC_START_URL}" in out
    assert "${HC_OK_URL}" in out
    assert "${HC_FAIL_URL}" in out
    assert "https://hc-ping.com/11111111-1111-1111-1111-111111111111/start" in out
    assert "https://hc-ping.com/11111111-1111-1111-1111-111111111111/fail" in out
    assert "exit $EXIT_CODE" in out.strip().splitlines()[-1]


def test_backup_script_hetzner_uses_sftp_url(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    hel = next(d for d in m.destinations if d.name == "hetzner-hel")
    out = render_backup_script(m, hel)
    assert "export RESTIC_REPOSITORY='sftp://u123456@u123456.your-storagebox.de:23//home/silentbox'" in out


def test_prune_script(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    silentds = next(d for d in m.destinations if d.name == "silentds")
    out = render_prune_script(m, silentds)
    assert out.startswith("#!/usr/bin/env bash\n")
    assert "restic forget --keep-daily 30 --prune" in out
    assert "https://hc-ping.com/11111111-1111-1111-1111-111111111111" in out


def test_cron_block(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    out = render_cron(m)
    # All 4 destinations show up for both backup and prune sections.
    for name in ("silentds", "ftp", "hetzner-hel", "hetzner-fsn"):
        assert f"backup-restic-{name}.sh" in out
        assert f"prune-restic-{name}.sh" in out
    # Hours and minutes are honored.
    assert "0 4  * * * /root/backup-scripts/backup-restic-silentds.sh" in out
    assert "0  22 * * 0 /root/backup-scripts/prune-restic-silentds.sh" in out
    assert "45 22 * * 0 /root/backup-scripts/prune-restic-hetzner-fsn.sh" in out


def test_rclone_conf(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    remotes = build_rclone_remotes(m)
    names = {r.name for r in remotes}
    assert names == {"silentds-webdav", "online-ftp"}
    conf = render_rclone_conf(remotes)
    assert "[silentds-webdav]" in conf
    assert "type = webdav" in conf
    assert "url = http://silentds:5005" in conf
    assert "vendor = other" in conf
    assert "user = silentbox" in conf
    assert "pass = TEST-WEBDAV-PASS" in conf   # cleartext at render time; obscured on target

    assert "[online-ftp]" in conf
    assert "type = ftp" in conf
    assert "host = dedibackup-dc3.online.net" in conf
    assert "port = 21" in conf
    assert "pass = TEST-FTP-PASS" in conf


def test_rclone_conf_ftp_custom_port(tmp_path: Path, example_secrets):
    machine_yaml = """
machine: ftp-custom-port
target:
  host: example.com
restic:
  password_ref: silentbox-restic-pass
sources: [/etc]
destinations:
  - name: ftp-alt
    kind: rclone-ftp
    healthcheck_ref: silentds-hc
    rclone_remote: ftp-alt
    repo_path: repo
    schedule: { hour: 5, prune_minute: 10 }
    ftp:
      host: ftp.example.com
      user: bob
      port: 2121
      password_ref: online-ftp-pass
"""
    p = tmp_path / "machine.yaml"
    p.write_text(machine_yaml)
    m = load_machine(p, load_secrets(example_secrets))
    conf = render_rclone_conf(build_rclone_remotes(m))
    assert "[ftp-alt]" in conf
    assert "port = 2121" in conf


def test_backup_script_kuma_skips_start_ping(kuma_machine_path, kuma_secrets):
    m = load_machine(kuma_machine_path, load_secrets(kuma_secrets))
    d = next(x for x in m.destinations if x.name == "silentds")
    out = render_backup_script(m, d)
    assert "https://kuma.example.com/api/push/abcDEF123?status=up&msg=OK&ping=" in out
    assert "https://kuma.example.com/api/push/abcDEF123?status=down&msg=fail&ping=" in out
    # Kuma has no /start endpoint — the start curl line should not be emitted.
    assert out.count('curl -fsS') == 2
    assert "HC_START_URL=''" in out
    assert "hc-ping.com" not in out


def test_prune_script_kuma_skips_start_ping(kuma_machine_path, kuma_secrets):
    m = load_machine(kuma_machine_path, load_secrets(kuma_secrets))
    d = next(x for x in m.destinations if x.name == "silentds")
    out = render_prune_script(m, d)
    assert "?status=up&msg=OK&ping=" in out
    assert "?status=down&msg=fail&ping=" in out
    assert out.count('curl -fsS') == 2
    assert "hc-ping.com" not in out


def test_backup_script_healthchecks_keeps_start_ping(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    d = next(x for x in m.destinations if x.name == "silentds")
    out = render_backup_script(m, d)
    # Healthchecks path: 3 curls (start + ok-or-fail branches).
    assert out.count('curl -fsS') == 3
    assert "/start" in out
    assert "/fail" in out


def test_render_all_writes_expected_files(tmp_path: Path, example_machine_path, example_secrets):
    from backup_setuper.render import write_bundle
    m = _machine(example_machine_path, example_secrets)
    bundle = render_all(m)
    out_dir = tmp_path / "out"
    write_bundle(bundle, out_dir)
    written = {p.name for p in out_dir.iterdir()}
    expected = {
        "backup-restic-silentds.sh", "backup-restic-ftp.sh",
        "backup-restic-hetzner-hel.sh", "backup-restic-hetzner-fsn.sh",
        "prune-restic-silentds.sh", "prune-restic-ftp.sh",
        "prune-restic-hetzner-hel.sh", "prune-restic-hetzner-fsn.sh",
        "cron.txt",
    }
    assert expected == written
    # Backup/prune scripts are executable.
    for n in expected - {"cron.txt"}:
        assert (out_dir / n).stat().st_mode & 0o100, f"{n} not executable"


# ---------- pack size ----------

def test_pack_size_exported_in_both_scripts_for_ftp(example_machine_path, example_secrets):
    """restic requires RESTIC_PACK_SIZE on every repo-modifying command, so it must
    appear in the backup script AND the prune script."""
    m = _machine(example_machine_path, example_secrets)
    ftp = next(d for d in m.destinations if d.name == "ftp")
    assert "export RESTIC_PACK_SIZE=128" in render_backup_script(m, ftp)
    assert "export RESTIC_PACK_SIZE=128" in render_prune_script(m, ftp)


def test_pack_size_block_keeps_the_blank_line_separator(example_machine_path, example_secrets):
    """The conditional block must not eat the blank line after RESTIC_REPOSITORY —
    a `{% endif -%}` here silently reflows every generated script."""
    m = _machine(example_machine_path, example_secrets)
    for name in ("silentds", "ftp"):
        d = next(x for x in m.destinations if x.name == name)
        for out in (render_backup_script(m, d), render_prune_script(m, d)):
            lines = out.splitlines()
            i = next(i for i, l in enumerate(lines) if l.startswith("export RESTIC_REPOSITORY="))
            tail = lines[i + 1:]
            if d.pack_size_mib:
                assert tail[0].startswith("# Taille des paquets"), name
                assert tail[1].startswith("export RESTIC_PACK_SIZE="), name
                assert tail[2] == "", f"{name}: blank line lost after the pack-size block"
            else:
                assert tail[0] == "", f"{name}: blank line lost after RESTIC_REPOSITORY"


def test_pack_size_absent_when_not_configured(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    for name in ("silentds", "hetzner-hel", "hetzner-fsn"):
        d = next(x for x in m.destinations if x.name == name)
        assert "RESTIC_PACK_SIZE" not in render_backup_script(m, d), name
        assert "RESTIC_PACK_SIZE" not in render_prune_script(m, d), name


# ---------- per-destination retention ----------

def test_ftp_prune_uses_destination_retention_override(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    ftp = next(d for d in m.destinations if d.name == "ftp")
    out = render_prune_script(m, ftp)
    assert "restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune" in out
    assert "--keep-daily 30" not in out


def test_other_destinations_keep_machine_global_retention(example_machine_path, example_secrets):
    m = _machine(example_machine_path, example_secrets)
    for name in ("silentds", "hetzner-hel", "hetzner-fsn"):
        d = next(x for x in m.destinations if x.name == name)
        assert "restic forget --keep-daily 30 --prune" in render_prune_script(m, d), name


# ---------- generated bash stays valid ----------

def test_all_rendered_scripts_are_valid_bash(tmp_path, example_machine_path, example_secrets):
    """Guards the Jinja whitespace control around the new conditional blocks."""
    import subprocess

    m = _machine(example_machine_path, example_secrets)
    bundle = render_all(m)
    for name, content in {**bundle.backup_scripts, **bundle.prune_scripts}.items():
        p = tmp_path / name
        p.write_text(content)
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} is not valid bash:\n{r.stderr}"
