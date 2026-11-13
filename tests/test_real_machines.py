"""Sanity checks on the operator's real (gitignored) machine configs.

Skipped on a fresh clone: they need machines/<name>.yaml and secrets.yaml, which
are never committed. No network, no SSH — load + render only.
"""
from pathlib import Path

import pytest

from backup_setuper.config import ConfigError, load_machine, load_secrets
from backup_setuper.render import render_all

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS = REPO_ROOT / "secrets.yaml"
REAL_MACHINES = sorted(
    p for p in (REPO_ROOT / "machines").glob("*.yaml")
    if not p.name.endswith(".example.yaml")
)

pytestmark = pytest.mark.skipif(
    not SECRETS.exists() or not REAL_MACHINES,
    reason="real secrets.yaml / machines/*.yaml not present",
)

ADDOK_NEW = "/home/ludo/address-autocomplete-ptifez0qbo.developpeur-freelance.io/addok-data"
ADDOK_OLD = "internal-use-api-w53.lfmaps.fr"


def _load(path: Path):
    try:
        return load_machine(path, load_secrets(SECRETS))
    except ConfigError as e:
        # A machine whose monitors are not created yet has unresolved refs: that
        # is a to-do for the operator, not a defect of the tool.
        if "not found in secrets.yaml" in str(e):
            pytest.skip(str(e))
        raise


def _real(name: str):
    path = REPO_ROOT / "machines" / f"{name}.yaml"
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    return _load(path)


@pytest.mark.parametrize("path", REAL_MACHINES, ids=lambda p: p.stem)
def test_real_machine_loads_and_renders(path):
    m = _load(path)
    bundle = render_all(m)
    assert set(bundle.backup_scripts) == {f"backup-restic-{d.name}.sh" for d in m.destinations}
    assert set(bundle.prune_scripts) == {f"prune-restic-{d.name}.sh" for d in m.destinations}
    for ex in m.excludes:
        for script in bundle.backup_scripts.values():
            assert f"--exclude='{ex}'" in script


@pytest.mark.parametrize("path", REAL_MACHINES, ids=lambda p: p.stem)
def test_real_machine_never_backs_up_at_hour_1(path):
    # unattended-upgrades may reboot the server between 1:00 and 1:59.
    m = _load(path)
    assert all(d.schedule.hour != 1 for d in m.destinations)


@pytest.mark.parametrize("name", ["silentbox", "silent2box"])
def test_addok_exclude_follows_the_new_domain(name):
    m = _real(name)
    assert ADDOK_NEW in m.excludes
    for script in render_all(m).backup_scripts.values():
        assert f"--exclude='{ADDOK_NEW}'" in script
        assert ADDOK_OLD not in script


def test_silent2box_destinations():
    m = _real("silent2box")
    assert m.target.host == "silent2box-2026"
    by_name = {d.name: d for d in m.destinations}
    assert set(by_name) == {"silentds", "hetzner-hel", "hetzner-fsn"}  # no FTP
    assert by_name["silentds"].repository_url == "rclone:silentds-webdav:silent2box-2026"
    assert by_name["hetzner-hel"].repository_url == (
        "sftp://u613928@u613928.your-storagebox.de:23//home/silent2box-2026"
    )
    assert by_name["hetzner-fsn"].repository_url == (
        "sftp://u589339@u589339.your-storagebox.de:23//home/silent2box-2026"
    )
    assert all(d.monitor.kind == "kuma" for d in m.destinations)


def test_silent2box_never_shares_a_slot_with_silentbox():
    """Same destination must not be hit at the same instant by both twins.

    silent2box-2026 runs on UTC, silentbox-2025 on Europe/Paris (UTC+1 / UTC+2).
    """
    new, old = _real("silent2box"), _real("silentbox")
    old_hours = {d.name: d.schedule.hour for d in old.destinations}
    for d in new.destinations:
        for offset in (1, 2):
            assert (d.schedule.hour + offset) % 24 != old_hours[d.name], d.name
