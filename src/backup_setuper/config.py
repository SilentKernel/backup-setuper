from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DESTINATION_KINDS = {"rclone-webdav", "rclone-ftp", "hetzner-sftp"}
MONITOR_KINDS = {"healthchecks", "kuma"}


class ConfigError(ValueError):
    pass


@dataclass
class Monitor:
    """Healthcheck/uptime monitor for a destination.

    healthchecks: base is a Healthchecks.io UUID; pings hit https://hc-ping.com/<uuid>[/start|/fail].
    kuma:         base is the full Uptime Kuma push URL (https://kuma.example.com/api/push/<token>);
                  status is passed as ?status=up|down. Kuma has no start ping.
    """
    kind: str
    base: str

    @property
    def has_start(self) -> bool:
        return self.kind == "healthchecks"

    @property
    def start_url(self) -> str:
        if self.kind == "healthchecks":
            return f"https://hc-ping.com/{self.base}/start"
        return ""

    @property
    def success_url(self) -> str:
        if self.kind == "healthchecks":
            return f"https://hc-ping.com/{self.base}"
        return f"{self.base}?status=up&msg=OK&ping="

    @property
    def fail_url(self) -> str:
        if self.kind == "healthchecks":
            return f"https://hc-ping.com/{self.base}/fail"
        return f"{self.base}?status=down&msg=fail&ping="


@dataclass
class Target:
    host: str
    user: str = "root"
    ssh_key: str | None = None
    port: int = 22
    sudo: bool = False


@dataclass
class Restic:
    password: str
    retention: str = "--keep-daily 30"


@dataclass
class Schedule:
    hour: int
    prune_minute: int


@dataclass
class WebDAVConf:
    url: str
    user: str
    password: str


@dataclass
class FTPConf:
    host: str
    user: str
    password: str
    port: int = 21


@dataclass
class SFTPConf:
    user: str
    host: str
    port: int = 23
    repo_path: str = ""


@dataclass
class Destination:
    name: str
    kind: str
    monitor: Monitor
    schedule: Schedule
    rclone_remote: str | None = None
    repo_path: str | None = None
    webdav: WebDAVConf | None = None
    ftp: FTPConf | None = None
    sftp: SFTPConf | None = None

    @property
    def repository_url(self) -> str:
        if self.kind in ("rclone-webdav", "rclone-ftp"):
            return f"rclone:{self.rclone_remote}:{self.repo_path}"
        if self.kind == "hetzner-sftp":
            s = self.sftp
            return f"sftp://{s.user}@{s.host}:{s.port}/{s.repo_path}"
        raise ConfigError(f"unknown kind {self.kind!r}")

    @property
    def label(self) -> str:
        labels = {
            "rclone-webdav": "WebDAV via rclone",
            "rclone-ftp": "FTP via rclone",
            "hetzner-sftp": "Hetzner Storage Box (SFTP)",
        }
        return f"{self.name} ({labels[self.kind]})"


@dataclass
class Machine:
    name: str
    target: Target
    restic: Restic
    sources: list[str]
    excludes: list[str]
    destinations: list[Destination] = field(default_factory=list)

    @property
    def hetzner_destinations(self) -> list[Destination]:
        return [d for d in self.destinations if d.kind == "hetzner-sftp"]


# ---------- loading ----------

def _resolve(refs: dict[str, str], key: str, where: str) -> str:
    if key not in refs:
        raise ConfigError(f"secret reference {key!r} (used in {where}) not found in secrets.yaml")
    return refs[key]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_secrets(path: Path) -> dict[str, str]:
    data = _load_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(v, (str, int)):
            raise ConfigError(f"{path}: secret {k!r} must be a string")
        out[str(k)] = str(v)
    return out


def load_machine(path: Path, secrets: dict[str, str]) -> Machine:
    raw = _load_yaml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")

    try:
        name = raw["machine"]
        target_raw = raw["target"]
        restic_raw = raw["restic"]
        sources = list(raw["sources"])
        excludes = list(raw.get("excludes") or [])
        dests_raw = list(raw["destinations"])
    except KeyError as e:
        raise ConfigError(f"{path}: missing required key {e}") from None

    target = Target(
        host=target_raw["host"],
        user=target_raw.get("user", "root"),
        ssh_key=target_raw.get("ssh_key"),
        port=int(target_raw.get("port", 22)),
        sudo=bool(target_raw.get("sudo", False)),
    )
    restic = Restic(
        password=_resolve(secrets, restic_raw["password_ref"], "restic.password_ref"),
        retention=restic_raw.get("retention", "--keep-daily 30"),
    )

    destinations = [_parse_destination(d, secrets) for d in dests_raw]
    _validate_destinations(destinations)
    _validate_strings(restic.password, "restic.password", quote_safe=True)

    return Machine(
        name=name,
        target=target,
        restic=restic,
        sources=sources,
        excludes=excludes,
        destinations=destinations,
    )


