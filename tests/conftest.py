from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def example_secrets(tmp_path: Path) -> Path:
    """A secrets file with deterministic, test-only values matching machines/silentbox.example.yaml."""
    data = {
        "silentbox-restic-pass": "TEST-RESTIC-PASS",
        "silentds-hc":            "11111111-1111-1111-1111-111111111111",
        "silentds-webdav-pass":   "TEST-WEBDAV-PASS",
        "online-ftp-hc":          "22222222-2222-2222-2222-222222222222",
        "online-ftp-pass":        "TEST-FTP-PASS",
        "hetzner-hel-hc":         "33333333-3333-3333-3333-333333333333",
        "hetzner-fsn-hc":         "44444444-4444-4444-4444-444444444444",
    }
    p = tmp_path / "secrets.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def example_machine_path() -> Path:
    return REPO_ROOT / "machines" / "silentbox.example.yaml"


@pytest.fixture
def kuma_secrets(tmp_path: Path) -> Path:
    """Secrets file for a machine using Uptime Kuma push monitors."""
    data = {
        "silentbox-restic-pass": "TEST-RESTIC-PASS",
        "silentds-kuma":         "https://kuma.example.com/api/push/abcDEF123",
        "hetzner-kuma":          "https://kuma.example.com/api/push/xyz999",
    }
    p = tmp_path / "secrets.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def kuma_machine_path(tmp_path: Path) -> Path:
    cfg = {
        "machine": "kuma-box",
        "target": {"host": "kuma-box.example.com"},
        "restic": {"password_ref": "silentbox-restic-pass"},
        "sources": ["/home/ludo"],
        "destinations": [
            {
                "name": "silentds",
                "kind": "hetzner-sftp",
                "monitor": {"kind": "kuma", "url_ref": "silentds-kuma"},
                "schedule": {"hour": 4, "prune_minute": 0},
                "sftp": {"user": "u1", "host": "h1", "repo_path": "/home/x"},
            },
            {
                "name": "hetzner",
                "kind": "hetzner-sftp",
                "monitor": {"kind": "kuma", "url_ref": "hetzner-kuma"},
                "schedule": {"hour": 5, "prune_minute": 30},
                "sftp": {"user": "u2", "host": "h2", "repo_path": "/home/y"},
            },
        ],
    }
    p = tmp_path / "kuma-box.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p
