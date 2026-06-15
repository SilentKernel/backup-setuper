from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from backup_setuper.config import Destination, Machine


def _env() -> Environment:
    return Environment(
        loader=PackageLoader("backup_setuper", "templates"),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render_backup_script(machine: Machine, dest: Destination) -> str:
    return _env().get_template("backup-restic.sh.j2").render(
        machine_name=machine.name,
        destination=dest,
        restic_password=machine.restic.password,
        sources=machine.sources,
        excludes=machine.excludes,
    )


def render_prune_script(machine: Machine, dest: Destination) -> str:
    return _env().get_template("prune-restic.sh.j2").render(
        destination=dest,
        restic_password=machine.restic.password,
        retention=machine.restic.retention,
    )


def render_cron(machine: Machine) -> str:
    return _env().get_template("cron.txt.j2").render(
        machine_name=machine.name,
        destinations=machine.destinations,
    )


@dataclass
class RcloneRemote:
    name: str
    type: str
    fields: dict[str, str]


def build_rclone_remotes(machine: Machine) -> list[RcloneRemote]:
    """Build the list of rclone remotes for this machine.

    Passwords are emitted in CLEARTEXT here — the caller is expected to pipe
    them through `rclone obscure` on the target before writing the file.
    """
    out: list[RcloneRemote] = []
    for d in machine.destinations:
        if d.kind == "rclone-webdav":
            w = d.webdav
            out.append(RcloneRemote(
                name=d.rclone_remote, type="webdav",
                fields={"url": w.url, "vendor": "other", "user": w.user, "pass": w.password},
            ))
        elif d.kind == "rclone-ftp":
            f = d.ftp
            out.append(RcloneRemote(
                name=d.rclone_remote, type="ftp",
                fields={"host": f.host, "user": f.user, "pass": f.password},
            ))
    return out


def render_rclone_conf(remotes: list[RcloneRemote]) -> str:
    return _env().get_template("rclone.conf.j2").render(remotes=remotes)


@dataclass
class RenderedBundle:
    """Everything bootstrap needs to push to a target."""
    backup_scripts: dict[str, str]  # filename -> content
    prune_scripts: dict[str, str]
    cron_txt: str
    rclone_remotes: list[RcloneRemote]  # passwords still in cleartext


def render_all(machine: Machine) -> RenderedBundle:
    backups = {
        f"backup-restic-{d.name}.sh": render_backup_script(machine, d)
        for d in machine.destinations
    }
    prunes = {
        f"prune-restic-{d.name}.sh": render_prune_script(machine, d)
        for d in machine.destinations
    }
    return RenderedBundle(
        backup_scripts=backups,
        prune_scripts=prunes,
        cron_txt=render_cron(machine),
        rclone_remotes=build_rclone_remotes(machine),
    )


def write_bundle(bundle: RenderedBundle, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in {**bundle.backup_scripts, **bundle.prune_scripts}.items():
        p = out_dir / name
        p.write_text(content)
        p.chmod(0o755)
    (out_dir / "cron.txt").write_text(bundle.cron_txt)
    # rclone.conf written separately: we don't run `rclone obscure` locally.