def _parse_destination(d: dict[str, Any], secrets: dict[str, str]) -> Destination:
    kind = d["kind"]
    if kind not in DESTINATION_KINDS:
        raise ConfigError(f"destination {d.get('name')!r}: unknown kind {kind!r}")

    sched = Schedule(hour=int(d["schedule"]["hour"]),
                     prune_minute=int(d["schedule"]["prune_minute"]))
    if not 0 <= sched.hour <= 23:
        raise ConfigError(f"destination {d['name']}: schedule.hour out of range")
    if not 0 <= sched.prune_minute <= 59:
        raise ConfigError(f"destination {d['name']}: schedule.prune_minute out of range")

    monitor = _parse_monitor(d, secrets)

    dest = Destination(
        name=d["name"],
        kind=kind,
        monitor=monitor,
        schedule=sched,
    )

    if kind == "rclone-webdav":
        w = d["webdav"]
        dest.rclone_remote = d["rclone_remote"]
        dest.repo_path = d["repo_path"]
        dest.webdav = WebDAVConf(
            url=w["url"], user=w["user"],
            password=_resolve(secrets, w["password_ref"], f"destinations[{d['name']}].webdav.password_ref"),
        )
        _validate_strings(dest.webdav.password, f"destinations[{d['name']}].webdav.password", quote_safe=True)
    elif kind == "rclone-ftp":
        f = d["ftp"]
        dest.rclone_remote = d["rclone_remote"]
        dest.repo_path = d["repo_path"]
        dest.ftp = FTPConf(
            host=f["host"], user=f["user"], port=int(f.get("port", 21)),
            password=_resolve(secrets, f["password_ref"], f"destinations[{d['name']}].ftp.password_ref"),
        )
        _validate_strings(dest.ftp.password, f"destinations[{d['name']}].ftp.password", quote_safe=True)
    elif kind == "hetzner-sftp":
        s = d["sftp"]
        dest.sftp = SFTPConf(
            user=s["user"], host=s["host"],
            port=int(s.get("port", 23)),
            repo_path=s["repo_path"],
        )

    return dest


def _parse_monitor(d: dict[str, Any], secrets: dict[str, str]) -> Monitor:
    name = d.get("name")
    has_legacy = "healthcheck_ref" in d
    has_block = "monitor" in d
    if has_legacy and has_block:
        raise ConfigError(
            f"destination {name!r}: specify either 'healthcheck_ref' or 'monitor', not both"
        )
    if not has_legacy and not has_block:
        raise ConfigError(
            f"destination {name!r}: missing 'monitor' (or legacy 'healthcheck_ref')"
        )

    if has_legacy:
        base = _resolve(secrets, d["healthcheck_ref"], f"destinations[{name}].healthcheck_ref")
        _validate_strings(base, f"destinations[{name}].healthcheck", quote_safe=True)
        return Monitor(kind="healthchecks", base=base)

    m = d["monitor"]
    if not isinstance(m, dict):
        raise ConfigError(f"destination {name!r}: 'monitor' must be a mapping")
    kind = m.get("kind")
    if kind not in MONITOR_KINDS:
        raise ConfigError(
            f"destination {name!r}: unknown monitor kind {kind!r} "
            f"(expected one of {sorted(MONITOR_KINDS)})"
        )
    try:
        ref = m["url_ref"]
    except KeyError:
        raise ConfigError(f"destination {name!r}: monitor.url_ref is required") from None
    base = _resolve(secrets, ref, f"destinations[{name}].monitor.url_ref")
    if kind == "kuma":
        # Kuma's UI hands you the push URL with an example query string attached
        # (?status=up&msg=OK&ping=). Strip it: success_url/fail_url append their own.
        base = base.split("?", 1)[0]
    _validate_strings(base, f"destinations[{name}].monitor", quote_safe=True)
    return Monitor(kind=kind, base=base)


def _validate_destinations(dests: list[Destination]) -> None:
    if not dests:
        raise ConfigError("at least one destination is required")
    names = [d.name for d in dests]
    if len(set(names)) != len(names):
        raise ConfigError(f"duplicate destination names: {names}")
    hours = [d.schedule.hour for d in dests]
    if len(set(hours)) != len(hours):
        raise ConfigError(f"destinations have colliding backup hours: {hours}")
    mins = [d.schedule.prune_minute for d in dests]
    if len(set(mins)) != len(mins):
        raise ConfigError(f"destinations have colliding prune minutes: {mins}")


def _validate_strings(s: str, where: str, quote_safe: bool = False) -> None:
    if quote_safe and "'" in s:
        raise ConfigError(f"{where}: single quote not allowed (breaks shell single-quoting)")
